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


def test_intent_reservation_consumes_once_and_enforces_cumulative_limits(tmp_path) -> None:
    ledger = WalletBudgetLedger(str(tmp_path / "noema.db"))
    limits = {
        "wallet_id": "agent", "mission_id": "mission", "per_action_limit": Decimal(10),
        "global_daily_limit": Decimal(15), "mission_limit": Decimal(15),
        "authority_daily_limit": Decimal(15), "maximum_exposure": Decimal(15),
    }
    assert ledger.reserve(intent_id="i1", notional_usd=Decimal(10), **limits) == "reserved"
    assert ledger.reserve(intent_id="i1", notional_usd=Decimal(10), **limits) == "replay"
    assert ledger.reserve(intent_id="i2", notional_usd=Decimal(10), **limits) == "rejected"
    assert ledger.daily_notional("agent") == Decimal(10)
    assert ledger.mission_exposure("agent", "mission") == Decimal(10)


def test_unknown_and_legacy_failed_intents_keep_their_reservations(tmp_path) -> None:
    ledger = WalletBudgetLedger(str(tmp_path / "noema.db"))
    for intent_id, status in (("ambiguous", "unknown"), ("legacy", "failed")):
        ledger.record(
            wallet_id="agent", intent_id=intent_id, notional_usd=Decimal(4),
            status=status, mission_id="mission",
        )
    assert ledger.daily_notional("agent") == Decimal(8)
    assert ledger.mission_exposure("agent", "mission") == Decimal(8)
