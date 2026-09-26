from decimal import Decimal

from noema.economic_ledger import EconomicLedger
from noema.economic_models import EconomicSnapshot


def test_economic_snapshot_roundtrip(tmp_path) -> None:
    ledger = EconomicLedger(str(tmp_path / "noema.db"))
    snapshot = EconomicSnapshot(
        starting_capital_usd=Decimal(500),
        current_equity_usd=Decimal(550),
        realized_net_pnl_usd=Decimal(50),
        high_water_equity_usd=Decimal(550),
        reserve_usd=Decimal(200),
        strategy_capital_usd=Decimal(200),
        research_budget_usd=Decimal(75),
        infrastructure_budget_usd=Decimal(25),
        treasury_sweep_usd=Decimal(0),
    )
    ledger.append_snapshot(snapshot)
    assert ledger.latest_snapshot() == snapshot
