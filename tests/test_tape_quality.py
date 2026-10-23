"""tape_quality: impossible pump.fun prints (mayhem / non-SOL-quote curves, AMM prints, spikes, instant grads)."""
import base64
import math
import sqlite3

from noema import launch_stream as ls
from noema import tape_quality as tq
from noema import trench_edges as te
from noema import trench_hft as hft

# Real pump.fun TradeEvents (Program data) from mainnet txs, Oct 22 2026.
MAYHEM = ("vdt/007mYe58TLSDK07Tuth/HQRzQ2QFlhS63Y8sZrp3zHwztPM4X5FzAQAAAAAA7rRe5TYAAAABnMGVF2209Uc+kCdpyQtI89ekIecKIHLew"
          "iVXyY2tRrHBwNpqAAAAAKXHLxMAAAAAhrVTM3jVAgBGl8oCAAAAAIYdQefm1gEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABfAAAAAA"
          "AAAIgDAAAAAAAA1woqAKjyU9gAAQeDMtOslfUnjE49VwzkNqSNRknWFZIeAAAAAAAAAB4BAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
          "AAAAAAAAEgAAAGJ1eV9leGFjdF9xdW90ZV9pbgEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
          "AAAAAAAAAAAAAAAJFzAQAAAAAApccvEwAAAABGl8oCAAAAAAAAAAAAAAAAAAAAAAAAAACijyUAAAAAAAAAAAAAAAAA")
STANDARD = ("vdt/007mYe5NQaZhx56csl7ucybWm7dySRVCzG4nLzIQcvRHgbyGr3qogAEAAAAAwiOQsIIAAAABQE/VzOtxBiwOU+8VYNqcZeH5tUEKBEYWf"
            "ObEZfYuV4fBwNpqAAAAAE0lDdsIAAAAMi/JjbsBAwBNeeneAQAAADKXtkEqAwIA4ATIfOuY+lzkf4A4Bv0seUXSlSSVmuwA3tl4FPOPeEZfAAAA"
            "AAAAAH6nAwAAAAAAaAwXEOuRjJQuJ7kAC59IV2fK13JEWvNG7c0YrDaZFfUeAAAAAAAAAGsnAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
            "AAAAAAAAAAAAEgAAAGJ1eV9leGFjdF9xdW90ZV9pbgAAAAAAAAAAAAAAAAAAAAAAiBMAAAAAAAC/0wEAAAAAAAEAAAAZ5Psrd+mtDA1CIqtAXXZl"
            "PVzljwbp2s3MKkYS1Y/rEBAnAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB6qIABAAAAAE0lDdsIAAAATXnp3gEAAAAAAAAAAAAAAAAAAA"
            "AAAAAAW4YmAAAAAAAAAAAAAAAAAA==")
NON_SOL = ("vdt/007mYe6xc0h3sT5WrmZtWAUicAyNf9qEa7YL+O4fFBrwOOtvfwAAAAAAAAAAZubi4/gTAAABoPKUMtaJEMnqLB4VA1CGJpgiDt4d9SSBx"
           "Vlnf458lLQeodpqAAAAAAAAAAAAAAAAE3Ms77dxAwAAAAAAAAAAABPbGaMmcwIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABfAAAAAA"
           "AAAAQDEQoAAAAAYzgaZd4c3dRHffEPd97GrQO+mWrfpip6AcWYnC47mjEeAAAAAAAAAA/LLQMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
           "AAAAAAAAEgAAAGJ1eV9leGFjdF9xdW90ZV9pbgAAAAAAAAAAAAAAAAAAAAAAiBMAAAAAAACCgQgFAAAAAAAAAAAMRfffjZ5ylWKEkz9tmLdXAy6D34"
           "RgT7XhF//2HVsS+c5loCMEAAAAnukz37oAAAALUksIEgAAAB4AAAAAAAAAD8stAwAAAAB7I7crAAAAAAAAAAAAAAAA")


