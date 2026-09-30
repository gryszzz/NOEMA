from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .economic_accounting import validate_snapshot
from .economic_models import EconomicSnapshot

RECONCILIATION_STATES = frozenset({
    "OBSERVED", "ESTIMATED", "MATCHED", "RECONCILED", "DISPUTED", "INCOMPLETE",
})
VALUE_STATES = frozenset({"realized", "unrealized", "paper", "reservation", "unknown"})
CAPITAL_CLASSES = frozenset({
    "owner_capital", "revenue", "trading_pnl", "cost", "transfer",
    "unclassified_cash", "unclassified_wallet_flow", "none",
})
CONFIDENCE_STATES = frozenset({
    "provider_confirmed", "operator_reported", "estimated", "derived", "unknown",
})
COMPLETENESS_STATES = frozenset({"complete", "incomplete", "unknown"})
COUNTERFACTUAL_TYPES = frozenset({
    "actual_action", "do_nothing", "baseline_strategy", "alternative_allocation",
})
COUNTERFACTUAL_BASES = frozenset({"hypothesis", "simulation", "paper", "realized"})
PROVIDER_COVERAGE_STATES = frozenset({
    "EXPECTED", "COMPLETE", "PARTIAL", "UNAVAILABLE", "NOT_APPLICABLE", "FAILED",
    "DISPUTED", "INCOMPLETE",  # accepted as a legacy input and normalized to PARTIAL
})
DEFAULT_PROVIDER_PERIOD_MANIFEST = {
    "stripe": ("captured_payments", "refunds", "disputes", "fees", "payouts"),
    "openai": ("usage", "provider_costs"),
    "cloudflare": ("workers_ai_usage", "provider_costs"),
    "render": ("hosting_invoice_or_authoritative_billing",),
    "wallets": ("confirmed_activity", "fees", "balances", "event_time_valuation"),
    "kalshi": ("fills", "fees", "settlements", "refunds_voids"),
    "polymarket_us": ("fills", "fees", "settlements", "refunds_voids"),
    "operator_expenses": ("receipts", "expenses"),
}


@dataclass(frozen=True)
class EconomicEvent:
    """Canonical append-only economic evidence, not financial authority."""

    provider: str
    event_type: str
    occurred_at: datetime
    currency: str
    amount: Decimal
    reconciliation_state: str = "OBSERVED"
    value_state: str = "unknown"
    capital_class: str = "none"
    confidence_state: str = "unknown"
    completeness_state: str = "unknown"
    external_reference_id: str | None = None
    amount_usd: Decimal | None = None
    mission_id: str | None = None
    strategy_id: str | None = None
    activity_id: str | None = None
    lane: str | None = None
    related_event_id: int | None = None
    owned_account_id: str | None = None
    counterparty_account_id: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.provider.strip() or not self.event_type.strip():
            raise ValueError("economic event provider and type are required")
        if not self.currency.strip() or len(self.currency) > 32:
            raise ValueError("economic event currency/asset is required")
        if self.occurred_at.tzinfo is None:
            raise ValueError("economic event timestamp must be timezone-aware")
        if not self.amount.is_finite() or (self.amount_usd is not None and not self.amount_usd.is_finite()):
            raise ValueError("economic event amounts must be finite")
        if self.reconciliation_state not in RECONCILIATION_STATES:
            raise ValueError("unsupported economic reconciliation state")
        if self.value_state not in VALUE_STATES:
            raise ValueError("unsupported economic value state")
        if self.capital_class not in CAPITAL_CLASSES:
            raise ValueError("unsupported economic capital classification")
        if self.confidence_state not in CONFIDENCE_STATES:
            raise ValueError("unsupported economic confidence state")
        if self.completeness_state not in COMPLETENESS_STATES:
            raise ValueError("unsupported economic completeness state")
        if self.external_reference_id is not None and not self.external_reference_id.strip():
            raise ValueError("external reference ID cannot be empty")
        # Atomic wallet units remain native-only unless an auditable event-time quote exists.
        if (self.provider.startswith("wallet:") and self.amount_usd is not None
                and self.currency.upper() != "USD"):
            required = {"decimals", "price_source", "price_timestamp", "valuation_basis", "price_usd"}
            if not required <= set(self.evidence):
                raise ValueError("wallet USD valuation requires decimals, event-time price provenance, and basis")
            if self.evidence.get("valuation_basis") != "event_time":
                raise ValueError("wallet USD valuation must use event-time basis")
            try:
                price_time = datetime.fromisoformat(str(self.evidence["price_timestamp"]))
                price = Decimal(str(self.evidence["price_usd"]))
                decimals = int(self.evidence["decimals"])
                converted = abs(self.amount) / (Decimal(10) ** decimals) * price
            except (ValueError, TypeError, ArithmeticError):
                raise ValueError("wallet valuation evidence is malformed") from None
            if (price_time.tzinfo is None or price_time.astimezone(UTC) > self.occurred_at.astimezone(UTC)
                    or not price.is_finite() or price < 0 or decimals < 0):
                raise ValueError("wallet valuation must use a valid quote no later than the event")
            if converted != abs(self.amount_usd):
                raise ValueError("wallet USD valuation does not match native amount and price evidence")


@dataclass(frozen=True)
class EconomicCounterfactual:
    """A mission comparison, kept outside cash/P&L and execution authority."""

    scenario_id: str
    mission_id: str
    scenario_type: str
    outcome_basis: str
    recorded_at: datetime
    currency: str = "USD"
    amount: Decimal | None = None
    amount_usd: Decimal | None = None
    activity_id: str | None = None
    strategy_id: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.scenario_id.strip() or not self.mission_id.strip():
            raise ValueError("counterfactual scenario and mission IDs are required")
        if self.scenario_type not in COUNTERFACTUAL_TYPES:
            raise ValueError("unsupported counterfactual scenario type")
        if self.outcome_basis not in COUNTERFACTUAL_BASES:
            raise ValueError("unsupported counterfactual outcome basis")
        if self.recorded_at.tzinfo is None or not self.currency.strip() or len(self.currency) > 32:
            raise ValueError("counterfactual timestamp and currency are required")
        if any(value is not None and not value.is_finite() for value in (self.amount, self.amount_usd)):
            raise ValueError("counterfactual amounts must be finite")


