from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

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
        resolved_at=str(settled), raw={**raw, "finality": "official_kalshi_settlement"},
        canonical_proposition_id=(
            str(raw["canonical_proposition_id"])
            if raw.get("canonical_proposition_id") else None
        ),
        source="kalshi_official_market_api",
        source_url=f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}",
    )
    return True


async def sync_kalshi_outcomes(
    store: OutcomeStore,
    history: KalshiHistory,
    *,
    max_markets: int | None = None,
    canonical_propositions: dict[str, str] | None = None,
) -> SyncResult:
    """Rotate bounded pages in both settlement partitions across sync cycles."""
    budget = 2000 if max_markets is None else max_markets
    if budget <= 0:
        raise ValueError("max_markets must be positive")
    scanned = 0
    imported = 0
    skipped = 0
    venue = f"kalshi:{history.config.environment}"
    canonical_propositions = canonical_propositions or {}
    # Resolve newly selected paper positions directly, without waiting for a
    # broad cursor sweep across all markets to reach their tickers.
    pending_source = f"{venue}:settlements:pending_id"
    for quote_id, ticker in store.pending_paper_quotes(venue, limit=min(10, budget // 10)):
        raw = await history.market_by_ticker(ticker)
        scanned += 1
        if raw is not None:
            if ticker in canonical_propositions:
                raw["canonical_proposition_id"] = canonical_propositions[ticker]
            imported_market = _import_market(store, venue, raw)
        else:
            imported_market = False
        if imported_market:
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
            ticker = raw.get("ticker")
            if ticker and str(ticker) in canonical_propositions:
                raw["canonical_proposition_id"] = canonical_propositions[str(ticker)]
            if _import_market(store, venue, raw):
                imported += 1
            else:
                skipped += 1
        # Never skip a page when a network request or database write fails.
        store.set_scan_cursor(source, next_cursor)
        store.set_scan_cursor(turn_key, "historical" if partition == "live" else "live")

    return SyncResult(scanned=scanned, imported=imported, skipped=skipped)


def cross_venue_outcome_targets(path: str, *, limit: int = 5) -> tuple[dict[str, str], dict[str, str]]:
    """Return newest unique settled-outcome targets from paper-filled cohort only."""
    if not 1 <= limit <= 5:
        raise ValueError("cohort target limit must be 1..5")
    db = Path(path)
    if not db.exists():
        return {}, {}
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='canonical_pair_observations'"
        ).fetchone():
            return {}, {}
        rows = conn.execute(
            "SELECT observation_json FROM canonical_pair_observations ORDER BY id DESC LIMIT 500"
        ).fetchall()
    finally:
        conn.close()
    kalshi: dict[str, str] = {}
    polymarket: dict[str, str] = {}
    seen: set[str] = set()
    for (payload,) in rows:
        try:
            observation = json.loads(payload)
            evaluation = observation.get("experiment_evaluation", {})
            if evaluation.get("paper_simulation", {}).get("status") != "paper_fill_simulated":
                continue
            proposition = evaluation.get("canonical_proposition_id")
            contracts = evaluation.get("native_contracts", [])
            if not proposition or not isinstance(contracts, list) or str(proposition) in seen:
                continue
            by_venue = {str(item.get("venue")): item for item in contracts if isinstance(item, dict)}
            k, p = by_venue.get("kalshi", {}), by_venue.get("polymarket_us", {})
            if not k.get("market_id") or not p.get("market_id"):
                continue
            identity_contracts = (observation.get("canonical_identity") or {}).get("contracts", [])
            open_at_decision = {
                str(item.get("venue")): item.get("active_at_observation")
                for item in identity_contracts if isinstance(item, dict)
            }
            if open_at_decision != {"kalshi": True, "polymarket_us": True}:
                continue
            seen.add(str(proposition))
            kalshi[str(k["market_id"])] = str(proposition)
            polymarket[str(p["market_id"])] = str(proposition)
            if len(seen) >= limit:
                break
        except (ValueError, TypeError, AttributeError):
            continue
    return kalshi, polymarket


async def sync_polymarket_us_outcomes(
    store: OutcomeStore,
    venue: object,
    canonical_propositions: dict[str, str],
) -> SyncResult:
    """Poll only the persisted experiment cohort using official read-only APIs.

    Polymarket US's current official settlement response documents a numeric
    settlement but no settled-at timestamp. ``resolved_at`` therefore remains
    unknown unless a timestamp is present in the authoritative response;
    first_seen_at records when NOEMA first observed the final settlement.
    """
    if len(canonical_propositions) > 5:
        raise ValueError("Polymarket US outcome sync is bounded to the fixed five-pair cohort")
    from datetime import UTC, datetime
    from decimal import Decimal, InvalidOperation

    scanned = imported = skipped = 0
    for slug, proposition_id in canonical_propositions.items():
        scanned += 1
        market = await venue.market_by_slug(slug)
        settlement = await venue.settlement(slug)
        observed_at = datetime.now(UTC)
        if (market.get("slug") != slug or market.get("closed") is not True
                or settlement.get("slug") != slug):
            skipped += 1
            continue
        value = settlement.get("settlement")
        try:
            settled_value = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            skipped += 1
            continue
        # The official binary settlement must be exactly 0 or 1. A fractional
        # value could represent a void/scalar treatment and is not mapped to YES.
        if not settled_value.is_finite() or settled_value not in {Decimal(0), Decimal(1)}:
            skipped += 1
            continue
        resolved_at = None
        resolved_field = None
        for field in ("settledAt", "resolvedAt", "settlementTime", "settlementTimestamp"):
            value = settlement.get(field)
            if not value:
                continue
            try:
                normalized = str(value)
                if normalized.endswith("Z"):
                    normalized = normalized[:-1] + "+00:00"
                parsed = datetime.fromisoformat(normalized)
            except ValueError:
                continue
            if parsed.tzinfo is not None:
                resolved_at = parsed.astimezone(UTC).isoformat()
                resolved_field = field
                break
        base = "https://api.polymarket.us/v1"
        raw = {
            "market": market,
            "settlement": settlement,
            "finality": "official_closed_market_and_settlement_endpoint",
            "observed_at": observed_at.isoformat(),
            "venue_resolved_at_field": resolved_field,
            "venue_resolved_at_available": resolved_at is not None,
            "outcome_semantics": "official binary settlement value; not inferred from price",
        }
        store.upsert(
            venue="polymarket-us", market_id=slug,
            outcome_yes=int(settled_value), resolved_at=resolved_at,
            raw=raw, seen_at=observed_at,
            canonical_proposition_id=proposition_id,
            source="polymarket_us_official_settlement_endpoint",
            source_url=f"{base}/markets/{slug}/settlement",
        )
        imported += 1
    return SyncResult(scanned=scanned, imported=imported, skipped=skipped)