def test_decoder_flags_mayhem_and_non_sol_quote_curves():
    std = ls.decode_event(base64.b64decode(STANDARD))
    assert std["quality"] is None and std["mayhem"] == 0 and math.isclose(std["market_cap_sol"], 44.9409)
    may = ls.decode_event(base64.b64decode(MAYHEM))
    assert may["quality"] == "mayhem" and may["market_cap_sol"] < tq.CURVE_MIN_MC  # 0.40 "SOL mcap": impossible
    ns = ls.decode_event(base64.b64decode(NON_SOL))
    assert ns["quality"] == "non_sol_quote" and ns["sol"] == 0 and ns["market_cap_sol"] is None
    assert ns["quote_mint"].startswith("pumpCmXq")
    # the standard print is exactly on the standard curve
    assert abs(tq.curve_ratio(True, std["sol"], std["tokens"], std["market_cap_sol"]) - 1) < 0.01


def test_legacy_short_event_still_decodes():
    ev = ls.decode_event(base64.b64decode(STANDARD)[:129])
    assert ev["quality"] is None and ev["mayhem"] is None and ev["market_cap_sol"] > 27.9


def test_ingest_writes_quality_column(tmp_path):
    c = ls.connect(str(tmp_path / "n.db"))
    ls.FOLLOW.clear()
    now = 1e9
    mints = {}
    for name, b in (("std", STANDARD), ("may", MAYHEM), ("ns", NON_SOL)):
        ev = ls.decode_event(base64.b64decode(b))
        mints[name] = ev["mint"]
        ls.handle_rpc_logs(c, f"sig-{name}", [f"Program data: {b}"], follow={ev["mint"]: now}, now=now, slot=1)
    c.commit()
    q = dict(c.execute("SELECT mint, quality FROM launch_trades"))
    assert q == {mints["std"]: None, mints["may"]: "mayhem", mints["ns"]: "non_sol_quote"}


def test_row_rules():
    assert tq.row_reason("buy", 0, 10, 0) == "zero_sol"
    assert tq.row_reason("buy", 0.1, 10, 0.4) == "below_curve"
    assert tq.row_reason("buy", 0.1, 10, 3000) == "above_curve"
    assert tq.row_reason("x", 0.1, 10, 50) == "bad_side"
    assert tq.row_reason("buy", 0.1, 10, 50, "mayhem") == "mayhem"
    mc = 50.0
    va = math.sqrt(mc * tq.K / 1e9)
    good = tq.K / (va - 0.5) - tq.K / va
    assert tq.row_reason("buy", 0.5, good, mc) is None
    assert tq.row_reason("buy", 0.5, good * 1.3, mc) == "off_curve"
    assert tq.row_reason("buy", 0.0005, good * 3, mc) is None  # too small to check


def _rows(mcs, slot0=None):
    return [tq.row(float(i), "buy", 0.0005, None, m, None if slot0 is None else slot0 + (i > 0), None, f"w{i}")
            for i, m in enumerate(mcs)]


def test_coin_rules():
    st = tq.Stats()
    kept, why, _ = tq.clean_coin(_rows([30, 31, 0.4, 32]), None, st)  # one sub-floor print -> non-standard curve
    assert kept == [] and why == "nonstd_curve"
    kept, why, _ = tq.clean_coin(_rows([30, 35, 120, 36, 40]), None, st)  # isolated spike reverted -> dropped
    assert why is None and [r[tq.MC] for r in kept] == [30, 35, 36, 40]
    kept, why, _ = tq.clean_coin(_rows([350, 380, 400], slot0=7), None, st)  # create-slot buys to 350 SOL
    assert kept == [] and why == "instant_grad"
    kept, why, _ = tq.clean_coin(_rows([30, 40]), 410.9, st)  # create mcap at graduation
    assert why == "instant_grad"
    kept, why, flags = tq.clean_coin(_rows([30, 100, 300, 411]), None, st)
    assert why is None and flags == {"fast_grad"}
    kept, why, _ = tq.clean_coin([tq.row(0.0, "buy", 0, 5, 0, None, None, "w")] * 3, None, st)
    assert kept == [] and st.coins["no_valid_rows"] == 1
    assert st.coins["nonstd_curve"] == 1 and st.coins["instant_grad"] == 2 and st.rows["price_jump"] == 1
    assert st.rows["zero_sol"] == 3


def test_spike_needs_size_mismatch_and_revert():
    big = [tq.row(0.0, "buy", 0.5, None, 30, None, None, "a"), tq.row(1.0, "buy", 40.0, None, 120, None, None, "b"),
           tq.row(2.0, "sell", 40.0, None, 31, None, None, "c")]
    assert tq.clean_coin(big)[0] == big          # a 40 SOL buy can take 30 -> 120 SOL: real, kept
    trend = _rows([30, 70, 75, 80])
    assert len(tq.clean_coin(trend)[0]) == 4     # no revert: a gap in our tape, not a bad tick


