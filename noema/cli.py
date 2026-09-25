from __future__ import annotations

import argparse
import asyncio
import json

from .config import KalshiConfig
from .outcomes import OutcomeStore
from .sync import sync_kalshi_outcomes
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

    sync = sub.add_parser("sync-outcomes")
    sync.add_argument("--db", default="data/noema.db")
    sync.add_argument("--limit", type=int, default=None)

    args = parser.parse_args()
    if args.command == "markets":
        asyncio.run(_markets(args.limit))
    elif args.command == "stream":
        asyncio.run(_stream(args.tickers))
    elif args.command == "sync-outcomes":
        asyncio.run(_sync_outcomes(args.db, args.limit))
    else:
        _evaluate(args.db)


if __name__ == "__main__":
    main()
