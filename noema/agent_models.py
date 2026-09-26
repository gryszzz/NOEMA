from __future__ import annotations

from dataclasses import dataclass
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
    cognition: AgentConnectionState = AgentConnectionState("unconfigured")
    note: str | None = None


@dataclass(frozen=True)
class AgentStatus:
    agent_id: str
    name: str
    version: str
    mission: str
    running: bool
    last_heartbeat_at: datetime | None
    last_cycle: AgentCycleState | None
