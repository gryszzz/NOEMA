from noema.agent_planner import choose_goal


def test_planner_prioritizes_broken_market_perception() -> None:
    result = choose_goal(
        radar=[],
        market_data_healthy=False,
    )
    assert result.goal == "restore_market_perception"


def test_planner_collects_when_no_radar_exists() -> None:
    result = choose_goal(
        radar=[],
        market_data_healthy=True,
    )
    assert result.goal == "collect_world_state"
