from decimal import Decimal

from noema.economic_dashboard import build_economic_overview
from noema.economic_ledger import EconomicLedger
from noema.economic_models import EconomicSnapshot


def test_dashboard_reads_latest_economic_snapshot(tmp_path) -> None:
    path = str(tmp_path / "noema.db")
    ledger = EconomicLedger(path)
    ledger.append_snapshot(
        EconomicSnapshot(
            starting_capital_usd=Decimal(500),
            current_equity_usd=Decimal(600),
            realized_net_pnl_usd=Decimal(100),
            high_water_equity_usd=Decimal(550),
            reserve_usd=Decimal(200),
            strategy_capital_usd=Decimal(250),
            research_budget_usd=Decimal(50),
            infrastructure_budget_usd=Decimal(0),
            treasury_sweep_usd=Decimal(0),
        )
    )
    overview = build_economic_overview(path)
    assert overview["snapshot"]["current_equity_usd"] == "600"
    assert overview["profit_plan"]["profit_above_high_water_usd"] == "50"
