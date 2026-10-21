import json
import sqlite3
from datetime import UTC, datetime, timedelta

from noema import edge_strategies as es
from noema.accuracy_ledger import ensure_accuracy_schema

NOW = datetime(2026, 10, 21, 22, 0, tzinfo=UTC)


def _q(venue, mid, yb, ya, title="", rules="", closes=None):
    return {"venue": venue, "market_id": mid, "title": title, "rules": rules, "closes_at": closes,
            "yes_bid": yb, "yes_ask": ya, "no_bid": round(1 - ya, 4), "no_ask": round(1 - yb, 4)}


def _conn():
    conn = sqlite3.connect(":memory:")
    es.ensure_schema(conn)
    ensure_accuracy_schema(conn)
    conn.execute("CREATE TABLE outcomes (venue TEXT, market_id TEXT, outcome_yes INTEGER, resolved_at TEXT, "
                 "first_seen_at TEXT, raw_json TEXT, PRIMARY KEY (venue, market_id))")
    conn.execute("CREATE TABLE market_snapshots (id INTEGER PRIMARY KEY, venue TEXT, market_id TEXT, captured_at TEXT, "
                 "valid INTEGER, validation_json TEXT, snapshot_json TEXT)")
    return conn


def test_ladder_monotonicity_arb_found_and_costed():
    t = "Will BTC be above {} at 5pm?"
    lo = _q(es.KALSHI, "KXBTC-26OCT22-T100", 0.40, 0.42, t.format(100))
    hi = _q(es.KALSHI, "KXBTC-26OCT22-T110", 0.55, 0.57, t.format(110))  # higher strike priced higher: violation
    arbs = es.ladder_arbs([lo, hi])
    assert len(arbs) == 1 and arbs[0]["kind"] == "ladder_monotonicity"
    a = arbs[0]
    assert a["legs"][0]["side"] == "yes" and a["legs"][1]["side"] == "no"
    assert abs(a["cost"] - (es.leg_cost(es.KALSHI, 0.42) + es.leg_cost(es.KALSHI, 0.45))) < 1e-4
    assert a["edge"] > 0
    ok_hi = _q(es.KALSHI, "KXBTC-26OCT22-T110", 0.30, 0.32, t.format(110))
    assert es.ladder_arbs([lo, ok_hi]) == []


def test_ladder_never_mixes_above_and_below_strikes():
    """Kalshi temperature events mix '<54' and '>61' T-markets; comparing them is a fake arb."""
    lo = _q(es.KALSHI, "KXLOWTHOU-26OCT22-T54", 0.01, 0.02, "Will the minimum temperature be <54° on Oct 22?")
    hi = _q(es.KALSHI, "KXLOWTHOU-26OCT22-T61", 0.69, 0.80, "Will the minimum temperature be >61° on Oct 22?")
    assert es.ladder_arbs([lo, hi], min_edge=-1) == []
    b_lo = _q(es.KALSHI, "KXLOWTHOU-26OCT22-T50", 0.40, 0.42, "Will it be <50°?")
    b_hi = _q(es.KALSHI, "KXLOWTHOU-26OCT22-T55", 0.30, 0.32, "Will it be <55°?")  # below-55 must be >= below-50
    arb = es.ladder_arbs([b_lo, b_hi])
    assert arb and arb[0]["legs"][0]["market_id"].endswith("T55") and arb[0]["legs"][0]["side"] == "yes"
    assert es.strike_direction("Will BTC be above 100 or below 90?") is None


def test_arb_requires_synchronous_quotes():
    t = "Will BTC be above {} at 5pm?"
    lo = {**_q(es.KALSHI, "KXBTC-26OCT22-T100", 0.40, 0.42, t.format(100)), "at": 1000.0}
    hi = {**_q(es.KALSHI, "KXBTC-26OCT22-T110", 0.55, 0.57, t.format(110)), "at": 1000.0 + 3600}
    assert es.ladder_arbs([lo, hi]) == []
    hi["at"] = 1030.0
    assert len(es.ladder_arbs([lo, hi])) == 1


