from noema.research_signals import (
    FlowWindow,
    assess_flow_toxicity,
    assess_queue_crowding,
    time_to_resolution_pressure,
)


def test_one_sided_flow_scores_more_toxic() -> None:
    balanced = assess_flow_toxicity(FlowWindow(100, 100, 0.0, 20))
    one_sided = assess_flow_toxicity(FlowWindow(200, 0, 0.05, 20))
    assert one_sided.toxicity_score > balanced.toxicity_score


def test_queue_crowding_grows_with_contracts_ahead() -> None:
    near = assess_queue_crowding(contracts_ahead=5, own_size=10)
    far = assess_queue_crowding(contracts_ahead=500, own_size=10)
    assert far.crowding_score > near.crowding_score


def test_resolution_pressure_rises_near_end() -> None:
    early = time_to_resolution_pressure(7200)
    late = time_to_resolution_pressure(60)
    assert late.normalized_pressure > early.normalized_pressure
