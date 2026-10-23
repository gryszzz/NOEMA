"""Paper-only other-chain scanner: cost model, buyability, paper state machine, pre-registered verdict."""
from __future__ import annotations

import sqlite3

import pytest

from noema import evm_swap
from noema import other_chains as oc


def test_router_matches_signer_and_signer_flags():
    assert oc.KYBER_ROUTER == evm_swap.KYBER_ROUTER
    for name, ch in oc.CHAINS.items():
        assert ch.signer == (name in evm_swap.SWAP_CHAINS), name


def test_paper_net_charges_all_costs():
    free, cost0 = oc.paper_net(0.10, size=25, buy_tax=0, sell_tax=0, rt_cost=0, entry_liq=1e5,
                               exit_liq=1e5, gas_usd=0)
    assert free == pytest.approx(0.10) and cost0 == pytest.approx(0)
    net, cost = oc.paper_net(0.10, size=25, buy_tax=0.02, sell_tax=0.03, rt_cost=0.02, entry_liq=1e5,
                             exit_liq=5e4, gas_usd=0.02, bridge_cost_usd=0.05)
    assert net < free and cost > 0
    # bridge is paid twice, gas x GAS_LEGS
    a, _ = oc.paper_net(0.0, size=25, buy_tax=0, sell_tax=0, rt_cost=0, entry_liq=1, exit_liq=1, gas_usd=0.1)
    assert a == pytest.approx(-0.1 * oc.GAS_LEGS / 25)
    b, _ = oc.paper_net(0.0, size=25, buy_tax=0, sell_tax=0, rt_cost=0, entry_liq=1, exit_liq=1, gas_usd=0,
                        bridge_cost_usd=0.5)
    assert b == pytest.approx(-1.0 / 25)
    # total loss stays bounded by size + fixed costs
    dead, _ = oc.paper_net(-1.0, size=25, buy_tax=0, sell_tax=0, rt_cost=0.02, entry_liq=1, exit_liq=None, gas_usd=0)
    assert dead == pytest.approx(-1.0)


def test_leg_costs_exit_grows_when_liquidity_shrinks():
    assert oc.leg_costs(0.04, 1e5, 1e5) == (0.02, 0.02)
    assert oc.leg_costs(0.04, 1e5, 2.5e4)[1] == pytest.approx(0.08)
    assert oc.leg_costs(0.04, 1e5, None)[1] == 0.5


@pytest.mark.parametrize("rec,reason", [
    (None, "security_unknown"),
    ({"is_honeypot": "1", "buy_tax": "0", "sell_tax": "0"}, "honeypot"),
    ({"cannot_sell_all": "1", "buy_tax": "0", "sell_tax": "0"}, "cannot_trade"),
    ({"is_honeypot": "0", "buy_tax": "", "sell_tax": ""}, "tax_unknown"),
    ({"is_honeypot": "0", "buy_tax": "0", "sell_tax": "0.25"}, "high_tax"),
    ({"is_honeypot": "0", "buy_tax": "0.01", "sell_tax": "0.02"}, None),
])
def test_security_verdict(rec, reason):
    assert oc.security_verdict(rec)[0] == reason


def test_signal_ok():
    s = {"buys_m5": 20, "sells_m5": 5, "vol_m5": 5000, "liq": max(oc.MIN_LIQ_USD, 20000)}
    assert oc.signal_ok(s, s["liq"])
    assert not oc.signal_ok({**s, "sells_m5": 19}, s["liq"])
    assert not oc.signal_ok({**s, "vol_m5": 10}, s["liq"])
    assert not oc.signal_ok(s, s["liq"] * 2)  # liquidity pulled since first seen


@pytest.fixture
def scanner(tmp_path):
    return oc.Scanner(str(tmp_path / "oc.db"), str(tmp_path / "main.db"), client=object())


def _row(sc, **kw):
    base = {"chain": "base", "pair": "0xp", "stage": "followed", "seen_at": 0, "follow_until": 900, "rt_cost": 0.02,
                "gas_usd": 0.02, "funded": 1, "buy_tax": 0, "sell_tax": 0}
    base.update(kw)
    cols = ",".join(base)
    sc.c.execute(f"INSERT OR REPLACE INTO oc_pairs({cols}) VALUES({','.join('?' * len(base))})",
                 tuple(base.values()))
    return sc.c.execute("SELECT * FROM oc_pairs WHERE chain='base' AND pair='0xp'").fetchone()


def _get(sc):
    return sc.c.execute("SELECT * FROM oc_pairs WHERE chain='base' AND pair='0xp'").fetchone()


def test_step_signal_entry_exit(scanner):
    liq = max(oc.MIN_LIQ_USD, 20000)
    snap = {"price": 1.0, "liq": liq, "vol_m5": 5000, "buys_m5": 20, "sells_m5": 2}
    scanner.step(_row(scanner), snap, 10)
    r = _get(scanner)
    assert r["signal_at"] == 10 and r["entry_at"] is None and r["first_liq"] == liq
    scanner.step(r, snap, 20)  # not yet past exec delay
    assert _get(scanner)["entry_at"] is None
    scanner.step(_get(scanner), {**snap, "price": 1.2}, 40)
    r = _get(scanner)
    assert r["entry_at"] == 40 and r["entry_price"] == 1.2 and r["follow_until"] == 40 + oc.HOLD_S
    scanner.step(r, {**snap, "price": 1.5}, 100)
    assert _get(scanner)["stage"] == "followed"
    scanner.step(_get(scanner), {**snap, "price": 1.5}, 40 + oc.HOLD_S)
    r = _get(scanner)
    assert r["stage"] == "closed" and r["exit_reason"] == "time"
    assert r["gross_ret"] == pytest.approx(0.25) and r["net_ret"] < 0.25 and r["day"] == "1970-01-01"


