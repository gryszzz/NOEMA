"""v2 paper arm: separate registration, GeckoTerminal fallback, tax re-check window. v1 stays frozen."""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from noema import other_chains as oc
from noema import otherchain_v2 as v2

TOK = "0x" + "ab" * 20
PAIR = "0x" + "cd" * 20


def test_v1_registration_is_unchanged():
    blob = json.dumps(oc.PREREG, sort_keys=True)
    assert hashlib.sha256(blob.encode()).hexdigest() == "0698799a617e51696327008a0a6fcb93ae709b962a3fe9b381549fa163add1ef"


def test_v2_registration_separate_same_bar(tmp_path):
    c = v2.connect(str(tmp_path / "v2.db"))
    sha = v2.preregister(c)
    assert sha == v2.preregister(c)                        # idempotent
    rows = c.execute("SELECT sha256, criteria_json FROM oc_preregistration").fetchall()
    assert len(rows) == 1 and rows[0][0] == sha
    crit = json.loads(rows[0][1])
    assert crit["study"] == "other_chains_v2" and crit["chains"] == ["base", "robinhood"]
    for k in ("min_trades_per_chain", "min_heldout_trades", "min_days", "split", "heldout_pass", "each_day",
              "early_no_go", "no_tuning"):
        assert crit[k] == oc.PREREG[k], k
    for k, v in oc.PREREG["rule"].items():
        assert crit["rule"][k] == v, k
    assert crit["costs"].startswith(oc.PREREG["costs"])
    assert sha != hashlib.sha256(json.dumps(oc.PREREG, sort_keys=True).encode()).hexdigest()


def test_next_tax_check_window():
    assert v2.next_tax_check(0) == 30
    assert v2.next_tax_check(30) == 30
    assert v2.next_tax_check(45) == 15
    assert v2.next_tax_check(60) == 60
    assert v2.next_tax_check(120) is None


def test_honeypot_verdict():
    assert v2.honeypot_verdict(None) == (None, None, None)
    assert v2.honeypot_verdict({"honeypotResult": {"isHoneypot": True}})[0] == "honeypot"
    assert v2.honeypot_verdict({"simulationSuccess": False}) == (None, None, None)
    ok = {"honeypotResult": {"isHoneypot": False}, "simulationSuccess": True,
          "simulationResult": {"buyTax": 1, "sellTax": 2, "transferTax": 0}}
    assert v2.honeypot_verdict(ok) == (None, pytest.approx(0.01), pytest.approx(0.02))
    ok["simulationResult"]["sellTax"] = 25
    assert v2.honeypot_verdict(ok)[0] == "high_tax"


def _gt_pool(price_base="0.5", price_quote="3000"):
    return {"attributes": {"address": PAIR, "base_token_price_usd": price_base, "quote_token_price_usd": price_quote,
                           "reserve_in_usd": "20000", "volume_usd": {"m5": "1500"},
                           "transactions": {"m5": {"buys": 12, "sells": 4}}, "fdv_usd": "90000"},
            "relationships": {"base_token": {"data": {"id": f"base_{TOK}"}},
                              "quote_token": {"data": {"id": "base_0x4200000000000000000000000000000000000006"}}}}


def test_gt_snapshot_prices_followed_token():
    tok, s = v2.gt_snapshot(_gt_pool(), None)
    assert tok == TOK and s["price"] == 0.5 and s["liq"] == 20000 and s["buys_m5"] == 12 and s["vol_m5"] == 1500
    _, s = v2.gt_snapshot(_gt_pool(), "0x4200000000000000000000000000000000000006")
    assert s["price"] == 3000                              # followed token is the quote side
    assert v2.gt_snapshot(_gt_pool(price_base=None), None)[1] is None


