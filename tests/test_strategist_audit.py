from noema.strategist import ModelBelief
from noema.strategist_audit import build_strategist_audit


def test_audit_combines_models_costs_and_uncertainty() -> None:
    audit = build_strategist_audit(
        market_probability=0.50,
        yes_ask=0.52,
        no_ask=0.49,
        beliefs=[
            ModelBelief("a", 0.70, reliability=1.0, sample_size=500),
            ModelBelief("b", 0.68, reliability=0.8, sample_size=400),
        ],
        data_staleness_seconds=1,
        liquidity_usd=100_000,
        spread=0.02,
        fee_fraction=0.005,
        slippage_fraction=0.005,
    )
    assert audit.ensemble.probability_yes > 0.50
    assert audit.uncertainty.penalty > 0
    assert audit.edges.best is not None