def test_bracket_no_sum_arb_and_no_false_positive():
    br = [_q(es.KALSHI, f"KXETH-26OCT22-B{k}", 0.30, 0.31) for k in (1, 2, 3)]
    assert es.ladder_arbs(br) == []  # NO costs ~0.70 each -> 2.1+ > 2 payoff
    cheap = [_q(es.KALSHI, f"KXETH-26OCT22-B{k}", 0.40, 0.41) for k in (1, 2, 3)]
    out = es.ladder_arbs(cheap)
    assert out and out[0]["kind"] == "bracket_sum" and out[0]["locked_payoff"] == 2.0


def test_cross_venue_requires_match_and_gap_after_fees():
    k = _q(es.KALSHI, "KXSENATETX-26-R", 0.50, 0.52, "Ken Paxton wins Texas Senate race Nov 3")
    p = _q(es.PMUS, "ewc-usse-tx-2026-11-03-rep", 0.60, 0.61, "Ken Paxton wins Texas Senate race Nov 3")
    gaps = es.cross_venue_gaps([k, p])
    assert gaps and gaps[0]["needs_resolution_review"] and gaps[0]["legs"][0]["venue"] == es.KALSHI
    p2 = _q(es.PMUS, "x", 0.51, 0.53, "Ken Paxton wins Texas Senate race Nov 3")
    assert es.cross_venue_gaps([k, p2]) == []  # 1c gap does not clear fees
    other = _q(es.PMUS, "y", 0.80, 0.81, "Lakers beat Celtics tonight")
    assert es.cross_venue_gaps([k, other]) == []


def _rows(n, mid, yes_rate, fam="KXNHLPTS"):
    out = []
    for i in range(n):
        out.append({"market_id": f"{fam}-{i}", "family": es.market_family(fam), "event": f"E{mid}-{i}",
                    "bid": mid - 0.01, "ask": mid + 0.01, "mid": mid, "y": int((i * 7919) % 100 < yes_rate * 100),
                    "close": NOW - timedelta(days=n - i)})
    return out


def test_fav_longshot_walk_forward_finds_real_bias_and_rejects_fair_prices():
    biased = _rows(400, 0.07, 0.0)  # longshots at 7c that never hit -> NO is +EV after fees
    bt = es.backtest_fav_longshot(biased)
    assert bt["ev"] > 0 and bt["n"] > 0 and bt["live_rules"]
    fair = _rows(400, 0.30, 0.30)
    bt2 = es.backtest_fav_longshot(fair)
    assert bt2["verdict"] == "no_edge_after_fees" and not bt2["live_rules"]
    assert es.backtest_fav_longshot(_rows(20, 0.07, 0.0))["verdict"] == "insufficient_history"


def test_wallet_scoring_needs_long_significant_record():
    conn = _conn()
    trades = []
    for i in range(20):
        trades += [{"tx_from_address": "W1", "kind": "buy", "tx_hash": f"b{i}", "volume_in_usd": 100,
                    "price_to_in_usd": 1, "block_timestamp": (NOW - timedelta(hours=5)).isoformat()},
                   {"tx_from_address": "W1", "kind": "sell", "tx_hash": f"s{i}", "volume_in_usd": 130 + i,
                    "price_from_in_usd": 1.3, "block_timestamp": (NOW - timedelta(hours=4)).isoformat()}]
    for i in range(20):
        es.ingest_trades(conn, "solana", f"T{i}", trades[2 * i: 2 * i + 2])
    es.ingest_trades(conn, "solana", "T0", [{"tx_from_address": "W2", "kind": "buy", "tx_hash": "x", "volume_in_usd": 5,
                                             "price_to_in_usd": 1, "block_timestamp": NOW.isoformat()}])
    res = es.score_wallets(conn, NOW)
    assert res["followed"] == 1
    assert conn.execute("SELECT followed FROM wallet_scores WHERE wallet='W2'").fetchone() == (0,)
    es.ingest_trades(conn, "solana", "NEW", [{"tx_from_address": "W1", "kind": "buy", "tx_hash": "n",
                                              "volume_in_usd": 50, "price_to_in_usd": 2,
                                              "block_timestamp": (NOW - timedelta(minutes=10)).isoformat()}])
    sigs = es.smart_money_signals(conn, NOW)
    assert [s["legs"][0]["market_id"] for s in sigs] == ["NEW"]


