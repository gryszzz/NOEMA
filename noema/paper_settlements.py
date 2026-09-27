"""One validated settlement cohort shared by paper audit and specialist evidence.

The first recorded decision per market/model wins, including a PASS. Later quotes
cannot replace it after the outcome is known. Values remain Decimal until scoring.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .history_forecaster import MODEL_VERSION
from .paper_execution import parse_aware_time


@dataclass(frozen=True)
class PaperSettlement:
    venue: str
    market_id: str
    observed_at: datetime
    debit_usd: Decimal
    net_pnl_usd: Decimal

    @property
    def event_id(self) -> tuple[str, str]:
        return self.venue, self.market_id.rsplit("-", 1)[0]


@dataclass(frozen=True)
class PaperSettlementAudit:
    settlements: tuple[PaperSettlement, ...] = ()
    observed_quote_count: int = 0
    selected_quote_count: int = 0
    duplicate_quote_count: int = 0
    invalid_quote_count: int = 0
    pending_quote_count: int = 0


def _positive_money(value: object) -> Decimal:
    if isinstance(value, bool):
        raise TypeError("invalid paper amount")
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0 or not math.isfinite(float(number)):
        raise ValueError("invalid paper amount")
    return number


def load_paper_settlements(
    path: str, *, model_version: str = MODEL_VERSION, now: datetime | None = None,
) -> PaperSettlementAudit:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("paper audit clock requires a timezone")
    db = Path(path)
    if not db.exists():
        return PaperSettlementAudit()
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        if "paper_quotes" not in tables:
            return PaperSettlementAudit()
        outcome_columns = (
            "o.outcome_yes, o.resolved_at, o.first_seen_at" if "outcomes" in tables
            else "NULL, NULL, NULL"
        )
        outcome_join = (
            "LEFT JOIN outcomes o ON o.venue=q.venue AND o.market_id=q.market_id"
            if "outcomes" in tables else ""
        )
        rows = conn.execute(f"""
            SELECT q.venue, q.market_id, q.selected, q.quoted_at, q.forecast_at,
                   q.quote_json, {outcome_columns}
            FROM paper_quotes q {outcome_join}
            WHERE q.model_version = ? ORDER BY q.id
        """, (model_version,)).fetchall()
    finally:
        conn.close()

    seen: set[tuple[str, str]] = set()
    settlements: list[PaperSettlement] = []
    duplicates = invalid = pending = selected_count = observed = 0
    for venue, market, selected, quoted, forecast, payload, outcome, resolved, first_seen in rows:
        try:
            quoted_at = parse_aware_time(quoted)
            if quoted_at > now:
                continue
        except (ValueError, TypeError):
            quoted_at = None
        observed += 1
        selected_count += int(selected == 1)
        key = (venue, market)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        if selected == 0:
            continue
        try:
            if selected != 1 or quoted_at is None or not venue or not market:
                raise ValueError("invalid paper decision")
            if parse_aware_time(forecast) >= quoted_at:
                raise ValueError("quote does not follow forecast")
            quote = json.loads(payload)
            debit = _positive_money(quote["total_debit_usd"])
            contracts = _positive_money(quote["contracts"])
            # Division is eventually used by statistical scoring; reject overflow too.
            if not math.isfinite(float(contracts / debit)):
                raise ValueError("invalid paper return")
            if outcome is None:
                pending += 1
                continue
            if type(outcome) is not int or outcome not in (0, 1):
                raise ValueError("invalid binary outcome")
            resolved_at = parse_aware_time(resolved)
            seen_at = parse_aware_time(first_seen)
            if resolved_at <= quoted_at or seen_at <= quoted_at:
                raise ValueError("settlement was not prospective")
            observed_at = max(resolved_at, seen_at)
            if observed_at > now:
                pending += 1
                continue
            settlements.append(PaperSettlement(
                venue, market, observed_at, debit, outcome * contracts - debit,
            ))
        except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
            invalid += 1
    # Cash becomes knowable only at the later of resolution and local observation.
    settlements.sort(key=lambda item: (item.observed_at, item.venue, item.market_id))
    return PaperSettlementAudit(
        tuple(settlements), observed, selected_count, duplicates, invalid, pending,
    )
