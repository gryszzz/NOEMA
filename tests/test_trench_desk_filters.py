from noema.trench_models import TokenControlState
from noema.trench_risk import (
    DeskEntryPolicy,
    evaluate_desk_entry,
    spike_acceleration,
    top10_holder_fraction,
)

GOOD = {"liquidity_usd": 50_000, "pool_age_seconds": 7200, "holder_shares": (0.5, 0.05, 0.04, 0.03),
            "price_change_m5": 1.0, "price_change_h1": 6.0}


def test_good_candidate_passes():
    ok, reasons, metrics = evaluate_desk_entry(**GOOD)
    assert ok, reasons
    assert metrics["top10_holder_fraction"] == 0.12  # largest (pool vault) excluded


def test_each_rule_rejects():
    cases = {
        "liquidity below desk floor": {"liquidity_usd": 11_999},
        "pool younger than minimum age": {"pool_age_seconds": 1800},
        "pool age unknown": {"pool_age_seconds": None},
        "top-10 holders above concentration limit": {"holder_shares": (0.5,) + (0.07,) * 10},
        "holder concentration unknown": {"holder_shares": None},
        "mid-spike: 5m move above limit": {"price_change_m5": 20.0},
        "mid-spike: price accelerating": {"price_change_m5": 8.0, "price_change_h1": 10.0},
    }
    for expected, override in cases.items():
        ok, reasons, _ = evaluate_desk_entry(**{**GOOD, **override})
        assert not ok and expected in reasons, (expected, reasons)


def test_control_flags_reject():
    ok, reasons, _ = evaluate_desk_entry(
        **GOOD, control=TokenControlState(freeze_authority_present=True))
    assert not ok and "freeze authority retained" in reasons


def test_thresholds_from_env(monkeypatch):
    monkeypatch.setenv("NOEMA_DESK_MIN_LIQUIDITY_USD", "100000")
    monkeypatch.setenv("NOEMA_DESK_EXCLUDE_LARGEST_HOLDER", "0")
    policy = DeskEntryPolicy.from_env()
    assert policy.min_liquidity_usd == 100_000 and not policy.exclude_largest_holder
    _ok, reasons, _ = evaluate_desk_entry(**GOOD, policy=policy)
    assert "liquidity below desk floor" in reasons


def test_helpers():
    assert top10_holder_fraction((), exclude_largest=True) is None
    assert spike_acceleration(None, 3) is None
    assert spike_acceleration(-2, 10) == 0.0
    assert spike_acceleration(5, 12) == 5.0


def test_aggregated_top10_override():
    ok, reasons, metrics = evaluate_desk_entry(**{**GOOD, "holder_shares": None},
                                               top10_fraction=0.7)
    assert not ok and "top-10 holders above concentration limit" in reasons
    assert metrics["top10_holder_fraction"] == 0.7
