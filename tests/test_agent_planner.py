from noema.agent_planner import choose_goal


def test_planner_prioritizes_broken_market_perception() -> None:
    result = choose_goal(
        radar=[],
        kalshi_healthy=False,
        wallet_healthy=True,
        economic_initialized=True,
    )
    assert result.goal == "restore_market_perception"


def test_planner_collects_when_no_radar_exists() -> None:
    result = choose_goal(
        radar=[],
        kalshi_healthy=True,
        wallet_healthy=True,
        economic_initialized=True,
    )
    assert result.goal == "collect_world_state"
