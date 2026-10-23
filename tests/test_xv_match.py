import sqlite3

from noema import edge_strategies as es
from noema import xv_match as x


def test_names_match_initials_and_cities():
    assert x.names_match("Los Angeles Clippers", "Los Angeles C")
    assert not x.names_match("Los Angeles Lakers", "Los Angeles C")
    assert x.names_match("Atlanta Hawks", "Atlanta")
    assert x.names_match("New York G", "New York G")
    assert not x.names_match("New York G", "New York J")
    assert x.names_match("Amon-Ra St. Brown", "Amon-Ra St Brown")


def _pm(mid, name, rules, **q):
    return {"venue": es.PMUS, "market_id": mid, "name": name, "title": name, "rules": rules, "closes_at": None, **q}


def _k(mid, name, rules, **q):
    return {"venue": es.KALSHI, "market_id": mid, "name": name, "title": name, "rules": rules, "closes_at": None, **q}


def test_unique_match_and_ambiguity_skip():
    pms = [_pm("a-nyg", "New York G", ""), _pm("a-ari", "Arizona", "")]
    ks = [_k("K-NYG", "New York G", ""), _k("K-NYJ", "New York J", ""), _k("K-ARI", "Arizona", "")]
    pairs, amb = x.match_family("winner", pms, ks)
    assert {(p["market_id"], k["market_id"]) for p, k in pairs} == {("a-nyg", "K-NYG"), ("a-ari", "K-ARI")}
    assert amb == 0


def test_strike_pairs_and_resolution():
    pms = [_pm("btc-110k", "Above $109,999.99", "above $109,999.99 at any point until 12:00 AM ET on January 1, 2027. "
               "CF Benchmarks BRTI")]
    ks = [_k("KXBTCMAXY-26DEC31-109999.99", "Above $109,999.99",
             "CF Bitcoin Real-Time Index is above $109999.99 before Dec 31, 2026 at 11:59 PM ET"),
          _k("KXBTCMAXY-26DEC31-119999.99", "Above $119,999.99", "")]
    pairs, _ = x.match_family("strike_touch", pms, ks)
    assert len(pairs) == 1 and pairs[0][1]["market_id"].endswith("109999.99")
    assert x.resolution_check("strike_touch", *pairs[0])["verdict"] == "equivalent"
    bad = dict(pairs[0][0], rules="price of bitcoin on Binance at close on December 31, 2026")
    assert x.resolution_check("strike_touch", bad, pairs[0][1])["verdict"] == "mismatch"


def test_year_mismatch_is_hard():
    v = x.resolution_check("winner", _pm("p", "Boston", "wins the 2027 NBA Finals"),
                           _k("k", "Boston", "wins the 2026 Pro Basketball Finals"))
    assert v["verdict"] == "mismatch"


def test_gaps_use_official_fees(monkeypatch):
    monkeypatch.delenv("NOEMA_PMUS_FEE_PER_CONTRACT", raising=False)
    assert abs(es.pmus_fee(0.5) - 0.017375) < 1e-9
    pm = _pm("p", "X", "", yes_ask=0.40, no_ask=0.62)
    k = _k("k", "X", "", yes_ask=0.36, no_ask=0.55)
    g = {(r["yes_venue"], r["no_venue"]): r for r in x.gaps(pm, k)}
    r = g[(es.PMUS, es.KALSHI)]
    assert r["raw_gap"] == 0.05
    want = 1 - (0.40 + es.pmus_fee(0.40) + 0.005 + 0.55 + es.kalshi_fee(0.55) + 0.005)
    assert abs(r["edge"] - round(want, 4)) < 1e-9


def test_arb_signals_require_equivalent_and_sync():
    best = {"edge": 0.03, "cost": 0.97, "raw_gap": 0.06, "yes_venue": es.PMUS, "yes_market": "p", "yes_ask": 0.4,
            "no_venue": es.KALSHI, "no_market": "k", "no_ask": 0.54}
    base = {"name": "X", "verdict": "equivalent", "skew_s": 2.0, "best": best, "issues": []}
    assert len(x.arb_signals([base])) == 1
    assert not x.arb_signals([dict(base, verdict="caveat")])
    assert not x.arb_signals([dict(base, skew_s=None)])
    assert not x.arb_signals([dict(base, skew_s=500)])


def test_stored_pairs_roundtrip():
    c = sqlite3.connect(":memory:")
    x.ensure_schema(c)
    c.execute("INSERT INTO xv_pairs VALUES ('p','k','F','X','equivalent','[]','{}','{}','null',1.0,datetime('now'))")
    assert x.stored_pairs(c)[0]["verdict"] == "equivalent"


def test_gap_stats_reports_independent_pair_bucket_samples():
    import sqlite3 as _sqlite3

    from noema import xv_match

    conn = _sqlite3.connect(":memory:")
    xv_match.ensure_schema(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(xv_gap_history)")]
    rows = [("2026-10-23T10:00:00+00:00", 0.05), ("2026-10-23T10:10:00+00:00", 0.06),
            ("2026-10-23T10:20:00+00:00", -0.01), ("2026-10-23T12:00:00+00:00", -0.02)]
    for at, edge in rows:
        values = {"at": at, "pm_market": "PM-1", "kalshi_market": "K-1", "edge": edge,
                  "verdict": "equivalent", "skew_s": 0.0}
        use = [c for c in cols if c in values]
        conn.execute(f"INSERT INTO xv_gap_history ({','.join(use)}) VALUES ({','.join('?' * len(use))})",
                     [values[c] for c in use])
    stats = xv_match.gap_stats(conn, buffer=0.02)
    assert stats["pair_observations"] == 4
    assert stats["observations_clearing"] == 2
    assert stats["independent_observations"] == 2
    assert stats["independent_clearing"] == 1
    assert stats["independent_hit_rate"] == 0.5
