"""Close forecast → proposal → venue → fill for one prediction cycle.

Paper is the default path. A paper fill and a live fill both pass through
``summarize_fills`` and ``EconomicLedger.record_event`` with the same evidence
fields. Live submission happens only when earned autonomy, the owner-review
flag, and the execution-gateway flags all allow it, and ``NOEMA_MASTER_HALT``
stops new orders immediately.

``market_qualification`` stays a research projection: its execution block is
hard-coded not live-eligible and is not an authority grant or a veto.
``market_discovery`` remains the agent's pair sampler. This coordinator qualifies
prediction opportunities with ``edge.compare_binary_edges`` and
``entry_quality.assess_entry_quality``, then asks ``RiskEngine`` to decide.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

from .autonomy import earned_autonomy
from .autonomy_authority import (
    authority_for,
    level_at_least,
    live_permission,
    master_halt_active,
    per_trade_cap,
    risk_policy_for_level,
)
from .autonomy_transition import transition_autonomy
from .economic_ledger import EconomicEvent, EconomicLedger
from .economic_models import AutonomyEvidence, AutonomyLevel
from .edge import CostAssumptions, Side, compare_binary_edges
from .edge_decay import estimate_edge_decay
from .entry_quality import EntryInputs, assess_entry_quality
from .execution_gateway import (
    ExecutionGateway,
    ExecutionProposal,
    ReconciliationObservation,
)
from .fill_accounting import FillAccounting, summarize_fills
from .models import Action, Decision, Forecast, MarketSnapshot, Opportunity
from .risk import RiskEngine, RiskPolicy
from .sqlite_util import connect as sqlite_connect
from .trade_telemetry import ObservedFill, ObservedOrder
from .venues.base import VenueAdapter

_OPEN_STATUSES = frozenset({"reserved", "submitted", "partial", "unknown"})
_CONSUMED_STATUSES = _OPEN_STATUSES | frozenset({
    "filled", "rejected", "cancelled", "expired", "failed",
})


@dataclass(frozen=True)
class TradingCycleReport:
    status: str
    level: str
    mode: str
    halted: bool
    proposals: int
    submitted: int
    paper_fills: int
    rejected: int
    cancelled: int
    reasons: tuple[str, ...]


class InactiveVenue(VenueAdapter):
    """Paper-cycle stand-in. It cannot submit a live order."""

    name = "unconfigured"
    supports_live_execution = False

    async def markets(self):
        if False:
            yield None

    async def execute(self, action: Action) -> str:
        raise RuntimeError("live venue adapter is not configured")


class TradingCoordinator:
    def __init__(
        self,
        db_path: str,
        gateway: ExecutionGateway,
        risk_engine: RiskEngine,
        venue: VenueAdapter,
        *,
        mission_id: str = "prediction-markets",
        ledger: EconomicLedger | None = None,
    ) -> None:
        self.db_path = db_path
        self.gateway = gateway
        self.risk_engine = risk_engine
        self.venue = venue
        self.mission_id = mission_id
        self._owns_ledger = ledger is None
        self.ledger = ledger or EconomicLedger(db_path)
        self._conn = sqlite_connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()
        self._cycle_level = AutonomyLevel.SHADOW
        self._cycle_bankroll = Decimal(0)
        self._cycle_live = False
        self._cycle_instruments: frozenset[str] = frozenset()
        self._cycle_venues: frozenset[str] = frozenset()

    def close(self) -> None:
        self._conn.close()
        if self._owns_ledger:
            self.ledger.conn.close()

    async def run_cycle(
        self,
        opportunities: Sequence[Opportunity] | None = None,
        *,
        evidence: AutonomyEvidence | None = None,
        bankroll_usd: float | None = None,
        settlements: set[tuple[str, str]] | None = None,
    ) -> TradingCycleReport:
        if evidence is not None:
            self.store_evidence(evidence)
        else:
            evidence = self.load_evidence()
        level = self._apply_autonomy(evidence)
        self._cycle_level = level
        halted = master_halt_active()
        live, live_reasons = live_permission(level)
        bankroll, bankroll_reason = await self._resolve_bankroll(
            bankroll_usd, live=live and not halted,
        )
        self._cycle_bankroll = bankroll or Decimal(0)
        self._cycle_live = bool(live and not halted and bankroll is not None and bankroll > 0)
        if halted:
            mode = "halted"
        elif live and not self._cycle_live:
            mode = "blocked"
        elif self._cycle_live:
            mode = "live"
        else:
            mode = "paper"
        reasons: list[str] = []
        if halted:
            reasons.append("owner master halt is active")
        elif mode == "blocked":
            reasons.extend(live_reasons)
            if bankroll_reason:
                reasons.append(bankroll_reason)
        elif live_reasons and level_at_least(level, AutonomyLevel.MICRO):
            reasons.extend(live_reasons)
        if bankroll_reason and mode == "paper" and level_at_least(level, AutonomyLevel.PAPER):
            reasons.append(bankroll_reason)
        if opportunities is None and level_at_least(level, AutonomyLevel.PAPER) and not halted:
            opportunities = self.recent_opportunities()
        observed = opportunities or ()
        tradeable = () if mode in {"halted", "blocked"} or not level_at_least(
            level, AutonomyLevel.PAPER,
        ) else observed
        self._cycle_instruments = frozenset(
            item.snapshot.market_id for item in observed
        ) or self._open_instruments()
        self._cycle_venues = frozenset(item.snapshot.venue for item in observed)
        if self.venue.name and self.venue.name != "unconfigured":
            self._cycle_venues = frozenset({self.venue.name})
        cancelled = await self.enforce_open_order_limits()
        self._observe_edges(observed)
        await self._exit_positions(observed, settlements or set())
        proposals = submitted = paper_fills = rejected = 0
        previous_resolver = self.gateway.authority_resolver
        self.gateway.authority_resolver = self._resolve_authority
        try:
            for opportunity in tradeable:
                if master_halt_active():
                    halted = True
                    if "owner master halt is active" not in reasons:
                        reasons.append("owner master halt is active")
                    break
                outcome, reason = await self._trade_one(opportunity, bankroll, mode)
                if reason:
                    reasons.append(reason)
                if outcome == "submitted":
                    proposals += 1
                    submitted += 1
                elif outcome == "paper":
                    proposals += 1
                    paper_fills += 1
                elif outcome == "rejected":
                    proposals += 1
                    rejected += 1
        finally:
            self.gateway.authority_resolver = previous_resolver
        return TradingCycleReport(
            status="halted" if halted else "completed",
            level=level.value,
            mode="paper" if mode in {"halted", "blocked"} else mode,
            halted=halted,
            proposals=proposals,
            submitted=submitted,
            paper_fills=paper_fills,
            rejected=rejected,
            cancelled=cancelled,
            reasons=tuple(reasons),
        )

    async def reconcile(self) -> dict[str, int]:
        drained = await self._drain_private_events()
        polled = await self._poll_order_status()
        try:
            counts = await self.gateway.reconcile_pending(lambda _venue: self.venue)
        except (sqlite3.Error, OSError, RuntimeError, TypeError, ValueError):
            counts = {"checked": 0, "resolved": 0, "unchanged": 0}
        booked = self.book_gateway_fills()
        cancelled = await self._cancel_stale_orders()
        return {
            "checked": int(counts.get("checked", 0)),
            "resolved": int(counts.get("resolved", 0)),
            "unchanged": int(counts.get("unchanged", 0)),
            "drained": drained,
            "polled": polled,
            "booked": booked,
            "cancelled": cancelled,
        }

    def store_evidence(self, evidence: AutonomyEvidence) -> None:
        payload = {
            "resolved_forecasts": evidence.resolved_forecasts,
            "live_observation_days": evidence.live_observation_days,
            "after_cost_return": str(evidence.after_cost_return),
            "realized_net_pnl_usd": str(evidence.realized_net_pnl_usd),
            "max_drawdown_fraction": str(evidence.max_drawdown_fraction),
            "calibration_error": (
                None if evidence.calibration_error is None else str(evidence.calibration_error)
            ),
            "reconciliation_ok_fraction": str(evidence.reconciliation_ok_fraction),
            "profitable_days": evidence.profitable_days,
            "losing_days": evidence.losing_days,
        }
        self._conn.execute(
            "INSERT INTO trading_autonomy_evidence (mission_id, evidence_json, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT(mission_id) DO UPDATE SET "
            "evidence_json=excluded.evidence_json, updated_at=excluded.updated_at",
            (self.mission_id, json.dumps(payload, sort_keys=True), datetime.now(UTC).isoformat()),
        )
        self._conn.commit()

    def load_evidence(self) -> AutonomyEvidence | None:
        row = self._conn.execute(
            "SELECT evidence_json FROM trading_autonomy_evidence WHERE mission_id=?",
            (self.mission_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["evidence_json"])
            calibration = payload.get("calibration_error")
            return AutonomyEvidence(
                resolved_forecasts=int(payload["resolved_forecasts"]),
                live_observation_days=int(payload["live_observation_days"]),
                after_cost_return=Decimal(str(payload["after_cost_return"])),
                realized_net_pnl_usd=Decimal(str(payload["realized_net_pnl_usd"])),
                max_drawdown_fraction=Decimal(str(payload["max_drawdown_fraction"])),
                calibration_error=None if calibration is None else Decimal(str(calibration)),
                reconciliation_ok_fraction=Decimal(str(payload["reconciliation_ok_fraction"])),
                profitable_days=int(payload["profitable_days"]),
                losing_days=int(payload["losing_days"]),
            )
        except (KeyError, TypeError, ValueError, InvalidOperation, json.JSONDecodeError):
            return None

    def store_level(self, level: AutonomyLevel) -> None:
        self._conn.execute(
            "INSERT INTO trading_autonomy_state (mission_id, level, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(mission_id) DO UPDATE SET level=excluded.level, updated_at=excluded.updated_at",
            (self.mission_id, level.value, datetime.now(UTC).isoformat()),
        )
        self._conn.commit()

    def load_level(self) -> AutonomyLevel:
        row = self._conn.execute(
            "SELECT level FROM trading_autonomy_state WHERE mission_id=?",
            (self.mission_id,),
        ).fetchone()
        if row is None:
            return AutonomyLevel.SHADOW
        try:
            return AutonomyLevel(str(row["level"]))
        except ValueError:
            return AutonomyLevel.SHADOW

    def recent_opportunities(self, *, limit: int = 25) -> tuple[Opportunity, ...]:
        try:
            rows = self._conn.execute(
                "SELECT opportunity_json FROM forecast_ledger ORDER BY id DESC LIMIT 200"
            ).fetchall()
        except sqlite3.Error:
            return ()
        chosen: list[Opportunity] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            opportunity = _opportunity_from_payload(row["opportunity_json"])
            if opportunity is None:
                continue
            key = (opportunity.snapshot.venue, opportunity.snapshot.market_id)
            if key in seen:
                continue
            seen.add(key)
            chosen.append(opportunity)
            if len(chosen) >= limit:
                break
        return tuple(chosen)

    async def enforce_open_order_limits(self) -> int:
        cap = self._current_per_trade_cap()
        cancelled = 0
        for row in self.gateway.pending_prediction_orders():
            notional = _decimal(row.get("notional_usd")) or Decimal(0)
            if notional > cap and await self._cancel_gateway_order(
                row, reason="above current autonomy limit",
            ):
                cancelled += 1
        return cancelled

    def book_gateway_fills(self) -> int:
        booked = 0
        for row in self._reconciled_rows():
            if self._book_row(row):
                booked += 1
        return booked

    def _apply_autonomy(self, evidence: AutonomyEvidence | None) -> AutonomyLevel:
        current = self.load_level()
        if evidence is None:
            if current is not AutonomyLevel.SHADOW:
                self._log_transition(
                    current, AutonomyLevel.SHADOW, AutonomyLevel.SHADOW, demoted=True,
                    reason="autonomy evidence unavailable; downshift immediately",
                )
                self.store_level(AutonomyLevel.SHADOW)
            return AutonomyLevel.SHADOW
        earned = earned_autonomy(evidence, current_level=current)
        transition = transition_autonomy(current, earned.earned_level)
        if transition.next_level is not current:
            self._log_transition(
                transition.previous, transition.evidence_level, transition.next_level,
                demoted=transition.demoted, reason=transition.reason,
                earned_reasons=earned.reasons,
            )
            self.store_level(transition.next_level)
        return transition.next_level

    def _log_transition(
        self, previous: AutonomyLevel, evidence_level: AutonomyLevel, nxt: AutonomyLevel,
        *, demoted: bool, reason: str, earned_reasons: tuple[str, ...] = (),
    ) -> None:
        occurred = datetime.now(UTC)
        self.ledger.record_event(EconomicEvent(
            provider="noema",
            event_type="autonomy_transition",
            occurred_at=occurred,
            currency="USD",
            amount=Decimal(0),
            amount_usd=Decimal(0),
            reconciliation_state="OBSERVED",
            value_state="unknown",
            capital_class="none",
            confidence_state="derived",
            completeness_state="complete",
            external_reference_id=(
                f"autonomy:{self.mission_id}:{previous.value}:{nxt.value}:{occurred.isoformat()}"
            ),
            mission_id=self.mission_id,
            evidence={
                "previous": previous.value,
                "evidence_level": evidence_level.value,
                "next_level": nxt.value,
                "demoted": demoted,
                "promoted": (not demoted) and nxt is not previous,
                "reason": reason,
                "earned_reasons": list(earned_reasons),
            },
        ))

    async def _resolve_bankroll(
        self, explicit: float | None, *, live: bool,
    ) -> tuple[Decimal | None, str | None]:
        quoted, failed = await self._quoted_bankroll()
        if quoted is not None:
            return quoted, None
        if live and failed:
            return None, "venue bankroll is unavailable"
        if explicit is not None:
            number = _decimal(explicit)
            if number is None or number < 0:
                return None, "bankroll is malformed or negative"
            return number, None
        snapshot = self.ledger.latest_snapshot()
        if snapshot is not None:
            return snapshot.current_equity_usd, None
        raw = os.getenv("NOEMA_PAPER_BANKROLL_USD", "").strip()
        if raw:
            number = _decimal(raw)
            if number is None or number < 0:
                return None, "paper bankroll setting is malformed"
            return number, None
        return None, "bankroll is unavailable"

    async def _quoted_bankroll(self) -> tuple[Decimal | None, bool]:
        method = getattr(self.venue, "balances", None)
        if not callable(method):
            return None, False
        try:
            payload = method()
            if inspect.isawaitable(payload):
                payload = await payload
        except (OSError, RuntimeError, ValueError, TypeError, sqlite3.Error):
            return None, True
        if isinstance(payload, Decimal | int | float) and not isinstance(payload, bool):
            number = _decimal(payload)
            return (number, False) if number is not None and number >= 0 else (None, True)
        if isinstance(payload, Mapping):
            for key in ("buying_power_usd", "cash_balance_usd", "bankroll_usd"):
                if payload.get(key) is not None:
                    number = _decimal(payload.get(key))
                    if number is None or number < 0:
                        return None, True
                    return number, False
        return None, True

    def _resolve_authority(self, mission_id: str):
        if mission_id != self.mission_id or not self._cycle_live:
            return None
        return authority_for(
            self._cycle_level,
            self._cycle_bankroll,
            mission_id=mission_id,
            allowed_venues=self._cycle_venues,
            allowed_instruments=self._cycle_instruments,
        )

    async def _trade_one(
        self, opportunity: Opportunity, bankroll: Decimal | None, mode: str,
    ) -> tuple[str, str | None]:
        qualification = _qualification_reason(opportunity)
        if qualification is not None:
            return "skipped", None
        if bankroll is None:
            return "rejected", "bankroll is unavailable"
        policy = risk_policy_for_level(
            self.risk_engine.policy, self._cycle_level, bankroll, live=mode == "live",
        )
        engine = RiskEngine(policy)
        action = engine.decide(opportunity, float(bankroll))
        if mode == "blocked":
            return "rejected", "live bankroll or venue authority is unavailable"
        if action.decision is Decision.PASS:
            return "skipped", None
        if not opportunity.forecast.evidence_ids:
            return "rejected", "mission or evidence references are missing"
        proposal_id = _proposal_id(self.mission_id, opportunity)
        if mode == "live" and action.decision is Decision.LIVE_BUY_YES:
            if not self._live_venue_accepts(opportunity):
                return "rejected", "live venue adapter is unavailable for this market"
            return await self._submit_live(opportunity, action, engine, float(bankroll), proposal_id)
        if action.decision is Decision.PAPER_BUY_YES:
            return self._record_paper(opportunity, action, proposal_id)
        return "skipped", None

    def _live_venue_accepts(self, opportunity: Opportunity) -> bool:
        if not getattr(self.venue, "supports_live_execution", False):
            return False
        venue_name = getattr(self.venue, "name", "")
        return bool(venue_name) and opportunity.snapshot.venue == venue_name

    async def _submit_live(
        self, opportunity: Opportunity, action: Action, engine: RiskEngine,
        bankroll_usd: float, proposal_id: str,
    ) -> tuple[str, str | None]:
        existing = self.gateway.request_status(proposal_id)
        if existing in _CONSUMED_STATUSES:
            return "skipped", None
        try:
            proposal = ExecutionProposal.from_opportunity(
                proposal_id=proposal_id,
                mission_id=self.mission_id,
                opportunity=opportunity,
                action=action,
                evidence_refs=opportunity.forecast.evidence_ids,
                expires_at=datetime.now(UTC) + timedelta(seconds=_order_ttl_or_default()),
            )
        except (TypeError, ValueError):
            return "rejected", "proposal could not be formed"
        self.gateway.record_proposal(proposal)
        result = await self.gateway.submit_prediction_order(
            proposal=proposal,
            opportunity=opportunity,
            action=action,
            risk_engine=engine,
            venue_adapter=self.venue,
            bankroll_usd=bankroll_usd,
        )
        if not result.allowed:
            return "rejected", result.reasons[0] if result.reasons else "gateway rejected the proposal"
        self._remember_position(
            proposal_id=proposal_id,
            venue=opportunity.snapshot.venue,
            instrument=opportunity.snapshot.market_id,
            mode="live",
            quantity=Decimal(0),
            average_price=None,
            notional=proposal.notional_usd,
            edge=opportunity.robust_edge,
            status="open",
        )
        return "submitted", None

    def _record_paper(
        self, opportunity: Opportunity, action: Action, proposal_id: str,
    ) -> tuple[str, str | None]:
        if self._position_status(proposal_id) is not None or self._event_exists(f"paper:{proposal_id}:fill"):
            return "skipped", None
        price = _decimal(action.max_price)
        stake = _decimal(action.stake_usd)
        if price is None or stake is None or not Decimal(0) < price < Decimal(1) or stake <= 0:
            return "rejected", "paper action has no executable price or stake"
        count = (stake / price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if count <= 0:
            return "rejected", "paper stake is below one contract increment"
        now = datetime.now(UTC)
        proposal = ExecutionProposal(
            proposal_id=proposal_id,
            mission_id=self.mission_id,
            venue=action.venue,
            instrument=action.market_id,
            side="buy_yes",
            notional_usd=stake,
            max_loss_usd=stake,
            limit_price=price,
            expected_edge=_decimal(opportunity.robust_edge) or Decimal(0),
            evidence_refs=opportunity.forecast.evidence_ids,
            created_at=now,
            expires_at=now + timedelta(seconds=_order_ttl_or_default()),
        )
        self.gateway.record_proposal(proposal)
        accounting = self.record_accounted_fill(
            proposal_id=proposal_id,
            venue=action.venue,
            instrument=action.market_id,
            mode="paper",
            order_id=proposal_id,
            fill_id=f"paper:{proposal_id}:fill",
            count=count,
            price=price,
            fee=Decimal(0),
            remaining=Decimal(0),
            observed_at=now,
            external_reference_id=f"paper:{proposal_id}:fill",
        )
        self._remember_position(
            proposal_id=proposal_id,
            venue=action.venue,
            instrument=action.market_id,
            mode="paper",
            quantity=accounting.observed_filled_count,
            average_price=accounting.average_fill_price,
            notional=stake,
            edge=opportunity.robust_edge,
            status="open",
        )
        return "paper", None

    def record_accounted_fill(
        self, *, proposal_id: str, venue: str, instrument: str, mode: str,
        order_id: str, fill_id: str, count: Decimal, price: Decimal, fee: Decimal,
        remaining: Decimal, observed_at: datetime, external_reference_id: str,
    ) -> FillAccounting:
        order = ObservedOrder(
            order_id=order_id, ticker=instrument,
            status="filled" if remaining == 0 else "partial",
            initial_count=count + remaining, fill_count=count, remaining_count=remaining,
            yes_price=price, no_price=None, taker_fill_cost=price * count,
            maker_fill_cost=Decimal(0), taker_fees=fee, maker_fees=Decimal(0),
            order_group_id=proposal_id, created_at=observed_at, updated_at=observed_at,
        )
        fill = ObservedFill(
            fill_id=fill_id, order_id=order_id, ticker=instrument, outcome_side="yes",
            count=count, yes_price=price, no_price=None, is_taker=True, fee_cost=fee,
            created_at=observed_at,
        )
        accounting = summarize_fills(order, [fill])
        self.ledger.record_event(EconomicEvent(
            provider=venue,
            event_type="prediction_fill",
            occurred_at=observed_at,
            currency="USD",
            amount=accounting.gross_fill_cost,
            amount_usd=accounting.gross_fill_cost,
            reconciliation_state="RECONCILED" if mode == "live" else "ESTIMATED",
            value_state="realized" if mode == "live" else "paper",
            capital_class="none",
            confidence_state="provider_confirmed" if mode == "live" else "estimated",
            completeness_state="complete",
            external_reference_id=external_reference_id,
            mission_id=self.mission_id,
            lane="prediction",
            evidence=_fill_evidence(accounting, proposal_id, venue, instrument, mode, fee),
        ))
        return accounting

    def _remember_position(
        self, *, proposal_id: str, venue: str, instrument: str, mode: str,
        quantity: Decimal, average_price: Decimal | None, notional: Decimal,
        edge: float, status: str,
    ) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO trading_coordinator_positions "
            "(proposal_id, venue, instrument, mode, status, quantity, average_price, notional_usd, "
            "opened_at, edge_history_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (proposal_id, venue, instrument, mode, status, str(quantity),
             None if average_price is None else str(average_price), str(notional),
             datetime.now(UTC).isoformat(), json.dumps([edge])),
        )
        self._conn.commit()

    def _observe_edges(self, opportunities: Sequence[Opportunity]) -> None:
        for opportunity in opportunities:
            rows = self._conn.execute(
                "SELECT proposal_id, edge_history_json FROM trading_coordinator_positions "
                "WHERE venue=? AND instrument=? AND status IN ('open', 'exit_pending')",
                (opportunity.snapshot.venue, opportunity.snapshot.market_id),
            ).fetchall()
            for row in rows:
                try:
                    history = json.loads(row["edge_history_json"])
                except json.JSONDecodeError:
                    history = []
                if not isinstance(history, list):
                    history = []
                history.append(opportunity.robust_edge)
                self._conn.execute(
                    "UPDATE trading_coordinator_positions SET edge_history_json=? WHERE proposal_id=?",
                    (json.dumps(history), row["proposal_id"]),
                )
        self._conn.commit()

    async def _exit_positions(
        self, opportunities: Sequence[Opportunity], settlements: set[tuple[str, str]],
    ) -> None:
        by_market = {
            (item.snapshot.venue, item.snapshot.market_id): item for item in opportunities
        }
        rows = self._conn.execute(
            "SELECT * FROM trading_coordinator_positions WHERE status IN ('open', 'exit_pending')"
        ).fetchall()
        for row in rows:
            key = (str(row["venue"]), str(row["instrument"]))
            opportunity = by_market.get(key)
            history = _edge_history(row["edge_history_json"])
            settled = key in settlements
            if not _should_exit(history, settled=settled):
                continue
            if str(row["status"]) == "exit_pending" and not settled:
                continue
            await self._close_position(row, opportunity, settled=settled)

    async def _close_position(self, row: sqlite3.Row, opportunity: Opportunity | None, *, settled: bool) -> None:
        proposal_id = str(row["proposal_id"])
        mode = str(row["mode"])
        reference = f"exit:{proposal_id}"
        price = None
        if opportunity is not None and opportunity.snapshot.yes_bid is not None:
            price = _decimal(opportunity.snapshot.yes_bid)
        quantity = _decimal(row["quantity"]) or Decimal(0)
        if mode == "live":
            closer = getattr(self.venue, "close_position", None)
            if not callable(closer):
                self._mark_position(proposal_id, "exit_pending")
                if not self._event_exists(reference):
                    self._record_exit_event(
                        proposal_id, str(row["venue"]), str(row["instrument"]), mode,
                        amount=Decimal(0), reference=reference, settled=settled,
                        reason="venue adapter cannot close a filled position",
                    )
                return
            await _maybe_await(closer(str(row["instrument"])))
        amount = price * quantity if price is not None and quantity > 0 else Decimal(0)
        if price is None and mode == "paper":
            self._mark_position(proposal_id, "exit_pending")
            return
        if not self._event_exists(reference):
            self._record_exit_event(
                proposal_id, str(row["venue"]), str(row["instrument"]), mode,
                amount=amount, reference=reference, settled=settled,
                reason="settlement" if settled else "edge decayed",
            )
        self._mark_position(proposal_id, "closed")

    def _record_exit_event(
        self, proposal_id: str, venue: str, instrument: str, mode: str, *,
        amount: Decimal, reference: str, settled: bool, reason: str,
    ) -> None:
        self.ledger.record_event(EconomicEvent(
            provider=venue, event_type="prediction_exit", occurred_at=datetime.now(UTC),
            currency="USD", amount=amount, amount_usd=amount,
            reconciliation_state="RECONCILED" if mode == "live" and reason != "venue adapter cannot close a filled position" else "ESTIMATED",
            value_state="realized" if mode == "live" else "paper",
            capital_class="none",
            confidence_state="estimated",
            completeness_state="complete" if reason != "venue adapter cannot close a filled position" else "incomplete",
            external_reference_id=reference, mission_id=self.mission_id, lane="prediction",
            evidence={
                "proposal_id": proposal_id, "instrument": instrument, "mode": mode,
                "settled": settled, "reason": reason,
            },
        ))

    async def _drain_private_events(self) -> int:
        method = getattr(self.venue, "stream_private", None)
        if not callable(method):
            return 0
        applied = 0
        try:
            result = method()
            if inspect.isasyncgen(result) or hasattr(result, "__anext__"):
                while True:
                    try:
                        event = await asyncio.wait_for(result.__anext__(), timeout=0.01)
                    except (StopAsyncIteration, TimeoutError):
                        break
                    if isinstance(event, ReconciliationObservation) and self.gateway.apply_reconciliation(event):
                        applied += 1
                closer = getattr(result, "aclose", None)
                if callable(closer):
                    await closer()
            elif inspect.isawaitable(result):
                payload = await result
                events = payload if isinstance(payload, list) else []
                for event in events:
                    if isinstance(event, ReconciliationObservation) and self.gateway.apply_reconciliation(event):
                        applied += 1
        except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
            return applied
        return applied

    async def _poll_order_status(self) -> int:
        method = getattr(self.venue, "order_status", None)
        if not callable(method):
            return 0
        applied = 0
        for row in self.gateway.pending_prediction_orders():
            reference = row.get("provider_reference")
            if not reference:
                continue
            try:
                status = await _maybe_await(method(reference))
            except (OSError, RuntimeError, TypeError, ValueError):
                continue
            if isinstance(status, ReconciliationObservation) and self.gateway.apply_reconciliation(status):
                applied += 1
        return applied

    def _book_row(self, row: Mapping[str, Any]) -> bool:
        try:
            evidence = json.loads(row.get("result_json") or "{}")
        except json.JSONDecodeError:
            return False
        if not isinstance(evidence, dict):
            return False
        fill_ids = evidence.get("external_fill_ids") or []
        if not isinstance(fill_ids, list) or not fill_ids:
            return False
        proposal_id = str(row["proposal_id"])
        known = self._booked_ids(proposal_id)
        new_ids = [str(item) for item in fill_ids if str(item) not in known]
        if not new_ids:
            return False
        quantity = _decimal(evidence.get("filled_quantity"))
        exposure = _decimal(row.get("filled_exposure_usd")) or _decimal(evidence.get("filled_exposure_usd"))
        fee = _decimal(evidence.get("fee_amount")) or Decimal(0)
        prior_qty, prior_exposure, prior_fee = self._booked_totals(proposal_id)
        if quantity is None or exposure is None:
            return False
        delta_qty = quantity - prior_qty
        delta_exposure = exposure - prior_exposure
        delta_fee = fee - prior_fee
        if delta_qty <= 0 or delta_exposure < 0 or delta_fee < 0:
            return False
        price = delta_exposure / delta_qty
        if not price.is_finite() or not Decimal(0) < price < Decimal(1):
            return False
        fill_key = ",".join(sorted(new_ids))
        observed_at = _parse_time(evidence.get("observed_at")) or datetime.now(UTC)
        self.record_accounted_fill(
            proposal_id=proposal_id, venue=str(row["venue"]), instrument=str(row.get("instrument") or ""),
            mode="live", order_id=str(row.get("provider_reference") or proposal_id),
            fill_id=fill_key, count=delta_qty, price=price, fee=delta_fee,
            remaining=_decimal(row.get("remaining_reserved_usd")) or _decimal(evidence.get("remaining_reserved_usd")) or Decimal(0),
            observed_at=observed_at, external_reference_id=f"live:{proposal_id}:{fill_key}",
        )
        self._insert_booking(fill_key, proposal_id, delta_exposure, delta_qty, delta_fee)
        for fill_id in new_ids:
            if fill_id != fill_key:
                self._insert_booking(fill_id, proposal_id, Decimal(0), Decimal(0), Decimal(0))
        self._conn.execute(
            "UPDATE trading_coordinator_positions SET quantity=?, average_price=? WHERE proposal_id=?",
            (str(quantity), str(price), proposal_id),
        )
        self._conn.commit()
        return True

    async def _cancel_stale_orders(self) -> int:
        ttl = _order_ttl_seconds()
        if ttl is None:
            return 0
        cancelled = 0
        now = datetime.now(UTC)
        for row in self.gateway.pending_prediction_orders():
            filled = _decimal(row.get("filled_exposure_usd"))
            if filled is not None and filled > 0:
                continue
            created = _parse_time(row.get("created_at"))
            if created is None or now - created < timedelta(seconds=ttl):
                continue
            if await self._cancel_gateway_order(row, reason="order exceeded NOEMA_ORDER_TTL_SECONDS"):
                cancelled += 1
        return cancelled

    async def _cancel_gateway_order(self, row: Mapping[str, Any], *, reason: str) -> bool:
        status = str(row.get("status") or "")
        if status not in _OPEN_STATUSES:
            return False
        reference = row.get("provider_reference")
        filled = _decimal(row.get("filled_exposure_usd")) or Decimal(0)
        if status == "reserved" and not reference:
            return self._mark_cancelled(row, filled=filled, reason=reason)
        cancel = getattr(self.venue, "cancel", None)
        if not callable(cancel) or not reference:
            self._note_cancel_required(row, reason)
            return False
        try:
            await _maybe_await(cancel(str(reference)))
        except (OSError, RuntimeError, TypeError, ValueError):
            self._note_cancel_required(row, reason)
            return False
        return self._mark_cancelled(row, filled=filled, reason=reason)

    def _mark_cancelled(self, row: Mapping[str, Any], *, filled: Decimal, reason: str) -> bool:
        observation = ReconciliationObservation(
            provider=str(row.get("venue") or ""),
            proposal_id=str(row["proposal_id"]),
            provider_reference=row.get("provider_reference") or None,
            status="cancelled",
            observed_at=datetime.now(UTC),
            source="trading_coordinator",
            filled_quantity=Decimal(0) if filled == 0 else None,
            filled_exposure_usd=filled,
            remaining_reserved_usd=Decimal(0),
        )
        try:
            return self.gateway.apply_reconciliation(observation)
        except ValueError:
            return False

    def _note_cancel_required(self, row: Mapping[str, Any], reason: str) -> None:
        proposal_id = str(row["proposal_id"])
        reference = f"cancel-required:{proposal_id}"
        if self._event_exists(reference):
            return
        self.ledger.record_event(EconomicEvent(
            provider=str(row.get("venue") or "unknown"),
            event_type="prediction_cancel_required",
            occurred_at=datetime.now(UTC),
            currency="USD",
            amount=Decimal(0),
            reconciliation_state="INCOMPLETE",
            value_state="unknown",
            capital_class="none",
            confidence_state="estimated",
            completeness_state="incomplete",
            external_reference_id=reference,
            mission_id=self.mission_id,
            evidence={"proposal_id": proposal_id, "reason": reason},
        ))

    def _current_per_trade_cap(self) -> Decimal:
        if not self._cycle_live:
            return Decimal(0)
        cap = per_trade_cap(self._cycle_level, self._cycle_bankroll)
        return cap if cap is not None else Decimal(0)

    def _open_instruments(self) -> frozenset[str]:
        rows = self._conn.execute(
            "SELECT instrument FROM trading_coordinator_positions WHERE status='open'"
        ).fetchall()
        return frozenset(str(row["instrument"]) for row in rows)

    def _position_status(self, proposal_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT status FROM trading_coordinator_positions WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        return None if row is None else str(row["status"])

    def _event_exists(self, external_reference_id: str) -> bool:
        row = self.ledger.conn.execute(
            "SELECT 1 FROM economic_events WHERE external_reference_id=? LIMIT 1",
            (external_reference_id,),
        ).fetchone()
        return row is not None

    def _reconciled_rows(self) -> list[dict[str, Any]]:
        try:
            with sqlite_connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM execution_gateway_requests WHERE route='prediction' AND tier='execution' "
                    "AND status IN ('partial', 'filled', 'cancelled', 'expired', 'rejected', 'failed')"
                ).fetchall()
        except sqlite3.Error:
            return []
        return [dict(row) for row in rows]

    def _booked_ids(self, proposal_id: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT fill_key FROM trading_fill_bookings WHERE proposal_id=?",
            (proposal_id,),
        ).fetchall()
        return {str(row["fill_key"]) for row in rows}

    def _booked_totals(self, proposal_id: str) -> tuple[Decimal, Decimal, Decimal]:
        rows = self._conn.execute(
            "SELECT quantity, exposure_usd, fee_usd FROM trading_fill_bookings WHERE proposal_id=?",
            (proposal_id,),
        ).fetchall()
        quantity = exposure = fee = Decimal(0)
        for row in rows:
            quantity += _decimal(row["quantity"]) or Decimal(0)
            exposure += _decimal(row["exposure_usd"]) or Decimal(0)
            fee += _decimal(row["fee_usd"]) or Decimal(0)
        return quantity, exposure, fee

    def _insert_booking(
        self, fill_key: str, proposal_id: str, exposure: Decimal, quantity: Decimal, fee: Decimal,
    ) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO trading_fill_bookings "
            "(fill_key, proposal_id, exposure_usd, quantity, fee_usd, booked_at) VALUES (?, ?, ?, ?, ?, ?)",
            (fill_key, proposal_id, str(exposure), str(quantity), str(fee), datetime.now(UTC).isoformat()),
        )
        self._conn.commit()

    def _mark_position(self, proposal_id: str, status: str) -> None:
        self._conn.execute(
            "UPDATE trading_coordinator_positions SET status=? WHERE proposal_id=?",
            (status, proposal_id),
        )
        self._conn.commit()

    def _ensure_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trading_autonomy_state (
                mission_id TEXT PRIMARY KEY,
                level TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trading_autonomy_evidence (
                mission_id TEXT PRIMARY KEY,
                evidence_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trading_coordinator_positions (
                proposal_id TEXT PRIMARY KEY,
                venue TEXT NOT NULL,
                instrument TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                quantity TEXT NOT NULL,
                average_price TEXT,
                notional_usd TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                edge_history_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trading_fill_bookings (
                fill_key TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                exposure_usd TEXT NOT NULL,
                quantity TEXT NOT NULL,
                fee_usd TEXT NOT NULL,
                booked_at TEXT NOT NULL,
                PRIMARY KEY (proposal_id, fill_key)
            );
            """
        )
        self._conn.commit()


