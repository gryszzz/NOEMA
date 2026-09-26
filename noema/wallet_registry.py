from __future__ import annotations

from dataclasses import dataclass, field

from .wallet_types import WalletDescriptor, WalletRole


@dataclass
class WalletRegistry:
    wallets: dict[str, WalletDescriptor] = field(default_factory=dict)

    def register(self, wallet: WalletDescriptor) -> None:
        if wallet.wallet_id in self.wallets:
            raise ValueError(f"wallet already registered: {wallet.wallet_id}")
        self.wallets[wallet.wallet_id] = wallet

    def get(self, wallet_id: str) -> WalletDescriptor:
        try:
            return self.wallets[wallet_id]
        except KeyError as exc:
            raise KeyError(f"unknown wallet: {wallet_id}") from exc

    def by_role(self, role: WalletRole) -> list[WalletDescriptor]:
        return [wallet for wallet in self.wallets.values() if wallet.role is role]