class EconomicLedger:
    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS economic_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                snapshot_json TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS economic_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                amount_usd TEXT,
                payload_json TEXT NOT NULL
            )
            """
        )
        ensure_economic_event_schema(self.conn)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS economic_counterfactuals ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT NOT NULL, scenario_id TEXT NOT NULL, "
            "mission_id TEXT NOT NULL, activity_id TEXT, strategy_id TEXT, scenario_type TEXT NOT NULL, "
            "outcome_basis TEXT NOT NULL, currency TEXT NOT NULL, amount TEXT, amount_usd TEXT, "
            "evidence_json TEXT NOT NULL)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS economic_counterfactual_mission "
            "ON economic_counterfactuals(mission_id,recorded_at)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS economic_reserve_attestations ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, month_utc TEXT NOT NULL, observed_at TEXT NOT NULL, "
            "designated_accounts_json TEXT NOT NULL, balances_json TEXT NOT NULL, liabilities_usd TEXT, "
            "reservations_usd TEXT, reserve_usd TEXT, state TEXT NOT NULL, blockers_json TEXT NOT NULL, "
            "provenance_json TEXT NOT NULL)"
        )
        self.conn.commit()

    def record_event(self, event: EconomicEvent) -> dict[str, Any]:
        """Append a provider event once; conflicting repeats become dispute evidence."""
        event.validate()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            result = _record_event(self.conn, event)
            self.conn.commit()
            return result
        except Exception:
            self.conn.rollback()
            raise

    def record_authoritative_billing_expense(
        self, *, provider: str, external_reference_id: str, month_utc: str,
        amount_usd: Decimal, occurred_at: datetime, service_scope: str,
        source: str, document_reference: str, attribution_basis: str,
        confidence_state: str = "provider_confirmed",
        mission_id: str | None = None, activity_id: str | None = None,
    ) -> dict[str, Any]:
        """Record an invoice/statement-backed expense without converting estimates.

        `amount_usd` must already be attributed to the NOEMA service. Shared
        workspace charges must be allocated from explicit source evidence before
        they can be recorded here. The document itself stays in its secure source;
        only its reference and provenance are retained in the ledger.
        """
        if (len(month_utc) != 7 or month_utc[4] != "-" or not month_utc[:4].isdigit()
                or not month_utc[5:].isdigit() or not 1 <= int(month_utc[5:]) <= 12):
            raise ValueError("billing period must be YYYY-MM")
        if (not external_reference_id.strip() or not provider.strip()
                or not source.strip() or not document_reference.strip()
                or not attribution_basis.strip()):
            raise ValueError("billing expense requires provider, invoice reference, source, and attribution")
        if service_scope != "noema_service":
            raise ValueError("only an explicitly attributed NOEMA service expense may be recorded")
        if not amount_usd.is_finite() or amount_usd < 0:
            raise ValueError("authoritative invoice amount must be finite and non-negative")
        if occurred_at.tzinfo is None or occurred_at.astimezone(UTC).strftime("%Y-%m") != month_utc:
            raise ValueError("expense event time must fall inside its billing period")
        if confidence_state not in {"provider_confirmed", "operator_reported"}:
            raise ValueError("invoice evidence confidence must be provider- or operator-reported")
        return self.record_event(EconomicEvent(
            provider=provider, external_reference_id=external_reference_id,
            event_type="authoritative_billing_expense", occurred_at=occurred_at,
            currency="USD", amount=amount_usd, amount_usd=amount_usd,
            reconciliation_state="RECONCILED", value_state="realized",
            capital_class="cost", confidence_state=confidence_state,
            completeness_state="complete", mission_id=mission_id,
            activity_id=activity_id, lane="infrastructure",
            evidence={"month_utc": month_utc, "service_scope": service_scope,
                      "source": source, "document_reference": document_reference,
                      "attribution_basis": attribution_basis,
                      "shared_workspace_cost_included": False},
        ))

    def append_reconciliation(
        self, event_id: int, *, state: str, evidence: dict[str, Any],
        adjustment_amount: Decimal | None = None, amount_usd: Decimal | None = None,
        occurred_at: datetime | None = None, confidence_state: str = "derived",
    ) -> int:
        """Append a correction/reconciliation record without rewriting its source event."""
        if state not in RECONCILIATION_STATES:
            raise ValueError("unsupported economic reconciliation state")
        source = self.conn.execute(
            "SELECT provider,currency,mission_id,strategy_id,activity_id,lane,capital_class "
            "FROM economic_events WHERE id=? AND provider IS NOT NULL", (event_id,),
        ).fetchone()
        if source is None:
            raise ValueError("canonical economic event not found")
        at = occurred_at or datetime.now(UTC)
        amount = adjustment_amount if adjustment_amount is not None else Decimal(0)
        adjustment = EconomicEvent(
            provider=str(source[0]), event_type=(
                "reconciliation_adjustment" if adjustment_amount is not None
                else "reconciliation_state_update"
            ),
            occurred_at=at, currency=str(source[1]), amount=amount,
            amount_usd=amount_usd, reconciliation_state=state,
            value_state="realized", capital_class=str(source[6]),
            confidence_state=confidence_state, completeness_state="complete",
            mission_id=source[2], strategy_id=source[3], activity_id=source[4],
            lane=source[5], related_event_id=event_id, evidence=evidence,
        )
        adjustment.validate()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            event_id_new = _insert_event(self.conn, adjustment)
            self.conn.commit()
            return event_id_new
        except Exception:
            self.conn.rollback()
            raise

    def reconcile_event_to_amount(
        self, event_id: int, *, authoritative_amount: Decimal,
        authoritative_amount_usd: Decimal | None, evidence: dict[str, Any],
        occurred_at: datetime, confidence_state: str = "provider_confirmed",
    ) -> list[int]:
        """Reconcile an estimate against a bill/statement by appending the delta and state evidence."""
        if not evidence or occurred_at.tzinfo is None or not authoritative_amount.is_finite():
            raise ValueError("authoritative reconciliation needs finite amount, time, and provenance")
        source = self.conn.execute(
            "SELECT amount,amount_usd,currency,capital_class FROM economic_events "
            "WHERE id=? AND provider IS NOT NULL", (event_id,),
        ).fetchone()
        if source is None:
            raise ValueError("canonical economic event not found")
        if source[3] not in {"cost", "revenue", "trading_pnl", "owner_capital"}:
            raise ValueError("only classified financial events can be reconciled to an amount")
        prior_amount = Decimal(str(source[0]))
        prior_usd = Decimal(str(source[1])) if source[1] is not None else None
        if prior_usd is not None and authoritative_amount_usd is None:
            raise ValueError("reconciliation cannot confirm an estimated USD amount without authoritative USD evidence")
        delta = authoritative_amount - prior_amount
        delta_usd = (authoritative_amount_usd - prior_usd
                     if authoritative_amount_usd is not None and prior_usd is not None
                     else authoritative_amount_usd)
        ids: list[int] = []
        if delta != 0 or delta_usd not in (None, Decimal(0)):
            ids.append(self.append_reconciliation(
                event_id, state="RECONCILED", evidence={**evidence, "correction": "authoritative amount delta"},
                adjustment_amount=delta, amount_usd=delta_usd, occurred_at=occurred_at,
                confidence_state=confidence_state,
            ))
        ids.append(self.append_reconciliation(
            event_id, state="RECONCILED", evidence=evidence, occurred_at=occurred_at,
            confidence_state=confidence_state,
        ))
        return ids

    def attest_provider_coverage(
        self, *, provider: str, month_utc: str, state: str, expected: bool = True,
        evidence: dict[str, Any] | None = None,
        expected_evidence_classes: tuple[str, ...] | list[str] = (),
        actual_evidence_classes: tuple[str, ...] | list[str] = (),
        observed_at: datetime | None = None,
        provenance: dict[str, Any] | None = None,
        unresolved_blockers: tuple[str, ...] | list[str] = (),
        applicable_activity_detected: bool | None = None,
    ) -> int:
        """Append an explicit provider-period completeness declaration."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            event_id = record_provider_coverage_on_connection(
                self.conn, provider=provider, month_utc=month_utc, state=state,
                expected=expected, evidence=evidence,
                expected_evidence_classes=expected_evidence_classes,
                actual_evidence_classes=actual_evidence_classes,
                observed_at=observed_at, provenance=provenance,
                unresolved_blockers=unresolved_blockers,
                applicable_activity_detected=applicable_activity_detected,
            )
            self.conn.commit()
            return event_id
        except Exception:
            self.conn.rollback()
            raise

    def declare_period_manifest(
        self, *, month_utc: str, providers: dict[str, tuple[str, ...] | list[str]],
        provenance: dict[str, Any],
    ) -> list[int]:
        """Append the expected source/evidence classes for a period; no source is inferred complete."""
        if not providers or not provenance:
            raise ValueError("period manifest requires providers and provenance")
        ids = []
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for provider, classes in sorted(providers.items()):
                existing = self.conn.execute(
                    "SELECT expected_classes_json FROM economic_provider_coverage "
                    "WHERE provider=? AND month_utc=? AND expected=1 ORDER BY id DESC LIMIT 1",
                    (provider, month_utc),
                ).fetchone()
                if existing is not None:
                    try:
                        if sorted(json.loads(existing[0])) == sorted(classes):
                            continue
                    except (TypeError, ValueError):
                        pass
                ids.append(record_provider_coverage_on_connection(
                    self.conn, provider=provider, month_utc=month_utc, state="EXPECTED",
                    expected=True, expected_evidence_classes=classes,
                    provenance=provenance, evidence={"kind": "expected-provider-manifest"},
                    unresolved_blockers=["provider has not attested this period"],
                ))
            self.conn.commit()
            return ids
        except Exception:
            self.conn.rollback()
            raise

    def ensure_current_period_manifest(self, *, provenance: dict[str, Any]) -> list[int]:
        """Idempotently declare core economic evidence sources for the current UTC month."""
        month_utc = datetime.now(UTC).strftime("%Y-%m")
        return self.declare_period_manifest(
            month_utc=month_utc, providers=DEFAULT_PROVIDER_PERIOD_MANIFEST,
            provenance={**provenance, "declared_at": datetime.now(UTC).isoformat()},
        )

    def append_counterfactual(self, scenario: EconomicCounterfactual) -> int:
        """Append a comparison input; it never creates a financial event or permission."""
        scenario.validate()
        prior = self.conn.execute(
            "SELECT recorded_at,outcome_basis FROM economic_counterfactuals "
            "WHERE scenario_id=? ORDER BY id LIMIT 1", (scenario.scenario_id,),
        ).fetchone()
        if scenario.outcome_basis == "hypothesis":
            if scenario.amount is not None or scenario.amount_usd is not None:
                raise ValueError("predeclared counterfactual hypotheses cannot contain an outcome")
        elif prior is None or prior["outcome_basis"] != "hypothesis":
            raise ValueError("counterfactual outcome requires a predeclared hypothesis")
        elif scenario.recorded_at.astimezone(UTC) < datetime.fromisoformat(prior["recorded_at"]):
            raise ValueError("counterfactual result cannot predate its hypothesis")
        cursor = self.conn.execute(
            "INSERT INTO economic_counterfactuals "
            "(recorded_at,scenario_id,mission_id,activity_id,strategy_id,scenario_type,"
            "outcome_basis,currency,amount,amount_usd,evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (scenario.recorded_at.astimezone(UTC).isoformat(), scenario.scenario_id,
             scenario.mission_id, scenario.activity_id, scenario.strategy_id,
             scenario.scenario_type, scenario.outcome_basis, scenario.currency.upper(),
             None if scenario.amount is None else str(scenario.amount),
             None if scenario.amount_usd is None else str(scenario.amount_usd),
             json.dumps(scenario.evidence, sort_keys=True, default=str)),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def predeclare_mission_counterfactuals(
        self, *, mission_id: str, declared_at: datetime, activity_id: str | None = None,
        strategy_id: str | None = None, evidence: dict[str, Any],
    ) -> list[int]:
        """Lock mission alternatives before result evidence exists; later outcomes append to these IDs."""
        if declared_at.tzinfo is None or not evidence:
            raise ValueError("counterfactual predeclaration requires aware time and provenance")
        ids = []
        for scenario_type in ("actual_action", "do_nothing", "baseline_strategy", "alternative_allocation"):
            ids.append(self.append_counterfactual(EconomicCounterfactual(
                scenario_id=f"{mission_id}:{scenario_type}", mission_id=mission_id,
                activity_id=activity_id, strategy_id=strategy_id, scenario_type=scenario_type,
                outcome_basis="hypothesis", recorded_at=declared_at,
                evidence={**evidence, "predeclared": True, "result_observed": False},
            )))
        return ids

    def attest_operating_reserve(
        self, *, month_utc: str, observed_at: datetime, designated_accounts: list[str],
        balances: list[dict[str, Any]], liabilities_usd: Decimal | None,
        reservations_usd: Decimal | None, liabilities_complete: bool,
        reservations_complete: bool, provenance: dict[str, Any],
    ) -> dict[str, Any]:
        """Append a reserve snapshot only when all designated accounts are owned, fresh and valued."""
        if observed_at.tzinfo is None or not designated_accounts or not provenance:
            raise ValueError("reserve requires an observation time, designated accounts, and provenance")
        ids = sorted(set(designated_accounts))
        blockers: list[str] = []
        by_id = {str(row.get("account_id")): row for row in balances}
        if len(by_id) != len(balances) or sorted(by_id) != ids:
            blockers.append("designated account balance set is incomplete or duplicated")
        total = Decimal(0)
        for account_id in ids:
            row = by_id.get(account_id, {})
            if row.get("owned") is not True:
                blockers.append(f"ownership unverified: {account_id}")
            try:
                amount = Decimal(str(row["amount_usd"]))
                at = datetime.fromisoformat(str(row["observed_at"]))
                if not amount.is_finite() or amount < 0 or at.tzinfo is None:
                    raise ValueError
            except (KeyError, TypeError, ValueError, ArithmeticError):
                blockers.append(f"balance valuation unavailable: {account_id}")
                continue
            balance_age = (observed_at.astimezone(UTC) - at.astimezone(UTC)).total_seconds()
            if balance_age < 0 or balance_age > 600:
                blockers.append(f"stale balance: {account_id}")
            if str(row.get("currency", "")).upper() != "USD" and not {
                "decimals", "price_source", "price_timestamp", "valuation_basis", "price_usd",
            } <= set(row):
                blockers.append(f"valuation provenance incomplete: {account_id}")
            elif str(row.get("currency", "")).upper() != "USD":
                try:
                    price_at = datetime.fromisoformat(str(row["price_timestamp"]))
                    price = Decimal(str(row["price_usd"]))
                    native = Decimal(str(row["amount_native"]))
                    decimals = int(row["decimals"])
                    valuation = native / (Decimal(10) ** decimals) * price
                    quote_age = (observed_at.astimezone(UTC) - price_at.astimezone(UTC)).total_seconds()
                    if (price_at.tzinfo is None or row["valuation_basis"] != "event_time"
                            or quote_age < 0 or quote_age > 600 or not price.is_finite()
                            or price < 0 or decimals < 0 or valuation != amount):
                        raise ValueError
                except (KeyError, TypeError, ValueError, ArithmeticError):
                    blockers.append(f"event-time valuation invalid: {account_id}")
            total += amount
        for name, amount, complete in (
            ("liabilities", liabilities_usd, liabilities_complete),
            ("reservations", reservations_usd, reservations_complete),
        ):
            if not complete or amount is None or not amount.is_finite() or amount < 0:
                blockers.append(f"{name} are incomplete")
        reserve = None
        if not blockers:
            assert liabilities_usd is not None and reservations_usd is not None
            reserve = total - liabilities_usd - reservations_usd
            if reserve < 0:
                blockers.append("liabilities and reservations exceed designated balances")
                reserve = None
        self.conn.execute(
            "INSERT INTO economic_reserve_attestations "
            "(month_utc,observed_at,designated_accounts_json,balances_json,liabilities_usd,"
            "reservations_usd,reserve_usd,state,blockers_json,provenance_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (month_utc, observed_at.astimezone(UTC).isoformat(), json.dumps(ids),
             json.dumps(balances, sort_keys=True, default=str),
             None if liabilities_usd is None else str(liabilities_usd),
             None if reservations_usd is None else str(reservations_usd),
             None if reserve is None else str(reserve), "RECONCILED" if reserve is not None else "INCOMPLETE",
             json.dumps(blockers), json.dumps(provenance, sort_keys=True, default=str)),
        )
        self.conn.commit()
        return {"state": "RECONCILED" if reserve is not None else "INCOMPLETE",
                "operating_reserve_usd": None if reserve is None else str(reserve),
                "unresolved_blockers": blockers}

    @staticmethod
    def read_projection(
        path: str = "data/noema.db", *, month_utc: str | None = None,
        additional_paths: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Read canonical events only. Incomplete coverage keeps net and ratio unknown."""
        requested_paths = (path, *additional_paths)
        distinct_paths: list[Path] = []
        seen_paths: set[Path] = set()
        for candidate in requested_paths:
            candidate_path = Path(candidate)
            try:
                resolved = candidate_path.resolve()
            except OSError:
                continue
            if resolved not in seen_paths:
                seen_paths.add(resolved)
                distinct_paths.append(resolved)

        # The worker database is a replaceable replica. During startup or a
        # snapshot gap it may be absent while the console sidecar still owns
        # durable canonical evidence, so select the first readable canonical
        # ledger from either location instead of making the primary path a gate.
        required = {"provider", "currency", "amount", "reconciliation_state",
                    "value_state", "capital_class", "confidence_state",
                    "completeness_state"}
        db: Path | None = None
        for candidate in distinct_paths:
            if not candidate.is_file():
                continue
            try:
                with closing(sqlite3.connect(
                    candidate.as_uri() + "?mode=ro", uri=True, timeout=2,
                )) as probe:
                    tables = {row[0] for row in probe.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )}
                    if "economic_events" not in tables:
                        continue
                    columns = {row[1] for row in probe.execute(
                        "PRAGMA table_info(economic_events)"
                    )}
                    if required <= columns:
                        db = candidate
                        break
            except sqlite3.Error:
                continue
        if db is None:
            return _empty_event_projection("canonical ledger unavailable")
        additional_paths = tuple(
            str(candidate) for candidate in distinct_paths if candidate != db
        )
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if "economic_events" not in tables:
                return _empty_event_projection("canonical ledger unavailable")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(economic_events)")}
            if not required <= columns:
                return _empty_event_projection("canonical schema not initialized")
            params: tuple[Any, ...] = ()
            month_clause = ""
            if month_utc:
                month_clause = " AND substr(occurred_at,1,7)=?"
                params = (month_utc,)
            rows = conn.execute(
                "SELECT * FROM economic_events WHERE provider IS NOT NULL" + month_clause
                + " ORDER BY id", params,
            ).fetchall()
            coverage = conn.execute(
                "SELECT * FROM economic_provider_coverage "
                "WHERE (? IS NULL OR month_utc=?) ORDER BY id",
                (month_utc, month_utc),
            ).fetchall() if "economic_provider_coverage" in tables else []
            event_rows = list(rows)
            coverage_rows = list(coverage)
            event_hashes = {_economic_event_identity(row)[0] for row in rows}
            main_event_id_by_hash = {
                _economic_event_identity(row)[0]: int(row["id"])
                for row in rows
            }
            coverage_hashes = {
                hashlib.sha256(json.dumps(
                    {key: value for key, value in dict(row).items() if key != "id"},
                    sort_keys=True, default=str,
                ).encode()).hexdigest()
                for row in coverage
            }
            main_max_id = max((int(row["id"]) for row in rows), default=0)
            for source_index, additional_path in enumerate(additional_paths):
                additional_db = Path(additional_path)
                if (not additional_db.is_file()
                        or additional_db.resolve() == db.resolve()):
                    continue
                try:
                    with closing(sqlite3.connect(
                        additional_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2,
                    )) as additional:
                        additional.row_factory = sqlite3.Row
                        additional_tables = {row[0] for row in additional.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )}
                        if "economic_events" in additional_tables:
                            extra_rows = additional.execute(
                                "SELECT * FROM economic_events WHERE provider IS NOT NULL"
                                + month_clause + " ORDER BY id", params,
                            ).fetchall()
                            source_id_map: dict[int, int] = {}
                            for index, row in enumerate(extra_rows):
                                digest, _ = _economic_event_identity(row)
                                source_id_map[int(row["id"])] = main_event_id_by_hash.get(
                                    digest, main_max_id + 1 + source_index * 1_000_000_000 + index,
                                )
                            for index, row in enumerate(extra_rows):
                                digest, _ = _economic_event_identity(row)
                                if digest in event_hashes:
                                    continue
                                merged = dict(row)
                                merged["id"] = source_id_map[int(row["id"])]
                                related = merged.get("related_event_id")
                                if related is not None and int(related) in source_id_map:
                                    merged["related_event_id"] = source_id_map[int(related)]
                                event_rows.append(merged)
                                event_hashes.add(digest)
                        if "economic_provider_coverage" in additional_tables:
                            for index, row in enumerate(additional.execute(
                                "SELECT * FROM economic_provider_coverage "
                                "WHERE (? IS NULL OR month_utc=?) ORDER BY id",
                                (month_utc, month_utc),
                            ).fetchall()):
                                identity = {key: value for key, value in dict(row).items() if key != "id"}
                                digest = hashlib.sha256(json.dumps(
                                    identity, sort_keys=True, default=str,
                                ).encode()).hexdigest()
                                if digest in coverage_hashes:
                                    continue
                                merged = dict(row)
                                merged["id"] = main_max_id + 1 + source_index * 1_000_000_000 + index
                                coverage_rows.append(merged)
                                coverage_hashes.add(digest)
                except sqlite3.Error:
                    continue
            projection = _project_events(event_rows, coverage_rows)
            projection["operating_reserve_usd"] = None
            projection["reserve_status"] = "UNKNOWN"
            projection["reserve_blockers"] = ["No complete, fresh treasury reserve attestation exists."]
            if "economic_reserve_attestations" in tables:
                reserve_sql = "SELECT * FROM economic_reserve_attestations"
                reserve_params: tuple[Any, ...] = ()
                if month_utc:
                    reserve_sql += " WHERE month_utc=?"
                    reserve_params = (month_utc,)
                reserve_row = conn.execute(reserve_sql + " ORDER BY id DESC LIMIT 1", reserve_params).fetchone()
                if reserve_row is not None:
                    blockers = json.loads(reserve_row["blockers_json"] or "[]")
                    observed = datetime.fromisoformat(reserve_row["observed_at"])
                    fresh = (datetime.now(UTC) - observed.astimezone(UTC)).total_seconds() <= 600
                    if reserve_row["state"] == "RECONCILED" and fresh and not blockers:
                        projection["operating_reserve_usd"] = reserve_row["reserve_usd"]
                        projection["reserve_status"] = "RECONCILED"
                        projection["reserve_blockers"] = []
                    else:
                        projection["reserve_status"] = "INCOMPLETE"
                        projection["reserve_blockers"] = blockers or ["Treasury reserve snapshot is stale."]
                    projection["reserve_observed_at"] = reserve_row["observed_at"]
            if "economic_counterfactuals" in tables:
                cf_params: tuple[Any, ...] = ()
                cf_clause = ""
                if month_utc:
                    cf_clause = "WHERE substr(recorded_at,1,7)=?"
                    cf_params = (month_utc,)
                projection["counterfactual_comparisons"] = [
                    {
                        "scenario_id": row["scenario_id"], "mission_id": row["mission_id"],
                        "activity_id": row["activity_id"], "strategy_id": row["strategy_id"],
                        "scenario_type": row["scenario_type"], "outcome_basis": row["outcome_basis"],
                        "currency": row["currency"], "amount": row["amount"],
                        "amount_usd": row["amount_usd"], "recorded_at": row["recorded_at"],
                    }
                    for row in conn.execute(
                        "SELECT * FROM economic_counterfactuals WHERE id IN ("
                        "SELECT MAX(id) FROM economic_counterfactuals GROUP BY scenario_id) "
                        + ("AND " + cf_clause.removeprefix("WHERE ") if cf_clause else "")
                        + " ORDER BY id DESC LIMIT 100", cf_params,
                    ).fetchall()
                ]
            else:
                projection["counterfactual_comparisons"] = []
            return projection
        except sqlite3.Error:
            return _empty_event_projection("canonical ledger unavailable")
        finally:
            conn.close()

    def append_snapshot(self, snapshot: EconomicSnapshot) -> None:
        validate_snapshot(snapshot)
        payload = {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in asdict(snapshot).items()
        }
        self.conn.execute(
            """
            INSERT INTO economic_snapshots (created_at, snapshot_json)
            VALUES (?, ?)
            """,
            (datetime.now(UTC).isoformat(), json.dumps(payload, sort_keys=True)),
        )
        self.conn.commit()

    def append_event(
        self,
        event_type: str,
        *,
        amount_usd: Decimal | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if amount_usd is not None and not amount_usd.is_finite():
            raise ValueError("economic event amount must be finite")
        self.conn.execute(
            """
            INSERT INTO economic_events
            (created_at, event_type, amount_usd, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                datetime.now(UTC).isoformat(),
                event_type,
                None if amount_usd is None else str(amount_usd),
                json.dumps(payload or {}, sort_keys=True, default=str),
            ),
        )
        self.conn.commit()

    def latest_snapshot(self) -> EconomicSnapshot | None:
        row = self.conn.execute(
            """
            SELECT snapshot_json
            FROM economic_snapshots
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        raw = json.loads(row[0])
        snapshot = EconomicSnapshot(
            **{key: None if value is None else Decimal(str(value))
               for key, value in raw.items()}
        )
        validate_snapshot(snapshot)
        return snapshot


def _ensure_event_columns(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS economic_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "created_at TEXT NOT NULL, event_type TEXT NOT NULL, amount_usd TEXT, payload_json TEXT NOT NULL)"
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(economic_events)")}
    definitions = {
        "provider": "TEXT", "external_reference_id": "TEXT", "occurred_at": "TEXT",
        "currency": "TEXT", "amount": "TEXT", "mission_id": "TEXT", "strategy_id": "TEXT",
        "activity_id": "TEXT", "lane": "TEXT", "evidence_json": "TEXT",
        "reconciliation_state": "TEXT", "value_state": "TEXT", "capital_class": "TEXT",
        "confidence_state": "TEXT", "completeness_state": "TEXT", "related_event_id": "INTEGER",
        "owned_account_id": "TEXT", "counterparty_account_id": "TEXT",
    }
    for name, declaration in definitions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE economic_events ADD COLUMN {name} {declaration}")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS economic_event_provider_reference "
        "ON economic_events(provider,external_reference_id,event_type) "
        "WHERE provider IS NOT NULL AND external_reference_id IS NOT NULL"
    )


def ensure_economic_event_schema(conn: sqlite3.Connection) -> None:
    _ensure_event_columns(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS economic_event_activity "
        "ON economic_events(activity_id,mission_id,strategy_id)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS economic_provider_coverage ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, provider TEXT NOT NULL, "
        "month_utc TEXT NOT NULL, state TEXT NOT NULL, expected INTEGER NOT NULL, "
        "evidence_json TEXT NOT NULL, expected_classes_json TEXT NOT NULL DEFAULT '[]', "
        "actual_classes_json TEXT NOT NULL DEFAULT '[]', observed_at TEXT, "
        "provenance_json TEXT NOT NULL DEFAULT '{}', blockers_json TEXT NOT NULL DEFAULT '[]', "
        "activity_detected INTEGER)"
    )
    coverage_columns = {row[1] for row in conn.execute("PRAGMA table_info(economic_provider_coverage)")}
    for name, declaration in {
        "expected_classes_json": "TEXT NOT NULL DEFAULT '[]'",
        "actual_classes_json": "TEXT NOT NULL DEFAULT '[]'", "observed_at": "TEXT",
        "provenance_json": "TEXT NOT NULL DEFAULT '{}'", "blockers_json": "TEXT NOT NULL DEFAULT '[]'",
        "activity_detected": "INTEGER",
    }.items():
        if name not in coverage_columns:
            conn.execute(f"ALTER TABLE economic_provider_coverage ADD COLUMN {name} {declaration}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS economic_provider_coverage_period "
        "ON economic_provider_coverage(month_utc,provider,id)"
    )


def record_provider_coverage_on_connection(
    conn: sqlite3.Connection, *, provider: str, month_utc: str, state: str,
    expected: bool = True, evidence: dict[str, Any] | None = None,
    expected_evidence_classes: tuple[str, ...] | list[str] = (),
    actual_evidence_classes: tuple[str, ...] | list[str] = (),
    observed_at: datetime | None = None,
    provenance: dict[str, Any] | None = None,
    unresolved_blockers: tuple[str, ...] | list[str] = (),
    applicable_activity_detected: bool | None = None,
) -> int:
    """Append a provider-period coverage declaration/attestation."""
    if (not provider.strip() or len(month_utc) != 7 or month_utc[4] != "-"
            or not month_utc[:4].isdigit() or not month_utc[5:].isdigit()
            or not 1 <= int(month_utc[5:]) <= 12):
        raise ValueError("provider and YYYY-MM coverage period are required")
    state = "PARTIAL" if state == "INCOMPLETE" else state
    if state not in PROVIDER_COVERAGE_STATES:
        raise ValueError("unsupported provider coverage state")
    previous = conn.execute(
        "SELECT expected_classes_json FROM economic_provider_coverage "
        "WHERE provider=? AND month_utc=? ORDER BY id DESC LIMIT 1", (provider, month_utc),
    ).fetchone()
    if previous is not None and not expected_evidence_classes:
        try:
            expected_evidence_classes = json.loads(previous[0])
        except (ValueError, TypeError):
            expected_evidence_classes = ()
    expected_classes = tuple(sorted({str(value).strip() for value in expected_evidence_classes if str(value).strip()}))
    actual_classes = tuple(sorted({str(value).strip() for value in actual_evidence_classes if str(value).strip()}))
    if state == "COMPLETE" and not expected_classes:
        raise ValueError("COMPLETE coverage requires an explicit expected evidence class set")
    if state == "COMPLETE" and not (evidence or provenance):
        raise ValueError("COMPLETE coverage requires provenance")
    if state == "COMPLETE" and not set(expected_classes) <= set(actual_classes):
        raise ValueError("COMPLETE coverage must include every expected evidence class")
    if state == "NOT_APPLICABLE" and not (evidence or provenance):
        raise ValueError("NOT_APPLICABLE coverage requires an auditable reason")
    if observed_at is not None and observed_at.tzinfo is None:
        raise ValueError("provider observation timestamp must be timezone-aware")
    if applicable_activity_detected not in (True, False, None):
        raise ValueError("activity-detected evidence must be true, false, or unknown")
    observed = observed_at.astimezone(UTC).isoformat() if observed_at else datetime.now(UTC).isoformat()
    cursor = conn.execute(
        "INSERT INTO economic_provider_coverage "
        "(created_at,provider,month_utc,state,expected,evidence_json,expected_classes_json,"
        "actual_classes_json,observed_at,provenance_json,blockers_json,activity_detected) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (datetime.now(UTC).isoformat(), provider, month_utc, state, int(expected),
         json.dumps(evidence or {}, sort_keys=True, default=str),
         json.dumps(expected_classes), json.dumps(actual_classes), observed,
         json.dumps(provenance or {}, sort_keys=True, default=str),
         json.dumps(sorted({str(value) for value in unresolved_blockers})),
         None if applicable_activity_detected is None else int(applicable_activity_detected)),
    )
    return int(cursor.lastrowid)


def _event_amount_usd(event: EconomicEvent) -> Decimal | None:
    if event.amount_usd is not None:
        return event.amount_usd
    return event.amount if event.currency.upper() == "USD" else None


def _event_facts(event: EconomicEvent) -> tuple[Any, ...]:
    return (
        event.event_type, event.currency.upper(), str(event.amount),
        None if _event_amount_usd(event) is None else str(_event_amount_usd(event)),
        event.mission_id, event.strategy_id, event.activity_id, event.lane,
        event.capital_class, event.value_state,
    )


def _record_event(conn: sqlite3.Connection, event: EconomicEvent) -> dict[str, Any]:
    if event.external_reference_id:
        cursor = conn.execute(
            "SELECT * FROM economic_events WHERE provider=? AND external_reference_id=? "
            "AND event_type=?", (event.provider, event.external_reference_id, event.event_type),
        )
        fetched = cursor.fetchone()
        row = (dict(fetched) if isinstance(fetched, sqlite3.Row)
               else dict(zip((column[0] for column in cursor.description), fetched))
               if fetched is not None else None)
        if row is not None:
            current_facts = (
                row["event_type"], row["currency"], row["amount"], row["amount_usd"],
                row["mission_id"], row["strategy_id"], row["activity_id"], row["lane"],
                row["capital_class"], row["value_state"],
            )
            if current_facts == _event_facts(event):
                if (row["reconciliation_state"] == event.reconciliation_state
                        and row["confidence_state"] == event.confidence_state
                        and row["completeness_state"] == event.completeness_state):
                    return {"event_id": int(row["id"]), "status": "duplicate"}
                state_ref = (
                    f"state:{row['id']}:{event.reconciliation_state}:"
                    f"{event.confidence_state}:{event.completeness_state}"
                )
                existing_state = conn.execute(
                    "SELECT id FROM economic_events WHERE provider=? AND external_reference_id=? "
                    "AND event_type='reconciliation_state_update'",
                    (event.provider, state_ref),
                ).fetchone()
                if existing_state is not None:
                    return {"event_id": int(existing_state[0]), "status": "duplicate"}
                state_event = EconomicEvent(
                    provider=event.provider,
                    event_type="reconciliation_state_update",
                    occurred_at=event.occurred_at,
                    currency=event.currency,
                    amount=Decimal(0),
                    reconciliation_state=event.reconciliation_state,
                    value_state="unknown", capital_class="none",
                    confidence_state=event.confidence_state,
                    completeness_state=event.completeness_state,
                    external_reference_id=state_ref,
                    related_event_id=int(row["id"]),
                    evidence={"source_evidence": event.evidence},
                )
                return {"event_id": _insert_event(conn, state_event), "status": "state_update"}
            incoming = {
                "event_type": event.event_type, "currency": event.currency,
                "amount": str(event.amount), "amount_usd": (None if _event_amount_usd(event) is None
                                                              else str(_event_amount_usd(event))),
                "mission_id": event.mission_id, "strategy_id": event.strategy_id,
                "activity_id": event.activity_id, "lane": event.lane,
                "capital_class": event.capital_class, "value_state": event.value_state,
                "source_evidence": event.evidence,
            }
            digest = hashlib.sha256(json.dumps(incoming, sort_keys=True, default=str).encode()).hexdigest()[:24]
            conflict = EconomicEvent(
                provider=event.provider, event_type="reconciliation_conflict",
                occurred_at=event.occurred_at, currency=event.currency, amount=Decimal(0),
                reconciliation_state="DISPUTED", value_state="unknown", capital_class="none",
                confidence_state="derived", completeness_state="incomplete",
                external_reference_id=f"conflict:{row['id']}:{digest}",
                related_event_id=int(row["id"]), evidence=incoming,
            )
            return {"event_id": _insert_event(conn, conflict), "status": "disputed"}
    return {"event_id": _insert_event(conn, event), "status": "inserted"}


def record_event_on_connection(conn: sqlite3.Connection, event: EconomicEvent) -> dict[str, Any]:
    """Record inside the caller's SQLite transaction after schema initialization."""
    event.validate()
    return _record_event(conn, event)


def _insert_event(conn: sqlite3.Connection, event: EconomicEvent) -> int:
    amount_usd = _event_amount_usd(event)
    cursor = conn.execute(
        "INSERT INTO economic_events (created_at,event_type,amount_usd,payload_json,provider,"
        "external_reference_id,occurred_at,currency,amount,mission_id,strategy_id,activity_id,lane,"
        "evidence_json,reconciliation_state,value_state,capital_class,confidence_state,"
        "completeness_state,related_event_id,owned_account_id,counterparty_account_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (datetime.now(UTC).isoformat(), event.event_type,
         None if amount_usd is None else str(amount_usd),
         json.dumps(event.evidence, sort_keys=True, default=str), event.provider,
         event.external_reference_id, event.occurred_at.astimezone(UTC).isoformat(),
         event.currency.upper(), str(event.amount), event.mission_id, event.strategy_id,
         event.activity_id, event.lane, json.dumps(event.evidence, sort_keys=True, default=str),
         event.reconciliation_state, event.value_state, event.capital_class,
         event.confidence_state, event.completeness_state, event.related_event_id,
         event.owned_account_id, event.counterparty_account_id),
    )
    return int(cursor.lastrowid)