def _qualification_reason(opportunity: Opportunity) -> str | None:
    snapshot = opportunity.snapshot
    if snapshot.yes_ask is None or snapshot.yes_bid is None:
        return "missing tradable YES quote"
    if snapshot.captured_at.tzinfo is None:
        return "market snapshot timestamp is not timezone-aware"
    try:
        comparison = compare_binary_edges(
            probability_yes=opportunity.forecast.probability_yes,
            yes_ask=snapshot.yes_ask,
            no_ask=snapshot.no_ask,
            costs=CostAssumptions(),
        )
    except (TypeError, ValueError):
        return "edge comparison rejected the quote"
    if comparison.best is None or comparison.best.side is not Side.YES:
        return "best executable side is not YES"
    if opportunity.robust_edge <= 0:
        return "robust edge is not positive"
    age_ms = max(0.0, (datetime.now(UTC) - snapshot.captured_at).total_seconds() * 1000.0)
    quality = assess_entry_quality(EntryInputs(
        after_cost_edge=opportunity.robust_edge,
        spread=snapshot.yes_ask - snapshot.yes_bid,
        depth_imbalance=None,
        feed_latency_ms=age_ms,
        edge_persistence_ms=0.0,
        price_change_last_second=None,
    ))
    if not quality.eligible:
        return "entry quality below threshold"
    return None


