from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx

from .outcomes import OutcomeStore
from .venues.kalshi_history import KalshiHistory, resolved_outcome


@dataclass(frozen=True)
class SyncResult:
    scanned: int
    imported: int
    skipped: int


def _import_market(store: OutcomeStore, venue: str, raw: dict[str, object]) -> bool:
    outcome = resolved_outcome(raw)
    ticker = raw.get("ticker")
    settled = raw.get("settlement_ts")
    try:
        valid_settlement = bool(settled and datetime.fromisoformat(str(settled)).tzinfo)
    except ValueError:
        valid_settlement = False
    if outcome is None or not ticker or not valid_settlement:
        return False
    store.upsert(
        venue=venue, market_id=str(ticker), outcome_yes=outcome,
        resolved_at=str(settled), raw=raw,
    )
    return True


async def sync_kalshi_outcomes(
    store: OutcomeStore,
    history: KalshiHistory,
    *,
    max_markets: int | None = None,
) -> SyncResult:
    """Rotate bounded pages in both settlement partitions across sync cycles."""
    budget = 2000 if max_markets is None else max_markets
    if budget <= 0:
        raise ValueError("max_markets must be positive")
    scanned = 0
    imported = 0
    skipped = 0
    venue = f"kalshi:{history.config.environment}"
    # Resolve newly selected paper positions directly, without waiting for a
    # broad cursor sweep across all markets to reach their tickers.
    pending_source = f"{venue}:settlements:pending_id"
    for quote_id, ticker in store.pending_paper_quotes(venue, limit=min(10, budget // 10)):
        raw = await history.market_by_ticker(ticker)
        scanned += 1
        if raw is not None and _import_market(store, venue, raw):
            imported += 1
        else:
            skipped += 1
        store.set_scan_cursor(pending_source, str(quote_id))

    turn_key = f"{venue}:settlements:next"
    first = store.scan_cursor(turn_key)
    if first not in {"live", "historical"}:
        first = "live"
    partitions = (first, "historical" if first == "live" else "live")
    for partition in partitions:
        if scanned >= budget:
            break
        remaining_partitions = 2 if partition == first else 1
        limit = min(1000, max(1, (budget - scanned) // remaining_partitions))
        source = f"{venue}:settlements:{partition}"
        cursor = store.scan_cursor(source)
        try:
            rows, next_cursor = await history.settled_page(
                partition, cursor=cursor, limit=limit,
            )
        except httpx.HTTPStatusError as exc:
            if cursor is None or exc.response.status_code != 400:
                raise
            # Resume from the beginning only when an old cursor is rejected.
            store.set_scan_cursor(source, None)
            rows, next_cursor = await history.settled_page(
                partition, cursor=None, limit=limit,
            )
        if len(rows) > limit:
            raise ValueError("settlement page exceeds requested limit")
        for raw in rows:
            scanned += 1
            if _import_market(store, venue, raw):
                imported += 1
            else:
                skipped += 1
        # Never skip a page when a network request or database write fails.
        store.set_scan_cursor(source, next_cursor)
        store.set_scan_cursor(turn_key, "historical" if partition == "live" else "live")

    return SyncResult(scanned=scanned, imported=imported, skipped=skipped)
