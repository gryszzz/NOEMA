from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .wallet_budget import WalletBudgetLedger
from .wallet_intents import WalletExecutionReceipt, WalletIntent
from .wallet_policy import (
    AgentWalletPolicy,
    AgentWalletState,
    WalletPolicyDecision,
    dedicated_wallet_policy,
    evaluate_wallet_intent,
)
from .wallet_signer import LocalOwnerWalletSigner, WalletSigner


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
        economic_ledger: Any | None = None,
        mission_store: Any | None = None,
        session_store: Any | None = None,
    ) -> None:
        self.wallet_id = wallet_id
        self.signer = signer
        self.policy = policy
        self.budget = budget
        self.economic_ledger = economic_ledger
        self.mission_store = mission_store
        self.session_store = session_store

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
            if intent.mission_id and self.mission_store is not None:
                self._persist_mission_event(
                    intent, "policy_rejected", {"reasons": list(decision.reasons)}
                )
            return AgentWalletResult(decision=decision, receipt=None)

        try:
            receipt = await self.signer.execute(intent)
        except Exception:
            self.budget.update_status(intent.intent_id, "failed")
            raise
        self.budget.update_status(
            intent.intent_id,
            "confirmed" if receipt.status == "confirmed" else "submitted",
        )
        event = {
            "wallet_id": self.wallet_id,
            "mission_id": intent.mission_id,
            "intent_id": intent.intent_id,
            "chain": intent.chain.value,
            "chain_id": receipt.chain_id,
            "venue": intent.venue,
            "action": intent.action,
            "asset_in": intent.asset_in,
            "asset_out": intent.asset_out,
            "amount_atomic": intent.amount_atomic,
            "destination": intent.destination,
            "operational_validation": intent.operational_validation,
            "transaction_reference": receipt.transaction_reference,
            "status": receipt.status,
            "fee_amount_atomic": receipt.fee_amount_atomic,
            "fee_lamports": receipt.fee_lamports,
            "slot": receipt.slot,
            "block_number": receipt.block_number,
            "pre_balance_lamports": receipt.pre_balance_lamports,
            "post_balance_lamports": receipt.post_balance_lamports,
            "post_balance_atomic": receipt.post_balance_atomic,
            "evidence_ids": list(intent.evidence_ids),
        }
        if self.economic_ledger is not None:
            self.economic_ledger.append_event(
                "wallet_transaction_" + receipt.status,
                payload=event,
            )
        if intent.mission_id and self.mission_store is not None:
            self._persist_mission_event(intent, receipt.status, event)
        return AgentWalletResult(decision=decision, receipt=receipt)

    def _persist_mission_event(
        self, intent: WalletIntent, status: str, payload: dict[str, Any]
    ) -> None:
        session_id = self.mission_store.record_wallet_event(
            intent.mission_id,
            status=status,
            detail=f"Wallet execution {status} on {intent.chain.value}",
            payload=payload,
        )
        if session_id and self.session_store is not None:
            self.session_store.event(
                session_id,
                "wallet_execution",
                status,
                f"{intent.chain.value}: {status}",
                tool=intent.venue,
                evidence_id=intent.evidence_ids[0] if intent.evidence_ids else None,
            )


def create_owner_agent_wallet(db_path: str = "data/noema.db") -> AgentWallet:
    """Construct the persistent NOEMA wallet coordinator from owner runtime config."""
    from .economic_ledger import EconomicLedger
    from .mission_store import MissionStore
    from .research_session import SessionStore

    return AgentWallet(
        wallet_id="noema-dedicated-wallets",
        signer=LocalOwnerWalletSigner(),
        policy=dedicated_wallet_policy(),
        budget=WalletBudgetLedger(db_path),
        economic_ledger=EconomicLedger(db_path),
        mission_store=MissionStore(db_path),
        session_store=SessionStore(db_path),
    )
