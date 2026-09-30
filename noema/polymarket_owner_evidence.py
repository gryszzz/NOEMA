"""Owner-controlled import of Polymarket US statement/activity exports.

The command is deliberately local-only. The owner's original export remains at its
source path; NOEMA records its hash/reference plus a conservative allowlisted
projection. Unknown columns are named but their values remain in the source file.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .economic_ledger import (
    DEFAULT_PROVIDER_PERIOD_MANIFEST,
    EconomicEvent,
    EconomicLedger,
    record_event_on_connection,
    record_provider_coverage_on_connection,
)

_ALIASES = {
    "activity_id": {"activity_id", "activityid", "transaction_id", "transactionid", "fill_id", "fillid", "order_id", "orderid", "id"},
    "type": {"type", "activity_type", "activitytype", "transaction_type", "transactiontype", "event_type", "eventtype"},
    "status": {"status", "state", "settlement_status", "settlementstatus", "transaction_status"},
    "timestamp": {"timestamp", "created_at", "createdat", "event_time", "eventtime", "occurred_at", "occurredat", "date", "time"},
    "amount": {"amount", "amount_usd", "amountusd", "cash_amount", "cashamount", "value", "cash_value", "cashvalue"},
    "fee": {"fee", "fees", "fee_amount", "feeamount", "fee_usd", "feeusd", "fees_usd", "feesusd", "commission"},
    "currency": {"currency", "asset", "symbol", "denomination"},
    "market": {"market_id", "marketid", "market_slug", "marketslug", "ticker", "contract_id", "contractid"},
    "side": {"side", "outcome", "position_side"},
    "finality": {"final", "is_final", "isfinal", "settled", "is_settled", "issettled"},
    "period_start": {"period_start", "periodstart", "start_date", "startdate", "from"},
    "period_end": {"period_end", "periodend", "end_date", "enddate", "to"},
    "origin": {"economic_origin", "origin", "initiated_by", "initiatedby", "actor", "account_actor"},
}
_FINAL_WORDS = {"complete", "completed", "confirmed", "final", "settled", "success", "successful", "resolved"}
_ACTUAL_CLASSES = set(DEFAULT_PROVIDER_PERIOD_MANIFEST["polymarket_us"])


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _field(row: dict[str, Any], name: str) -> Any:
    aliases = _ALIASES[name]
    for key, value in row.items():
        if _key(key) in aliases and value not in (None, ""):
            return value
    return None


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    raw = str(value).strip()
    if len(raw) == 10:
        try:
            return datetime.combine(date.fromisoformat(raw), time.min, tzinfo=UTC)
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    # A naive timestamp is deliberately unknown because silently guessing the
    # venue/account timezone is unsafe.
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    raw = str(value).strip().replace(",", "").replace("$", "")
    if raw.startswith("(") and raw.endswith(")"):
        raw = "-" + raw[1:-1]
    try:
        result = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.casefold() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    if path.suffix.casefold() in {".json", ".jsonl", ".ndjson"}:
        raw = path.read_text(encoding="utf-8")
        if path.suffix.casefold() in {".jsonl", ".ndjson"}:
            return [dict(json.loads(line)) for line in raw.splitlines() if line.strip()]
        value = json.loads(raw)
        if isinstance(value, list):
            records = value
        elif isinstance(value, dict):
            records = next((value[key] for key in ("activities", "transactions", "data", "records", "results")
                            if isinstance(value.get(key), list)), None)
            if records is None:
                raise ValueError("JSON export must contain an activities/transactions/data/records array")
        else:
            raise ValueError("unsupported JSON export shape")
        return [dict(row) for row in records if isinstance(row, dict)]
    raise ValueError("supported owner exports are CSV, JSON, JSONL, or NDJSON")


def inspect_polymarket_owner_export(input_path: str) -> dict[str, Any]:
    """Inspect schema and coverage hints without opening or changing the NOEMA DB."""
    path = Path(input_path).expanduser().resolve(strict=True)
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError("owner evidence must be a non-empty regular file")
    if path.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("owner export exceeds the 50 MiB safety limit")
    data = path.read_bytes()
    records = _rows(path)
    columns = sorted({str(name) for row in records for name in row})
    mapped = {
        semantic: sorted(name for name in columns if _key(name) in aliases)
        for semantic, aliases in _ALIASES.items()
    }
    classifications: dict[str, int] = {}
    months: dict[str, int] = {}
    id_rows = fee_rows = explicit_final_rows = 0
    for row in records:
        kind = _classification(row) or "unknown"
        classifications[kind] = classifications.get(kind, 0) + 1
        at = _parse_time(_field(row, "timestamp"))
        if at is not None:
            month = at.strftime("%Y-%m")
            months[month] = months.get(month, 0) + 1
        id_rows += int(_field(row, "activity_id") is not None)
        fee_rows += int(_field(row, "fee") is not None)
        explicit_final_rows += int(_is_final(row))
    return {
        "status": "inspected_no_database_changes", "filename": path.name,
        "source_sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data),
        "format": path.suffix.casefold(), "row_count": len(records), "columns": columns,
        "mapped_columns": mapped, "classified_rows": classifications,
        "rows_with_activity_id": id_rows, "rows_with_fee_field": fee_rows,
        "rows_with_explicit_final_status": explicit_final_rows,
        "parseable_rows_by_utc_month": months,
        "raw_values_returned": False,
    }


def _classification(row: dict[str, Any]) -> str | None:
    kind = str(_field(row, "type") or "").casefold()
    if "trade" in kind or "fill" in kind:
        return "trade"
    if any(term in kind for term in ("settlement", "resolution", "redeem")):
        return "settlement"
    if "refund" in kind or "void" in kind or "correction" in kind:
        return "refund_void"
    if "deposit" in kind or "withdraw" in kind or "transfer" in kind or "balance" in kind:
        return "balance"
    if "fee" in kind or "commission" in kind:
        return "fee"
    return None


def _is_final(row: dict[str, Any]) -> bool:
    status = str(_field(row, "status") or "").strip().casefold()
    flag = str(_field(row, "finality") or "").strip().casefold()
    return status in (_FINAL_WORDS | {"finalized"}) or flag in {"true", "1", "yes", "y"}


def _origin(row: dict[str, Any]) -> str:
    value = str(_field(row, "origin") or "").strip().casefold()
    if value in {"owner", "manual", "user", "owner_manual"}:
        return "owner"
    if value in {"noema", "agent", "automated", "bot"}:
        return "noema"
    if value in {"external", "provider", "venue", "third_party"}:
        return "external"
    return "unknown"


def _safe_row_facts(row: dict[str, Any]) -> dict[str, Any]:
    known_names = {_key(alias) for aliases in _ALIASES.values() for alias in aliases}
    return {
        "type": None if _field(row, "type") is None else str(_field(row, "type"))[:120],
        "status": None if _field(row, "status") is None else str(_field(row, "status"))[:80],
        "timestamp": None if _field(row, "timestamp") is None else str(_field(row, "timestamp"))[:80],
        "activity_id_present": _field(row, "activity_id") is not None,
        "market_reference_present": _field(row, "market") is not None,
        "currency": None if _field(row, "currency") is None else str(_field(row, "currency"))[:20],
        "economic_origin": _origin(row),
        "unknown_fields": sorted(str(name)[:100] for name in row if _key(name) not in known_names),
        "finality_proven": _is_final(row),
    }


def ingest_polymarket_owner_export(
    input_path: str, *, db_path: str = "data/noema.db", source_reference: str | None = None,
    month_utc: str = "2026-09", now: datetime | None = None,
) -> dict[str, Any]:
    """Append owner-supplied evidence and mature the original September decision.

    Never reads credentials, sends provider requests, closes the period, or changes
    financial authority. Import is idempotent by source SHA-256.
    """
    path = Path(input_path).expanduser().resolve(strict=True)
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError("owner evidence must be a non-empty regular file")
    if path.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("owner export exceeds the 50 MiB safety limit")
    data = path.read_bytes()
    source_hash = hashlib.sha256(data).hexdigest()
    imported_at = (now or datetime.now(UTC)).astimezone(UTC)
    source_ref = source_reference or path.name
    records = _rows(path)
    if not records:
        raise ValueError("owner export contains no data rows")
    if len(records) > 100_000:
        raise ValueError("owner export exceeds the 100,000-row safety limit")

    ledger = EconomicLedger(db_path)
    conn = ledger.conn
    conn.execute("""CREATE TABLE IF NOT EXISTS polymarket_owner_evidence_imports (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_sha256 TEXT NOT NULL UNIQUE,
        source_reference TEXT NOT NULL, source_filename TEXT NOT NULL, source_size_bytes INTEGER NOT NULL,
        imported_at TEXT NOT NULL, month_utc TEXT NOT NULL, row_count INTEGER NOT NULL,
        summary_json TEXT NOT NULL, result_recorded INTEGER NOT NULL DEFAULT 0,
        decision_id INTEGER, next_decision_id INTEGER)""")
    prior = conn.execute("SELECT * FROM polymarket_owner_evidence_imports WHERE source_sha256=?",
                         (source_hash,)).fetchone()
    if prior is not None:
        ledger.conn.close()
        return {"status": "duplicate_source", "maturation": "owned_by_normal_agent_cycle",
                "source_sha256": source_hash, "source_reference": prior["source_reference"],
                "owner_input_decision_id": prior["decision_id"],
                "summary": json.loads(prior["summary_json"])}

    counters = {"rows": len(records), "matched_existing_events": 0, "unmatched_rows": 0,
                "fee_rows": 0, "settlement_rows": 0, "balance_rows": 0,
                "refund_void_rows": 0, "unknown_rows": 0, "pending_balance_rows": 0,
                "final_balance_rows": 0, "ambiguous_rows": 0, "event_ids": []}
    actual_classes: set[str] = set()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row_number, raw_row in enumerate(records, 1):
            row = {_key(key): value for key, value in raw_row.items()}
            kind = _classification(row)
            activity_id = _field(raw_row, "activity_id")
            occurred = _parse_time(_field(raw_row, "timestamp"))
            row_digest = hashlib.sha256(json.dumps(raw_row, sort_keys=True, default=str).encode()).hexdigest()
            facts = _safe_row_facts(raw_row)
            amount = _decimal(_field(raw_row, "amount"))
            fee = _decimal(_field(raw_row, "fee"))
            currency = str(_field(raw_row, "currency") or "UNKNOWN").strip().upper()[:20]
            final = _is_final(raw_row)
            base_evidence = {
                "source": "owner_supplied_polymarket_us_statement_or_activity_export",
                "source_reference": source_ref, "source_sha256": source_hash,
                "source_row_number": row_number, "source_row_sha256": row_digest,
                "row_facts": facts, "classification": kind or "unknown",
                "classification_confidence": "explicit_type_only" if kind else "unknown",
                "finality": "final" if final else "unknown_or_pending",
                # Provenance and economic origin are separate. An export imported by
                # NOEMA is not thereby evidence that NOEMA initiated the activity.
                "economic_origin": _origin(raw_row),
                "raw_export_persisted": False,
            }
            if kind is None or occurred is None:
                counters["unknown_rows"] += 1
                counters["ambiguous_rows"] += 1
                continue
            if occurred.strftime("%Y-%m") != month_utc:
                counters["ambiguous_rows"] += 1
                continue

            existing = None
            if activity_id is not None:
                existing = conn.execute(
                    "SELECT id,event_type,amount,currency FROM economic_events "
                    "WHERE provider='polymarket_us' AND external_reference_id=? "
                    "AND event_type IN ('trade_observed','position_resolution_observed',"
                    "'account_balance_change_observed') ORDER BY id LIMIT 1", (str(activity_id),),
                ).fetchone()
            if existing is not None:
                counters["matched_existing_events"] += 1
                base_evidence["matched_canonical_event_id"] = int(existing["id"])
                base_evidence["matched_canonical_event_type"] = str(existing["event_type"])
            else:
                counters["unmatched_rows"] += 1

            if kind == "trade":
                actual_classes.add("fills")
                if fee is not None:
                    fee_amount = abs(fee)
                    counters["fee_rows"] += 1
                    actual_classes.add("fees")
                    fee_ref = f"polymarket-fee:{activity_id or row_digest}"
                    prior_fee = conn.execute(
                        "SELECT id,amount,currency FROM economic_events WHERE provider='polymarket_us' "
                        "AND external_reference_id=? AND event_type='statement_fee_observed' LIMIT 1",
                        (fee_ref,),
                    ).fetchone()
                    if prior_fee is not None:
                        evidence_event = EconomicEvent(
                            provider="polymarket_us",
                            external_reference_id=f"statement:{source_hash}:{activity_id or row_digest}:fee-evidence",
                            event_type="statement_evidence_observation", occurred_at=occurred,
                            currency=currency, amount=Decimal(0), reconciliation_state="MATCHED",
                            value_state="unknown", capital_class="none", confidence_state="operator_reported",
                            completeness_state="complete", related_event_id=int(prior_fee["id"]),
                            lane="prediction_markets",
                            evidence={**base_evidence, "field": "fee", "reported_fee": str(fee_amount),
                                      "matches_existing_fee": Decimal(str(prior_fee["amount"])) == fee_amount
                                      and str(prior_fee["currency"]).upper() == currency},
                        )
                        saved = record_event_on_connection(conn, evidence_event)
                    else:
                        event = EconomicEvent(
                            provider="polymarket_us", external_reference_id=fee_ref,
                            event_type="statement_fee_observed", occurred_at=occurred,
                            currency=currency, amount=fee_amount, amount_usd=fee_amount if currency == "USD" else None,
                            reconciliation_state="RECONCILED" if final and existing else "MATCHED" if existing else "OBSERVED",
                            value_state="realized" if final else "unknown", capital_class="cost",
                            confidence_state="operator_reported", completeness_state="complete",
                            lane="prediction_markets", evidence={**base_evidence, "fee_amount": str(fee_amount)},
                        )
                        saved = record_event_on_connection(conn, event)
                    counters["event_ids"].append(saved["event_id"])
                elif existing is not None:
                    saved = record_event_on_connection(conn, EconomicEvent(
                        provider="polymarket_us",
                        external_reference_id=f"statement:{source_hash}:{activity_id or row_digest}:trade-evidence",
                        event_type="statement_evidence_observation", occurred_at=occurred,
                        currency=currency, amount=Decimal(0), reconciliation_state="MATCHED",
                        value_state="unknown", capital_class="none", confidence_state="operator_reported",
                        completeness_state="complete", related_event_id=int(existing["id"]),
                        lane="prediction_markets", evidence={**base_evidence,
                            "observed_fact": "trade identity/status only; fee remains unknown"},
                    ))
                    counters["event_ids"].append(saved["event_id"])
            elif kind == "fee":
                fee_amount = fee if fee is not None else amount
                if fee_amount is None:
                    counters["unknown_rows"] += 1
                    counters["ambiguous_rows"] += 1
                    continue
                fee_amount = abs(fee_amount)
                counters["fee_rows"] += 1
                actual_classes.add("fees")
                fee_ref = f"polymarket-fee:{activity_id or row_digest}"
                prior_fee = conn.execute(
                    "SELECT id,amount,currency FROM economic_events WHERE provider='polymarket_us' "
                    "AND external_reference_id=? AND event_type='statement_fee_observed' LIMIT 1",
                    (fee_ref,),
                ).fetchone()
                if prior_fee is not None:
                    saved = record_event_on_connection(conn, EconomicEvent(
                        provider="polymarket_us",
                        external_reference_id=f"statement:{source_hash}:{activity_id or row_digest}:fee-evidence",
                        event_type="statement_evidence_observation", occurred_at=occurred,
                        currency=currency, amount=Decimal(0), reconciliation_state="MATCHED",
                        value_state="unknown", capital_class="none", confidence_state="operator_reported",
                        completeness_state="complete", related_event_id=int(prior_fee["id"]),
                        lane="prediction_markets", evidence={**base_evidence, "field": "fee",
                            "reported_fee": str(fee_amount),
                            "matches_existing_fee": Decimal(str(prior_fee["amount"])) == fee_amount
                            and str(prior_fee["currency"]).upper() == currency},
                    ))
                else:
                    saved = record_event_on_connection(conn, EconomicEvent(
                        provider="polymarket_us", external_reference_id=fee_ref,
                        event_type="statement_fee_observed", occurred_at=occurred,
                        currency=currency, amount=fee_amount,
                        amount_usd=fee_amount if currency == "USD" else None,
                        reconciliation_state="RECONCILED" if final and existing else "MATCHED" if existing else "OBSERVED",
                        value_state="realized" if final else "unknown", capital_class="cost",
                        confidence_state="operator_reported", completeness_state="complete",
                        lane="prediction_markets", evidence={**base_evidence, "fee_amount": str(fee_amount)},
                    ))
                counters["event_ids"].append(saved["event_id"])
            elif kind == "settlement":
                counters["settlement_rows"] += 1
                actual_classes.add("settlements")
                if existing is not None and final:
                    event = EconomicEvent(
                        provider="polymarket_us", external_reference_id=f"polymarket-settlement:{activity_id or row_digest}",
                        event_type="statement_settlement_observed", occurred_at=occurred,
                        currency=currency, amount=amount or Decimal(0),
                        amount_usd=(amount if currency == "USD" else None),
                        reconciliation_state="MATCHED", value_state="realized" if amount is not None else "unknown",
                        capital_class="unclassified_cash", confidence_state="operator_reported",
                        completeness_state="complete",
                        lane="prediction_markets", evidence={**base_evidence, "cash_amount_known": amount is not None},
                    )
                    saved = record_event_on_connection(conn, event)
                    counters["event_ids"].append(saved["event_id"])
                elif existing is not None:
                    saved = record_event_on_connection(conn, EconomicEvent(
                        provider="polymarket_us",
                        external_reference_id=f"statement:{source_hash}:{activity_id or row_digest}:settlement-evidence",
                        event_type="statement_evidence_observation", occurred_at=occurred,
                        currency=currency, amount=Decimal(0), reconciliation_state="MATCHED",
                        value_state="unknown", capital_class="none", confidence_state="operator_reported",
                        completeness_state="complete", related_event_id=int(existing["id"]),
                        lane="prediction_markets", evidence={**base_evidence,
                            "observed_fact": "settlement status not final; cashflow remains unknown"},
                    ))
                    counters["event_ids"].append(saved["event_id"])
            elif kind == "refund_void":
                counters["refund_void_rows"] += 1
                actual_classes.add("refunds_voids")
                if occurred is not None:
                    saved = record_event_on_connection(conn, EconomicEvent(
                        provider="polymarket_us",
                        external_reference_id=f"polymarket-refund-void:{activity_id or row_digest}",
                        event_type="statement_refund_void_observed", occurred_at=occurred,
                        currency=currency, amount=amount or Decimal(0),
                        amount_usd=amount if currency == "USD" else None,
                        reconciliation_state="MATCHED" if final else "OBSERVED",
                        value_state="realized" if final and amount is not None else "unknown",
                        capital_class="unclassified_cash", confidence_state="operator_reported",
                        completeness_state="complete" if amount is not None else "unknown",
                        lane="prediction_markets", evidence={**base_evidence,
                            "cash_amount_known": amount is not None},
                    ))
                    counters["event_ids"].append(saved["event_id"])
            elif kind == "balance":
                counters["balance_rows"] += 1
                if not final or existing is None or amount is None:
                    counters["pending_balance_rows"] += 1
                    if existing is not None:
                        saved = record_event_on_connection(conn, EconomicEvent(
                            provider="polymarket_us",
                            external_reference_id=f"statement:{source_hash}:{activity_id or row_digest}:balance-evidence",
                            event_type="statement_evidence_observation", occurred_at=occurred,
                            currency=currency, amount=Decimal(0), reconciliation_state="MATCHED",
                            value_state="unknown", capital_class="none", confidence_state="operator_reported",
                            completeness_state="complete", related_event_id=int(existing["id"]),
                            evidence={**base_evidence, "observed_fact": "balance change remains pending or incomplete"},
                        ))
                        counters["event_ids"].append(saved["event_id"])
                    continue
                # A final source row with an exact activity ID and amount match
                # may mature the pre-existing observed balance item. No row can
                # classify a different amount or infer a deposit/refund from sign.
                same_amount = Decimal(str(existing["amount"])) == amount
                same_currency = str(existing["currency"]).upper() == currency
                if not (same_amount and same_currency):
                    counters["ambiguous_rows"] += 1
                    counters["pending_balance_rows"] += 1
                    continue
                event = EconomicEvent(
                    provider="polymarket_us", external_reference_id=f"state:{existing['id']}:RECONCILED:operator_reported:complete",
                    event_type="reconciliation_state_update", occurred_at=occurred,
                    currency=currency, amount=Decimal(0), reconciliation_state="RECONCILED",
                    value_state="unknown", capital_class="none", confidence_state="operator_reported",
                    completeness_state="complete", related_event_id=int(existing["id"]),
                    evidence={**base_evidence, "amount_identity_verified": True,
                              "classification": str(_field(raw_row, "type")),
                              "nature_and_finality_supported": True},
                )
                saved = record_event_on_connection(conn, event)
                counters["event_ids"].append(saved["event_id"])
                counters["final_balance_rows"] += 1

        # Coverage is only advanced for classes directly represented by typed
        # rows. Absence of rows never establishes zero activity or completeness.
        previous = conn.execute(
            "SELECT expected_classes_json,actual_classes_json FROM economic_provider_coverage "
            "WHERE provider='polymarket_us' AND month_utc=? ORDER BY id DESC LIMIT 1", (month_utc,),
        ).fetchone()
        expected = json.loads(previous[0]) if previous else list(_ACTUAL_CLASSES)
        actual = set(json.loads(previous[1])) if previous else set()
        actual |= actual_classes
        blockers = []
        if "fees" not in actual:
            blockers.append("Trade fees remain unavailable or absent from the supplied export.")
        if "settlements" not in actual:
            blockers.append("Settlement cashflows remain unavailable or absent from the supplied export.")
        if "refunds_voids" not in actual:
            blockers.append("Refund/void/correction coverage remains unknown; no absence inferred.")
        if counters["pending_balance_rows"]:
            blockers.append(f"{counters['pending_balance_rows']} balance-change row(s) remain pending or ambiguous.")
        if counters["ambiguous_rows"] or counters["unknown_rows"]:
            blockers.append(f"{counters['ambiguous_rows'] + counters['unknown_rows']} row(s) remain ambiguous or unsupported.")
        if counters["unmatched_rows"]:
            blockers.append(f"{counters['unmatched_rows']} row(s) did not match an existing canonical activity ID.")
        if counters["event_ids"] or actual != set(json.loads(previous[1]) if previous else []):
            coverage_id = record_provider_coverage_on_connection(
                conn, provider="polymarket_us", month_utc=month_utc, state="PARTIAL",
                expected_evidence_classes=expected, actual_evidence_classes=sorted(actual),
                observed_at=imported_at,
                provenance={"source": "owner-supplied local statement/activity export",
                            "source_reference": source_ref, "source_sha256": source_hash,
                            "export_filename": path.name, "row_count": len(records)},
                evidence={"imported_rows": len(records), **{k: v for k, v in counters.items() if k != "event_ids"},
                          "source_sha256": source_hash, "export_period": month_utc,
                          "absence_inferred": False},
                unresolved_blockers=blockers or ["Provider statement evidence is operator-supplied and not yet independently verified."],
                applicable_activity_detected=True,
            )
            counters["coverage_record_id"] = coverage_id
        else:
            counters["coverage_record_id"] = None
        decision_row = conn.execute(
            "SELECT id FROM economic_investigation_decisions WHERE month_utc=? "
            "AND action='REQUEST_OWNER_INPUT' AND selected_candidate='polymarket_us' "
            "ORDER BY id DESC LIMIT 1", (month_utc,),
        ).fetchone()
        if decision_row is None:
            raise ValueError("no persisted Polymarket owner-input decision exists for this period")
        owner_decision_id = int(decision_row[0])
        conn.execute(
            "INSERT INTO polymarket_owner_evidence_imports(source_sha256,source_reference,source_filename,"
            "source_size_bytes,imported_at,month_utc,row_count,summary_json,decision_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (source_hash, source_ref, path.name, len(data), imported_at.isoformat(), month_utc,
             len(records), json.dumps(counters, sort_keys=True, default=str), owner_decision_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    return {"status": "imported", "maturation": "owned_by_normal_agent_cycle",
            "source_sha256": source_hash, "source_reference": source_ref,
            "source_filename": path.name, "summary": counters,
            "owner_input_decision_id": owner_decision_id,
            "financial_authority_changed": False}
