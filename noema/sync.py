from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .outcomes import OutcomeStore
from .venues.kalshi_history import KalshiHistory, resolved_outcome


@dataclass(frozen=True)
class SyncResult:
    scanned: int
    imported: int
    skipped: int


async def sync_kalshi_outcomes(
    store: OutcomeStore,
    history: KalshiHistory,
    *,
    max_markets: int | None = None,
) -> SyncResult:
    scanned = 0
    imported = 0
    skipped = 0
    venue = f"kalshi:{history.config.environment}"

    async for raw in history.settled_markets():
        scanned += 1
        outcome = resolved_outcome(raw)
        ticker = raw.get("ticker")
        settled = raw.get("settlement_ts")
        try:
            valid_settlement = bool(settled and datetime.fromisoformat(settled).tzinfo)
        except (TypeError, ValueError):
            valid_settlement = False
        if outcome is None or not ticker or not valid_settlement:
            skipped += 1
        else:
            store.upsert(
                venue=venue,
                market_id=str(ticker),
                outcome_yes=outcome,
                resolved_at=raw.get("settlement_ts"),
                raw=raw,
            )
            imported += 1

        if max_markets is not None and scanned >= max_markets:
            break

    return SyncResult(scanned=scanned, imported=imported, skipped=skipped)