def _proposal_id(mission_id: str, opportunity: Opportunity) -> str:
    created = opportunity.forecast.created_at.astimezone(UTC).isoformat()
    return f"{mission_id}:{opportunity.snapshot.venue}:{opportunity.snapshot.market_id}:{created}"


def _fill_evidence(
    accounting: FillAccounting, proposal_id: str, venue: str, instrument: str, mode: str, fee: Decimal,
) -> dict[str, Any]:
    return {
        "proposal_id": proposal_id,
        "order_id": accounting.order_id,
        "venue": venue,
        "instrument": instrument,
        "mode": mode,
        "side": "buy_yes",
        "filled_count": str(accounting.observed_filled_count),
        "remaining_count": str(accounting.remaining_count),
        "gross_fill_cost": str(accounting.gross_fill_cost),
        "fees_paid": str(accounting.fees_paid if accounting.fees_paid else fee),
        "average_fill_price": (
            None if accounting.average_fill_price is None else str(accounting.average_fill_price)
        ),
        "maker_fraction": (
            None if accounting.maker_fraction is None else str(accounting.maker_fraction)
        ),
        "fee_model": "provider" if mode == "live" else "not_modeled",
    }


def _should_exit(history: list[float], *, settled: bool) -> bool:
    if settled:
        return True
    if not history:
        return False
    if history[-1] <= 0:
        return True
    decay = estimate_edge_decay(history)
    if decay.half_life_steps is None:
        return False
    return len(history) > decay.half_life_steps and history[-1] < decay.initial_edge / 2


