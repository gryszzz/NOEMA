from __future__ import annotations

from typing import Any

from .agent_dashboard import build_agent_overview
from .dashboard_data import build_overview
from .doctor import doctor_report


def build_ladder_report(path: str = "data/noema.db") -> dict[str, Any]:
    """Report observed readiness; never infer trading permission from configuration."""
    agent = build_agent_overview(path)
    cycle = agent.get("last_cycle") or {}
    checks = {item["name"]: item for item in doctor_report(path)["checks"]}
    overview = build_overview(path)
    market = cycle.get("market_data") or {}
    kalshi = cycle.get("kalshi") or {}
    wallet = cycle.get("evm_wallet") or {}
    cognition = cycle.get("cognition") or {}

    steps = [
        {
            "rung": "observe",
            "status": "observed" if market.get("status") == "connected" else "pending",
            "evidence": market.get("detail") or "no successful market collection recorded",
            "next_action": "noema agent-once" if market.get("status") != "connected" else None,
        },
        {
            "rung": "stay_online",
            "status": "observed" if agent["alive"] else "pending",
            "evidence": "recent agent heartbeat" if agent["alive"] else "agent has no recent live heartbeat",
            "next_action": None if agent["alive"] else "noema-agent",
        },
        {
            "rung": "forecast_and_score",
            "status": "observed" if overview.get("forecasts", 0) > 0 else "pending",
            "evidence": f"{overview.get('forecasts', 0)} recorded forecasts; "
                        f"{overview.get('resolved_markets', 0)} resolved markets",
            "next_action": "noema sync-outcomes && noema evaluate",
        },
        {
            "rung": "model_research",
            "status": "observed" if cognition.get("status") == "completed" else "pending",
            "evidence": f"Foundry: {cognition.get('status', 'unconfigured')}; "
                        f"configuration: {checks['foundry']['status']}",
            "next_action": "noema setup && noema doctor"
                           if checks["foundry"]["status"] != "ready" else "noema agent-once",
        },
        {
            "rung": "account_and_wallet_observation",
            "status": "observed"
                      if kalshi.get("status") == wallet.get("status") == "connected"
                      else "pending",
            "evidence": f"Kalshi account: {kalshi.get('status', 'unconfigured')}; "
                        f"EVM: {wallet.get('status', 'unconfigured')}",
            "next_action": "noema setup && noema doctor && noema agent-once",
        },
        {
            "rung": "live_capital",
            "status": "locked",
            "evidence": "This report cannot authorize live orders or wallet signing.",
            "next_action": "Validate paper results and risk controls before a separate live review.",
        },
    ]
    return {"agent_alive": agent["alive"], "steps": steps}
