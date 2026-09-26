from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime

from .cognition_store import CognitionStore
from .opportunity_radar import RadarRow


@dataclass(frozen=True)
class CognitionPolicy:
    min_attention: float = 0.70
    min_robust_edge: float = 0.01
    max_freshness_seconds: float = 120.0
    max_uncertainty_width: float = 0.20
    cooldown_seconds: float = 300.0
    max_calls_per_hour: int = 6
    max_tokens_per_hour: int = 20000

    @classmethod
    def from_env(cls) -> CognitionPolicy:
        return cls(
            min_attention=float(
                os.getenv("NOEMA_COGNITION_MIN_ATTENTION", "0.70")
            ),
            min_robust_edge=float(
                os.getenv("NOEMA_COGNITION_MIN_ROBUST_EDGE", "0.01")
            ),
            max_freshness_seconds=float(
                os.getenv("NOEMA_COGNITION_MAX_FRESHNESS_SECONDS", "120")
            ),
            max_uncertainty_width=float(
                os.getenv("NOEMA_COGNITION_MAX_UNCERTAINTY", "0.20")
            ),
            cooldown_seconds=float(
                os.getenv("NOEMA_COGNITION_COOLDOWN_SECONDS", "300")
            ),
            max_calls_per_hour=int(
                os.getenv("NOEMA_COGNITION_MAX_CALLS_PER_HOUR", "6")
            ),
            max_tokens_per_hour=int(
                os.getenv("NOEMA_COGNITION_MAX_TOKENS_PER_HOUR", "20000")
            ),
        )


@dataclass(frozen=True)
class CognitionGate:
    eligible: bool
    reasons: tuple[str, ...]


def assess_cognition(
    row: RadarRow,
    store: CognitionStore,
    policy: CognitionPolicy,
    *,
    now: datetime | None = None,
) -> CognitionGate:
    now = now or datetime.now(UTC)
    reasons: list[str] = []

    if row.attention_score is None:
        reasons.append("automatic cognition suppressed for this market")
    elif row.attention_score < policy.min_attention:
        reasons.append("attention below cognition threshold")
    if row.robust_edge < policy.min_robust_edge:
        reasons.append("robust edge below cognition threshold")
    if row.freshness_seconds > policy.max_freshness_seconds:
        reasons.append("market state too stale for cognition")
    if row.uncertainty_width > policy.max_uncertainty_width:
        reasons.append("forecast uncertainty too wide")
    if store.calls_last_hour(now=now) >= policy.max_calls_per_hour:
        reasons.append("hourly cognition call budget exhausted")
    if store.tokens_last_hour(now=now) >= policy.max_tokens_per_hour:
        reasons.append("hourly cognition token budget exhausted")

    since = store.seconds_since_market_call(row.market_id, now=now)
    if since is not None and since < policy.cooldown_seconds:
        reasons.append("market cognition cooldown active")

    return CognitionGate(eligible=not reasons, reasons=tuple(reasons))