def _edge_history(payload: object) -> list[float]:
    try:
        raw = json.loads(str(payload))
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    history: list[float] = []
    for item in raw:
        number = _float(item)
        if number is None:
            return []
        history.append(number)
    return history


def _opportunity_from_payload(payload: object) -> Opportunity | None:
    try:
        raw = json.loads(payload) if isinstance(payload, str) else payload
        if not isinstance(raw, dict):
            return None
        snapshot_raw = raw["snapshot"]
        forecast_raw = raw["forecast"]
        snapshot = MarketSnapshot(
            venue=str(snapshot_raw["venue"]),
            market_id=str(snapshot_raw["market_id"]),
            title=str(snapshot_raw.get("title") or ""),
            yes_bid=_optional_float(snapshot_raw.get("yes_bid")),
            yes_ask=_optional_float(snapshot_raw.get("yes_ask")),
            no_bid=_optional_float(snapshot_raw.get("no_bid")),
            no_ask=_optional_float(snapshot_raw.get("no_ask")),
            liquidity_usd=_optional_float(snapshot_raw.get("liquidity_usd")),
            closes_at=_parse_time(snapshot_raw.get("closes_at")),
            resolution_rules=snapshot_raw.get("resolution_rules"),
            captured_at=_require_time(snapshot_raw.get("captured_at")),
        )
        evidence = forecast_raw.get("evidence_ids") or ()
        forecast = Forecast(
            market_id=str(forecast_raw["market_id"]),
            venue=str(forecast_raw["venue"]),
            probability_yes=float(forecast_raw["probability_yes"]),
            lower_bound=float(forecast_raw["lower_bound"]),
            upper_bound=float(forecast_raw["upper_bound"]),
            model_version=str(forecast_raw["model_version"]),
            evidence_ids=tuple(str(item) for item in evidence),
            created_at=_require_time(forecast_raw.get("created_at")),
        )
        return Opportunity(
            forecast=forecast,
            snapshot=snapshot,
            market_probability=float(raw["market_probability"]),
            raw_edge=float(raw["raw_edge"]),
            estimated_cost=float(raw["estimated_cost"]),
            uncertainty_penalty=float(raw["uncertainty_penalty"]),
            robust_edge=float(raw["robust_edge"]),
        )
    except (KeyError, TypeError, ValueError, InvalidOperation, json.JSONDecodeError):
        return None


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _order_ttl_seconds() -> int | None:
    raw = os.getenv("NOEMA_ORDER_TTL_SECONDS", "120").strip()
    if raw == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    if value < 0:
        return None
    return value


