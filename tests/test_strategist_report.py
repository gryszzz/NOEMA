from noema.strategist import ModelBelief
from noema.strategist_audit import build_strategist_audit
from noema.strategist_report import strategist_report


def test_report_exposes_reasoning_components() -> None:
    audit = build_strategist_audit(
        market_probability=0.50,
        yes_ask=0.52,
        no_ask=0.49,
        beliefs=[ModelBelief("model", 0.65, 1.0, 500)],
        data_staleness_seconds=1,
        liquidity_usd=10000,
        spread=0.03,
    )
    report = strategist_report(audit)
    assert "ensemble" in report
    assert "uncertainty" in report
    assert "decision_quality" in report
    assert report["best_side"] in {"yes", "no"}
