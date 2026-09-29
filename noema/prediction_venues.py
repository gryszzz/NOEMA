"""Read-only live health and public market data for prediction venues."""

from __future__ import annotations

import asyncio
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
        if not kalshi_events:
            return {"status": "no_current_candidate", "matches": [], "reason": "No active Kalshi MLB champion event."}

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
        if chosen_event is None:
            return {"status": "no_current_candidate", "matches": [], "reason": "No active Polymarket US World Series champion event."}
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
                    candidates.append({
                        "observed_at": datetime.now(UTC).isoformat(),
                        "status": "canonical_identity_match_settlement_unverified",
                        "team_code": k_identity.outcome_id,
                        "event": str(pm_event.get("title") or chosen_event.get("title")),
                        "year": season,
                        "canonical_identity": {
                            "comparison": identity_comparison,
                            "contracts": [k_identity.to_dict(), pm_identity.to_dict()],
                        },
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
        if not candidates:
            return {"status": "no_current_candidate", "matches": [], "reason": "No same-outcome contract with current quotes was found on both venues."}
        candidates.sort(key=lambda item: abs(item["unadjusted_yes_ask_difference"]))
        return {
            "status": "candidate_only",
            "matches": candidates[:1],
            "reason": "A real overlapping outcome is visible; rules and fees prevent treating the quote difference as executable edge.",
        }
    except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError, OSError):
        return {"status": "unavailable", "matches": [], "reason": "Cross-venue candidate discovery failed safely."}
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
        else:
            comparison["persistence_status"] = "no_candidate_to_record"
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
    try:
        ledger = ForecastLedger(os.getenv("NOEMA_DB_PATH", "data/noema.db"))
        inserted = sum(ledger.append_canonical_pair_observation(item) for item in observations)
        return "recorded" if inserted else "already_recorded"
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return "unavailable"
    finally:
        if ledger is not None:
            ledger.conn.close()
