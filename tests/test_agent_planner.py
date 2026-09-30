import pytest

from noema.agent_identity import MISSION_OPERATIONAL_GOALS
from noema.agent_planner import AgentGoalSelection, choose_goal


def test_planner_prioritizes_broken_market_perception() -> None:
    result = choose_goal(
        radar=[],
        market_data_healthy=False,
    )
    assert result.goal == "restore_market_perception"
    assert result.mission_objective == MISSION_OPERATIONAL_GOALS[result.goal]


def test_planner_collects_when_no_radar_exists() -> None:
    result = choose_goal(
        radar=[],
        market_data_healthy=True,
    )
    assert result.goal == "collect_world_state"
    assert result.mission_objective == MISSION_OPERATIONAL_GOALS[result.goal]


def test_planner_can_follow_ecosystem_specialist_focus() -> None:
    result = choose_goal(
        radar=[],
        market_data_healthy=True,
        ecosystem_focus="trench-1",
    )
    assert result.goal == "develop_specialist"
    assert "trench-1" in result.reason
    assert result.mission_objective == MISSION_OPERATIONAL_GOALS[result.goal]


def test_operational_goal_outside_mission_is_rejected() -> None:
    with pytest.raises(ValueError, match="not aligned with the NOEMA mission"):
        AgentGoalSelection("maximize_trade_count", "unsupported objective")
