"""Replay resolved public markets and pool candles through the paper decision rules.

No signer, no API key, and no price from after the forecast timestamp. A missing
pre-horizon quote stays missing. Terminal settlement prices are not used as the
market price.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from .accuracy_config import AccuracyConfig
from .accuracy_influence import influence_for
from .accuracy_ledger import ensure_accuracy_schema, record_forecast, resolve_open_forecasts
from .accuracy_scoring import score_agents
from .accuracy_text import anchor_probability, no_edge_skip
from .evaluation import expected_calibration_error
from .sqlite_util import connect as sqlite_connect
from .trench_risk import evaluate_desk_entry, spike_acceleration

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"
CLOB_HISTORY = "https://clob.polymarket.com/prices-history"
POLYMARKET_US_LIST = "https://gateway.polymarket.us/v1/markets"
POLYMARKET_US_HISTORY = "https://api.polymarket.us/v1/markets/{slug}/prices-history"
HEADERS = {"User-Agent": "NOEMA-research/0.1", "accept": "application/json"}
KALSHI_SERIES = "KXRAINNYC"


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def _book_price(level: Any) -> float | None:
    if not isinstance(level, dict):
        return None
    for key in ("close_dollars", "close"):
        number = _number(level.get(key))
        if number is not None:
            return number
    return None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def preclose_quote(
    candles: list[dict[str, Any]],
    *,
    close_ts: float,
    horizon_seconds: int,
) -> dict[str, float] | None:
    """Latest bid/ask at or before close minus the horizon. Settlement books are ignored."""
    cutoff = close_ts - horizon_seconds
    chosen: dict[str, float] | None = None
    for candle in candles:
        end = _number(candle.get("end_period_ts"))
        if end is None or end > cutoff:
            continue
        bid = _book_price(candle.get("yes_bid"))
        ask = _book_price(candle.get("yes_ask"))
        if bid is None or ask is None or not 0 <= bid <= ask <= 1:
            continue
        if bid <= 0.0 and ask >= 0.99:
            continue
        mid = (bid + ask) / 2.0
        if not 0 < mid < 1:
            continue
        chosen = {"at": end, "bid": bid, "ask": ask, "mid": mid}
    return chosen


def preclose_history_price(
    history: list[dict[str, Any]],
    *,
    closed_ts: float,
    horizon_seconds: int,
) -> dict[str, float] | None:
    cutoff = closed_ts - horizon_seconds
    chosen: dict[str, float] | None = None
    for point in history:
        ts = _number(point.get("t"))
        price = _number(point.get("p"))
        if ts is None or price is None or ts > cutoff or not 0 < price < 1:
            continue
        chosen = {"at": ts, "mid": price}
    return chosen


def _frequency(prior_yes: int, prior_n: int) -> tuple[float, float]:
    probability = (prior_yes + 1) / (prior_n + 2)
    variance = probability * (1.0 - probability) / (prior_n + 3)
    half = max(0.10, 1.96 * variance**0.5)
    return probability, half


def replay_binary_markets(
    conn: sqlite3.Connection,
    markets: list[dict[str, Any]],
    *,
    family: str,
    config: AccuracyConfig,
    min_prior_events: int = 30,
    frequency: bool = True,
) -> dict[str, int]:
    """Walk forward. Frequency uses only outcomes already settled before the forecast."""
    ensure_accuracy_schema(conn)
    ordered = sorted(markets, key=lambda item: item["forecast_at"])
    prior: list[tuple[float, int]] = []
    counts = {
        "midpoint": 0,
        "frequency": 0,
        "frequency_rejected": 0,
        "chief_shadow": 0,
        "chief_take": 0,
        "chief_skip": 0,
    }
    resolved_freq: list[tuple[float, int]] = []
    for market in ordered:
        forecast_at = float(market["forecast_at"])
        settled = float(market["settled_at"])
        if forecast_at >= settled:
            continue
        outcome = int(market["outcome"])
        if outcome not in {0, 1}:
            continue
        price = float(market["market_price"])
        venue = str(market["venue"])
        market_id = str(market["market_id"])
        when = datetime.fromtimestamp(forecast_at, UTC)
        resolved_at = datetime.fromtimestamp(settled, UTC).isoformat()
        record_forecast(
            conn,
            agent_id=f"{family}:market-midpoint-v1",
            venue=venue,
            market_id=market_id,
            predicted_probability=price,
            confidence=None,
            market_price=price,
            reasoning={
                "model": "market-midpoint-v1",
                "bid": market.get("bid"),
                "ask": market.get("ask"),
            },
            recorded_at=when,
            decision="benchmark",
            horizon=f"{config.backtest_horizon_seconds}s",
            metadata={"kind": "binary"},
        )
        resolve_open_forecasts(
            conn,
            venue=venue,
            market_id=market_id,
            outcome_yes=outcome,
            resolved_at=resolved_at,
            source="backtest",
            config=config,
        )
        counts["midpoint"] += 1
        if not frequency:
            prior.append((settled, outcome))
            continue
        usable = [(ts, yes) for ts, yes in prior if ts < forecast_at]
        if len(usable) < min_prior_events:
            counts["frequency_rejected"] += 1
            prior.append((settled, outcome))
            continue
        yes = sum(item[1] for item in usable)
        probability, half = _frequency(yes, len(usable))
        anchored = anchor_probability(probability, price, config.base_rate_strength)
        cost = config.binary_slippage + config.binary_fee
        decision = (
            "no_edge_skip"
            if no_edge_skip(
                anchored,
                price,
                min_edge=config.min_edge,
                cost=cost,
            )
            else "paper_candidate"
        )
        record_forecast(
            conn,
            agent_id=f"{family}:series-frequency-v1",
            venue=venue,
            market_id=market_id,
            predicted_probability=anchored,
            confidence=max(0.0, min(1.0, 1.0 - 2 * half)),
            market_price=price,
            reasoning={
                "model": "series-frequency-v1",
                "prior_n": len(usable),
                "prior_yes": yes,
                "unanchored": probability,
            },
            recorded_at=when,
            decision=decision,
            horizon=f"{config.backtest_horizon_seconds}s",
            metadata={"kind": "binary", "robust_edge": anchored - price - cost},
        )
        running = {
            "sample_size": len(resolved_freq),
            "brier": None
            if not resolved_freq
            else sum((p - y) ** 2 for p, y in resolved_freq) / len(resolved_freq),
            "calibration_error": (
                expected_calibration_error(resolved_freq) if resolved_freq else None
            ),
        }
        info = influence_for(
            f"{family}:series-frequency-v1",
            running if resolved_freq else None,
            config,
        )
        shrunk = info.weight * anchored + (1.0 - info.weight) * price
        if info.weight <= 0 or info.benched:
            chief_decision = "shadow"
        elif no_edge_skip(shrunk, price, min_edge=config.min_edge, cost=cost):
            chief_decision = "no_edge_skip"
        else:
            chief_decision = "paper_candidate"
        record_forecast(
            conn,
            agent_id="chief",
            venue=venue,
            market_id=market_id,
            predicted_probability=shrunk,
            confidence=info.weight,
            market_price=price,
            reasoning={"source": f"{family}:series-frequency-v1", "weight": info.weight},
            recorded_at=when,
            decision=chief_decision,
            horizon=f"{config.backtest_horizon_seconds}s",
            metadata={"kind": "binary", "benched": info.benched},
        )
        resolve_open_forecasts(
            conn,
            venue=venue,
            market_id=market_id,
            outcome_yes=outcome,
            resolved_at=resolved_at,
            source="backtest",
            config=config,
        )
        resolved_freq.append((anchored, outcome))
        counts["frequency"] += 1
        if chief_decision == "shadow":
            counts["chief_shadow"] += 1
        elif chief_decision == "paper_candidate":
            counts["chief_take"] += 1
        else:
            counts["chief_skip"] += 1
        prior.append((settled, outcome))
    conn.commit()
    return counts


def replay_pool_ohlcv(
    candles: list[list[float]],
    *,
    mint: str,
    symbol: str,
    config: AccuracyConfig | None = None,
) -> list[dict[str, Any]]:
    """Price-rule replay. Each decision sees only candles at or before its index.

    Liquidity and holder concentration are not in OHLCV, so the live entry gate
    is reported as not passed. The calm-score forecast is research-only.
    """
    config = config or AccuracyConfig()
    ordered = sorted(candles, key=lambda row: row[0])
    horizon = config.ohlcv_horizon_bars
    if horizon < 1:
        raise ValueError("ohlcv horizon must be positive")
    closes = [float(row[4]) for row in ordered]
    forecasts: list[dict[str, Any]] = []
    for index in range(horizon, len(closes) - horizon):
        window = closes[: index + 1]
        if min(window[index], window[index - 1], window[index - horizon]) <= 0:
            continue
        change_m5 = (window[index] - window[index - 1]) / window[index - 1] * 100.0
        change_h1 = (window[index] - window[index - horizon]) / window[index - horizon] * 100.0
        accel = spike_acceleration(change_m5, change_h1)
        _passed, reasons, _metrics = evaluate_desk_entry(
            liquidity_usd=0.0,
            pool_age_seconds=None,
            holder_shares=None,
            price_change_m5=change_m5,
            price_change_h1=change_h1,
        )
        calm = 1.0 - min(1.0, abs(change_m5) / 15.0)
        # Only windows whose outcome bar is already in `window` (ends at or before index).
        prior = [
            1 if closes[end] > closes[start] else 0
            for start in range(horizon, index - horizon + 1)
            for end in (start + horizon,)
        ]
        prior_n = len(prior)
        base = config.base_rate if prior_n == 0 else (sum(prior) + 1) / (prior_n + 2)
        anchored = anchor_probability(calm, base, config.base_rate_strength)
        spiked = any(reason.startswith("mid-spike") for reason in reasons)
        decision = (
            "skip"
            if spiked
            or no_edge_skip(
                anchored,
                base,
                min_edge=config.min_edge,
            )
            else "research_shadow"
        )
        future = closes[index + horizon]
        outcome = 1 if future > window[index] else 0
        forecasts.append(
            {
                "mint": mint,
                "symbol": symbol,
                "recorded_at": datetime.fromtimestamp(float(ordered[index][0]), UTC),
                "probability": anchored,
                "market_price": window[index],
                "decision": decision,
                "outcome": outcome,
                "exit_price": future,
                "live_gate_passed": False,
                "live_gate_reasons": list(reasons),
                "base_rate": base,
                "spike_acceleration": accel,
            }
        )
    return forecasts


def store_ohlcv_replay(
    conn: sqlite3.Connection,
    forecasts: list[dict[str, Any]],
    *,
    config: AccuracyConfig,
) -> int:
    from .accuracy_ledger import token_ev
    from .trench_desks import CostModel

    ensure_accuracy_schema(conn)
    costs = CostModel()
    written = 0
    for item in forecasts:
        forecast_id = record_forecast(
            conn,
            agent_id="trench_rules:ohlcv_shadow",
            venue="solana",
            market_id=item["mint"],
            symbol=item["symbol"],
            predicted_probability=item["probability"],
            confidence=None,
            market_price=item["market_price"],
            reasoning={
                "rule": "calm_score",
                "base_rate": item["base_rate"],
                "live_gate_passed": False,
                "reasons": item["live_gate_reasons"],
            },
            recorded_at=item["recorded_at"],
            decision=item["decision"],
            horizon="1h",
            metadata={
                "kind": "token_up",
                "exit_price": item["exit_price"],
                "eligible_for_influence": False,
                "price_impact": "not_charged_depth_unknown",
            },
        )
        ev = (
            0.0
            if item["decision"] != "research_shadow"
            else token_ev(
                item["market_price"],
                item["exit_price"],
                fee_bps=costs.swap_fee_bps,
                slippage_bps=costs.base_slippage_bps,
            )
        )
        from .accuracy_ledger import _write_resolution

        _write_resolution(
            conn,
            forecast_id=forecast_id,
            agent_id="trench_rules:ohlcv_shadow",
            market_id=item["mint"],
            probability=item["probability"],
            market_price=item["market_price"],
            decision=item["decision"]
            if item["decision"] != "research_shadow"
            else "paper_candidate",
            outcome=item["outcome"],
            resolved_at=(
                item["recorded_at"] + timedelta(seconds=config.trench_horizon_seconds)
            ).isoformat(),
            source="ohlcv_replay",
            edge=item["probability"] - item["base_rate"],
            ev_after_cost=ev,
            db_path=None,
        )
        written += 1
    conn.commit()
    return written


def _cache_get(client: httpx.Client, url: str, cache_dir: Path) -> Any:
    digest = hashlib.sha256(url.encode()).hexdigest()
    path = cache_dir / f"{digest}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    response = client.get(url, headers=HEADERS)
    response.raise_for_status()
    payload = response.json()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _kalshi_markets(client: httpx.Client, cache_dir: Path, limit: int) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    cursor: str | None = None
    while len(markets) < limit:
        url = f"{KALSHI_BASE}/historical/markets?limit=100&series_ticker={KALSHI_SERIES}"
        if cursor:
            url += f"&cursor={cursor}"
        payload = _cache_get(client, url, cache_dir)
        page = payload.get("markets") if isinstance(payload, dict) else None
        if not isinstance(page, list):
            break
        markets.extend(item for item in page if isinstance(item, dict))
        cursor = payload.get("cursor") if isinstance(payload, dict) else None
        if not cursor:
            break
    return markets[:limit]


def _attach_kalshi_quotes(
    client: httpx.Client,
    cache_dir: Path,
    markets: list[dict[str, Any]],
    config: AccuracyConfig,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    priced: list[dict[str, Any]] = []
    skipped = 0
    for market in markets:
        result = str(market.get("result") or "").lower()
        if result not in {"yes", "no"}:
            skipped += 1
            continue
        close_raw = market.get("close_time")
        ticker = market.get("ticker")
        if not isinstance(close_raw, str) or not isinstance(ticker, str):
            skipped += 1
            continue
        try:
            close = _parse_ts(close_raw)
        except ValueError:
            skipped += 1
            continue
        start = int((close - timedelta(days=5)).timestamp())
        end = int(close.timestamp())
        url = (
            f"{KALSHI_BASE}/historical/markets/{ticker}/candlesticks"
            f"?start_ts={start}&end_ts={end}&period_interval=60"
        )
        try:
            payload = _cache_get(client, url, cache_dir)
        except httpx.HTTPError:
            skipped += 1
            continue
        candles = payload.get("candlesticks") if isinstance(payload, dict) else None
        if not isinstance(candles, list):
            skipped += 1
            continue
        quote = preclose_quote(
            [item for item in candles if isinstance(item, dict)],
            close_ts=close.timestamp(),
            horizon_seconds=config.backtest_horizon_seconds,
        )
        settled_raw = market.get("settlement_ts") or close_raw
        try:
            settled = _parse_ts(str(settled_raw)).timestamp()
        except ValueError:
            settled = close.timestamp()
        if quote is None or quote["at"] >= settled:
            skipped += 1
            continue
        priced.append(
            {
                "venue": "kalshi:production",
                "market_id": ticker,
                "forecast_at": quote["at"],
                "settled_at": settled,
                "market_price": quote["mid"],
                "bid": quote["bid"],
                "ask": quote["ask"],
                "outcome": 1 if result == "yes" else 0,
            }
        )
    return priced, {"skipped_no_preclose_price": skipped, "quoted": len(priced)}


def _event_sizes(markets: list[dict[str, Any]]) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for market in markets:
        event = str(market.get("event_ticker") or market.get("event") or "")
        if event:
            sizes[event] = sizes.get(event, 0) + 1
    return sizes


def _polymarket_markets(
    client: httpx.Client,
    cache_dir: Path,
    limit: int,
    config: AccuracyConfig,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    url = f"{GAMMA_MARKETS}?closed=true&limit={limit}&order=volume24hr&ascending=false"
    payload = _cache_get(client, url, cache_dir)
    if not isinstance(payload, list):
        raise TypeError("polymarket market list was not a list")
    priced: list[dict[str, Any]] = []
    skipped = 0
    for market in payload:
        if not isinstance(market, dict):
            skipped += 1
            continue
        try:
            prices = json.loads(market.get("outcomePrices") or "[]")
            tokens = json.loads(market.get("clobTokenIds") or "[]")
            closed = _parse_ts(str(market.get("closedTime")).replace(" ", "T"))
        except (TypeError, ValueError, KeyError, IndexError):
            skipped += 1
            continue
        if not tokens or not prices or str(prices[0]) not in {"0", "1"}:
            skipped += 1
            continue
        end = int(closed.timestamp())
        start = end - 3 * 86400
        history_url = f"{CLOB_HISTORY}?market={tokens[0]}&startTs={start}&endTs={end}&fidelity=60"
        try:
            history_payload = _cache_get(client, history_url, cache_dir)
        except httpx.HTTPError:
            skipped += 1
            continue
        history = history_payload.get("history") if isinstance(history_payload, dict) else None
        if not isinstance(history, list):
            skipped += 1
            continue
        quote = preclose_history_price(
            [item for item in history if isinstance(item, dict)],
            closed_ts=closed.timestamp(),
            horizon_seconds=config.backtest_horizon_seconds,
        )
        if quote is None:
            skipped += 1
            continue
        priced.append(
            {
                "venue": "polymarket",
                "market_id": str(market.get("slug") or market.get("id")),
                "forecast_at": quote["at"],
                "settled_at": closed.timestamp(),
                "market_price": quote["mid"],
                "outcome": 1 if str(prices[0]) == "1" else 0,
            }
        )
    return priced, {"skipped_no_preclose_price": skipped, "quoted": len(priced)}


def _polymarket_us_status(client: httpx.Client) -> dict[str, Any]:
    """Confirm the public list, and do not score terminal prices when history is closed."""
    note: dict[str, Any] = {"list_status": None, "history_status": None, "scored": 0}
    try:
        listed = client.get(f"{POLYMARKET_US_LIST}?closed=true&limit=1", headers=HEADERS)
        note["list_status"] = listed.status_code
        slug = None
        if listed.status_code == 200:
            body = listed.json()
            markets = body.get("markets") if isinstance(body, dict) else None
            if isinstance(markets, list) and markets:
                slug = markets[0].get("slug")
        if slug:
            history = client.get(POLYMARKET_US_HISTORY.format(slug=slug), headers=HEADERS)
            note["history_status"] = history.status_code
        note["status"] = "unavailable" if note.get("history_status") != 200 else "ok"
        if note["status"] != "ok":
            note["reason"] = (
                "Polymarket US price history is not public "
                f"(HTTP {note.get('history_status')}). Resolved outcome prices were not used."
            )
    except httpx.HTTPError as exc:
        note["status"] = "unavailable"
        note["reason"] = type(exc).__name__
    return note


async def _solana_ohlcv(limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from .solana_research import GeckoTerminalPoolClient

    client = GeckoTerminalPoolClient()
    summary: dict[str, Any] = {"pools": 0, "forecasts": 0, "live_gate_passes": 0}
    forecasts: list[dict[str, Any]] = []
    try:
        pools = await client.trending_pools()
    except Exception as exc:  # noqa: BLE001 - public source degrades without inventing bars
        summary["status"] = "unavailable"
        summary["reason"] = type(exc).__name__
        return forecasts, summary
    summary["pools_seen"] = len(pools)
    used = 0
    for pool in pools:
        if used >= limit or not pool.pool_address:
            continue
        try:
            candles = await client.pool_ohlcv(pool.pool_address, limit=100)
        except Exception as exc:  # noqa: BLE001
            summary.setdefault("pool_errors", []).append(type(exc).__name__)
            continue
        replayed = replay_pool_ohlcv(candles, mint=pool.mint, symbol=pool.symbol)
        forecasts.extend(replayed)
        used += 1
    summary["pools"] = used
    summary["forecasts"] = len(forecasts)
    summary["status"] = "ok" if used else "unavailable"
    summary["note"] = (
        "Candles have no holder concentration or historical liquidity, so the live "
        "entry gate does not pass. The calm-score rows are research shadow forecasts."
    )
    return forecasts, summary


async def run_accuracy_backtest(
    db_path: str,
    cache_dir: str,
    *,
    config: AccuracyConfig | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Fetch public history, replay it, and return scores from the accuracy ledger."""
    config = config or AccuracyConfig.from_env()
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    db = Path(db_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    owns_client = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(30.0), headers=HEADERS)
    report: dict[str, Any] = {
        "as_of": datetime.now(UTC).isoformat(),
        "paper_only": True,
        "lookahead": (
            "Quotes are the last book or trade at least one horizon before close. "
            "Series frequency counts only outcomes whose settlement time is earlier "
            "than the forecast. OHLCV features use closes at or before the decision bar."
        ),
        "config": config.public_dict(),
        "sources": {},
    }
    conn = sqlite_connect(db)
    try:
        try:
            raw = _kalshi_markets(client, cache, config.backtest_kalshi_limit)
            sizes = _event_sizes(raw)
            homogeneous = all(size == 1 for size in sizes.values()) and len(sizes) >= 1
            priced, kalshi_counts = _attach_kalshi_quotes(client, cache, raw, config)
            counts = replay_binary_markets(
                conn,
                priced,
                family="kalshi",
                config=config,
                frequency=homogeneous,
            )
            report["sources"]["kalshi"] = {
                "status": "ok",
                "series": KALSHI_SERIES,
                "fetched": len(raw),
                "events": len(sizes),
                "homogeneous_single_market": homogeneous,
                **kalshi_counts,
                **counts,
            }
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            report["sources"]["kalshi"] = {"status": "unavailable", "reason": type(exc).__name__}
        try:
            priced, poly_counts = _polymarket_markets(
                client,
                cache,
                config.backtest_polymarket_limit,
                config,
            )
            counts = replay_binary_markets(
                conn,
                priced,
                family="polymarket",
                config=config,
                frequency=False,
            )
            report["sources"]["polymarket"] = {"status": "ok", **poly_counts, **counts}
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            report["sources"]["polymarket"] = {
                "status": "unavailable",
                "reason": type(exc).__name__,
            }
        report["sources"]["polymarket_us"] = _polymarket_us_status(client)
        if config.backtest_pools <= 0:
            report["sources"]["solana_ohlcv"] = {"status": "skipped", "pools": 0, "stored": 0}
        else:
            try:
                ohlcv, ohlcv_summary = await _solana_ohlcv(config.backtest_pools)
                stored = store_ohlcv_replay(conn, ohlcv, config=config) if ohlcv else 0
                report["sources"]["solana_ohlcv"] = {**ohlcv_summary, "stored": stored}
            except (httpx.HTTPError, ValueError, TypeError, OSError) as exc:
                report["sources"]["solana_ohlcv"] = {
                    "status": "unavailable",
                    "reason": type(exc).__name__,
                }
        report["notes"] = [
            (
                "Rolling windows compare forecast timestamps with wall-clock now. "
                "A historical replay's baseline is the all-time score; its 7-day and "
                "30-day windows stay empty when every forecast is older than that."
            )
        ]
        agents = score_agents(conn, config=config)
        enriched = []
        for metrics in agents:
            info = influence_for(metrics["agent_id"], metrics, config)
            enriched.append({**metrics, **info.as_dict()})
        report["leaderboard"] = enriched
    finally:
        conn.close()
        if owns_client:
            client.close()
    return report