def test_catalyst_screen_rejects_wash_and_requires_two_catalysts():
    class C:
        symbol, address = "MOON", "M"
        volume_24h, liquidity, age_hours, h6, h24 = 900_000, 600_000, 100, 5, 10
        buys_h1, sells_h1, volume_h1, buys_h24 = 300, 100, 90_000, 2400
    got = es.catalyst_screen(C, None, NOW)
    assert all(v["pass"] for v in got.values())
    C.volume_24h = 50_000_000  # 83x turnover -> wash
    assert es.catalyst_screen(C, None, NOW)["no_wash_trading"]["pass"] is False
    C.volume_24h, C.age_hours = 900_000, 10  # fresh launch: only one catalyst left
    assert es.catalyst_screen(C, None, NOW)["catalyst"]["pass"] is False
    assert es.catalyst_screen(C, None, NOW, {"graphInsidersDetected": 3})["not_bundled_launch"]["pass"] is False


def test_momentum_backtest_and_regime():
    up = [100 * 1.01 ** i for i in range(60)]
    assert es.regime_signal(up)["regime"] == "risk_on"
    assert es.regime_signal(list(reversed(up)))["regime"] == "flat"
    bt = es.backtest_momentum(up)
    assert bt["ev"] > 0 and bt["days_in_market"] > 0


def test_cycle_opens_paper_signal_settles_and_allocator_benches(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    es.ensure_schema(conn)
    ensure_accuracy_schema(conn)
    c2 = _conn()
    for sql in c2.execute("SELECT sql FROM sqlite_master WHERE name IN ('outcomes','market_snapshots')"):
        conn.execute(sql[0])
    t = "Will BTC be above {} at 5pm?"
    for mid, yb, ya in (("KXBTC-26OCT22-T100", 0.40, 0.42), ("KXBTC-26OCT22-T110", 0.55, 0.57)):
        snap = {"yes_bid": yb, "yes_ask": ya, "no_bid": 1 - ya, "no_ask": 1 - yb, "title": t.format(mid[-3:])}
        conn.execute("INSERT INTO market_snapshots (venue, market_id, captured_at, valid, validation_json, snapshot_json)"
                     " VALUES (?,?,?,1,'{}',?)", (es.KALSHI, mid, NOW.isoformat(), json.dumps(snap)))
    rep = es.run_cycle(conn, now=NOW, fetch=False)
    assert rep["opened"].get("pm_arb") == 1
    row = conn.execute("SELECT size_usd, forecast_id FROM strategy_signals WHERE strategy='pm_arb'").fetchone()
    assert 0 < row[0] <= 25 and row[1]
    assert conn.execute("SELECT agent_id, decision FROM accuracy_forecasts").fetchone() == ("strategy:pm_arb",
                                                                                           "paper_trade")
    for mid, y in (("KXBTC-26OCT22-T100", 1), ("KXBTC-26OCT22-T110", 0)):
        conn.execute("INSERT INTO outcomes VALUES (?,?,?,?,?,?)", (es.KALSHI, mid, y, NOW.isoformat(), NOW.isoformat(), "{}"))
    closed = es.settle(conn, [], NOW + timedelta(hours=1))
    assert closed and closed[0]["pnl_usd"] > 0
    for i in range(31):
        conn.execute("INSERT INTO strategy_signals (at, strategy, key, legs_json, cost, size_usd, status, pnl_usd) "
                     "VALUES (?, 'news_latency', ?, '[]', 0.5, 10, 'closed', ?)", (NOW.isoformat(), f"k{i}", -1 - i % 3))
    alloc = {a["strategy"]: a for a in es.allocate(conn, NOW)}
    assert alloc["news_latency"]["status"] == "benched" and alloc["news_latency"]["weight"] == 0
    conn.commit()
    board = es.strategies_board(str(db))
    assert board["live_supported"] is False and {s["strategy"] for s in board["strategies"]} == set(es.STRATEGIES)
