import sqlite3

from noema import pm_crypto as pc


def _db(rows):
    c = sqlite3.connect(":memory:")
    pc.ensure_schema(c)
    c.executemany("INSERT INTO pm_ref_1m VALUES (?,?,?,?,?,?,?)", rows)
    return c


def test_index_uses_only_closed_bars_and_median():
    c = _db([("KXBTC", "coinbase", 60, 1, 1, 1, 100.0), ("KXBTC", "coinbase", 120, 1, 1, 1, 110.0),
             ("KXBTC", "bitstamp", 60, 1, 1, 1, 102.0), ("KXBTC", "bitstamp", 120, 1, 1, 1, 112.0)])
    ix = pc.Index1m(c, "KXBTC")
    assert ix.at(119) == 101.0  # bars closing at 120 are not yet known at 119
    assert ix.at(120) == 111.0
    assert ix.settle_mirror(120) == (1 + 1 + 1 + 110) / 4 / 2 + (1 + 1 + 1 + 112) / 4 / 2


def test_coinbase_rows_keyed_by_close_time():
    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return [[1000, 9.0, 11.0, 10.0, 10.5, 1.0]]

    class C:
        def get(self, *a, **k):
            return R()

    assert pc._coinbase(C(), "BTC-USD", 0, 2000) == [(1060, 10.0, 11.0, 9.0, 10.5)]


def test_model_prob_monotone_in_strike():
    rows = []
    px = 100.0
    for i in range(1, 400):
        px *= 1.0005 if i % 2 else 0.9995
        rows.append(("KXBTC", "coinbase", i * 60, px, px, px, px))
    ix = pc.Index1m(_db(rows), "KXBTC")
    t = 399 * 60
    lo = pc.model_prob(ix, t, t + 900, "greater", 99.0, None)
    hi = pc.model_prob(ix, t, t + 900, "greater", 101.0, None)
    assert lo and hi and lo[0] > hi[0]
    assert pc.asset_of("KXETH15M") == "KXETH" and pc.asset_of("KXBNBD") is None
