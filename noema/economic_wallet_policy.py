from __future__ import annotations

from dataclasses import replace

from .economic_models import AutonomyLevel
from .economic_policy import limits_for_level
from .wallet_policy import AgentWalletPolicy


def wallet_policy_for_autonomy(
    base: AgentWalletPolicy,
    level: AutonomyLevel,
) -> AgentWalletPolicy:
    limits = limits_for_level(level)

    if level in {
        AutonomyLevel.SHADOW,
        AutonomyLevel.PAPER,
        AutonomyLevel.DEMO,
    }:
        return replace(
            base,
            max_transaction_usd=limits.max_transaction_usd,
            max_daily_notional_usd=limits.max_daily_notional_usd,
            minimum_reserve_usd=max(
                base.minimum_reserve_usd,
                limits.minimum_reserve_usd,
            ),
            master_halt=True,
        )

    return replace(
        base,
        max_transaction_usd=min(
            base.max_transaction_usd,
            limits.max_transaction_usd,
        ),
        max_daily_notional_usd=min(
            base.max_daily_notional_usd,
            limits.max_daily_notional_usd,
        ),
        minimum_reserve_usd=max(
            base.minimum_reserve_usd,
            limits.minimum_reserve_usd,
        ),
    )