def _empty_event_projection(reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable", "event_count": 0, "verified_realized_revenue_usd": None,
        "reconciled_revenue_subtotal_usd": None, "reconciled_cost_subtotal_usd": None,
        "verified_realized_trading_pnl_usd": None, "reconciled_trading_pnl_subtotal_usd": None,
        "verified_attributable_costs_usd": None,
        "unreconciled_amount_usd": None, "unreconciled_event_count": 0,
        "owner_funded_amount_usd": None, "owner_funded_subtotal_usd": None,
        "operating_reserve_usd": None,
        "net_verified_contribution_usd": None, "self_funding_ratio": None,
        "cost_per_useful_experiment_usd": None, "contribution_by_mission": [],
        "contribution_by_strategy": [], "contribution_by_lane": [],
        "contribution_by_provider": [], "top_cost_drivers": [],
        "model_cost_vs_measured_benefit": [], "unknowns": [reason],
        "counterfactual_comparisons": [],
    }


def _economic_event_identity(row: Any) -> tuple[str, dict[str, Any]]:
    """Build a polling-stable event key, preferring the provider's durable ID."""
    values = dict(row)
    if values.get("provider") and values.get("external_reference_id"):
        identity = {
            "provider": values["provider"],
            "external_reference_id": values["external_reference_id"],
            "event_type": values.get("event_type"),
        }
    else:
        identity = {
            key: value for key, value in values.items()
            if key not in {"id", "created_at"}
        }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()
    return digest, identity


