from noema.agent_models import AgentConnectionState
from noema.agent_runtime import _cycle_health


def test_unselected_provider_and_idle_research_do_not_degrade_core_health() -> None:
    assert _cycle_health(
        market_data=AgentConnectionState("connected"),
        ecosystem_state="active",
        ecosystem_focus="kalshi-history",
        trench=AgentConnectionState("degraded", "stale observations"),
        active_goal="develop_specialist",
        cognition=AgentConnectionState("idle", "no eligible edge"),
        research_status="idle",
    ) == "healthy"


def test_trench_failure_does_not_degrade_an_idle_research_cycle() -> None:
    assert _cycle_health(
        market_data=AgentConnectionState("connected"),
        ecosystem_state="active",
        ecosystem_focus="trench-1",
        trench=AgentConnectionState("degraded", "stale observations"),
        active_goal="develop_specialist",
        cognition=AgentConnectionState("idle"),
        research_status="idle",
    ) == "healthy"


def test_selected_trench_failure_degrades_active_research() -> None:
    assert _cycle_health(
        market_data=AgentConnectionState("connected"),
        ecosystem_state="active",
        ecosystem_focus="trench-1",
        trench=AgentConnectionState("degraded", "provider data unavailable"),
        active_goal="execute_registered_research",
        cognition=AgentConnectionState("idle"),
        research_status="idle",
    ) == "degraded"


def test_required_market_data_failure_is_surfaced() -> None:
    assert _cycle_health(
        market_data=AgentConnectionState("degraded", "no current observations"),
        ecosystem_state="active",
        ecosystem_focus=None,
        trench=AgentConnectionState("connected"),
        active_goal="collect_world_state",
        cognition=AgentConnectionState("idle"),
        research_status="idle",
    ) == "degraded"


def test_required_but_unconfigured_ecosystem_is_partial() -> None:
    assert _cycle_health(
        market_data=AgentConnectionState("connected"),
        ecosystem_state="disabled",
        ecosystem_focus=None,
        trench=AgentConnectionState("connected"),
        active_goal="collect_world_state",
        cognition=AgentConnectionState("idle"),
        research_status="idle",
    ) == "partial"
