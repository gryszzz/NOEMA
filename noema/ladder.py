from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .agent_dashboard import build_agent_overview
from .dashboard_data import build_overview
from .doctor import doctor_report
from .history_forecaster import MODEL_VERSION
from .outcomes import OutcomeStore
from .paired_evaluation import compare_history_to_market


def _candidate_count(path: str) -> int:
    if not Path(path).exists():
        return 0
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            """
            SELECT COUNT(*) FROM forecast_ledger
            WHERE json_extract(forecast_json, '$.model_version') = ?
            """,
            (MODEL_VERSION,),
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def build_ladder_report(path: str = "data/noema.db") -> dict[str, Any]:
    """Report observed readiness; never infer trading permission from configuration."""
    agent = build_agent_overview(path)
    cycle = agent.get("last_cycle") or {}
    checks = {item["name"]: item for item in doctor_report(path)["checks"]}
    overview = build_overview(path)
    scored = OutcomeStore(path).evaluate_ledger()
    paired = compare_history_to_market(path)
    candidates = _candidate_count(path)
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
            "status": "observed" if scored.count > 0 else "pending",
            "evidence": f"{overview.get('forecasts', 0)} recorded forecasts; "
                        f"{scored.count} distinct market/model pairs scored",
            "next_action": "noema sync-outcomes --limit 2000 && noema evaluate"
                           if scored.count == 0 else None,
        },
        {
            "rung": "independent_forecast",
            "status": "observed" if candidates > 0 else "pending",
            "evidence": f"{candidates} paper-only history forecasts; "
                        f"{paired.distinct_resolved_markets} paired markets in "
                        f"{paired.distinct_resolved_events} resolved events",
            "next_action": "noema sync-outcomes --limit 2000 && noema agent-once"
                           if candidates == 0 else "noema sync-outcomes && noema compare",
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
