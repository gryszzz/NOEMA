"""Read-only live health and public market data for prediction venues."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import monotonic
from typing import Any

import httpx
from polymarket_us.errors import PolymarketUSError

from .account import KalshiAccount
from .canonical_comparability import (
    PROPOSITION_FAMILIES,
    EconomicQuote,
    SettlementTerms,
    append_shared_economic_event,
    assess_economic_comparability,
    assess_settlement_equivalence,
    canonical_observation_event,
    identify_tesla_q3_deliveries_threshold,
)
from .canonical_market_identity import (
    compare_contract_identities,
    identify_mlb_world_series_champion,
    registered_resolvers,
)
from .config import KalshiConfig, kalshi_production_read_only_config
from .ledger import ForecastLedger
from .venues.kalshi import KalshiVenue
from .venues.polymarket_us import PolymarketUSVenue
from .wallet_credentials import (
    load_polymarket_us_credentials_in_api_boundary,
    polymarket_us_credentials_present,
)

_cache: dict[str, Any] = {"at": 0.0, "payload": None}
_lock = asyncio.Lock()
_registered_quote_pair: list[dict[str, Any]] | None = None
_registered_identity_review_at = 0.0


async def _kalshi_status() -> dict[str, Any]:
    config = KalshiConfig.from_env()
    # Real production read-only telemetry is separate from the configured order
    # environment. No setting or execution authority is changed by this check.
    production = kalshi_production_read_only_config()
    started = monotonic()
    venue = KalshiVenue(production)
    try:
        markets, cursor = await venue.market_page(limit=10)
        exchange = await venue.exchange_status()
        open_market = markets[0] if markets else None
        book_ok = False
        book_levels = 0
        if open_market:
            response = await venue.client.get(f"/markets/{open_market.market_id}/orderbook")
            if response.status_code == 200:
                payload = response.json()
                book = payload.get("orderbook_fp") or payload.get("orderbook") or {}
                book_levels = sum(
                    len(book.get(side) or []) for side in ("yes_dollars", "no_dollars", "yes", "no")
                )
                book_ok = True
        market_data = {
            "status": "connected",
            "open_markets_sampled": len(markets),
            "more_markets_available": bool(cursor),
            "order_book_readable": book_ok,
            "order_book_levels": book_levels if book_ok else None,
            "sample_market": None if open_market is None else {
                "market_id": open_market.market_id,
                "title": open_market.title,
                "yes_bid": open_market.yes_bid,
                "yes_ask": open_market.yes_ask,
                "spread": (
                    None if open_market.yes_bid is None or open_market.yes_ask is None
                    else round(open_market.yes_ask - open_market.yes_bid, 6)
                ),
                "closes_at": None if open_market.closes_at is None else open_market.closes_at.isoformat(),
                "rules_available": bool(open_market.resolution_rules),
                "liquidity_usd": open_market.liquidity_usd,
            },
            "exchange_trading_active": exchange.get("trading_active"),
        }
    except (PolymarketUSError, httpx.HTTPError, RuntimeError, ValueError,
            KeyError, TypeError, OSError):
        market_data = {"status": "degraded"}
    finally:
        await venue.close()

    keychain_present = config.key_id is not None
    pem = Path(config.private_key_path) if config.private_key_path else None
    protected_key_present = bool(pem and pem.is_file() and (pem.stat().st_mode & 0o077) == 0)
    account: dict[str, Any] = {
        "status": "unconfigured",
        "balance_available": False,
        "positions": None,
        "open_orders": None,
        "fills": None,
    }
    if keychain_present and protected_key_present:
        account_config = KalshiConfig(
            environment="production", key_id=config.key_id,
            private_key_path=config.private_key_path,
            allow_live_orders=False, master_halt=True,
        )
        client = KalshiAccount(account_config)
        try:
            snapshot = await client.snapshot()
            account = {
                "status": "authenticated_read_only",
                "balance_available": bool(snapshot.balance),
                "cash_balance_usd": _number(snapshot.balance.get("balance_dollars")),
                "positions": len(snapshot.positions.get("market_positions", [])),
                "open_orders": sum(
                    str(order.get("status", "")).lower() in {"resting", "open", "pending"}
                    for order in snapshot.orders.get("orders", [])
                ),
                "fills": len(snapshot.fills.get("fills", [])),
                "limits_available": bool(snapshot.limits),
                "user_data_as_of": (
                    None if snapshot.user_data_as_of is None
                    else snapshot.user_data_as_of.isoformat()
                ),
            }
        except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError, OSError):
            account["status"] = "authentication_failed"
        finally:
            await client.close()
    elif not keychain_present:
        account["status"] = "missing_key_id"
    elif not protected_key_present:
        account["status"] = "missing_or_unprotected_private_key"

    return {
        "venue": "Kalshi",
        "environment": "production_read_only",
        "execution": "disabled_in_this_read_only_check",
        "latency_ms": round((monotonic() - started) * 1000),
        "market_data": market_data,
        "account": account,
    }


async def _polymarket_us_status() -> dict[str, Any]:
    started = monotonic()
    venue = PolymarketUSVenue()
    try:
        markets, _ = await venue.market_page(limit=1)
        sample = next((item for item in markets if item.yes_bid is not None or item.yes_ask is not None), None)
        book: dict[str, Any] | None = None
        if sample:
            try:
                book = await venue.book(sample.market_id)
            except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError):
                pass
        key_id_present, secret_present = polymarket_us_credentials_present()
        account: dict[str, Any] = {"status": "unconfigured"}
        if key_id_present and secret_present:
            account_status = "authenticated_read_only"
            authenticated_client = None
            try:
                from polymarket_us import PolymarketUS

                key_id, secret_key = load_polymarket_us_credentials_in_api_boundary()
                authenticated_client = PolymarketUS(
                    key_id=key_id, secret_key=secret_key, timeout=5.0, max_retries=0,
                )
                balances = await asyncio.to_thread(authenticated_client.account.balances)
                positions = await asyncio.to_thread(authenticated_client.portfolio.positions)
                activities = await asyncio.to_thread(authenticated_client.portfolio.activities)
                orders = await asyncio.to_thread(authenticated_client.orders.list)
                cash_balances = []
                raw_balances = balances.get("balances", []) if isinstance(balances, dict) else []
                if isinstance(raw_balances, list):
                    for item in raw_balances:
                        if not isinstance(item, dict):
                            continue
                        cash_balances.append({
                            "currency": str(item.get("currency") or "unknown"),
                            "current_balance": _number(item.get("currentBalance")),
                            "buying_power": _number(item.get("buyingPower")),
                            "available_to_withdraw": _number(item.get("availableToWithdraw")),
                        })
                usd_balance = next(
                    (item for item in cash_balances if item["currency"].upper() == "USD"),
                    None,
                )
                account = {
                    "status": account_status,
                    "balance_available": bool(balances),
                    "cash_balances": cash_balances,
                    "cash_balance_usd": None if usd_balance is None else usd_balance["current_balance"],
                    "buying_power_usd": None if usd_balance is None else usd_balance["buying_power"],
                    "available_to_withdraw_usd": None if usd_balance is None else usd_balance["available_to_withdraw"],
                    "positions": _count_records(positions, "positions"),
                    "open_orders": _count_records(orders, "orders"),
                    "activity_records": _count_records(activities, "activities"),
                }
            except (PolymarketUSError, httpx.HTTPError, RuntimeError, ValueError,
                    KeyError, TypeError, OSError) as exc:
                account_status = "authentication_or_read_failed"
                account = {
                    "status": account_status,
                    "error_type": type(exc).__name__,
                    "balance_available": False,
                    "positions": None,
                    "open_orders": None,
                    "activity_records": None,
                }
            finally:
                if authenticated_client is not None:
                    authenticated_client.close()
        elif key_id_present:
            account_status = "missing_secret_key"
            account = {"status": account_status}
        elif secret_present:
            account_status = "missing_key_id"
            account = {"status": account_status}
        else:
            account_status = "missing_key_id_and_secret_key"
            account = {"status": account_status}
        return {
            "venue": "Polymarket US",
            "environment": "production_read_only_public_data",
            "execution": "disabled",
            "latency_ms": round((monotonic() - started) * 1000),
            "market_data": {
                "status": "connected",
                "open_markets_sampled": len(markets),
                "sample_market": None if sample is None else {
                    "market_id": sample.market_id,
                    "title": sample.title,
                    "yes_bid": sample.yes_bid,
                    "yes_ask": sample.yes_ask,
                    "spread": (
                        None if sample.yes_bid is None or sample.yes_ask is None
                        else round(sample.yes_ask - sample.yes_bid, 6)
                    ),
                    "closes_at": None if sample.closes_at is None else sample.closes_at.isoformat(),
                    "rules_available": bool(sample.resolution_rules),
                    # USD depth remains unknown until actual book levels can be
                    # interpreted consistently with contract units.
                    "liquidity_usd": sample.liquidity_usd,
                },
                "order_book_readable": book is not None,
                "order_book_levels": (
                    None if book is None else len(book.get("bids", [])) + len(book.get("offers", []))
                ),
                "market_state": None if book is None else book.get("state"),
            },
            "account": account,
        }
    except (PolymarketUSError, httpx.HTTPError, RuntimeError, ValueError,
            KeyError, TypeError, OSError):
        return {
            "venue": "Polymarket US",
            "environment": "production_read_only_public_data",
            "execution": "disabled",
            "latency_ms": round((monotonic() - started) * 1000),
            "market_data": {"status": "degraded"},
            "account": {"status": "unconfigured"},
        }
    finally:
        venue.close()


async def _cross_venue_candidate() -> dict[str, Any]:
    """Discover one current public same-outcome candidate; never infer edge."""
    global _registered_quote_pair, _registered_identity_review_at
    now_mono = monotonic()
    if _registered_quote_pair is None:
        _registered_quote_pair = _load_registered_quote_pair()
        if _registered_quote_pair is not None:
            # A restart loads established identities from the canonical graph;
            # persisted links remain the fast-path source of truth.
            _registered_identity_review_at = now_mono
    if _registered_quote_pair is not None and now_mono - _registered_identity_review_at < 900:
        # Fast path: use persisted/previously established contract identities;
        # this refresh reads quotes only and deliberately does not run resolver
        # or settlement reasoning on each quote update.
        refreshed_pairs = await asyncio.gather(*(
            _refresh_registered_pair(pair) for pair in _registered_quote_pair
        ))
        if refreshed_pairs and all(pair is not None for pair in refreshed_pairs):
            return {
                "status": "candidate_only", "matches": refreshed_pairs,
                "reason": "Registered canonical proposition; quotes refreshed without rerunning identity resolution.",
                "path": "fast_quote_refresh",
            }
    kalshi = KalshiVenue(KalshiConfig(
        environment="production", allow_live_orders=False, master_halt=True,
    ))
    polymarket = PolymarketUSVenue()
    try:
        kalshi_response = await kalshi.client.get(
            "/events",
            params={"series_ticker": "KXMLB", "status": "open", "limit": 100,
                    "with_nested_markets": "true"},
        )
        kalshi_response.raise_for_status()
        kalshi_events = kalshi_response.json().get("events", [])

        season = datetime.now(UTC).year
        search = await asyncio.to_thread(
            polymarket.client.search.query,
            {"query": f"{season} MLB World Series Champion"},
        )
        events = search.get("events", []) if isinstance(search, dict) else []
        chosen_event = next((
            event for event in events
            if str(event.get("title", "")).lower() == "world series champion"
            and event.get("active") is True and event.get("closed") is not True
        ), None)
        pm_event: dict[str, Any] = {}
        pm_markets: list[dict[str, Any]] = []
        if chosen_event is not None:
            detail = await asyncio.to_thread(
                polymarket.client.events.retrieve_by_slug, chosen_event["slug"],
            )
            pm_event = detail.get("event", {}) if isinstance(detail, dict) else {}
            pm_markets = pm_event.get("markets", [])
        candidates: list[dict[str, Any]] = []
        for event in kalshi_events:
            for k_market in event.get("markets", []):
                ticker = str(k_market.get("ticker", ""))
                k_title = str(k_market.get("title") or "")
                k_identity = identify_mlb_world_series_champion(
                    venue="kalshi", contract_id=ticker, season=season,
                    title=k_title, outcome_text=ticker,
                    resolution_rules=str(k_market.get("rules_primary") or "") or None,
                )
                if k_identity.identity_status != "identified":
                    continue
                for pm_market in pm_markets:
                    slug = str(pm_market.get("slug", ""))
                    pm_title = str(pm_market.get("title") or pm_market.get("question") or "")
                    pm_identity = identify_mlb_world_series_champion(
                        venue="polymarket-us", contract_id=slug, season=season,
                        title=pm_title, outcome_text=slug,
                        resolution_rules=str(pm_market.get("description") or "") or None,
                    )
                    identity_comparison = compare_contract_identities(k_identity, pm_identity)
                    if identity_comparison["semantic_match"] != "confirmed":
                        continue
                    try:
                        bbo_result = await asyncio.to_thread(polymarket.client.markets.bbo, slug)
                    except (PolymarketUSError, httpx.HTTPError, RuntimeError,
                            ValueError, KeyError, TypeError, OSError):
                        continue
                    bbo = bbo_result.get("marketData", {}) if isinstance(bbo_result, dict) else {}
                    k_bid = _number(k_market.get("yes_bid_dollars"))
                    k_ask = _number(k_market.get("yes_ask_dollars"))
                    p_bid = _number((bbo.get("bestBid") or {}).get("value"))
                    p_ask = _number((bbo.get("bestAsk") or {}).get("value"))
                    if None in (k_bid, k_ask, p_bid, p_ask):
                        continue
                    pm_rules = str(pm_market.get("description") or "")
                    kalshi_rules = str(k_market.get("rules_primary") or "")
                    observed_at = datetime.now(UTC).isoformat()
                    settlement_assessment = assess_settlement_equivalence(
                        SettlementTerms(
                            evidence_ref=f"kalshi:{ticker}:rules_primary",
                            evidence_sha256=k_identity.settlement_rule_sha256,
                            rule_version=(
                                f"rules-sha256:{k_identity.settlement_rule_sha256}"
                                if k_identity.settlement_rule_sha256 else None
                            ),
                            observed_at=observed_at,
                        ),
                        SettlementTerms(
                            evidence_ref=f"polymarket-us:{slug}:description",
                            evidence_sha256=pm_identity.settlement_rule_sha256,
                            rule_version=(
                                f"rules-sha256:{pm_identity.settlement_rule_sha256}"
                                if pm_identity.settlement_rule_sha256 else None
                            ),
                            observed_at=observed_at,
                        ),
                    )
                    economic_comparison = assess_economic_comparability(
                        EconomicQuote(
                            denomination="USD per binary contract",
                            bid=str(k_bid), ask=str(k_ask), quote_timestamp=observed_at,
                            evidence_ref=f"kalshi:{ticker}:market_quote", observed_at=observed_at,
                        ),
                        EconomicQuote(
                            denomination="USD per binary contract",
                            bid=str(p_bid), ask=str(p_ask), quote_timestamp=observed_at,
                            evidence_ref=f"polymarket-us:{slug}:bbo", observed_at=observed_at,
                        ),
                        settlement_status=settlement_assessment["status"],
                    )
                    identity_comparison["settlement_equivalence"] = settlement_assessment["status"]
                    identity_comparison["economic_comparability"] = economic_comparison["status"]
                    identity_comparison["levels"]["settlement_equivalence"] = settlement_assessment["status"]
                    identity_comparison["levels"]["economic_comparability"] = economic_comparison["status"]
                    identity_comparison["levels"]["executable_comparability"] = "unavailable"
                    candidates.append({
                        "observed_at": observed_at,
                        "status": "canonical_identity_match_settlement_unverified",
                        "team_code": k_identity.outcome_id,
                        "event": str(pm_event.get("title") or chosen_event.get("title")),
                        "year": season,
                        "canonical_identity": {
                            "comparison": identity_comparison,
                            "contracts": [k_identity.to_dict(), pm_identity.to_dict()],
                        },
                        "settlement_assessment": settlement_assessment,
                        "economic_comparability": economic_comparison,
                        "kalshi": {
                            "market_id": ticker,
                            "title": k_title,
                            "yes_bid": k_bid,
                            "yes_ask": k_ask,
                            "spread": round(k_ask - k_bid, 6),
                            "settlement_rule_available": bool(kalshi_rules),
                        },
                        "polymarket_us": {
                            "market_id": slug,
                            "title": pm_title,
                            "yes_bid": p_bid,
                            "yes_ask": p_ask,
                            "spread": round(p_ask - p_bid, 6),
                            "settlement_rule_available": bool(pm_rules),
                            "has_postponement_or_cancellation_clause": (
                                "postpon" in pm_rules.lower() or "cancel" in pm_rules.lower()
                            ),
                        },
                        "settlement_difference": (
                            "Polymarket US publishes postponement/cancellation fair-price contingencies; "
                            "Kalshi's primary rule text states the team-win resolution condition. "
                            "Full venue-rule equivalence is unverified, so execution value is not comparable yet."
                        ),
                        "unadjusted_yes_ask_difference": round(p_ask - k_ask, 6),
                        "executable_edge": None,
                        "reason": (
                            "The same team and championship outcome appear on both venues, but "
                            "venue-specific postponement/cancellation and settlement terms have "
                            "not been proven equivalent. Prices are descriptive, not an arbitrage claim."
                        ),
                    })
                    # One real overlap is sufficient to prove the comparison
                    # path. Avoid fetching dozens of candidate books every UI poll.
                    break
                if candidates:
                    break
            if candidates:
                break
        tesla_candidate = await _discover_tesla_threshold_candidate(kalshi, polymarket)
        if tesla_candidate is not None:
            candidates.append(tesla_candidate)
        if not candidates:
            return {"status": "no_current_candidate", "matches": [], "reason": "No evidence-backed overlapping proposition with current quotes was found on both venues."}
        candidates.sort(key=lambda item: abs(item["unadjusted_yes_ask_difference"]))
        return {
            "status": "candidate_only",
            "matches": candidates[:5],
            "reason": "Live canonical overlaps are visible; venue-rule equivalence, fees, and executable depth remain separate gates.",
            "path": "slow_identity_resolution",
        }
    except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError, OSError):
        return {"status": "unavailable", "matches": [], "reason": "Cross-venue candidate discovery failed safely."}
    finally:
        await kalshi.close()
        polymarket.close()


async def _discover_tesla_threshold_candidate(
    kalshi: KalshiVenue, polymarket: PolymarketUSVenue,
) -> dict[str, Any] | None:
    """Resolve the live Tesla Q3 delivery threshold only via strict source IDs."""
    try:
        k_response = await kalshi.client.get(
            "/events", params={"series_ticker": "KXTSLA", "status": "open",
                                "limit": 100, "with_nested_markets": "true"},
        )
        k_response.raise_for_status()
        k_events = k_response.json().get("events", [])
        search = await asyncio.to_thread(
            polymarket.client.search.query,
            {"query": "Tesla Q3 Total Deliveries"},
        )
        pm_event_ref = next((
            event for event in (search.get("events", []) if isinstance(search, dict) else [])
            if event.get("slug") == "tsla-dlvrs-2026-q3-above"
            and event.get("active") is True and event.get("closed") is not True
        ), None)
        if pm_event_ref is None:
            return None
        detail = await asyncio.to_thread(
            polymarket.client.events.retrieve_by_slug, pm_event_ref["slug"],
        )
        pm_event = detail.get("event", {}) if isinstance(detail, dict) else {}
        pm_markets = pm_event.get("markets", [])
        for event in k_events:
            if event.get("event_ticker") != "KXTSLA-26OCTDELIV":
                continue
            for k_market in event.get("markets", []):
                k_ticker = str(k_market.get("ticker") or "")
                k_identity = identify_tesla_q3_deliveries_threshold(
                    venue="kalshi", contract_id=k_ticker,
                    resolution_rules=str(k_market.get("rules_primary") or ""),
                )
                if k_identity.identity_status != "identified":
                    continue
                for pm_market in pm_markets:
                    slug = str(pm_market.get("slug") or "")
                    pm_identity = identify_tesla_q3_deliveries_threshold(
                        venue="polymarket-us", contract_id=slug,
                        resolution_rules=str(pm_market.get("description") or ""),
                    )
                    comparison = compare_contract_identities(k_identity, pm_identity)
                    if comparison["semantic_match"] != "confirmed":
                        continue
                    bbo_response = await asyncio.to_thread(polymarket.client.markets.bbo, slug)
                    bbo = bbo_response.get("marketData", {}) if isinstance(bbo_response, dict) else {}
                    if bbo.get("marketSlug") != slug:
                        continue
                    k_bid = _number(k_market.get("yes_bid_dollars"))
                    k_ask = _number(k_market.get("yes_ask_dollars"))
                    pm_bid = _number((bbo.get("bestBid") or {}).get("value"))
                    pm_ask = _number((bbo.get("bestAsk") or {}).get("value"))
                    if None in (k_bid, k_ask, pm_bid, pm_ask):
                        continue
                    observed_at = datetime.now(UTC).isoformat()
                    settlement = assess_settlement_equivalence(
                        SettlementTerms(
                            evidence_ref=f"kalshi:{k_ticker}:rules_primary",
                            evidence_sha256=k_identity.settlement_rule_sha256,
                            rule_version=(f"rules-sha256:{k_identity.settlement_rule_sha256}"
                                          if k_identity.settlement_rule_sha256 else None),
                            observed_at=observed_at,
                        ),
                        SettlementTerms(
                            evidence_ref=f"polymarket-us:{slug}:description",
                            evidence_sha256=pm_identity.settlement_rule_sha256,
                            rule_version=(f"rules-sha256:{pm_identity.settlement_rule_sha256}"
                                          if pm_identity.settlement_rule_sha256 else None),
                            observed_at=observed_at,
                        ),
                    )
                    economics = assess_economic_comparability(
                        EconomicQuote(
                            denomination="USD per binary contract", bid=str(k_bid), ask=str(k_ask),
                            quote_timestamp=observed_at,
                            evidence_ref=f"kalshi:{k_ticker}:market_quote", observed_at=observed_at,
                        ),
                        EconomicQuote(
                            denomination="USD per binary contract", bid=str(pm_bid), ask=str(pm_ask),
                            quote_timestamp=observed_at,
                            evidence_ref=f"polymarket-us:{slug}:bbo", observed_at=observed_at,
                        ), settlement_status=settlement["status"],
                    )
                    comparison["settlement_equivalence"] = settlement["status"]
                    comparison["economic_comparability"] = economics["status"]
                    comparison["levels"]["settlement_equivalence"] = settlement["status"]
                    comparison["levels"]["economic_comparability"] = economics["status"]
                    comparison["levels"]["executable_comparability"] = "unavailable"
                    return {
                        "observed_at": observed_at,
                        "status": "canonical_numeric_threshold_match_settlement_unverified",
                        "event": "Tesla Q3 2026 total deliveries",
                        "proposition_label": f"deliveries > {k_ticker.rsplit('-', 1)[-1]}",
                        "canonical_identity": {
                            "comparison": comparison,
                            "contracts": [k_identity.to_dict(), pm_identity.to_dict()],
                        },
                        "settlement_assessment": settlement,
                        "economic_comparability": economics,
                        "kalshi": {
                            "market_id": k_ticker, "title": str(k_market.get("title") or ""),
                            "yes_bid": k_bid, "yes_ask": k_ask,
                            "spread": round(k_ask - k_bid, 6),
                        },
                        "polymarket_us": {
                            "market_id": slug, "title": str(pm_market.get("title") or ""),
                            "yes_bid": pm_bid, "yes_ask": pm_ask,
                            "spread": round(pm_ask - pm_bid, 6),
                        },
                        "unadjusted_yes_ask_difference": round(pm_ask - k_ask, 6),
                        "executable_edge": None,
                        "reason": (
                            "Exact structured thresholds match across both active venue contracts; "
                            "publication, revisions, expiry, fees, and executable depth are not reconciled."
                        ),
                    }
        return None
    except (httpx.HTTPError, PolymarketUSError, RuntimeError, ValueError,
            KeyError, TypeError, OSError):
        return None


def _load_registered_quote_pair() -> list[dict[str, Any]] | None:
    ledger: ForecastLedger | None = None
    try:
        ledger = ForecastLedger(os.getenv("NOEMA_DB_PATH", "data/noema.db"))
        rows = ledger.conn.execute(
            """SELECT canonical_proposition_id, observation_json FROM canonical_pair_observations
               WHERE semantic_status='confirmed'
               ORDER BY id DESC LIMIT 100"""
        ).fetchall()
        registered: list[dict[str, Any]] = []
        seen: set[str] = set()
        for proposition_id, payload in rows:
            if proposition_id in seen:
                continue
            value = json.loads(payload)
            identities = value.get("canonical_identity", {}).get("contracts", [])
            venues = {item.get("venue") for item in identities if isinstance(item, dict)}
            if venues == {"kalshi", "polymarket-us"}:
                registered.append(value)
                seen.add(proposition_id)
        return registered or None
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return None
    finally:
        if ledger is not None:
            ledger.conn.close()


async def _refresh_registered_pair(previous: dict[str, Any]) -> dict[str, Any] | None:
    """Refresh quotes for a registered pair without title/rule identity work."""
    identities = previous.get("canonical_identity", {}).get("contracts", [])
    if len(identities) != 2:
        return None
    kalshi_identity = next((item for item in identities if item.get("venue") == "kalshi"), None)
    pm_identity = next((item for item in identities if item.get("venue") == "polymarket-us"), None)
    if not kalshi_identity or not pm_identity:
        return None
    kalshi = KalshiVenue(KalshiConfig(
        environment="production", allow_live_orders=False, master_halt=True,
    ))
    polymarket = PolymarketUSVenue()
    try:
        market_response, bbo_response = await asyncio.gather(
            kalshi.client.get(f"/markets/{kalshi_identity['contract_id']}"),
            asyncio.to_thread(polymarket.client.markets.bbo, pm_identity["contract_id"]),
        )
        market_response.raise_for_status()
        market_payload = market_response.json()
        k_market = market_payload.get("market", market_payload)
        bbo = bbo_response.get("marketData", {}) if isinstance(bbo_response, dict) else {}
        if bbo.get("marketSlug") != pm_identity["contract_id"]:
            return None
        k_bid, k_ask = _number(k_market.get("yes_bid_dollars")), _number(k_market.get("yes_ask_dollars"))
        p_bid = _number((bbo.get("bestBid") or {}).get("value"))
        p_ask = _number((bbo.get("bestAsk") or {}).get("value"))
        if None in (k_bid, k_ask, p_bid, p_ask):
            return None
        observed_at = datetime.now(UTC).isoformat()
        refreshed = dict(previous)
        refreshed.update(observed_at=observed_at, quote_path="fast_quote_refresh")
        refreshed["kalshi"] = {**previous.get("kalshi", {}), "yes_bid": k_bid,
                                "yes_ask": k_ask, "spread": round(k_ask-k_bid, 6)}
        refreshed["polymarket_us"] = {**previous.get("polymarket_us", {}), "yes_bid": p_bid,
                                      "yes_ask": p_ask, "spread": round(p_ask-p_bid, 6)}
        refreshed["unadjusted_yes_ask_difference"] = round(p_ask-k_ask, 6)
        # Update only quote observation fields. Venue quote source timestamps,
        # fee schedules, executable depth, and capital lock remain unknown.
        refreshed["economic_comparability"] = assess_economic_comparability(
            EconomicQuote(denomination="USD per binary contract", bid=str(k_bid), ask=str(k_ask),
                          quote_timestamp=observed_at, evidence_ref=f"kalshi:{kalshi_identity['contract_id']}:quote",
                          observed_at=observed_at),
            EconomicQuote(denomination="USD per binary contract", bid=str(p_bid), ask=str(p_ask),
                          quote_timestamp=observed_at, evidence_ref=f"polymarket-us:{pm_identity['contract_id']}:bbo",
                          observed_at=observed_at),
            settlement_status=refreshed["settlement_assessment"]["status"],
        )
        return refreshed
    except (httpx.HTTPError, PolymarketUSError, RuntimeError, ValueError,
            KeyError, TypeError, OSError):
        return None
    finally:
        await kalshi.close()
        polymarket.close()


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return float(number) if number.is_finite() else None


def _count_records(payload: Any, key: str) -> int | None:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        records = payload.get(key)
        if isinstance(records, list):
            return len(records)
        if isinstance(records, dict):
            return len(records)
        if key == "activities":
            for candidate in ("activity", "items", "data"):
                if isinstance(payload.get(candidate), list):
                    return len(payload[candidate])
    return None


async def build_prediction_venue_status(*, force: bool = False) -> dict[str, Any]:
    """Live read-only checks, cached briefly so Home cannot hammer venues."""
    now = monotonic()
    if not force and _cache["payload"] is not None and now - _cache["at"] < 60:
        return _cache["payload"]
    async with _lock:
        now = monotonic()
        if not force and _cache["payload"] is not None and now - _cache["at"] < 60:
            return _cache["payload"]
        kalshi, polymarket, comparison = await asyncio.gather(
            _kalshi_status(), _polymarket_us_status(), _cross_venue_candidate(),
        )
        if comparison.get("matches"):
            comparison["persistence_status"] = await asyncio.to_thread(
                _persist_canonical_observations, comparison["matches"],
            )
            if comparison["persistence_status"] in {"recorded", "already_recorded"}:
                global _registered_quote_pair, _registered_identity_review_at
                _registered_quote_pair = list(comparison["matches"])
                _registered_identity_review_at = monotonic()
        else:
            comparison["persistence_status"] = "no_candidate_to_record"
        live_resolver_keys: set[tuple[str, str]] = set()
        live_proposition_families: set[str] = set()
        for candidate in comparison.get("matches", []):
            contracts = candidate.get("canonical_identity", {}).get("contracts", [])
            if not contracts:
                continue
            source_identity = contracts[0]
            if source_identity.get("topic_id") == "sports:baseball":
                live_resolver_keys.add(("sports:baseball", "world_series_champion"))
                live_proposition_families.add("winner")
            elif source_identity.get("topic_id") == "company:automotive":
                live_resolver_keys.add(("company:automotive", "q3_deliveries_numeric_threshold"))
                live_proposition_families.add("numeric_threshold")
        live_resolvers = [
            f"{topic}/{family}" for topic, family in sorted(live_resolver_keys)
        ]
        family_coverage = {
            "winner": ("verified_live_overlap" if "winner" in live_proposition_families else "no_live_overlap_verified",
                       "MLB World Series champion is the only registered winner-family discovery probe."),
            "numeric_threshold": ("verified_live_overlap" if "numeric_threshold" in live_proposition_families else "no_live_overlap_verified",
                                  "Tesla Q3 total deliveries use strict Kalshi ticker, Polymarket slug, and rule-text mapping."),
            "occurrence_by_deadline": ("source_resolver_only", "Kalshi NYC daily precipitation is structurally identified; no exact Polymarket US counterpart is registered."),
            "asset_price_at_time": ("source_resolver_only", "Kalshi ETH timestamped price contract is structurally identified; no exact Polymarket US counterpart is registered."),
            "numeric_bucket": ("unresolved_ambiguity", "Bucket boundary inclusivity is not normalized; range rules using 'between' remain unresolved."),
            "economic_release_bucket": ("unresolved_unsupported", "No current source mapping proves a matching release, vintage, bucket boundaries, and resolution source."),
        }
        payload = {
            "as_of": datetime.now(UTC).isoformat(),
            "execution_enabled": False,
            "canonical_market_identity": {
                "ontology_levels": [
                    "topic", "event", "proposition", "outcome", "semantic_match",
                    "settlement_equivalence", "economic_comparability",
                    "executable_comparability",
                ],
                "registered_resolvers": [
                    {"topic_id": topic, "proposition_family": family}
                    for topic, family in registered_resolvers()
                ],
                "proposition_families": [
                    {
                        "family": family,
                        "resolver_status": "ontology_supported",
                        "production_adapter_status": family_coverage[family][0],
                        "coverage_reason": family_coverage[family][1],
                    }
                    for family in PROPOSITION_FAMILIES
                ],
                "coverage": {
                    "live_resolver_families": live_resolvers,
                    "verified_live_overlap_count": len(comparison.get("matches", [])),
                    "discovery_scope": "registered live overlap probes only; not a full-catalog scan",
                    "unresolved_families": [
                        {"family": family, "status": status, "reason": reason}
                        for family, (status, reason) in family_coverage.items()
                        if status in {"source_resolver_only", "unresolved_ambiguity", "unresolved_unsupported"}
                    ],
                    "unresolved_candidates": [] if comparison.get("matches") else [{
                        "status": comparison.get("status", "unresolved"),
                        "reason": comparison.get("reason", "No evidence-backed overlap is available."),
                    }],
                    "rejection_reasons": [] if comparison.get("matches") else [
                        comparison.get("reason", "No evidence-backed overlap is available.")
                    ],
                    "unresolved_reason": comparison.get("reason"),
                    "slow_path": "discovery and canonical identity assessment",
                    "fast_path": "registered identity links only; quote refresh must not rerun resolver",
                },
                "unsupported_families": "remain unresolved; no fuzzy identity promotion",
                "settlement_equivalence_default": "unverified",
                "execution_authority": "disabled",
            },
            "venues": [kalshi, polymarket],
            "cross_venue_comparison": comparison,
        }
        _cache.update(at=monotonic(), payload=payload)
        return payload


def _persist_canonical_observations(observations: list[dict[str, Any]]) -> str:
    ledger: ForecastLedger | None = None
    economic_ledger = None
    try:
        ledger = ForecastLedger(os.getenv("NOEMA_DB_PATH", "data/noema.db"))
        inserted_observations = [
            item for item in observations if ledger.append_canonical_pair_observation(item)
        ]
        inserted = len(inserted_observations)
        if inserted_observations:
            # Canonical observations enter the existing economy event stream as
            # non-monetary provenance. No parallel accounting store is created.
            from .economic_ledger import EconomicLedger

            economic_ledger = EconomicLedger(os.getenv("NOEMA_DB_PATH", "data/noema.db"))
            for item in inserted_observations:
                append_shared_economic_event(
                    economic_ledger, canonical_observation_event(item), amount_usd=None,
                )
        return "recorded" if inserted else "already_recorded"
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return "unavailable"
    finally:
        if ledger is not None:
            ledger.conn.close()
        if economic_ledger is not None:
            economic_ledger.conn.close()
