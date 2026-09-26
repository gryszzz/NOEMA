from decimal import Decimal

from noema.economic_models import AutonomyLevel
from noema.economic_wallet_policy import wallet_policy_for_autonomy
from noema.wallet_policy import AgentWalletPolicy


def test_paper_level_forces_wallet_halt() -> None:
    base = AgentWalletPolicy(master_halt=False)
    policy = wallet_policy_for_autonomy(base, AutonomyLevel.PAPER)
    assert policy.master_halt is True
    assert policy.max_transaction_usd == Decimal(0)


def test_autonomy_never_exceeds_human_ceiling() -> None:
    base = AgentWalletPolicy(
        max_transaction_usd=Decimal(7),
        max_daily_notional_usd=Decimal(30),
        minimum_reserve_usd=Decimal(120),
        master_halt=False,
    )
    policy = wallet_policy_for_autonomy(base, AutonomyLevel.EXPANSION)
    assert policy.max_transaction_usd == Decimal(7)
    assert policy.max_daily_notional_usd == Decimal(30)
    assert policy.minimum_reserve_usd >= Decimal(120)
