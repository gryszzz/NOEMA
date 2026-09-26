from noema.agent_planner import AgentGoalSelection, refine_goal_with_cognition
from noema.cognition_models import CognitionPacket, CognitionResult


def result(mode: str) -> CognitionResult:
    return CognitionResult(
        status="completed",
        packet=CognitionPacket(
            market_id="M",
            thesis="test",
            confidence=0.8,
            attention_reason="edge",
            counterarguments=(),
            unknowns=(),
            requested_research=(),
            recommended_mode=mode,
            evidence_ids=(),
        ),
    )


def test_cognition_can_refine_research_goal() -> None:
    goal = AgentGoalSelection("calibrate_and_collect", "baseline")
    refined = refine_goal_with_cognition(goal, result("investigate"))
    assert refined.goal == "model_guided_investigation"


def test_cognition_cannot_override_recovery_goal() -> None:
    goal = AgentGoalSelection("restore_market_perception", "broken")
    refined = refine_goal_with_cognition(goal, result("investigate"))
    assert refined == goal