def _project_events(rows: list[sqlite3.Row], coverage_rows: list[sqlite3.Row] | None = None) -> dict[str, Any]:
    states: dict[int, str] = {}
    confidence_overrides: dict[int, str] = {}
    completeness_overrides: dict[int, str] = {}
    disputed: set[int] = set()
    for row in rows:
        related = row["related_event_id"]
        if related is None:
            continue
        if row["reconciliation_state"] == "DISPUTED":
            disputed.add(int(related))
        elif row["event_type"] == "reconciliation_state_update":
            states[int(related)] = str(row["reconciliation_state"])
            confidence_overrides[int(related)] = str(row["confidence_state"])
            completeness_overrides[int(related)] = str(row["completeness_state"])

    totals = {key: Decimal(0) for key in (
        "revenue", "trading_pnl", "costs", "owner_capital", "unreconciled",
        "reserved", "unclassified_cash",
    )}
    known: set[str] = set()
    unresolved_capital: set[str] = set()
    unresolved_count = len(disputed)
    reservation_count = 0
    groups: dict[str, dict[str, dict[str, Decimal | int]]] = {
        name: {} for name in ("mission", "strategy", "lane", "provider")
    }
    cost_drivers: dict[tuple[str, str], Decimal] = {}
    cost_driver_verified: dict[tuple[str, str], bool] = {}
    cognition: dict[str, dict[str, Decimal | None]] = {}
    for row in rows:
        row_id = int(row["id"])
        is_adjustment = row["event_type"] == "reconciliation_adjustment"
        if (row["event_type"].startswith("reconciliation_") and not is_adjustment
                or row["event_type"] == "reconciliation_state_update"):
            continue
        if row_id in disputed or (row["related_event_id"] is not None and not is_adjustment):
            continue
        status = (row["reconciliation_state"] if is_adjustment
                  else states.get(row_id, row["reconciliation_state"]))
        usd = Decimal(str(row["amount_usd"])) if row["amount_usd"] is not None else None
        capital = row["capital_class"]
        if row["value_state"] == "reservation":
            reservation_count += 1
            if usd is not None:
                totals["reserved"] += abs(usd)
            continue
        realized = row["value_state"] == "realized"
        confidence = confidence_overrides.get(row_id, row["confidence_state"])
        completeness = completeness_overrides.get(row_id, row["completeness_state"])
        verified = (
            status == "RECONCILED" and completeness == "complete"
            and confidence in {"provider_confirmed", "derived"}
            and realized and usd is not None
        )
        if capital in {"revenue", "trading_pnl", "cost", "owner_capital"}:
            if verified:
                key = {"revenue": "revenue", "trading_pnl": "trading_pnl",
                       "cost": "costs", "owner_capital": "owner_capital"}[capital]
                totals[key] += usd
                known.add(key)
            else:
                unresolved_count += 1
                unresolved_capital.add(str(capital))
                if usd is not None and capital != "owner_capital":
                    totals["unreconciled"] += abs(usd)
        elif capital == "unclassified_cash":
            unresolved_count += 1
            unresolved_capital.add("unclassified_cash")
            totals["unclassified_cash"] += abs(usd) if usd is not None else Decimal(0)
            totals["unreconciled"] += abs(usd) if usd is not None else Decimal(0)
        if capital == "cost":
            driver = (str(row["provider"] or "unknown"), str(row["lane"] or "unknown"))
            cost_drivers[driver] = cost_drivers.get(driver, Decimal(0)) + (
                abs(usd) if usd is not None else Decimal(0)
            )
            cost_driver_verified[driver] = cost_driver_verified.get(driver, True) and verified
        impact = Decimal(0)
        if verified and capital in {"revenue", "trading_pnl"}:
            impact = usd
        elif verified and capital == "cost":
            impact = -usd
        for group_name, field_name in (
            ("mission", "mission_id"), ("strategy", "strategy_id"),
            ("lane", "lane"), ("provider", "provider"),
        ):
            group_key = str(row[field_name] or "unattributed")
            target = groups[group_name].setdefault(group_key, {
                "verified_contribution_usd": Decimal(0), "event_count": 0,
                "unreconciled_event_count": 0,
            })
            target["event_count"] += 1
            if verified and capital in {"revenue", "trading_pnl", "cost"}:
                target["verified_contribution_usd"] += impact
            elif capital in {"revenue", "trading_pnl", "cost"}:
                target["unreconciled_event_count"] += 1
        if row["provider"] in {"openai", "groq", "cloudflare", "docker_model_runner", "cognition"}:
            activity = str(row["activity_id"] or row["mission_id"] or "unattributed")
            cognition.setdefault(activity, {"estimated_or_verified_cost_usd": Decimal(0),
                                            "measured_benefit_usd": None})
            if capital == "cost" and usd is not None:
                cognition[activity]["estimated_or_verified_cost_usd"] += abs(usd)

    latest_coverage: dict[tuple[str, str], sqlite3.Row] = {}
    for coverage in coverage_rows or []:
        latest_coverage[(str(coverage["provider"]), str(coverage["month_utc"]))] = coverage
    expected_coverage = [item for item in latest_coverage.values() if item["expected"]]
    canonical_counts: dict[tuple[str, str], int] = {}
    reconciled_by_provider: dict[tuple[str, str], Decimal] = {}
    estimated_by_provider: dict[tuple[str, str], Decimal] = {}
    for row in rows:
        provider = str(row["provider"] or "unknown")
        month = str(row["occurred_at"] or "")[:7]
        key = (provider, month)
        event_type = str(row["event_type"])
        if not event_type.startswith("reconciliation_"):
            canonical_counts[key] = canonical_counts.get(key, 0) + 1
        if event_type == "reconciliation_state_update" or (
            row["related_event_id"] is not None and event_type != "reconciliation_adjustment"
        ):
            continue
        if row["capital_class"] not in {"revenue", "trading_pnl", "cost", "owner_capital", "unclassified_cash"}:
            continue
        usd = Decimal(str(row["amount_usd"])) if row["amount_usd"] is not None else None
        row_id = int(row["id"])
        state = row["reconciliation_state"] if event_type == "reconciliation_adjustment" else states.get(row_id, row["reconciliation_state"])
        confidence = confidence_overrides.get(row_id, row["confidence_state"])
        completeness = completeness_overrides.get(row_id, row["completeness_state"])
        verified = (state == "RECONCILED" and completeness == "complete"
                    and confidence in {"provider_confirmed", "derived"}
                    and row["value_state"] == "realized" and usd is not None)
        if verified and row["capital_class"] in {"revenue", "trading_pnl", "cost"}:
            contribution = -usd if row["capital_class"] == "cost" else usd
            reconciled_by_provider[key] = reconciled_by_provider.get(key, Decimal(0)) + contribution
        elif (usd is not None and row["value_state"] not in {"paper", "reservation"}
              and (row["reconciliation_state"] == "ESTIMATED"
                   or row["confidence_state"] == "estimated")):
            estimated_by_provider[key] = estimated_by_provider.get(key, Decimal(0)) + abs(usd)
    provider_coverage: list[dict[str, Any]] = []
    blocked_providers: list[str] = []
    for item in sorted(expected_coverage, key=lambda row: (str(row["month_utc"]), str(row["provider"]))):
        expected_classes = _json_list(item, "expected_classes_json")
        actual_classes = _json_list(item, "actual_classes_json")
        missing_classes = sorted(set(expected_classes) - set(actual_classes))
        blockers = _json_list(item, "blockers_json")
        state = "PARTIAL" if item["state"] == "INCOMPLETE" else str(item["state"])
        if state == "COMPLETE" and missing_classes:
            blockers = sorted(set(blockers + [f"missing evidence class: {value}" for value in missing_classes]))
            state = "PARTIAL"
        if state not in {"COMPLETE", "NOT_APPLICABLE"}:
            blocked_providers.append(str(item["provider"]))
        if state == "NOT_APPLICABLE" and not _json_object(item, "evidence_json") and not _json_object(item, "provenance_json"):
            blockers = sorted(set(blockers + ["NOT_APPLICABLE lacks provenance"]))
            blocked_providers.append(str(item["provider"]))
        provider = str(item["provider"])
        month = str(item["month_utc"])
        provider_keys = ([key for key in canonical_counts
                          if key[1] == month and (key[0] == provider
                             or (provider == "wallets" and key[0].startswith("wallet:")))])
        event_count = sum(canonical_counts[key] for key in provider_keys)
        activity_value = _row_value(item, "activity_detected")
        actual_activity_count = sum(
            1 for row in rows
            if str(row["occurred_at"] or "")[:7] == month
            and (str(row["provider"] or "unknown") == provider
                 or (provider == "wallets" and str(row["provider"] or "").startswith("wallet:")))
            and not str(row["event_type"]).startswith("reconciliation_")
            and row["reconciliation_state"] != "ESTIMATED"
            and row["value_state"] != "reservation"
        )
        if activity_value is not None:
            activity_detected = bool(activity_value)
        else:
            activity_detected = True if actual_activity_count else None
        if state == "NOT_APPLICABLE" and activity_detected is None:
            activity_detected = False
        reconciled_amount = sum((amount for (key_provider, key_month), amount in reconciled_by_provider.items()
                                 if key_month == month and (key_provider == provider
                                     or (provider == "wallets" and key_provider.startswith("wallet:")))),
                                Decimal(0)) if any(key_month == month and (key_provider == provider
                                    or (provider == "wallets" and key_provider.startswith("wallet:")))
                                    for key_provider, key_month in reconciled_by_provider) else None
        if reconciled_amount is None and state in {"COMPLETE", "NOT_APPLICABLE"} and event_count == 0:
            reconciled_amount = Decimal(0)
        estimated_amount = sum((amount for (key_provider, key_month), amount in estimated_by_provider.items()
                                if key_month == month and (key_provider == provider
                                    or (provider == "wallets" and key_provider.startswith("wallet:")))),
                               Decimal(0)) if any(key_month == month and (key_provider == provider
                                   or (provider == "wallets" and key_provider.startswith("wallet:")))
                                   for key_provider, key_month in estimated_by_provider) else None
        provider_coverage.append({
            "provider": provider, "month_utc": month,
            "state": state, "expected": True, "expected_evidence_classes": expected_classes,
            "actual_evidence_classes": actual_classes, "observed_at": _row_value(item, "observed_at"),
            "provenance": _json_object(item, "provenance_json"),
            "latest_evidence": _json_object(item, "evidence_json"),
            "applicable_activity_detected": activity_detected,
            "canonical_event_count": event_count,
            "reconciled_amount_usd": None if reconciled_amount is None else str(reconciled_amount),
            "estimated_amount_usd": None if estimated_amount is None else str(estimated_amount),
            "unresolved_blockers": blockers,
        })
    coverage_complete = bool(expected_coverage) and not blocked_providers and all(
        item["state"] in {"COMPLETE", "NOT_APPLICABLE"} for item in provider_coverage
    )
    revenue_complete = coverage_complete and unresolved_count == 0 and "revenue" not in unresolved_capital
    costs_complete = coverage_complete and unresolved_count == 0 and "cost" not in unresolved_capital
    financial_events_complete = coverage_complete and unresolved_count == 0
    net = (totals["revenue"] + totals["trading_pnl"] - totals["costs"]
           if financial_events_complete else None)
    ratio = (totals["revenue"] / totals["costs"] if coverage_complete and totals["costs"] > 0
             and costs_complete and revenue_complete and financial_events_complete else None)
    def grouped(name: str) -> list[dict[str, Any]]:
        return [
            {"key": key, "reconciled_event_contribution_subtotal_usd": str(value["verified_contribution_usd"]),
             "event_count": value["event_count"],
             "unreconciled_event_count": value["unreconciled_event_count"]}
            for key, value in sorted(groups[name].items())
        ]
    unknowns = []
    if not revenue_complete:
        unknowns.append("No complete provider-reconciled realized revenue set is available.")
    if not costs_complete:
        unknowns.append("Attributable operating costs are incomplete or unreconciled.")
    if not coverage_complete:
        unknowns.append("Provider-period coverage is missing or incomplete; net contribution and self-funding ratio remain unknown.")
        if blocked_providers:
            unknowns.append("Blocking providers: " + ", ".join(sorted(set(blocked_providers))) + ".")
    return {
        "status": "incomplete" if unknowns else "reconciled",
        "event_count": len(rows),
        "verified_realized_revenue_usd": str(totals["revenue"]) if revenue_complete else None,
        "reconciled_revenue_subtotal_usd": str(totals["revenue"]) if "revenue" in known else None,
        "verified_realized_trading_pnl_usd": str(totals["trading_pnl"]) if financial_events_complete else None,
        "reconciled_trading_pnl_subtotal_usd": str(totals["trading_pnl"]) if "trading_pnl" in known else None,
        "verified_attributable_costs_usd": str(totals["costs"]) if costs_complete else None,
        "reconciled_cost_subtotal_usd": str(totals["costs"]) if "costs" in known else None,
        "unreconciled_amount_usd": str(totals["unreconciled"]),
        "unreconciled_event_count": unresolved_count,
        "reserved_amount_usd": str(totals["reserved"]),
        "reservation_count": reservation_count,
        "unclassified_cash_subtotal_usd": str(totals["unclassified_cash"]),
        "owner_funded_amount_usd": str(totals["owner_capital"])
        if coverage_complete and "owner_capital" not in unresolved_capital else None,
        "owner_funded_subtotal_usd": str(totals["owner_capital"]) if "owner_capital" in known else None,
        "operating_reserve_usd": None,
        "net_verified_contribution_usd": None if net is None else str(net),
        "self_funding_ratio": None if ratio is None else str(ratio),
        "coverage_complete": coverage_complete,
        "period_closure": "CLOSED" if coverage_complete and financial_events_complete else "OPEN",
        "coverage_status": "COMPLETE" if coverage_complete else ("PARTIAL" if expected_coverage else "UNKNOWN"),
        "blocking_providers": sorted(set(blocked_providers)),
        "financial_events_complete": financial_events_complete,
        "provider_coverage": provider_coverage,
        "cost_per_useful_experiment_usd": None,
        "contribution_by_mission": grouped("mission"),
        "contribution_by_strategy": grouped("strategy"),
        "contribution_by_lane": grouped("lane"),
        "contribution_by_provider": grouped("provider"),
        "top_cost_drivers": [
            {"provider": provider, "lane": lane, "known_amount_usd": str(amount),
             "all_events_reconciled": cost_driver_verified.get((provider, lane), False)}
            for (provider, lane), amount in sorted(cost_drivers.items(), key=lambda item: item[1], reverse=True)[:8]
        ],
        "model_cost_vs_measured_benefit": [
            {"activity_id": activity, "cost_usd": (
                str(value["estimated_or_verified_cost_usd"])
                if value["estimated_or_verified_cost_usd"] else None),
             "measured_benefit_usd": None}
            for activity, value in sorted(cognition.items())
        ],
        "unknowns": unknowns or ["Economic coverage is incomplete."],
    }


def _json_list(row: sqlite3.Row, key: str) -> list[str]:
    try:
        value = json.loads(row[key])
    except (KeyError, TypeError, ValueError, IndexError):
        return []
    return sorted({str(item) for item in value}) if isinstance(value, list) else []


def _row_value(row: sqlite3.Row, key: str) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _json_object(row: sqlite3.Row, key: str) -> dict[str, Any]:
    try:
        value = json.loads(row[key])
    except (KeyError, TypeError, ValueError, IndexError):
        return {}
    return value if isinstance(value, dict) else {}
