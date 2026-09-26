import pytest

from noema.wallet_intents import WalletIntent
from noema.wallet_signer import DisabledWalletSigner
from noema.wallet_types import Chain


@pytest.mark.asyncio
async def test_disabled_signer_fails_closed() -> None:
    signer = DisabledWalletSigner()
    with pytest.raises(RuntimeError, match="execution disabled"):
        await signer.execute(
            WalletIntent(
                intent_id="i1",
                chain=Chain.SOLANA,
                venue="jupiter",
                action="swap",
                asset_in="USDC",
                asset_out="SOL",
                notional_usd=1,
                expected_slippage_bps=1,
                evidence_ids=("e1",),
            )
        )
