from decimal import Decimal

from noema import equity_feed as ef


def test_chains_config_driven():
    for c in ("ethereum", "base", "polygon", "arbitrum", "optimism", "robinhood"):
        assert f"evm_{c}" in ef.SOURCES and f"evm_{c}" in ef.WALLET_SOURCES
    assert ef.EVM_CHAINS["robinhood"]["chain_id"] == 4663


def test_new_wallet_source_balance_is_deposit_not_profit():
    r = ef.SourceReading("evm_robinhood", Decimal(8), {"ETH": Decimal("0.002")}, {"ETH": Decimal(4000)})
    prev = {"solana": {"status": "ok", "holdings": {}, "equity_usd": "1"}}
    out = ef.detect_deposits(prev, {"evm_robinhood": r}, trades=3)
    assert out == [("evm_robinhood", "ETH", Decimal("0.002"), Decimal("8.000"))]
