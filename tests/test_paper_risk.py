import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from noema import paper_risk as pr
from noema.public_quotes import Quote

NOW = datetime(2026, 10, 23, 3, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("NOEMA_SHARED_PAPER_BANKROLL_USD", "NOEMA_STRATEGY_PAPER_BANKROLL_USD", "NOEMA_ENTRY_BUFFER",
              "NOEMA_STRATEGY_SLIPPAGE", "NOEMA_STEP_LADDER_USD", "NOEMA_UNPROVEN_BET_CAP_USD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NOEMA_STRATEGY_CAPACITY_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("NOEMA_RISK_BUDGET_PATH", str(tmp_path / "rb.json"))


def test_default_capacity_path_is_mqgb_owned(monkeypatch):
    monkeypatch.delenv("NOEMA_STRATEGY_CAPACITY_PATH")
    assert pr.capacity_path() == "/workspace/mqgb/capacity/strategy_capacity.json"


def test_classify_groups_correlated_markets():
    assert pr.classify("kalshi", "KXBTCD-26OCT2317-T110000", "")[1] == pr.classify("kalshi", "KXBTC-26OCT23-B1", "")[1]
    assert pr.classify("kalshi", "KXFEDDECISION-26DEC-H0", "")[1] == pr.classify("kalshi", "KXFED-26DEC-T4.00", "")[1]
    assert pr.classify("kalshi", "GOVPARTYTX-26-D", "Texas governor")[0] == "elections"
    assert pr.classify("kalshi", "KXEURUSD-26OCT23-T1.1", "EUR/USD")[0] == "fx"


def test_gate_is_strict_and_net_of_fee_slippage_buffer():
    p = 0.40
    edge0 = pr.fee("kalshi", p) + pr.slippage() + 0.01
    assert not pr.passes_gate("kalshi", p + edge0 - 1e-9, p)  # at the threshold is not enough
    assert pr.passes_gate("kalshi", p + edge0 + 0.001, p)
    assert not pr.passes_gate("kalshi", None, p)


def test_quarter_kelly_and_caps():
    b = pr.Budget(1000.0)
    k = pr.kelly_stake("kalshi", 0.70, 0.50, 1000)
    c = pr.all_in_cost("kalshi", 0.50)
    assert k == pytest.approx(0.25 * (0.70 - c) / (1 - c) * 1000, rel=1e-6)
    d = b.size("pm_arb", "kalshi", "GOVPARTYTX-26-D", "Texas governor", 0.70, 0.50, bot="noema", reserve=False)
    assert d.usd <= 1.0 + 1e-9  # unmeasured strategy -> minimum $1 step
    d2 = b.size("pm_arb", "kalshi", "GOVPARTYTX-26-D", "x", 0.70, 0.50, bot="noema", reserve=False, step_cap=False)
    assert d2.usd == pytest.approx(5.0)  # no held-out record -> $5 hard cap


def _cap_file(tmp_path, doc):
    p = tmp_path / "cap.json"
    p.write_text(json.dumps(doc))
    return str(p)


def test_capacity_file_validation(tmp_path):
    ok = pr.load_capacity(_cap_file(tmp_path, {"schema": "tev.strategy_capacity.v1", "author": "MQGB",
                                               "strategies": {"fast_settle": {"measured_capacity_usd": 40,
                                                                              "step_size_usd": 10},
                                                              "pm_arb": {"measured_capacity_usd": None},
                                                              "bad": {"measured_capacity_usd": 5, "evil": 1}}}))
    assert ok["status"] == "ok" and "bad" in ok["rejected"]
    lim = pr.strategy_limits("fast_settle", ok, 50.0)
    assert lim["current_step_size_usd"] == 10 and lim["cap_usd"] == 10
    assert pr.strategy_limits("pm_arb", ok, 50.0)["cap_usd"] == 1.0
    bad = pr.load_capacity(_cap_file(tmp_path, {"schema": "tev.strategy_capacity.v1", "strategies": {}, "x": 1}))
    assert bad["status"].startswith("rejected") and bad["strategies"] == {}
    missing = pr.load_capacity(str(tmp_path / "nope.json"))
    assert missing["status"] == "missing"
    assert pr.strategy_limits("fast_settle", missing, 50.0)["cap_usd"] == 1.0
    assert pr.load_capacity(_cap_file(tmp_path, {"schema": "nope", "strategies": {}}))["status"].startswith("rejected")


def test_capacity_never_exceeded_and_symlink_rejected(tmp_path):
    cap = pr.load_capacity(_cap_file(tmp_path, {"schema": "tev.strategy_capacity.v1",
                                                "strategies": {"pm_arb": {"measured_capacity_usd": 3}}}))
    assert pr.strategy_limits("pm_arb", cap, 50.0)["cap_usd"] <= 3
    link = tmp_path / "link.json"
    link.symlink_to(tmp_path / "cap.json")
    assert pr.load_capacity(str(link))["status"].startswith("rejected")


def test_write_budget_atomic_0644_with_lineage(tmp_path):
    c = sqlite3.connect(":memory:")
    pr.seed_lineage(c, NOW)
    pr.register_strategy(c, "pm_arb_v2", parent="pm_arb", born_at=NOW.isoformat())
    b = pr.load_budget(c, pred_db="")
    snap = pr.write_budget(b, str(tmp_path / "rb.json"), NOW)
    assert oct(os.stat(tmp_path / "rb.json").st_mode & 0o777) == "0o644"
    doc = json.loads((tmp_path / "rb.json").read_text())
    assert doc["schema"] == "tev.risk_budget.v1" and doc["owner"] == "NOEMA" and doc["paper_only"] is True
    st = doc["strategies"]
    assert st["fast_settle"]["parent_id"] is None and st["fast_settle"]["generation"] == 0
    assert st["pm_arb_v2"]["parent_id"] == "noema:pm_arb" and st["pm_arb_v2"]["generation"] == 1
    assert {v["status"] for v in st.values()} <= {"active", "scaling", "retired"}
    fam = {f["family_id"]: f for f in snap["families"]}
    assert set(fam["noema:pm_arb"]["members"]) == {"noema:pm_arb", "noema:pm_arb_v2"}
    assert not [p for p in os.listdir(tmp_path) if p.startswith(".risk_budget.")]


def test_lifecycle_status():
    assert pr.lineage_fields("x", {"strategy_id": "noema:x", "alloc_status": "benched"})["status"] == "retired"
    assert pr.lineage_fields("x", None, {"measured_capacity_usd": 40, "current_step_size_usd": 10})["status"] == "scaling"
    assert pr.lineage_fields("x", None, {"measured_capacity_usd": None, "current_step_size_usd": 1})["status"] == "active"


def test_pick_stake_min_of_kelly_stake_capacity_cap():
    big = pr.pick_stake("kalshi", 0.9, 0.5, None, 1000)
    assert big == pytest.approx(50.0)  # 5% cap binds
    assert pr.pick_stake("kalshi", 0.9, 0.5, 12.0, 1000) == pytest.approx(12.0)
    assert pr.pick_stake("kalshi", 0.9, 0.5, 12.0, 1000, stake_usd=7) == pytest.approx(7.0)
    assert pr.pick_stake("kalshi", 0.51, 0.5, None, 1000) == pytest.approx(0.5)  # gate fails -> one contract


class _Q:
    def __init__(self, px):
        self.px = px
        self.calls = 0

    def quote(self, venue, mid, text=""):
        self.calls += 1
        return Quote(venue, mid or "", None, None, self.px, None, "active", text, 0, "test")


def test_capture_moves_only_when_due_and_utc_normalised():
    c = sqlite3.connect(":memory:")
    opened = datetime(2026, 10, 22, 22, 10, tzinfo=UTC) + timedelta(hours=4)  # 02:10Z
    local = opened.astimezone(__import__("datetime").timezone(timedelta(hours=-4)))
    pr.schedule_moves(c, [pr.Bet("prediction_bot", "p1", "kalshi", "T-1", "x", "YES", 0.40, local)])
    q = _Q(0.45)
    pr.capture_moves(c, opened + timedelta(minutes=40), q)  # 1h not due yet
    assert q.calls == 0
    pr.capture_moves(c, opened + timedelta(hours=1, minutes=1), q)
    row = c.execute("SELECT status, move FROM paper_bet_moves WHERE offset_s=3600").fetchone()
    assert row == ("ok", pytest.approx(0.05))
    pr.capture_moves(c, opened + timedelta(days=3), q)  # 1d far past window -> missed
    assert c.execute("SELECT status FROM paper_bet_moves WHERE offset_s=86400").fetchone()[0] == "missed"


def test_note_stake_counts_in_exposure(tmp_path):
    from noema import prediction_inbox as pi

    assert pr.note_stake("stake $4.78, cap $184") == 4.78 and pr.note_stake("cap $5") is None
    db = str(tmp_path / "p.db")
    c = sqlite3.connect(db)
    pi.ensure_schema(c)
    c.execute("INSERT INTO prediction_picks (pick_id, post_id, idx, venue, market, market_id, side, entry_price, fair, "
              "confidence, note, opened_at, status) VALUES ('a#0','a',0,'polymarket','SC Senate','sc-senate','YES',"
              "0.2,0.43,'low','stake $5, cap $90','2026-10-22T00:00:00+00:00','open')")
    c.commit()
    c.close()
    pos = pr.prediction_positions(db, pr.Budget(bank=1000.0))
    assert [round(p.stake_usd, 2) for p in pos] == [5.0]
