from __future__ import annotations

from dataclasses import dataclass

from .wallet_budget import WalletBudgetLedger
from .wallet_intents import WalletExecutionReceipt, WalletIntent
from .wallet_policy import (
    AgentWalletPolicy,
    AgentWalletState,
    WalletPolicyDecision,
    evaluate_wallet_intent,
)
from .wallet_signer import WalletSigner


@dataclass(frozen=True)
class AgentWalletResult:
    decision: WalletPolicyDecision
    receipt: WalletExecutionReceipt | None


class AgentWallet:
    """Policy-gated agent wallet coordinator.

    The decision engine may propose intents, but this layer independently
    evaluates wallet policy before any signer backend is called.
    """

    def __init__(
        self,
        *,
        wallet_id: str,
        signer: WalletSigner,
        policy: AgentWalletPolicy,
        budget: WalletBudgetLedger,
    ) -> None:
        self.wallet_id = wallet_id
        self.signer = signer
        self.policy = policy
        self.budget = budget

    async def process(
        self,
        intent: WalletIntent,
        state: AgentWalletState,
    ) -> AgentWalletResult:
        decision = evaluate_wallet_intent(intent, state, self.policy)
        self.budget.record(
            wallet_id=self.wallet_id,
            intent_id=intent.intent_id,
            notional_usd=intent.notional_usd,
            status="approved" if decision.approved else "rejected",
        )
        if not decision.approved:
            return AgentWalletResult(decision=decision, receipt=None)

        receipt = await self.signer.execute(intent)
        return AgentWalletResult(decision=decision, receipt=receipt)
