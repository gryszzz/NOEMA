from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class AgentConnectionState:
    status: str
    detail: str | None = None


@dataclass(frozen=True)
class AgentCycleState:
    cycle_id: int
    started_at: datetime
    completed_at: datetime | None
    active_goal: str
    health: str
    kalshi: AgentConnectionState
    evm_wallet: AgentConnectionState
    radar_markets: int
    economic_state: str
    ecosystem_state: str = "uninitialized"
    ecosystem_focus: str | None = None
    cognition: AgentConnectionState = AgentConnectionState("unconfigured")
    note: str | None = None
    market_data: AgentConnectionState = AgentConnectionState("unconfigured")
    trench: AgentConnectionState = AgentConnectionState("disabled")
    polymarket_us: AgentConnectionState = AgentConnectionState("unconfigured")
    duration_seconds: float | None = None
    cadence_seconds: float | None = None
    stage_timings: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentStatus:
    agent_id: str
    name: str
    version: str
    mission: str
    running: bool
    last_heartbeat_at: datetime | None
    last_cycle: AgentCycleState | None
