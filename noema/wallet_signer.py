from __future__ import annotations

from typing import Protocol

from .wallet_intents import WalletExecutionReceipt, WalletIntent


class WalletSigner(Protocol):
    async def execute(self, intent: WalletIntent) -> WalletExecutionReceipt: ...


class DisabledWalletSigner:
    """Fail-closed signer used until a programmable wallet provider is configured."""

    async def execute(self, intent: WalletIntent) -> WalletExecutionReceipt:
        raise RuntimeError(
            f"wallet execution disabled for intent {intent.intent_id}; "
            "configure a policy-enforced signer backend first"
        )