def test_step_stop_unpriced_dead_and_stale(scanner):
    liq = max(oc.MIN_LIQ_USD, 20000)
    snap = {"price": 1.0, "liq": liq, "vol_m5": 0, "buys_m5": 0, "sells_m5": 0}
    scanner.step(_row(scanner, entry_at=1, entry_price=1.0, entry_liq=liq, first_liq=liq, follow_until=601),
                 {**snap, "price": 0.5}, 30)
    assert _get(scanner)["exit_reason"] == "stop"
    scanner.step(_row(scanner, entry_at=1, entry_price=1.0, entry_liq=liq, first_liq=liq, follow_until=601),
                 None, 650)
    assert _get(scanner)["stage"] == "followed"  # grace period for a missing snapshot
    scanner.step(_get(scanner), None, 601 + 121)
    r = _get(scanner)
    assert r["exit_reason"] == "unpriced" and r["net_ret"] == pytest.approx(-1 - 0.02 * oc.GAS_LEGS / oc.PAPER_SIZE_USD)
    scanner.step(_row(scanner, first_liq=liq), {**snap, "liq": liq * 0.3}, 30)
    assert _get(scanner)["exit_reason"] == "dead_before_signal"
    scanner.step(_row(scanner, first_liq=liq, signal_at=10), snap, 10 + oc.EXEC_DELAY_S + 200)
    r = _get(scanner)
    assert r["signal_at"] is None and r["entry_at"] is None  # stale signal dropped, never entered
    scanner.step(_row(scanner, first_liq=liq), snap, 901)
    assert _get(scanner)["exit_reason"] == "no_signal"


def test_bridge_delay_and_cost_for_unfunded(scanner):
    liq = max(oc.MIN_LIQ_USD, 20000)
    snap = {"price": 1.0, "liq": liq, "vol_m5": 5000, "buys_m5": 20, "sells_m5": 2}
    r = _row(scanner, funded=0, bridge_s=30, bridge_cost_usd=0.1, first_liq=liq, signal_at=0)
    scanner.step(r, snap, oc.EXEC_DELAY_S + 10)
    assert _get(scanner)["entry_at"] is None  # still bridging
    scanner.step(_get(scanner), snap, oc.EXEC_DELAY_S + 31)
    r = _get(scanner)
    assert r["entry_at"] is not None
    scanner.step(r, snap, r["follow_until"])
    funded_net, _ = oc.paper_net(0, size=oc.PAPER_SIZE_USD, buy_tax=0, sell_tax=0, rt_cost=0.02, entry_liq=liq,
                                 exit_liq=liq, gas_usd=0.02)
    assert _get(scanner)["net_ret"] == pytest.approx(funded_net - 0.2 / oc.PAPER_SIZE_USD)


def _days(n_days, per_day, val):
    return [(f"2026-11-{d + 1:02d}", val(d, i)) for d in range(n_days) for i in range(per_day)]


def test_verdict_collecting_go_and_no_go():
    assert oc.verdict_for([], 6)["verdict"] == "collecting"
    assert oc.verdict_for(_days(3, 40, lambda d, i: 0.05), 6)["verdict"] == "collecting"  # < 7 days
    go = oc.verdict_for(_days(8, 15, lambda d, i: 0.05 + 0.01 * (i % 3)), 6)
    assert go["verdict"] == "go", go
    early = oc.verdict_for(_days(2, 60, lambda d, i: -0.05 + 0.01 * (i % 3)), 6)
    assert early["verdict"] == "no_go" and "early" in early["why"]
    # positive overall but a losing held-out day -> no_go
    bad = oc.verdict_for(_days(8, 15, lambda d, i: (-0.2 if d == 7 else 0.2) + 0.01 * (i % 3)), 6)
    assert bad["verdict"] == "no_go"


def test_preregister_idempotent_and_board(tmp_path):
    db = str(tmp_path / "oc.db")
    assert oc.board(db)["status"] == "not started"
    c = oc.connect(db)
    a, b = oc.preregister(c), oc.preregister(c)
    assert a == b and c.execute("SELECT count(*) FROM oc_preregistration").fetchone()[0] == 1
    c.close()
    out = oc.board(db)
    assert out["paper_only"] is True and out["prereg_sha256"] == a
    assert [x["chain"] for x in out["chains"]] == list(oc.CHAINS)
    assert all(x["verdict"]["verdict"] == "collecting" for x in out["chains"])
    with pytest.raises(sqlite3.OperationalError):  # board is read-only
        sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute("CREATE TABLE x(a)")


def test_kyber_route_not_found_is_unroutable_not_error(tmp_path):
    import asyncio

    class R:
        def __init__(self, code, text):
            self.status_code, self.text = code, text

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)

        def json(self):
            import json
            return json.loads(self.text)

    class C:
        def __init__(self, resp):
            self.resp = resp

        async def get(self, *a, **k):
            return self.resp

    sc = oc.Scanner(str(tmp_path / "oc.db"), str(tmp_path / "m.db"),
                    client=C(R(400, '{"code":4008,"message":"route not found"}')))
    assert asyncio.run(sc.kyber(oc.CHAINS["base"], oc.NATIVE, "0xt", 1)) is None
    sc.http = C(R(500, "boom"))
    with pytest.raises(RuntimeError):
        asyncio.run(sc.kyber(oc.CHAINS["base"], oc.NATIVE, "0xt", 1))
