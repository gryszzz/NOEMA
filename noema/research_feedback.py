"""Measured research failures and data defects can reduce future work, never capital risk."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from .ecosystem import EcosystemPlan


def apply_research_feedback(path: str, plan: EcosystemPlan) -> EcosystemPlan:
    with sqlite3.connect(path) as conn:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='autonomous_research_runs'"
        ).fetchone():
            return plan
        rows = conn.execute(
            "SELECT specialist,kind,status,result_json FROM autonomous_research_runs "
            "WHERE created_at>=? ORDER BY id DESC LIMIT 100",
            ((datetime.now(UTC) - timedelta(days=1)).isoformat(),),
        ).fetchall()
    factors = {}
    for item in plan.allocations:
        recent = [row for row in rows if row[0] == item.specialist][:3]
        failures = sum(row[2] in {"failed", "timed_out", "interrupted", "quarantined"}
                       for row in recent)
        factor = .5 ** failures
        # A successful worker run is not a reward. An explicit independent critic
        # rejection is evidence to reduce future attention even when the handler
        # itself completed; unreviewed/unknown results do not change this factor.
        rejected = 0
        for row in recent:
            try:
                payload = json.loads(row[3])
                critic = payload.get("critic_review") if isinstance(payload, dict) else None
                if isinstance(critic, dict) and critic.get("result_accepted") is False:
                    rejected += 1
            except (ValueError, TypeError):
                continue
        factor *= .5 ** rejected
        quality = next((row for row in recent if row[1] == 'market_data_quality'
                        and row[2] == 'completed'), None)
        if quality:
            try:
                payload = json.loads(quality[3])
                valid, total = payload['valid_markets'], payload['observations']
                if type(valid) is int and type(total) is int and 0 <= valid <= total and total > 0:
                    factor *= valid / total
                else:
                    factor = 0.0
            except (ValueError, TypeError, KeyError):
                factor = 0.0
        factors[item.specialist] = factor
    allocations = tuple(
        replace(item, attention_fraction=item.attention_fraction * factors[item.specialist],
                reason=item.reason + ('; reduced by measured failures, critic rejection, or data defects'
                                      if factors[item.specialist] < 1 else ''))
        for item in plan.allocations
    )
    dominant = max(allocations, key=lambda item: item.attention_fraction, default=None)
    return replace(
        plan, allocations=allocations,
        idle_fraction=max(0.0, 1-sum(item.attention_fraction for item in allocations)),
        dominant_specialist=(dominant.specialist if dominant and dominant.attention_fraction > 0
                             else None),
    )
