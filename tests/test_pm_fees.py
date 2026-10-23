import pytest

from noema import paper_risk as pr
from noema import pm_fees
from noema import prediction_inbox as pi

ZERO = {"id": "5951034", "feesEnabled": True, "feeType": "zero_fees",
        "feeSchedule": {"exponent": 1, "rate": 0, "takerOnly": True, "rebateRate": 0}}
CRYPTO = {"id": "5739564", "feesEnabled": True, "feeType": "crypto_fees_v2",
          "feeSchedule": {"exponent": 1, "rate": 0.07, "takerOnly": True, "rebateRate": 0.2}}
OFF = {"id": "5686979", "feesEnabled": False, "feeType": None, "feeSchedule": None}


def _fetcher(mid):
    m = {"5951034": ZERO, "5739564": CRYPTO, "5686979": OFF}.get(mid)
    if m is None:
        raise OSError("down")
    return pm_fees.parse(m)


def test_parse_gamma_fee_fields():
    assert pm_fees.parse(ZERO)["rate"] == 0 and pm_fees.parse(ZERO)["known"]
    assert pm_fees.parse(CRYPTO)["rate"] == 0.07
    assert pm_fees.parse(OFF) == {**pm_fees.parse(OFF), "known": True, "rate": 0.0}
    assert not pm_fees.parse({"id": "1", "feesEnabled": True})["known"]


def test_per_market_fee_and_flat_fallback():
    s = pm_fees.refresh(["5951034", "5739564", "5686979", "999"], fetcher=_fetcher)
    assert s == {"due": 4, "known": 3, "unknown": 1, "errors": 1}
    assert pr.fee_info("polymarket", 0.56, "5951034") == (0.0, False)
    assert pr.fee_info("polymarket", 0.5, "5739564") == (pytest.approx(0.0175), False)
    assert pr.fee_info("polymarket", 0.5, "5686979") == (0.0, False)
    f, assumed = pr.fee_info("polymarket", 0.5, "999")
    assert assumed and f == pytest.approx(0.0695 * 0.25)
    assert pr.fee_info("polymarket-us", 0.5, "5951034") == (pytest.approx(0.0695 * 0.25), False)
    assert pr.fee_info("kalshi", 0.5) == (pytest.approx(0.0175), False)
    assert pm_fees.due(["5951034", "999"]) == []  # cached; unknown retried after RETRY_S


def test_pair_cost_uses_market_fee():
    pm_fees.refresh(["5951034"], fetcher=_fetcher)
    legs = [{"venue": "kalshi", "market": "k", "market_id": "KX", "side": "YES", "price": 0.39},
            {"venue": "polymarket", "market": "p", "market_id": "5951034", "side": "NO", "price": 0.56}]
    m = pi.pair_metrics(legs, None, None)
    assert m["combined_cost"] == 0.9667 and m["fee_assumed"] is False
    legs[1]["market_id"] = "123"
    m = pi.pair_metrics(legs, None, None)
    assert m["combined_cost"] == 0.9838 and m["fee_assumed"] is True


def test_parlay_and_kelly_use_leg_fees():
    pm_fees.refresh(["5951034"], fetcher=_fetcher)
    legs = '[{"venue":"polymarket","market_id":"5951034","price":0.5},{"venue":"polymarket","market_id":"x1","price":0.5}]'
    f, assumed = pr.pick_fee("mixed", 0.25, None, legs, "parlay")
    assert f == pytest.approx(0.0695 * 0.25) and assumed
    free = pr.pick_stake("polymarket", 0.60, 0.56, None, 1000.0, market_id="5951034")
    flat = pr.pick_stake("polymarket", 0.60, 0.56, None, 1000.0, market_id="nope")
    assert free > flat
