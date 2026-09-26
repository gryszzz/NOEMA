"""Prospective paper quotes and later settlement-only net-return audit."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from .history_forecaster import MODEL_VERSION
from .paper_execution import FeeTerms, PaperQuote, parse_aware_time, quote_yes_taker
from .risk import RiskPolicy


@dataclass(frozen=True)
class Candidate:
    venue: str
    ticker: str
    model_version: str
    probability_yes: float
    lower_bound: float
    upper_bound: float
    forecast_at: datetime


@dataclass(frozen=True)
class PaperQuoteResult:
    status: str
    detail: str
    ticker: str
    quote: PaperQuote | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status, "detail": self.detail, "ticker": self.ticker,
            "quote": self.quote.as_dict() if self.quote else None,
        }


class PaperVenue(Protocol):
    name: str

    async def taker_fee_terms(self, ticker: str) -> FeeTerms: ...

    async def paper_book(self, ticker: str) -> tuple[dict[str, Any], str]: ...


class PaperResearchStore:
    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS paper_quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                venue TEXT NOT NULL, market_id TEXT NOT NULL,
                model_version TEXT NOT NULL, forecast_at TEXT NOT NULL,
                quoted_at TEXT NOT NULL, probability_yes REAL NOT NULL,
                lower_bound REAL NOT NULL, upper_bound REAL NOT NULL,
                selected INTEGER NOT NULL CHECK(selected IN (0, 1)),
                quote_json TEXT NOT NULL, fee_terms_json TEXT NOT NULL,
                UNIQUE (venue, market_id, model_version, forecast_at)
            )
        """)
        self.conn.commit()

    def earliest_candidate(self, venue: str, ticker: str) -> Candidate | None:
        try:
            rows = self.conn.execute("""
                SELECT snapshot_json, forecast_json FROM forecast_ledger
                WHERE venue = ? AND market_id = ?
                  AND json_extract(forecast_json, '$.model_version') = ?
                ORDER BY id ASC LIMIT 1
            """, (venue, ticker, MODEL_VERSION)).fetchall()
        except sqlite3.OperationalError:
            return None
        if not rows:
            return None
        try:
            snapshot, forecast = (json.loads(value) for value in rows[0])
            created = parse_aware_time(forecast["created_at"])
            captured = parse_aware_time(snapshot["captured_at"])
            p = float(forecast["probability_yes"])
            lo = float(forecast["lower_bound"])
            hi = float(forecast["upper_bound"])
            if (created < captured or not 0 <= lo <= p <= hi <= 1
                    or forecast.get("venue") != venue
                    or forecast.get("market_id") != ticker):
                return None
        except (KeyError, ValueError, TypeError, OverflowError, AttributeError):
            return None
        return Candidate(venue, ticker, MODEL_VERSION, p, lo, hi, created)

    def has_quote(self, candidate: Candidate) -> bool:
        return self.conn.execute("""
            SELECT 1 FROM paper_quotes WHERE venue = ? AND market_id = ?
              AND model_version = ? AND forecast_at = ? LIMIT 1
        """, (candidate.venue, candidate.ticker, candidate.model_version,
               candidate.forecast_at.isoformat())).fetchone() is not None

    def record(self, candidate: Candidate, quoted_at: datetime, quote: PaperQuote,
               terms: FeeTerms, *, selected: bool) -> bool:
        if quoted_at.tzinfo is None or quoted_at <= candidate.forecast_at:
            raise ValueError("paper quote must follow the independent forecast")
        cursor = self.conn.execute("""
            INSERT OR IGNORE INTO paper_quotes
            (venue, market_id, model_version, forecast_at, quoted_at,
             probability_yes, lower_bound, upper_bound, selected, quote_json, fee_terms_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            candidate.venue, candidate.ticker, candidate.model_version,
            candidate.forecast_at.isoformat(), quoted_at.astimezone(UTC).isoformat(),
            candidate.probability_yes, candidate.lower_bound, candidate.upper_bound,
            int(selected), json.dumps(quote.as_dict(), sort_keys=True),
            json.dumps({key: str(value) for key, value in asdict(terms).items()},
                       sort_keys=True),
        ))
        self.conn.commit()
        return cursor.rowcount == 1

    def audit(self) -> dict[str, object]:
        observed = self.conn.execute("SELECT COUNT(*), SUM(selected) FROM paper_quotes").fetchone()
        try:
            rows = self.conn.execute("""
                SELECT q.venue, q.market_id, q.quoted_at, q.quote_json,
                       o.outcome_yes, o.resolved_at, o.first_seen_at
                FROM paper_quotes q JOIN outcomes o
                  ON q.venue = o.venue AND q.market_id = o.market_id
                WHERE q.selected = 1 ORDER BY q.quoted_at, q.id
            """).fetchall()
        except sqlite3.OperationalError:
            rows = []
        pnl: list[tuple[str, Decimal]] = []
        for venue, ticker, quoted, serialized, outcome, settled, first_seen in rows:
            try:
                quote_time = parse_aware_time(quoted)
                if (parse_aware_time(settled) <= quote_time
                        or parse_aware_time(first_seen) <= quote_time):
                    continue
                quote = json.loads(serialized)
                earned = Decimal(str(outcome)) * Decimal(quote["contracts"])
                debit = Decimal(quote["total_debit_usd"])
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
            pnl.append((f"{venue}:{ticker.rsplit('-', 1)[0]}", earned - debit))
        event_pnl: dict[str, Decimal] = {}
        for event, amount in pnl:
            event_pnl[event] = event_pnl.get(event, Decimal(0)) + amount
        total = sum((amount for _, amount in pnl), Decimal(0))
        return {
            "status": "no_settled_paper_quotes" if not pnl else "research_only",
            "observed_quote_count": observed[0],
            "selected_quote_count": observed[1] or 0,
            "market_count": len(pnl), "event_count": len(event_pnl),
            "net_pnl_usd": str(total),
            "mean_net_pnl_per_event_usd": str(total / len(event_pnl)) if event_pnl else None,
            "live_eligible": False,
            "assumption": "Hypothetical instantaneous full fills at recorded orderbook depth; latency and competing orders not measured",
        }


async def collect_paper_quote(
    venue: PaperVenue, store: PaperResearchStore, ticker: str,
    *, contracts: Decimal = Decimal(1), max_forecast_age_seconds: int = 30,
) -> PaperQuoteResult:
    """No orders are placed; only a fresh, independent, first forecast qualifies."""
    candidate = store.earliest_candidate(venue.name, ticker)
    if candidate is None:
        return PaperQuoteResult("unavailable", "no verified independent forecast", ticker)
    if store.has_quote(candidate):
        return PaperQuoteResult("already_recorded", "first quote is immutable", ticker)
    age = (datetime.now(UTC) - candidate.forecast_at).total_seconds()
    if age < 0 or age > max_forecast_age_seconds:
        return PaperQuoteResult("unavailable", "independent forecast is stale", ticker)
    terms = await venue.taker_fee_terms(ticker)
    book, source = await venue.paper_book(ticker)
    quoted_at = datetime.now(UTC)
    if (quoted_at - candidate.forecast_at).total_seconds() > max_forecast_age_seconds:
        return PaperQuoteResult("unavailable", "quote arrived after forecast expired", ticker)
    quote = quote_yes_taker(
        book, contracts=contracts, probability_yes=candidate.probability_yes,
        fee_terms=terms, quote_source=source,
    )
    # The forecast's lower bound must clear average price, taker fees, a
    # 1-cent latency reserve, and the risk engine's minimum edge and stake gates.
    policy = RiskPolicy()
    selected = (
        candidate.lower_bound - float(quote.total_debit_usd / quote.contracts)
        >= policy.min_robust_edge + 0.01
        and quote.total_debit_usd <= Decimal(str(policy.max_stake_usd))
    )
    if not store.record(candidate, quoted_at, quote, terms, selected=selected):
        return PaperQuoteResult("already_recorded", "first quote is immutable", ticker)
    return PaperQuoteResult(
        "paper_candidate" if selected else "paper_pass",
        "hypothetical full fill; live execution remains disabled"
        if selected else "conservative after-cost edge below paper threshold",
        ticker, quote,
    )