def _order_ttl_or_default() -> int:
    return _order_ttl_seconds() or 120


def _parse_time(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _require_time(value: object) -> datetime:
    parsed = _parse_time(value)
    if parsed is None:
        raise ValueError("timestamp is required")
    return parsed


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    number = _float(value)
    if number is None:
        raise ValueError("malformed number")
    return number


def _float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    return number


async def run_scheduled_trading_cycle(db_path: str) -> dict[str, Any]:
    """One agent-loop pass. Missing evidence or bankroll reconciles and does not order."""
    return await _scheduled(db_path, reconcile_only=False)


async def reconcile_scheduled_trading(db_path: str) -> dict[str, Any]:
    """Shutdown reconcile. It does not open orders."""
    return await _scheduled(db_path, reconcile_only=True)


async def _scheduled(db_path: str, *, reconcile_only: bool) -> dict[str, Any]:
    ledger = EconomicLedger(db_path)
    coordinator = TradingCoordinator(
        db_path,
        gateway=ExecutionGateway(db_path),
        risk_engine=RiskEngine(RiskPolicy()),
        venue=_scheduled_venue(),
        ledger=ledger,
    )
    try:
        if reconcile_only:
            counts = await coordinator.reconcile()
            return {"status": "reconciled", **counts}
        evidence = coordinator.load_evidence()
        report = await coordinator.run_cycle(evidence=evidence)
        counts = await coordinator.reconcile()
        payload = asdict(report)
        payload.update(counts)
        payload["reasons"] = list(report.reasons)
        return payload
    except (sqlite3.Error, OSError, RuntimeError, ValueError, TypeError, InvalidOperation):
        return {"status": "skipped", "reason": "trading coordinator pass failed closed"}
    finally:
        coordinator.close()
        ledger.conn.close()
        closer = getattr(coordinator.venue, "close", None)
        if callable(closer):
            await _maybe_await(closer())


def _scheduled_venue() -> VenueAdapter:
    flags_open = (
        not master_halt_active()
        and os.getenv("NOEMA_LIVE_REQUIRES_OWNER_REVIEW", "1") == "0"
        and os.getenv("NOEMA_EXECUTION_GATEWAY_ENABLED", "0") == "1"
        and os.getenv("NOEMA_ALLOW_LIVE_ORDERS", "0") == "1"
    )
    venues = [item.strip() for item in os.getenv("NOEMA_LIVE_VENUES", "").split(",") if item.strip()]
    if not flags_open or not venues:
        return InactiveVenue()
    if any(name.startswith("kalshi") for name in venues):
        try:
            from .venues.kalshi import KalshiVenue
            return KalshiVenue()
        except (OSError, RuntimeError, ValueError, TypeError):
            return InactiveVenue()
    return InactiveVenue()
