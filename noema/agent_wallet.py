from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .economic_ledger import EconomicEvent
from .execution_gateway import ExecutionGateway
from .wallet_budget import WalletBudgetLedger
from .wallet_intents import WalletExecutionReceipt, WalletIntent
from .wallet_policy import (
    AgentWalletPolicy,
    AgentWalletState,
    FinancialMissionAuthority,
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
        authority_resolver: Callable[[str], FinancialMissionAuthority | None] | None = None,
        state_resolver: Callable[[WalletIntent], AgentWalletState] | None = None,
        policy_resolver: Callable[[], AgentWalletPolicy] | None = None,
        live_execution_enabled: bool = False,
        signer_isolated: bool = False,
    ) -> None:
        self.wallet_id = wallet_id
        self.signer = signer
        self.policy = policy
        self.budget = budget
        self.economic_ledger = economic_ledger
        self.mission_store = mission_store
        self.session_store = session_store
        # Authority is resolved from a separate trusted source. Mission fields,
        # prompts, research results and specialist output are never grant sources.
        self.authority_resolver = authority_resolver
        self.state_resolver = state_resolver
        self.policy_resolver = policy_resolver
        self.live_execution_enabled = live_execution_enabled
        self.signer_isolated = signer_isolated
        self.execution_gateway = ExecutionGateway(budget.db_path)

    async def process(
        self,
        intent: WalletIntent,
        state: AgentWalletState,
    ) -> AgentWalletResult:
        return await self.execution_gateway.submit_wallet_intent(
            intent=intent,
            handler=lambda: self._process_at_gateway(intent, state),
        )

    async def _process_at_gateway(
        self,
        intent: WalletIntent,
        state: AgentWalletState,
    ) -> AgentWalletResult:
        if self.budget.has_intent(intent.intent_id):
            return AgentWalletResult(
                WalletPolicyDecision(False, ("intent has already been consumed",)), None,
            )
        mission_status = (
            self.mission_store.current_status(intent.mission_id)
            if intent.mission_id and self.mission_store is not None else None
        )
        authority = (
            self.authority_resolver(intent.mission_id)
            if intent.mission_id and self.authority_resolver is not None else None
        )
        prior_mission_exposure = (
            self.budget.mission_exposure(self.wallet_id, intent.mission_id)
            if intent.mission_id else Decimal(0)
        )
        prior_daily_exposure = self.budget.daily_notional(self.wallet_id)
        current_state = self.state_resolver(intent) if self.state_resolver is not None else state
        current_policy = self.policy_resolver() if self.policy_resolver is not None else self.policy
        decision = evaluate_wallet_intent(
            intent, current_state, current_policy, authority=authority,
            mission_status=mission_status, mission_exposure_usd=prior_mission_exposure,
            daily_exposure_usd=prior_daily_exposure,
            live_execution_enabled=self.live_execution_enabled,
            signer_isolated=self.signer_isolated,
            state_is_current=self.state_resolver is not None,
        )
        if not decision.approved:
            self.budget.record(
                wallet_id=self.wallet_id, intent_id=intent.intent_id,
                notional_usd=intent.notional_usd, status="rejected",
                mission_id=intent.mission_id,
            )
            if mission_status and intent.mission_id and self.mission_store is not None:
                self._persist_mission_event(
                    intent, "policy_rejected", {"reasons": list(decision.reasons)}
                )
            return AgentWalletResult(decision=decision, receipt=None)

        # Consume the id before crossing the signer boundary. The unique ledger
        # constraint makes concurrent/replayed submissions fail closed.
        reservation = self.budget.reserve(
            wallet_id=self.wallet_id, intent_id=intent.intent_id,
            mission_id=intent.mission_id or "", notional_usd=intent.notional_usd,
            per_action_limit=authority.per_action_limit_usd or Decimal(0),
            global_daily_limit=current_policy.max_daily_notional_usd or Decimal(0),
            mission_limit=authority.per_mission_limit_usd or Decimal(0),
            authority_daily_limit=authority.daily_limit_usd or Decimal(0),
            maximum_exposure=authority.maximum_exposure_usd or Decimal(0),
        )
        if reservation != "reserved":
            reason = "intent has already been consumed" if reservation == "replay" else "cumulative authority cap exceeded"
            return AgentWalletResult(WalletPolicyDecision(False, (reason,)), None)

        # Re-read mission and owner authority immediately before signing.
        mission_status = (
            self.mission_store.current_status(intent.mission_id)
            if intent.mission_id and self.mission_store is not None else None
        )
        authority = (
            self.authority_resolver(intent.mission_id)
            if intent.mission_id and self.authority_resolver is not None else None
        )
        current_state = self.state_resolver(intent) if self.state_resolver is not None else state
        current_policy = self.policy_resolver() if self.policy_resolver is not None else self.policy
        current_mission_exposure = (
            self.budget.mission_exposure(
                self.wallet_id, intent.mission_id, exclude_intent_id=intent.intent_id,
            ) if intent.mission_id else Decimal(0)
        )
        current_daily_exposure = self.budget.daily_notional(
            self.wallet_id, exclude_intent_id=intent.intent_id,
        )
        final_decision = evaluate_wallet_intent(
            intent, current_state, current_policy, authority=authority,
            mission_status=mission_status, mission_exposure_usd=current_mission_exposure,
            daily_exposure_usd=current_daily_exposure,
            live_execution_enabled=self.live_execution_enabled,
            signer_isolated=self.signer_isolated,
            state_is_current=self.state_resolver is not None,
        )
        if not final_decision.approved:
            self.budget.update_status(intent.intent_id, "rejected")
            if intent.mission_id and self.mission_store is not None:
                self._persist_mission_event(
                    intent, "policy_rejected_before_signing",
                    {"reasons": list(final_decision.reasons)},
                )
            return AgentWalletResult(decision=final_decision, receipt=None)

        try:
            receipt = await self.signer.execute(intent)
        except Exception:
            # Signing/broadcast transport failures are ambiguous: the chain may
            # have accepted a transaction before the error was observed.
            self.budget.update_status(intent.intent_id, "unknown")
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
            if (receipt.transaction_reference
                    and receipt.status in {"confirmed", "reverted"}):
                chain = intent.chain.value
                unit = {"solana": "SOL_lamports", "ethereum": "ETH_wei",
                        "base": "ETH_wei", "bitcoin": "BTC_sats"}.get(chain, "unknown_atomic")
                confirmed = receipt.status == "confirmed"
                self.economic_ledger.record_event(EconomicEvent(
                    provider=f"wallet:{chain}",
                    external_reference_id=receipt.transaction_reference,
                    event_type="wallet_action",
                    occurred_at=datetime.now(UTC), currency=f"{intent.asset_in}:{unit}",
                    amount=Decimal(str(intent.amount_atomic)), reconciliation_state=(
                        "RECONCILED" if confirmed else "OBSERVED"),
                    value_state="realized" if confirmed else "unknown",
                    capital_class="unclassified_wallet_flow",
                    confidence_state="provider_confirmed" if confirmed else "unknown",
                    completeness_state="incomplete", mission_id=intent.mission_id,
                    activity_id=intent.mission_id, lane="web3",
                    owned_account_id=self.wallet_id,
                    evidence={"transaction_reference": receipt.transaction_reference,
                              "intent_id": intent.intent_id, "chain": chain,
                              "chain_id": receipt.chain_id, "action": intent.action,
                              "asset_in": intent.asset_in, "asset_out": intent.asset_out,
                              "amount_atomic": intent.amount_atomic,
                              "post_balance_atomic": receipt.post_balance_atomic,
                              "evidence_ids": list(intent.evidence_ids)},
                ))
                fee_atomic = receipt.fee_lamports or receipt.fee_amount_atomic
                if fee_atomic is not None and Decimal(str(fee_atomic)) > 0:
                    self.economic_ledger.record_event(EconomicEvent(
                        provider=f"wallet:{chain}",
                        external_reference_id=f"{receipt.transaction_reference}:fee",
                        event_type="wallet_network_fee", occurred_at=datetime.now(UTC),
                        currency=unit, amount=Decimal(str(fee_atomic)),
                        reconciliation_state="RECONCILED" if confirmed else "OBSERVED",
                        value_state="realized" if confirmed else "unknown",
                        capital_class="cost", confidence_state=(
                            "provider_confirmed" if confirmed else "unknown"),
                        completeness_state="incomplete", mission_id=intent.mission_id,
                        activity_id=intent.mission_id, lane="web3",
                        owned_account_id=self.wallet_id,
                        evidence={"transaction_reference": receipt.transaction_reference,
                                  "fee_atomic": str(fee_atomic), "chain": chain},
                    ))
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
        policy_resolver=dedicated_wallet_policy,
        live_execution_enabled=False,
        signer_isolated=True,
    )
