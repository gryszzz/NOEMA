"""Single fail-closed boundary for proposals that may move economic value.

The gateway accepts structured proposals only. Provider adapters remain
venue-specific, and their existing policy/signer boundaries are still required.
It never receives credentials or arbitrary model text.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from inspect import signature
from pathlib import Path
from typing import Any

from .models import Action, Decision, Opportunity


@dataclass(frozen=True)
class ExecutionProposal:
    proposal_id: str
    mission_id: str
    venue: str
    instrument: str
    side: str
    notional_usd: Decimal
    max_loss_usd: Decimal
    limit_price: Decimal
    expected_edge: Decimal
    evidence_refs: tuple[str, ...]
    created_at: datetime
    expires_at: datetime
    decision_id: str | None = None
    strategy_id: str | None = None
    experiment_id: str | None = None

    @classmethod
    def from_opportunity(
        cls, *, proposal_id: str, mission_id: str, opportunity: Opportunity,
        action: Action, evidence_refs: tuple[str, ...], expires_at: datetime,
        decision_id: str | None = None, strategy_id: str | None = None,
        experiment_id: str | None = None,
    ) -> ExecutionProposal:
        if action.decision is not Decision.LIVE_BUY_YES or action.max_price is None:
            raise ValueError("only a deterministic live YES order can form this proposal")
        if not mission_id or not proposal_id or not evidence_refs:
            raise ValueError("proposal identity, mission, and evidence are required")
        now = datetime.now(UTC)
        return cls(
            proposal_id=proposal_id, mission_id=mission_id,
            venue=action.venue, instrument=action.market_id, side="buy_yes",
            notional_usd=Decimal(str(action.stake_usd)),
            max_loss_usd=Decimal(str(action.stake_usd)),
            limit_price=Decimal(str(action.max_price)),
            expected_edge=Decimal(str(opportunity.robust_edge)),
            evidence_refs=tuple(evidence_refs), created_at=now, expires_at=expires_at,
            decision_id=decision_id, strategy_id=strategy_id, experiment_id=experiment_id,
        )


@dataclass(frozen=True)
class GatewayResult:
    allowed: bool
    status: str
    reasons: tuple[str, ...]
    provider_reference: str | None = None
    tier: str = "execution"


@dataclass(frozen=True)
class ReconciliationObservation:
    """Provider-authenticated state for one exact request; never model-authored."""

    provider: str
    proposal_id: str
    provider_reference: str | None
    status: str
    observed_at: datetime
    source: str
    filled_quantity: Decimal | None = None
    fee_amount: Decimal | None = None
    fee_currency: str | None = None
    external_fill_ids: tuple[str, ...] = ()
    filled_exposure_usd: Decimal | None = None
    remaining_reserved_usd: Decimal | None = None

    def validate(self) -> None:
        if not self.provider or not self.proposal_id or not self.source:
            raise ValueError("provider, proposal identity, and authoritative source are required")
        if self.observed_at.tzinfo is None:
            raise ValueError("provider observation timestamp must be timezone-aware")
        if self.status not in {"submitted", "partial", "filled", "rejected", "cancelled", "expired", "failed"}:
            raise ValueError("unsupported provider observation status")
        for value in (self.filled_quantity, self.fee_amount):
            if value is not None and (not value.is_finite() or value < 0):
                raise ValueError("provider quantities and fees must be finite and non-negative")
        if self.fee_amount is not None and not self.fee_currency:
            raise ValueError("fee currency is required when a fee is reported")
        if any(not value for value in self.external_fill_ids):
            raise ValueError("provider fill identifiers cannot be empty")
        if len(set(self.external_fill_ids)) != len(self.external_fill_ids):
            raise ValueError("provider fill identifiers must be unique in a snapshot")
        for value in (self.filled_exposure_usd, self.remaining_reserved_usd):
            if value is not None and (not value.is_finite() or value < 0):
                raise ValueError("exposure amounts must be finite and non-negative")
        if self.status in {"partial", "filled", "rejected", "cancelled", "expired", "failed"}:
            if self.filled_exposure_usd is None or self.remaining_reserved_usd is None:
                raise ValueError("terminal and partial observations require cumulative exposure amounts")
            if self.status in {"filled", "rejected", "cancelled", "expired", "failed"} and self.remaining_reserved_usd != 0:
                raise ValueError("terminal observations cannot retain an unfilled reservation")


@dataclass(frozen=True)
class PredictionExecutionAuthority:
    """Current owner-granted, mission-scoped authority for venue execution."""

    authority_id: str
    mission_id: str
    allowed_venues: frozenset[str]
    allowed_actions: frozenset[str]
    allowed_instruments: frozenset[str]
    per_action_limit_usd: Decimal
    per_mission_limit_usd: Decimal
    daily_limit_usd: Decimal
    maximum_exposure_usd: Decimal
    expires_at: datetime
    revoked: bool = False


class ExecutionGateway:
    """Execution entry point for typed venue proposals and wallet intents.

    Live order submission is disabled unless a separate gateway enable flag,
    the legacy venue-specific enable flag, a positive daily limit, and an
    explicit venue allowlist are all present. Treasury-class wallet transfers
    require their own owner-controlled flag in addition to WalletPolicy.
    """

    _TREASURY_ACTIONS = frozenset({
        "sol_transfer", "evm_native_transfer", "evm_erc20_transfer",
        "bitcoin_send", "token_approve", "contract_call", "withdraw",
    })

    def __init__(
        self, db_path: str = "data/noema.db", *, initialize: bool = True,
        authority_resolver: Callable[[str], PredictionExecutionAuthority | None] | None = None,
    ) -> None:
        path = Path(db_path)
        self.db_path = str(path)
        self.authority_resolver = authority_resolver
        if initialize:
            path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS execution_gateway_requests (
                proposal_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                route TEXT NOT NULL,
                tier TEXT NOT NULL,
                venue TEXT,
                status TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                notional_usd TEXT,
                mission_id TEXT,
                instrument TEXT,
                side TEXT,
                filled_exposure_usd TEXT,
                remaining_reserved_usd TEXT,
                provider_reference TEXT,
                result_json TEXT NOT NULL,
                request_json TEXT NOT NULL DEFAULT '{}'
            )""")
                columns = {row[1] for row in conn.execute(
                    "PRAGMA table_info(execution_gateway_requests)"
                )}
                if "request_json" not in columns:
                    conn.execute(
                        "ALTER TABLE execution_gateway_requests ADD COLUMN request_json "
                        "TEXT NOT NULL DEFAULT '{}'"
                    )
                if "mission_id" not in columns:
                    conn.execute(
                        "ALTER TABLE execution_gateway_requests ADD COLUMN mission_id TEXT"
                    )
                if "instrument" not in columns:
                    conn.execute(
                        "ALTER TABLE execution_gateway_requests ADD COLUMN instrument TEXT"
                    )
                if "side" not in columns:
                    conn.execute(
                        "ALTER TABLE execution_gateway_requests ADD COLUMN side TEXT"
                    )
                if "filled_exposure_usd" not in columns:
                    conn.execute("ALTER TABLE execution_gateway_requests ADD COLUMN filled_exposure_usd TEXT")
                if "remaining_reserved_usd" not in columns:
                    conn.execute("ALTER TABLE execution_gateway_requests ADD COLUMN remaining_reserved_usd TEXT")
                conn.execute("""CREATE TABLE IF NOT EXISTS execution_gateway_transitions (
                    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '{}'
                )""")
                conn.execute("CREATE INDEX IF NOT EXISTS execution_gateway_transition_request "
                             "ON execution_gateway_transitions(proposal_id,transition_id)")
                conn.execute("""CREATE TRIGGER IF NOT EXISTS execution_gateway_transitions_no_update
                    BEFORE UPDATE ON execution_gateway_transitions BEGIN
                    SELECT RAISE(ABORT, 'execution transitions are append-only'); END""")
                conn.execute("""CREATE TRIGGER IF NOT EXISTS execution_gateway_transitions_no_delete
                    BEFORE DELETE ON execution_gateway_transitions BEGIN
                    SELECT RAISE(ABORT, 'execution transitions are append-only'); END""")

    def overview(self, *, limit: int = 25) -> dict[str, Any]:
        """Return a credential-free, read-only workstation projection."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        try:
            if not Path(self.db_path).is_file():
                raise sqlite3.OperationalError("database is not initialized")
            uri = Path(self.db_path).resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
            "SELECT proposal_id,created_at,route,tier,venue,status,notional_usd,"
                    "mission_id,provider_reference,result_json,request_json FROM execution_gateway_requests "
                    "ORDER BY created_at DESC LIMIT ?", (limit,),
                ).fetchall()
                transitions = conn.execute(
                    "SELECT proposal_id,occurred_at,from_status,to_status,source,evidence_json "
                    "FROM execution_gateway_transitions ORDER BY transition_id DESC LIMIT ?", (limit,),
                ).fetchall()
        except sqlite3.Error:
            rows = []
            transitions = []
        enabled = os.getenv("NOEMA_EXECUTION_GATEWAY_ENABLED", "0") == "1"
        halted = os.getenv("NOEMA_MASTER_HALT", "0") == "1"
        return {
            "status": "ARMED · POLICY GATED" if enabled and not halted else "FAIL CLOSED · NOT ARMED",
            "enabled": enabled,
            "master_halt": halted,
            "prediction_execution_enabled": (
                enabled and not halted and os.getenv("NOEMA_ALLOW_LIVE_ORDERS", "0") == "1"
                and self.authority_resolver is not None
            ),
            "treasury_actions_enabled": os.getenv("NOEMA_TREASURY_ACTIONS_ENABLED", "0") == "1",
            "recent_requests": [dict(row) for row in rows],
            "recent_transitions": [dict(row) for row in transitions],
        }

    def record_proposal(self, proposal: ExecutionProposal) -> GatewayResult:
        """Persist proposal authority without granting order authority."""
        self._record(
            proposal.proposal_id + ":proposal", "prediction", "proposal",
            proposal.venue, "proposed", proposal.notional_usd,
            self._proposal_payload(proposal), [],
        )
        return GatewayResult(False, "proposed", (), tier="proposal")

    async def submit_prediction_order(
        self, *, proposal: ExecutionProposal, opportunity: Opportunity,
        action: Action, risk_engine: Any, venue_adapter: Any,
        bankroll_usd: float,
    ) -> GatewayResult:
        reasons = self._prediction_checks(
            proposal=proposal, opportunity=opportunity, action=action,
            risk_engine=risk_engine, venue_adapter=venue_adapter,
            bankroll_usd=bankroll_usd,
        )
        payload = self._proposal_payload(proposal)
        client_order_id = self._stable_client_order_id(proposal.proposal_id)
        payload["client_order_id"] = client_order_id
        if reasons:
            self._record(proposal.proposal_id, "prediction", "execution", proposal.venue,
                         "rejected", proposal.notional_usd, payload, reasons)
            return GatewayResult(False, "rejected", tuple(reasons))
        authority = self._resolve_authority(proposal.mission_id)
        if authority is None or not self._reserve_prediction(proposal, payload, authority):
            return GatewayResult(False, "rejected", ("proposal already consumed or daily cap exceeded",))
        # Re-read authority, market freshness and deterministic risk immediately
        # before crossing the venue boundary. The reservation prevents races.
        final_reasons = self._prediction_checks(
            proposal=proposal, opportunity=opportunity, action=action,
            risk_engine=risk_engine, venue_adapter=venue_adapter,
            bankroll_usd=bankroll_usd,
        )
        if final_reasons:
            self._finish(proposal.proposal_id, "rejected", None,
                         {"reasons": final_reasons})
            return GatewayResult(False, "rejected", tuple(final_reasons))
        try:
            if "client_order_id" in signature(venue_adapter.execute).parameters:
                reference = await venue_adapter.execute(action, client_order_id=client_order_id)
            else:
                reference = await venue_adapter.execute(action)
        except Exception:
            # A transport error can happen after the provider accepted the
            # request. Keep its reservation consumed until explicit
            # reconciliation establishes whether an order exists.
            self._finish(proposal.proposal_id, "unknown", None,
                         {"reason": "provider outcome is unknown; reconciliation required"})
            raise
        self._finish(proposal.proposal_id, "submitted", str(reference), {})
        return GatewayResult(True, "submitted", (), str(reference))

    async def submit_wallet_intent(
        self, *, intent: Any, handler: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Route a structured WalletIntent into the existing deterministic coordinator."""
        action = str(getattr(intent, "action", ""))
        tier = "treasury" if action in self._TREASURY_ACTIONS else "execution"
        intent_id = str(getattr(intent, "intent_id", ""))
        mission_id = getattr(intent, "mission_id", None)
        payload = {
            "intent_id": intent_id, "mission_id": mission_id,
            "wallet_id": getattr(intent, "wallet_id", None),
            "chain": getattr(getattr(intent, "chain", None), "value", None),
            "venue": getattr(intent, "venue", None), "action": action,
            "asset_in": getattr(intent, "asset_in", None),
            "asset_out": getattr(intent, "asset_out", None),
            "amount_atomic": getattr(intent, "amount_atomic", None),
            "destination": getattr(intent, "destination", None),
            "evidence_refs": list(getattr(intent, "evidence_ids", ())),
        }
        request_hash = self._hash(payload)
        if not intent_id:
            raise ValueError("wallet intent must have a stable ID")
        if tier == "treasury" and os.getenv("NOEMA_TREASURY_ACTIONS_ENABLED", "0") != "1":
            self._record(intent_id, "wallet", tier, payload["venue"], "rejected", None,
                         payload, ["owner treasury permission is disabled"])
            from .agent_wallet import AgentWalletResult
            from .wallet_policy import WalletPolicyDecision
            return AgentWalletResult(
                WalletPolicyDecision(False, ("owner treasury permission is disabled",)), None,
            )
        if not self._reserve_generic(intent_id, "wallet", tier, payload["venue"], None,
                                     payload, request_hash):
            from .agent_wallet import AgentWalletResult
            from .wallet_policy import WalletPolicyDecision
            return AgentWalletResult(
                WalletPolicyDecision(False, ("intent has already been consumed",)), None,
            )
        try:
            result = await handler()
        except Exception:
            self._finish(intent_id, "unknown", None,
                         {"reason": "wallet outcome is unknown; reconciliation required"})
            raise
        decision = getattr(result, "decision", None)
        receipt = getattr(result, "receipt", None)
        status = "rejected" if not getattr(decision, "approved", False) else (
            getattr(receipt, "status", "approved") if receipt else "approved"
        )
        reference = getattr(receipt, "transaction_reference", None) if receipt else None
        self._finish(intent_id, status, reference, {})
        return result

    def _prediction_checks(
        self, *, proposal: ExecutionProposal, opportunity: Opportunity,
        action: Action, risk_engine: Any, venue_adapter: Any,
        bankroll_usd: float,
    ) -> list[str]:
        reasons: list[str] = []
        now = datetime.now(UTC)
        numeric_fields = (
            proposal.notional_usd, proposal.max_loss_usd,
            proposal.limit_price, proposal.expected_edge,
        )
        numeric_valid = all(
            isinstance(value, Decimal) and value.is_finite() for value in numeric_fields
        )
        if os.getenv("NOEMA_EXECUTION_GATEWAY_ENABLED", "0") != "1":
            reasons.append("execution gateway is disabled")
        if os.getenv("NOEMA_ALLOW_LIVE_ORDERS", "0") != "1":
            reasons.append("owner live-order setting is disabled")
        if os.getenv("NOEMA_MASTER_HALT", "0") == "1":
            reasons.append("owner master halt is active")
        cap = self._decimal(os.getenv("NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD", "0"))
        if cap is None or cap <= 0:
            reasons.append("positive owner daily execution limit is missing")
        allowed_venues = {
            item.strip() for item in os.getenv("NOEMA_LIVE_VENUES", "").split(",")
            if item.strip()
        }
        if proposal.venue not in allowed_venues:
            reasons.append("venue is not explicitly allowlisted")
        authority = self._resolve_authority(proposal.mission_id)
        if not isinstance(authority, PredictionExecutionAuthority):
            reasons.append("separate mission execution authority is absent")
        else:
            if authority.revoked or authority.mission_id != proposal.mission_id:
                reasons.append("mission execution authority is revoked or mismatched")
            if not authority.authority_id.strip():
                reasons.append("mission execution authority has no stable identity")
            if proposal.venue not in authority.allowed_venues:
                reasons.append("mission authority does not include this venue")
            if "buy_yes" not in authority.allowed_actions:
                reasons.append("mission authority does not include this action")
            if proposal.instrument not in authority.allowed_instruments:
                reasons.append("mission authority does not include this instrument")
            if (authority.expires_at.tzinfo is None or authority.expires_at <= now):
                reasons.append("mission execution authority is expired")
            for label, limit in (
                ("per-action", authority.per_action_limit_usd),
                ("per-mission", authority.per_mission_limit_usd),
                ("daily", authority.daily_limit_usd),
                ("maximum-exposure", authority.maximum_exposure_usd),
            ):
                if not isinstance(limit, Decimal) or not limit.is_finite() or limit <= 0:
                    reasons.append(f"mission {label} limit is missing or invalid")
            if (numeric_valid and isinstance(authority.per_action_limit_usd, Decimal)
                    and authority.per_action_limit_usd.is_finite()
                    and proposal.notional_usd > authority.per_action_limit_usd):
                reasons.append("mission per-action limit exceeded")
            if numeric_valid:
                mission_used = self._mission_exposure(proposal.mission_id, proposal.proposal_id)
                daily_used = self._daily_exposure(proposal.proposal_id)
            else:
                mission_used = daily_used = Decimal(0)
            if (numeric_valid and isinstance(authority.per_mission_limit_usd, Decimal)
                    and authority.per_mission_limit_usd.is_finite()
                    and mission_used + proposal.notional_usd > authority.per_mission_limit_usd):
                reasons.append("mission cumulative limit exceeded")
            if (numeric_valid and isinstance(authority.maximum_exposure_usd, Decimal)
                    and authority.maximum_exposure_usd.is_finite()
                    and mission_used + proposal.notional_usd > authority.maximum_exposure_usd):
                reasons.append("mission maximum exposure exceeded")
            if (numeric_valid and isinstance(authority.daily_limit_usd, Decimal)
                    and authority.daily_limit_usd.is_finite()
                    and daily_used + proposal.notional_usd > authority.daily_limit_usd):
                reasons.append("mission daily limit exceeded")
        if not getattr(venue_adapter, "supports_live_execution", False):
            reasons.append("venue adapter does not support live execution")
        if not getattr(venue_adapter, "execution_economics_verified", False):
            reasons.append("venue fees, depth, and fill economics are not verified")
        if not getattr(venue_adapter, "execution_account_state_current", False):
            reasons.append("current venue balance, positions, and open orders are not verified")
        try:
            stable_id_supported = "client_order_id" in signature(venue_adapter.execute).parameters
        except (TypeError, ValueError):
            stable_id_supported = False
        if (not getattr(venue_adapter, "supports_authoritative_reconciliation", False)
                or not callable(getattr(venue_adapter, "reconcile_execution", None))
                or not stable_id_supported):
            reasons.append("venue adapter cannot reconcile a dispatch by stable provider identity")
        mode = getattr(risk_engine.policy, "mode", None)
        if getattr(mode, "value", mode) != "live":
            reasons.append("deterministic risk engine is not in live mode")
        if proposal.mission_id == "" or not proposal.evidence_refs:
            reasons.append("mission or evidence references are missing")
        if proposal.expires_at.tzinfo is None or proposal.expires_at <= now:
            reasons.append("proposal is expired")
        if not numeric_valid:
            reasons.append("proposal contains a malformed numeric field")
        elif proposal.notional_usd <= 0 or proposal.max_loss_usd < proposal.notional_usd:
            reasons.append("notional or maximum loss is invalid")
        if (isinstance(proposal.limit_price, Decimal) and proposal.limit_price.is_finite()
                and not Decimal(0) < proposal.limit_price < Decimal(1)):
            reasons.append("prediction-market limit price is invalid")
        if (isinstance(proposal.expected_edge, Decimal) and proposal.expected_edge.is_finite()
                and proposal.expected_edge <= 0):
            reasons.append("proposal has no positive deterministic expected edge")
        if (action.decision is not Decision.LIVE_BUY_YES
                or action.market_id != proposal.instrument or action.venue != proposal.venue
                or Decimal(str(action.stake_usd)) != proposal.notional_usd
                or Decimal(str(action.max_price or 0)) != proposal.limit_price):
            reasons.append("proposal does not match the deterministic action")
        snapshot = opportunity.snapshot
        if snapshot.market_id != proposal.instrument or snapshot.venue != proposal.venue:
            reasons.append("proposal does not match its market evidence")
        expected = risk_engine.decide(opportunity, bankroll_usd)
        if (expected.decision is not Decision.LIVE_BUY_YES
                or expected.market_id != action.market_id
                or expected.stake_usd != action.stake_usd
                or expected.max_price != action.max_price):
            reasons.append("independent risk recheck did not approve this exact action")
        return reasons

    def _reserve_prediction(
        self, proposal: ExecutionProposal, payload: dict[str, Any],
        authority: PredictionExecutionAuthority,
    ) -> bool:
        request_hash = self._hash(payload)
        cap = self._decimal(os.getenv("NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD", "0")) or Decimal(0)
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM execution_gateway_requests WHERE proposal_id=?",
                            (proposal.proposal_id,)).fetchone():
                conn.rollback()
                return False
            if conn.execute(
                "SELECT 1 FROM execution_gateway_requests WHERE route='prediction' "
                "AND tier='execution' AND venue=? AND instrument=? AND side=? "
                "AND status IN ('reserved','submitted','partial','unknown','failed','filled','cancelled','expired','rejected') "
                "AND ((filled_exposure_usd IS NULL AND remaining_reserved_usd IS NULL "
                "AND status IN ('reserved','submitted','partial','unknown','failed','filled')) "
                "OR COALESCE(CAST(filled_exposure_usd AS REAL),0)>0 "
                "OR COALESCE(CAST(remaining_reserved_usd AS REAL),0)>0) LIMIT 1",
                (proposal.venue, proposal.instrument, proposal.side),
            ).fetchone():
                conn.rollback()
                return False
            prefix = datetime.now(UTC).date().isoformat() + "%"
            rows = conn.execute(
                "SELECT notional_usd,status,filled_exposure_usd,remaining_reserved_usd "
                "FROM execution_gateway_requests WHERE route='prediction' "
                "AND created_at LIKE ? AND status IN ('reserved','submitted','partial','confirmed','unknown','failed','filled','cancelled','expired','rejected')",
                (prefix,),
            ).fetchall()
            used = sum((self._row_exposure(row) for row in rows), Decimal(0))
            mission_rows = conn.execute(
                "SELECT notional_usd,status,filled_exposure_usd,remaining_reserved_usd FROM execution_gateway_requests WHERE route='prediction' "
                "AND mission_id=? AND proposal_id<>? "
                "AND status IN ('reserved','submitted','partial','confirmed','unknown','failed','filled','cancelled','expired','rejected')",
                (proposal.mission_id, proposal.proposal_id),
            ).fetchall()
            mission_used = sum((self._row_exposure(row) for row in mission_rows), Decimal(0))
            if (used + proposal.notional_usd > cap
                    or proposal.notional_usd > authority.per_action_limit_usd
                    or mission_used + proposal.notional_usd > authority.per_mission_limit_usd
                    or mission_used + proposal.notional_usd > authority.maximum_exposure_usd
                    or used + proposal.notional_usd > authority.daily_limit_usd):
                conn.rollback()
                return False
            conn.execute("INSERT INTO execution_gateway_requests "
                         "(proposal_id,created_at,route,tier,venue,status,request_hash,"
                         "notional_usd,mission_id,instrument,side,provider_reference,result_json,request_json) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (proposal.proposal_id, datetime.now(UTC).isoformat(), "prediction",
                          "execution", proposal.venue, "reserved", request_hash,
                          str(proposal.notional_usd), proposal.mission_id, proposal.instrument,
                          proposal.side, None, "{}",
                          json.dumps(payload, sort_keys=True, default=str)))
            self._append_transition(conn, proposal.proposal_id, None, "reserved", "gateway", {})
            conn.commit()
        return True

    def _record(
        self, request_id: str, route: str, tier: str, venue: str | None,
        status: str, notional: Decimal | None, payload: dict[str, Any],
        reasons: list[str],
    ) -> None:
        result = json.dumps({"reasons": reasons}, sort_keys=True)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("INSERT OR IGNORE INTO execution_gateway_requests "
                         "(proposal_id,created_at,route,tier,venue,status,request_hash,"
                         "notional_usd,mission_id,instrument,side,provider_reference,result_json,request_json) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (request_id, datetime.now(UTC).isoformat(), route, tier, venue,
                          status, self._hash(payload), str(notional) if notional is not None else None,
                          payload.get("mission_id"), payload.get("instrument"), payload.get("side"),
                          None, result,
                          json.dumps(payload, sort_keys=True, default=str)))
            if cursor.rowcount:
                self._append_transition(conn, request_id, None, status, "gateway", {"reasons": reasons})
                conn.commit()

    def _reserve_generic(
        self, request_id: str, route: str, tier: str, venue: str | None,
        notional: Decimal | None, payload: dict[str, Any], request_hash: str,
    ) -> bool:
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            try:
                conn.execute("INSERT INTO execution_gateway_requests "
                             "(proposal_id,created_at,route,tier,venue,status,request_hash,"
                             "notional_usd,mission_id,instrument,side,provider_reference,result_json,request_json) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (request_id, datetime.now(UTC).isoformat(), route, tier, venue,
                              "reserved", request_hash, str(notional) if notional is not None else None,
                              payload.get("mission_id"), payload.get("instrument"), payload.get("side"),
                              None, "{}",
                              json.dumps(payload, sort_keys=True, default=str)))
                self._append_transition(conn, request_id, None, "reserved", "gateway", {})
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                conn.rollback()
                return False

    def _finish(
        self, request_id: str, status: str, reference: str | None,
        result: dict[str, Any],
    ) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT status FROM execution_gateway_requests WHERE proposal_id=?",
                               (request_id,)).fetchone()
            conn.execute("UPDATE execution_gateway_requests SET status=?, provider_reference=COALESCE(?,provider_reference), "
                         "result_json=? WHERE proposal_id=?",
                         (status, reference, json.dumps(result, sort_keys=True), request_id))
            if old and old[0] != status:
                self._append_transition(conn, request_id, old[0], status, "gateway", result)
            conn.commit()

    @staticmethod
    def _append_transition(
        conn: sqlite3.Connection, proposal_id: str, from_status: str | None,
        to_status: str, source: str, evidence: dict[str, Any],
    ) -> None:
        conn.execute("INSERT INTO execution_gateway_transitions "
                     "(proposal_id,occurred_at,from_status,to_status,source,evidence_json) "
                     "VALUES (?,?,?,?,?,?)", (proposal_id, datetime.now(UTC).isoformat(),
                     from_status, to_status, source, json.dumps(evidence, sort_keys=True, default=str)))

    def apply_reconciliation(self, observation: ReconciliationObservation) -> bool:
        """Apply exact provider truth. Mismatches never mutate the reservation."""
        observation.validate()
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM execution_gateway_requests WHERE proposal_id=?",
                               (observation.proposal_id,)).fetchone()
            if (row is None or row["venue"] != observation.provider
                    or row["status"] not in {"unknown", "submitted", "reserved", "partial",
                                             "filled", "rejected", "cancelled", "expired", "failed"}):
                conn.rollback()
                return False
            if observation.provider_reference and row["provider_reference"] and (
                    observation.provider_reference != row["provider_reference"]):
                conn.rollback()
                return False
            previous = json.loads(row["result_json"] or "{}")
            previous_fills = set(previous.get("external_fill_ids", ()))
            incoming_fills = set(observation.external_fill_ids)
            if not previous_fills.issubset(incoming_fills):
                # Provider fill histories must be cumulative, not a smaller page/delta.
                conn.rollback()
                return False
            old_fee = self._decimal(previous.get("fee_amount"))
            if (old_fee is not None and observation.fee_amount is not None
                    and observation.fee_amount < old_fee):
                conn.rollback()
                return False
            old_quantity = self._decimal(previous.get("filled_quantity"))
            if (old_quantity is not None and observation.filled_quantity is not None
                    and observation.filled_quantity < old_quantity):
                conn.rollback()
                return False
            old_filled = self._decimal(row["filled_exposure_usd"])
            old_reserved = self._decimal(row["remaining_reserved_usd"])
            new_filled = observation.filled_exposure_usd
            new_reserved = observation.remaining_reserved_usd
            if new_filled is not None and old_filled is not None and new_filled < old_filled:
                conn.rollback()
                return False
            if new_reserved is not None and old_reserved is not None and new_reserved > old_reserved:
                conn.rollback()
                return False
            terminal = {"filled", "rejected", "cancelled", "expired", "failed"}
            allowed_next = {
                "reserved": {"submitted", "partial", "filled", "unknown", "rejected", "cancelled", "expired", "failed"},
                "submitted": {"submitted", "partial", "filled", "rejected", "cancelled", "expired", "failed"},
                "unknown": {"submitted", "partial", "filled", "rejected", "cancelled", "expired", "failed"},
                "partial": {"partial", "filled", "rejected", "cancelled", "expired", "failed"},
                "filled": {"filled"}, "rejected": {"rejected"},
                "cancelled": {"cancelled"}, "expired": {"expired"}, "failed": {"failed"},
            }
            if observation.status not in allowed_next.get(row["status"], set()):
                conn.rollback()
                return False
            if observation.status in terminal and row["status"] == "partial" and new_filled is None:
                conn.rollback()
                return False
            # For an observation without new cumulative facts, preserve the previous values.
            if new_filled is None:
                new_filled = old_filled or Decimal(0)
            if new_reserved is None:
                new_reserved = old_reserved if old_reserved is not None else (
                    self._decimal(row["notional_usd"]) or Decimal(0)
                )
            if observation.status in terminal:
                new_reserved = Decimal(0)
            requested_notional = self._decimal(row["notional_usd"])
            if (requested_notional is not None and new_filled + new_reserved > requested_notional):
                conn.rollback()
                return False
            if row["status"] in terminal and observation.status == row["status"]:
                # Same terminal snapshot is idempotent; new fill/fee facts still append below.
                pass
            evidence = {
                "source": observation.source, "observed_at": observation.observed_at.isoformat(),
                "filled_quantity": (str(observation.filled_quantity)
                                     if observation.filled_quantity is not None else None),
                "fee_amount": str(observation.fee_amount) if observation.fee_amount is not None else None,
                "fee_currency": observation.fee_currency,
                "external_fill_ids": sorted(incoming_fills | previous_fills),
                "filled_exposure_usd": str(new_filled),
                "remaining_reserved_usd": str(new_reserved),
            }
            if observation.fee_amount is None and "fee_amount" in previous:
                evidence["fee_amount"] = previous["fee_amount"]
                evidence["fee_currency"] = previous.get("fee_currency")
            if observation.filled_quantity is None and "filled_quantity" in previous:
                evidence["filled_quantity"] = previous["filled_quantity"]
            changed = (row["status"] != observation.status
                       or observation.provider_reference is not None
                       and observation.provider_reference != row["provider_reference"]
                       or new_filled != old_filled or new_reserved != old_reserved
                       or incoming_fills != previous_fills
                       or observation.filled_quantity is not None
                       and observation.filled_quantity != old_quantity
                       or observation.fee_amount is not None and observation.fee_amount != old_fee)
            if changed:
                conn.execute("UPDATE execution_gateway_requests SET status=?,"
                             "provider_reference=COALESCE(?,provider_reference),result_json=?,"
                             "filled_exposure_usd=?,remaining_reserved_usd=? WHERE proposal_id=?",
                             (observation.status, observation.provider_reference,
                              json.dumps(evidence, sort_keys=True), str(new_filled), str(new_reserved),
                              observation.proposal_id))
                self._append_transition(conn, observation.proposal_id, row["status"],
                                        observation.status, observation.source, evidence)
            conn.commit()
            return True

    async def reconcile_pending(self, adapter_resolver: Callable[[str], Any]) -> dict[str, int]:
        """Query adapters; absent or inconclusive provider evidence stays reserved."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM execution_gateway_requests WHERE status IN "
                                "('unknown','submitted','partial') ORDER BY created_at").fetchall()
        counts = {"checked": 0, "resolved": 0, "unchanged": 0}
        for row in rows:
            try:
                adapter = adapter_resolver(row["venue"])
                reconcile = getattr(adapter, "reconcile_execution", None) if adapter else None
                if not callable(reconcile):
                    counts["unchanged"] += 1
                    continue
                counts["checked"] += 1
                observation = await reconcile(dict(row))
                if observation is None or not self.apply_reconciliation(observation):
                    counts["unchanged"] += 1
                else:
                    counts["resolved"] += 1
            except Exception:  # noqa: BLE001 - provider failures must preserve the reservation.
                # Provider errors and malformed responses are not evidence of failure.
                counts["unchanged"] += 1
        return counts

    def _mission_exposure(self, mission_id: str, exclude_id: str) -> Decimal:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT notional_usd,status,filled_exposure_usd,remaining_reserved_usd "
                "FROM execution_gateway_requests WHERE route='prediction' "
                "AND mission_id=? AND proposal_id<>? "
                "AND status IN ('reserved','submitted','partial','confirmed','unknown','failed','filled','cancelled','expired','rejected')",
                (mission_id, exclude_id),
            ).fetchall()
        return sum((self._row_exposure(row) for row in rows), Decimal(0))

    def _resolve_authority(
        self, mission_id: str,
    ) -> PredictionExecutionAuthority | None:
        if self.authority_resolver is None:
            return None
        try:
            return self.authority_resolver(mission_id)
        except (OSError, RuntimeError, ValueError, TypeError, sqlite3.Error):
            return None

    def _daily_exposure(self, exclude_id: str) -> Decimal:
        prefix = datetime.now(UTC).date().isoformat() + "%"
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT notional_usd,status,filled_exposure_usd,remaining_reserved_usd "
                "FROM execution_gateway_requests WHERE route='prediction' "
                "AND created_at LIKE ? AND proposal_id<>? "
                "AND status IN ('reserved','submitted','partial','confirmed','unknown','failed','filled','cancelled','expired','rejected')",
                (prefix, exclude_id),
            ).fetchall()
        return sum((self._row_exposure(row) for row in rows), Decimal(0))

    @staticmethod
    def _row_exposure(row: Any) -> Decimal:
        notional = Decimal(row[0]) if row[0] is not None else Decimal(0)
        filled = row[2]
        remaining = row[3]
        if filled is not None or remaining is not None:
            return (Decimal(filled) if filled is not None else Decimal(0)) + (
                Decimal(remaining) if remaining is not None else Decimal(0)
            )
        if row[1] in {"rejected", "cancelled", "expired"}:
            return Decimal(0)
        # Legacy ambiguous rows remain charged conservatively. Old failed rows
        # without explicit reconciliation facts are never assumed to be resolved.
        return notional

    @staticmethod
    def _proposal_payload(proposal: ExecutionProposal) -> dict[str, Any]:
        value = asdict(proposal)
        value["notional_usd"] = str(proposal.notional_usd)
        value["max_loss_usd"] = str(proposal.max_loss_usd)
        value["limit_price"] = str(proposal.limit_price)
        value["expected_edge"] = str(proposal.expected_edge)
        value["created_at"] = proposal.created_at.isoformat()
        value["expires_at"] = proposal.expires_at.isoformat()
        value["evidence_refs"] = list(proposal.evidence_refs)
        return value

    @staticmethod
    def _stable_client_order_id(proposal_id: str) -> str:
        import uuid
        return str(uuid.uuid5(uuid.NAMESPACE_URL, "noema:" + proposal_id))

    @staticmethod
    def _hash(payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def _decimal(value: object) -> Decimal | None:
        try:
            result = Decimal(str(value))
            return result if result.is_finite() else None
        except (InvalidOperation, ValueError, TypeError):
            return None
