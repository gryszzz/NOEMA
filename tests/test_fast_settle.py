from datetime import UTC, datetime, timedelta

from noema import fast_settle as fs


class FakeApi:
    calls = 0

    def __init__(self, now):
        self.now, self.result = now, ""

    def get(self, url, params=None):
        m = {"ticker": "KXHIGHNY-1-B60", "event_ticker": "KXHIGHNY-1", "market_type": "binary",
             "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.44", "last_price_dollars": "0.42",
             "close_time": (self.now + timedelta(hours=1)).isoformat(), "strike_type": "between",
             "status": "finalized" if self.result else "active", "result": self.result, "title": "NYC high"}
        return {"markets": [m], "cursor": None}


def test_forecast_before_close_then_settle_scores(tmp_path):
    db = str(tmp_path / "t.db")
    now = datetime(2026, 10, 21, 12, tzinfo=UTC)
    api = FakeApi(now)
    out = fs.run_once(db, api, now, force_discover=True)
    assert out["discovered"] == 1 and out["forecasts_written"] == 2  # mid benchmark + longshot fade
    assert out["by_category"] == {"weather": 1}
    api.result = "yes"
    later = now + timedelta(hours=2)
    out = fs.run_once(db, api, later)
    assert out["settled_markets"] == 1 and out["resolved_forecasts"] == 2
    mid = next(a for a in out["agents"] if a["agent"] == "fs-market-mid")
    assert abs(mid["brier"] - (0.42 - 1) ** 2) < 1e-6
    # idempotent: a second pass neither re-forecasts nor re-scores
    out = fs.run_once(db, api, later + timedelta(minutes=1))
    assert out["forecasts_written"] == 0 and out["settled_markets"] == 0


def test_spot_probability_between():
    row = {"strike_type": "between", "floor_strike": 99.0, "cap_strike": 101.0}
    p = fs.spot_probability(row, 100.0, 1e-4, 3600)
    assert 0.5 < p < 0.99
