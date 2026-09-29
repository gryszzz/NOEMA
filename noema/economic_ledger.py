from __future__ import annotations

import hashlib
import json
import sqlite3
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

    def append_reconciliation(
        self, event_id: int, *, state: str, evidence: dict[str, Any],
        adjustment_amount: Decimal | None = None, amount_usd: Decimal | None = None,
        occurred_at: datetime | None = None,
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
            confidence_state="derived", completeness_state="complete",
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

    def attest_provider_coverage(
        self, *, provider: str, month_utc: str, state: str, expected: bool = True,
        evidence: dict[str, Any] | None = None,
    ) -> int:
        """Append an explicit provider-period completeness declaration."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            event_id = record_provider_coverage_on_connection(
                self.conn, provider=provider, month_utc=month_utc, state=state,
                expected=expected, evidence=evidence,
            )
            self.conn.commit()
            return event_id
        except Exception:
            self.conn.rollback()
            raise

    @staticmethod
    def read_projection(path: str = "data/noema.db", *, month_utc: str | None = None) -> dict[str, Any]:
        """Read canonical events only. Incomplete coverage keeps net and ratio unknown."""
        db = Path(path)
        if not db.exists():
            return _empty_event_projection("canonical ledger unavailable")
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if "economic_events" not in tables:
                return _empty_event_projection("canonical ledger unavailable")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(economic_events)")}
            required = {"provider", "currency", "amount", "reconciliation_state",
                        "value_state", "capital_class", "confidence_state",
                        "completeness_state"}
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
                "SELECT provider,month_utc,state,expected FROM economic_provider_coverage "
                "WHERE (? IS NULL OR month_utc=?) ORDER BY id",
                (month_utc, month_utc),
            ).fetchall() if "economic_provider_coverage" in tables else []
            return _project_events(rows, coverage)
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
        "evidence_json TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS economic_provider_coverage_period "
        "ON economic_provider_coverage(month_utc,provider,id)"
    )


def record_provider_coverage_on_connection(
    conn: sqlite3.Connection, *, provider: str, month_utc: str, state: str,
    expected: bool = True, evidence: dict[str, Any] | None = None,
) -> int:
    """Append a provider-period coverage declaration/attestation."""
    if (not provider.strip() or len(month_utc) != 7 or month_utc[4] != "-"
            or not month_utc[:4].isdigit() or not month_utc[5:].isdigit()
            or not 1 <= int(month_utc[5:]) <= 12):
        raise ValueError("provider and YYYY-MM coverage period are required")
    if state not in {"EXPECTED", "COMPLETE", "INCOMPLETE", "DISPUTED"}:
        raise ValueError("unsupported provider coverage state")
    cursor = conn.execute(
        "INSERT INTO economic_provider_coverage "
        "(created_at,provider,month_utc,state,expected,evidence_json) VALUES (?,?,?,?,?,?)",
        (datetime.now(UTC).isoformat(), provider, month_utc, state, int(expected),
         json.dumps(evidence or {}, sort_keys=True, default=str)),
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
    }


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
    coverage_complete = bool(expected_coverage) and all(
        item["state"] == "COMPLETE" for item in expected_coverage
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
        "financial_events_complete": financial_events_complete,
        "provider_coverage": [
            {"provider": provider, "month_utc": month, "state": row["state"],
             "expected": bool(row["expected"])}
            for (provider, month), row in sorted(latest_coverage.items())
        ],
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
