from __future__ import annotations

import sqlite3
import time

from noema import fx_spotvol as fx


def test_fee_rounds_up_per_order_and_slippage_capped() -> None:
    assert fx.fee_per_contract(0.5) == 0.018  # 0.175 -> $0.18 / 10
    assert fx.fee_per_contract(0.99) == 0.001
    assert fx.fill("yes", 0.40, 0.42) == 0.43
    assert fx.fill("no", 0.40, 0.42) == 0.61
    assert fx.fill("yes", 0.97, 0.99) == 0.99


def test_decide_matches_backtest_rule() -> None:
    assert fx.decide(0.70, 0.48, 0.50) == "yes"
    assert fx.decide(0.30, 0.48, 0.50) == "no"
    assert fx.decide(0.505, 0.48, 0.50) is None


def test_aligned_bars_drop_incomplete_and_unaligned() -> None:
    td = 1_800_000_000 - 1_800_000_000 % 300 + 150
    rows = [(td - 150 - 300 * k, 1.0 + k * 1e-4) for k in range(5)] + [(td - 10, 9.9), (td - 150, 5.0)]
    b = fx.aligned(rows, td)
    assert all(t % 300 == 0 and t + 300 <= td for t in b.ts)
    assert 9.9 not in b.px and 5.0 not in b.px


def test_evaluate_criteria_and_kill() -> None:
    days = ["2026-10-23", "2026-10-25", "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"]
    tr = [{"close_time": f"{d}T{h:02d}:{m:02d}:00+00:00", "decision_at": f"{d}T{h:02d}:{m:02d}:00+00:00",
           "pnl_per_contract": 0.6 if (h + m) % 3 else -0.4} for d in days for h in range(4, 20) for m in (0, 15, 30, 45)]
    e = fx.evaluate_trades(tr, n_boot=500)
    assert e["n"] == len(tr) == 384 and e["checks"]["n>=500"] is False
    assert e["mean_per_contract"] > 0 and e["checks"]["ci97.5_excludes_0"]
    bad = [{**t, "pnl_per_contract": -0.5} for t in tr[:250]]
    assert fx.evaluate_trades(bad, n_boot=200)["kill"] is True


class FakeHttp:
    def __init__(self, close_ts: int) -> None:
        self.close_ts = close_ts
        self.result = ""

    def get(self, url: str, params=None):
        if "finance.yahoo" in url:
            base = self.close_ts - self.close_ts % 300 - 2 * 86400
            ts = list(range(base, self.close_ts, 300))
            return {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [{"close": [
                1.3 + 1e-4 * ((i * 7) % 5 - 2) for i in range(len(ts))]}]}}]}}
        m = {"ticker": "KXGBPUSD15M-T", "event_ticker": "E", "close_time": fx._iso(self.close_ts),
             "yes_bid_dollars": "0.10", "yes_ask_dollars": "0.12", "floor_strike": 1.29,
             "strike_type": "greater_or_equal", "result": self.result}
        if url.endswith("/markets"):
            return {"markets": [m] if params["series_ticker"] == "KXGBPUSD15M" else []}
        return {"market": m}


def test_live_signal_fill_settle_cycle(tmp_path, monkeypatch) -> None:
    train = tmp_path / "t.db"
    sqlite3.connect(train).executescript("CREATE TABLE pm_ref_bars (symbol TEXT, ts INTEGER, close REAL);")
    now = time.time()
    close_ts = int(now) + fx.LEAD_S - 5  # decision time was 5s ago
    h = FakeHttp(close_ts)
    lv = fx.Live(str(tmp_path / "fx.db"), str(train), http=h)  # type: ignore[arg-type]
    lv.cycle()
    rows = lv.c.execute("SELECT agent, status, side, signal_ask, spot_bar_at FROM fx_decisions").fetchall()
    assert {r[0] for r in rows} == set(fx.AGENTS)
    assert all(r[1] == "pending_fill" and r[2] == "yes" for r in rows), rows  # spot far above strike, ask 0.12
    monkeypatch.setattr(fx.time, "time", lambda: close_ts - fx.LEAD_S + 70)
    lv.fills(close_ts - fx.LEAD_S + 70)
    assert {r[0] for r in lv.c.execute("SELECT status FROM fx_decisions")} == {"open"}
    assert lv.c.execute("SELECT fill_price, fee FROM fx_decisions").fetchone() == (0.13, 0.08)
    h.result = "yes"
    lv.settle(close_ts + 60)
    pnl = lv.c.execute("SELECT status, pnl_per_contract FROM fx_decisions").fetchall()
    assert all(s == "settled" and abs(p - (1 - 0.13 - 0.008)) < 1e-9 for s, p in pnl)


def test_late_fill_is_skipped_as_stale(tmp_path) -> None:
    train = tmp_path / "t.db"
    sqlite3.connect(train).executescript("CREATE TABLE pm_ref_bars (symbol TEXT, ts INTEGER, close REAL);")
    close_ts = int(time.time()) + fx.LEAD_S - 5
    lv = fx.Live(str(tmp_path / "fx.db"), str(train), http=FakeHttp(close_ts))  # type: ignore[arg-type]
    lv.cycle()
    lv.fills(close_ts - fx.LEAD_S + 200)
    assert {r for r in lv.c.execute("SELECT status, skip_reason FROM fx_decisions")} == {("skip", "stale_fill")}