def test_mcap_unsupported():
    assert tq.mcap_unsupported(1_200_000, 900)          # $1.2M mcap on a $900 pool
    assert tq.mcap_unsupported(5_000_000, 0)
    assert not tq.mcap_unsupported(1_500_000, 150_000)
    assert not tq.mcap_unsupported(200_000, 50)         # below the $1M runner scale: not judged here


def test_backfill_row_reason():
    base = {"mayhem_mode": "0", "quote_mint": "11111111111111111111111111111111",
            "virtual_sol_reserves": str(38_000_000_000), "real_sol_reserves": str(8_000_000_000)}
    assert tq.backfill_row_reason(base) is None
    assert tq.backfill_row_reason({**base, "mayhem_mode": "1"}) == "mayhem"
    assert tq.backfill_row_reason({**base, "quote_mint": "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"}) == "non_sol_quote"
    assert tq.backfill_row_reason({**base, "real_sol_reserves": "1"}) == "nonstd_curve"


def _db(tmp_path):
    p = str(tmp_path / "n.db")
    c = ls.connect(p)
    c.execute("INSERT INTO launch_stream (mint, received_at, source, creator, initial_sol, market_cap_sol)"
              " VALUES ('goodpump','2026-10-22T17:00:00+00:00','pumpportal','cr',0,27.96)")
    c.execute("INSERT INTO launch_stream (mint, received_at, source, creator, initial_sol, market_cap_sol)"
              " VALUES ('gradpump','2026-10-22T17:00:00+00:00','pumpportal','cr',85,410.88)")
    rows = [("goodpump", m) for m in (30, 32, 34, 100, 35, 36)] + [("gradpump", m) for m in (3000, 3100, 3200)]
    rows += [("maypump", m) for m in (0.4, 2.0, 60.0, 80.0)] + [("rpcpump", m) for m in (30, 33, 36, 40)]
    for i, (mint, mc) in enumerate(rows):
        c.execute("INSERT INTO launch_trades (signature, mint, received_at, side, sol, tokens, trader, market_cap_sol,"
                  " source) VALUES (?,?,?,?,?,?,?,?,?)",
                  (f"s{i}", mint, f"2026-10-22T17:00:{i + 1:02d}+00:00", "buy", 0.0005, None, f"w{i}", mc, "helius"))
    c.commit()
    c.close()
    return p


def test_loaders_exclude_and_count(tmp_path):
    p = _db(tmp_path)
    coins, _ = hft.load(p)
    assert set(coins) == {"goodpump"} and [x[4] for x in coins["goodpump"]["tr"]] == [30, 32, 34, 35, 36]
    assert hft.QUALITY.coins["instant_grad"] == 1 and hft.QUALITY.rows["price_jump"] == 1
    rpc = te._rpc_only(p, set(coins))
    assert set(rpc) == {"rpcpump"} and hft.QUALITY.coins["nonstd_curve"] == 1   # maypump dropped
    old, _ = hft.load(p, quality=False)
    assert "gradpump" in old and max(x[4] for x in old["goodpump"]["tr"]) == 100
    r = tq.report(p)
    assert r["before"]["adj_max"] > 2.5 and r["after"]["adj_max"] < 1.2


def test_flag_history_is_additive_and_idempotent(tmp_path):
    p = _db(tmp_path)
    n = tq.flag_history(p, pause_s=0)
    assert n == {"price_jump": 1, "instant_grad": 3, "nonstd_curve": 4}
    assert tq.flag_history(p, pause_s=0) == {}
    c = sqlite3.connect(p)
    assert c.execute("SELECT COUNT(*) FROM launch_trades").fetchone()[0] == 17   # nothing deleted
    assert c.execute("SELECT market_cap_sol FROM launch_trades WHERE quality='price_jump'").fetchone()[0] == 100
    c.close()
    coins, _ = hft.load(p)   # flagged rows read back as exclusions with the same result
    assert set(coins) == {"goodpump"} and len(coins["goodpump"]["tr"]) == 5
