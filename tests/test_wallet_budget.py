from decimal import Decimal

from noema.wallet_budget import WalletBudgetLedger


def test_daily_wallet_budget_roundtrip(tmp_path) -> None:
    ledger = WalletBudgetLedger(str(tmp_path / "noema.db"))
    ledger.record(
        wallet_id="agent",
        intent_id="i1",
        notional_usd=Decimal("12.50"),
        status="approved",
    )
    assert ledger.daily_notional("agent") == Decimal("12.50")
