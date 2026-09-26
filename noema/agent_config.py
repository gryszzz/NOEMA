from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentConfig:
    db_path: str = "data/noema.db"
    cycle_interval_seconds: float = 30.0
    heartbeat_interval_seconds: float = 15.0
    max_radar_rows: int = 50
    max_markets_per_cycle: int = 100
    max_event_checks_per_cycle: int = 12
    outcome_sync_interval_seconds: float = 900.0
    max_outcomes_per_sync: int = 2000
    evm_rpc_url: str | None = None
    evm_address: str | None = None

    @classmethod
    def from_env(cls) -> AgentConfig:
        return cls(
            db_path=os.getenv("NOEMA_DB_PATH", "data/noema.db"),
            cycle_interval_seconds=float(
                os.getenv("NOEMA_AGENT_CYCLE_SECONDS", "30")
            ),
            heartbeat_interval_seconds=float(
                os.getenv("NOEMA_AGENT_HEARTBEAT_SECONDS", "15")
            ),
            max_radar_rows=int(os.getenv("NOEMA_AGENT_RADAR_LIMIT", "50")),
            max_markets_per_cycle=int(
                os.getenv("NOEMA_AGENT_MAX_MARKETS_PER_CYCLE", "100")
            ),
            max_event_checks_per_cycle=int(
                os.getenv("NOEMA_AGENT_MAX_EVENT_CHECKS_PER_CYCLE", "12")
            ),
            outcome_sync_interval_seconds=float(
                os.getenv("NOEMA_AGENT_OUTCOME_SYNC_SECONDS", "900")
            ),
            max_outcomes_per_sync=int(
                os.getenv("NOEMA_AGENT_MAX_OUTCOMES_PER_SYNC", "2000")
            ),
            evm_rpc_url=os.getenv("NOEMA_EVM_RPC_URL"),
            evm_address=os.getenv("NOEMA_EVM_ADDRESS"),
        )

    def validate(self) -> None:
        if self.cycle_interval_seconds < 5:
            raise ValueError("cycle_interval_seconds must be >= 5")
        if self.heartbeat_interval_seconds < 5:
            raise ValueError("heartbeat_interval_seconds must be >= 5")
        if self.max_radar_rows <= 0:
            raise ValueError("max_radar_rows must be positive")
        if self.max_markets_per_cycle <= 0:
            raise ValueError("max_markets_per_cycle must be positive")
        if self.max_event_checks_per_cycle <= 0:
            raise ValueError("max_event_checks_per_cycle must be positive")
        if self.outcome_sync_interval_seconds < 60:
            raise ValueError("outcome_sync_interval_seconds must be >= 60")
        if self.max_outcomes_per_sync <= 0:
            raise ValueError("max_outcomes_per_sync must be positive")
        if bool(self.evm_rpc_url) != bool(self.evm_address):
            raise ValueError(
                "NOEMA_EVM_RPC_URL and NOEMA_EVM_ADDRESS must be configured together"
            )
