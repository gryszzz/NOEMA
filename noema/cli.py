from __future__ import annotations

import argparse
import asyncio
import json

from .account import KalshiAccount
from .config import KalshiConfig
from .diagnostics import diagnostic_dict
from .kalshi_telemetry import KalshiTelemetry
from .outcomes import OutcomeStore
from .soak import SoakStore
from .soak_report import build_soak_quality_report
from .soak_runner import collect_market_snapshot_batch, run_soak_loop
from .sync import sync_kalshi_outcomes
from .telemetry_report import build_telemetry_report
from .venues.kalshi import KalshiVenue
from .venues.kalshi_history import KalshiHistory
from .venues.kalshi_stream import KalshiStream


async def _markets(limit: int) -> None:
    venue = KalshiVenue()
    count = 0
    try:
        async for market in venue.markets():
            print(
                json.dumps(
                    {
                        "ticker": market.market_id,
                        "title": market.title,
                        "yes_bid": market.yes_bid,
                        "yes_ask": market.yes_ask,
                        "liquidity_usd": market.liquidity_usd,
                        "closes_at": str(market.closes_at) if market.closes_at else None,
                    },
                    sort_keys=True,
                )
            )
            count += 1
            if count >= limit:
                break
    finally:
        await venue.close()


async def _stream(tickers: list[str]) -> None:
    stream = KalshiStream(KalshiConfig.from_env())
    async for message in stream.messages(
        channels=["ticker", "trade", "orderbook_delta"],
        market_tickers=tickers or None,
    ):
        print(json.dumps(message.payload, sort_keys=True))


def _evaluate(db: str) -> None:
    summary = OutcomeStore(db).evaluate_ledger()
    print(json.dumps(summary.__dict__, sort_keys=True))


async def _account() -> None:
    account = KalshiAccount()
    try:
        snapshot = await account.snapshot()
        print(
            json.dumps(
                {
                    "balance": snapshot.balance,
                    "positions": snapshot.positions,
                    "orders": snapshot.orders,
                    "fills": snapshot.fills,
                    "limits": snapshot.limits,
                    "user_data_as_of": (
                        snapshot.user_data_as_of.isoformat()
                        if snapshot.user_data_as_of
                        else None
                    ),
                },
                sort_keys=True,
                default=str,
            )
        )
    finally:
        await account.close()


async def _telemetry() -> None:
    telemetry = KalshiTelemetry()
    try:
        orders, fills, positions = await asyncio.gather(
            telemetry.orders(),
            telemetry.fills(),
            telemetry.positions(),
        )
        print(
            json.dumps(
                build_telemetry_report(
                    orders=orders,
                    fills=fills,
                    positions=positions,
                ),
                sort_keys=True,
                default=str,
            )
        )
    finally:
        await telemetry.close()


async def _soak_once(db: str, limit: int | None) -> None:
    store = SoakStore(db)
    venue = KalshiVenue()
    try:
        result = await collect_market_snapshot_batch(
            venue,
            store,
            max_markets=limit,
        )
        print(json.dumps(result.__dict__, sort_keys=True))
    finally:
        await venue.close()


async def _soak_loop(db: str, interval: float, limit: int | None) -> None:
    store = SoakStore(db)
    venue = KalshiVenue()
    try:
        await run_soak_loop(
            venue,
            store,
            interval_seconds=interval,
            max_markets=limit,
        )
    finally:
        await venue.close()


def _soak_report(db: str) -> None:
    report = build_soak_quality_report(db)
    print(json.dumps(report.__dict__, sort_keys=True))


async def _sync_outcomes(db: str, limit: int | None) -> None:
    store = OutcomeStore(db)
    history = KalshiHistory()
    try:
        result = await sync_kalshi_outcomes(store, history, max_markets=limit)
        print(json.dumps(result.__dict__, sort_keys=True))
    finally:
        await history.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="noema")
    sub = parser.add_subparsers(dest="command", required=True)

    markets = sub.add_parser("markets")
    markets.add_argument("--limit", type=int, default=20)

    stream = sub.add_parser("stream")
    stream.add_argument("tickers", nargs="*")

    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--db", default="data/noema.db")

    sub.add_parser("account")
    sub.add_parser("check-config")
    sub.add_parser("telemetry")

    sync = sub.add_parser("sync-outcomes")
    sync.add_argument("--db", default="data/noema.db")
    sync.add_argument("--limit", type=int, default=None)

    soak_once = sub.add_parser("soak-once")
    soak_once.add_argument("--db", default="data/noema.db")
    soak_once.add_argument("--limit", type=int, default=None)

    soak_loop = sub.add_parser("soak-loop")
    soak_loop.add_argument("--db", default="data/noema.db")
    soak_loop.add_argument("--interval", type=float, default=60.0)
    soak_loop.add_argument("--limit", type=int, default=None)

    soak_report = sub.add_parser("soak-report")
    soak_report.add_argument("--db", default="data/noema.db")

    args = parser.parse_args()
    if args.command == "markets":
        asyncio.run(_markets(args.limit))
    elif args.command == "stream":
        asyncio.run(_stream(args.tickers))
    elif args.command == "sync-outcomes":
        asyncio.run(_sync_outcomes(args.db, args.limit))
    elif args.command == "soak-once":
        asyncio.run(_soak_once(args.db, args.limit))
    elif args.command == "soak-loop":
        asyncio.run(_soak_loop(args.db, args.interval, args.limit))
    elif args.command == "soak-report":
        _soak_report(args.db)
    elif args.command == "account":
        asyncio.run(_account())
    elif args.command == "telemetry":
        asyncio.run(_telemetry())
    elif args.command == "check-config":
        print(json.dumps(diagnostic_dict(), sort_keys=True))
    else:
        _evaluate(args.db)


if __name__ == "__main__":
    main()