class _R:
    def __init__(self, code, body):
        self.status_code, self.body = code, body
        self.text = json.dumps(body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self.body


class _Client:
    def __init__(self, goplus_tax=None, honeypot=None):
        self.goplus_tax, self.honeypot, self.urls = goplus_tax, honeypot, []

    async def get(self, url, params=None, **k):
        self.urls.append(url)
        if "dexscreener" in url:
            return _R(200, {"pairs": []})                  # not listed on DexScreener
        if "geckoterminal" in url:
            return _R(200, {"data": [_gt_pool()]})
        if "coinbase" in url:
            return _R(200, {"data": {"amount": "3000"}})
        if "kyberswap" in url:
            out = "8000000000000000" if params["tokenIn"] == oc.NATIVE else "8100000000000000"
            return _R(200, {"code": 0, "data": {"routerAddress": oc.KYBER_ROUTER,
                                                 "routeSummary": {"amountOut": out, "gasUsd": "0.01"}}})
        if "gopluslabs" in url:
            rec = {"is_honeypot": "0", "buy_tax": self.goplus_tax, "sell_tax": self.goplus_tax}
            return _R(200, {"code": 1, "result": {TOK: rec}})
        if "honeypot.is" in url:
            return _R(200, self.honeypot or {})
        raise AssertionError(url)


def _scanner(tmp_path, client, chain):
    sc = v2.ScannerV2(str(tmp_path / "v2.db"), str(tmp_path / "m.db"), str(tmp_path / "v1.db"), client=client)
    for k in sc.lim:
        sc.lim[k] = oc.Limiter(1e6)
    sc.funded = {chain: 100.0}
    sc.funded_at = 1e18
    sc.add_pair(chain, PAIR, oc._now() - 60, "geckoterminal", TOK, "T", "pons-v2", "WETH")
    return sc


def test_vet_gt_fallback_and_second_tax_source(tmp_path):
    hp = {"honeypotResult": {"isHoneypot": False}, "simulationSuccess": True,
          "simulationResult": {"buyTax": 0, "sellTax": 1, "transferTax": 0}}
    sc = _scanner(tmp_path, _Client(goplus_tax=None, honeypot=hp), "base")
    asyncio.run(sc.vet("base", PAIR))
    r = sc.c.execute("SELECT * FROM oc_pairs").fetchone()
    assert r["stage"] == "followed" and r["price_source"] == "geckoterminal" and r["tax_source"] == "honeypot.is"
    assert r["sell_tax"] == pytest.approx(0.01) and r["vet_wait_s"] >= 0 and r["followed_at"]


def test_vet_tax_unknown_waits_then_skips(tmp_path, monkeypatch):
    sc = _scanner(tmp_path, _Client(goplus_tax=None), "robinhood")   # honeypot.is has no Robinhood

    async def go():
        await sc.vet("robinhood", PAIR)
        r = sc.c.execute("SELECT stage, tax_unknown_at FROM oc_pairs").fetchone()
        assert r[0] == "seen" and r[1]                     # re-check scheduled, not skipped yet
        t0 = r[1]
        monkeypatch.setattr(v2, "_now", lambda: t0 + 121)
        await sc.vet("robinhood", PAIR)
    asyncio.run(go())
    r = sc.c.execute("SELECT stage, skip_reason FROM oc_pairs").fetchone()
    assert tuple(r) == ("skipped", "tax_unknown")
    assert not any("honeypot.is" in u for u in sc.http.urls)


def test_vet_known_tax_follows_on_goplus(tmp_path):
    sc = _scanner(tmp_path, _Client(goplus_tax="0.01"), "robinhood")
    asyncio.run(sc.vet("robinhood", PAIR))
    r = sc.c.execute("SELECT stage, tax_source FROM oc_pairs").fetchone()
    assert tuple(r) == ("followed", "goplus")


def test_board_v2(tmp_path):
    assert v2.board(str(tmp_path / "none.db"))["status"] == "not started"
    sc = _scanner(tmp_path, _Client(goplus_tax="0.01"), "base")
    asyncio.run(sc.vet("base", PAIR))
    sc.c.commit()
    b = v2.board(str(tmp_path / "v2.db"))
    assert b["study"] == "other_chains_v2" and b["prereg_sha256"] == sc.sha
    base = {c["chain"]: c for c in b["chains"]}["base"]
    assert base["seen"] == 1 and base["buyable_followed"] == 1
    assert base["followed_by_price_source"] == {"geckoterminal": 1}


def test_v2_marker_does_not_alias_v1():
    from noema.heartbeats import LOOPS
    assert LOOPS["agent:other_chains"][0] not in LOOPS["agent:otherchain_v2"][0]


def test_geckoterminal_heartbeat_counts_any_successful_reader(tmp_path):
    import sqlite3
    import time

    from noema import heartbeats as hb
    t = sqlite3.connect(tmp_path / "timing.db")
    t.execute("CREATE TABLE pools(mint TEXT, pool TEXT, reserve_usd REAL, fetched_at REAL)")
    t.execute("CREATE TABLE candles(pool TEXT, ts INTEGER)")
    now = time.time()
    t.execute("INSERT INTO pools VALUES('m','p',1,?)", (now - 7200,))   # no new mint for 2 h (cache hits only)
    t.execute("INSERT INTO candles VALUES('p',?)", (int(now - 90),))    # but candle refreshes keep succeeding
    t.commit()
    r = hb._gecko_activity(lambda f: str(tmp_path / f))
    assert r["name"] == "feed:geckoterminal" and r["success"] == pytest.approx(int(now - 90), abs=1)
    assert "timing candles" in r["detail"]
