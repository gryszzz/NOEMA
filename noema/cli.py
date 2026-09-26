from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from decimal import Decimal

import httpx

from .account import KalshiAccount
from .agent_config import AgentConfig
from .agent_runtime import run_cycle
from .bill_tracker import BillTracker
from .config import KalshiConfig
from .diagnostics import diagnostic_dict
from .doctor import doctor_report
from .economic_bootstrap import bootstrap_economy
from .economic_dashboard import build_economic_overview
from .economic_ledger import EconomicLedger
from .ecosystem_dashboard import build_ecosystem_overview
from .kalshi_telemetry import KalshiTelemetry
from .ladder import build_ladder_report
from .local_env import load_local_env
from .outcomes import OutcomeStore
from .paired_evaluation import compare_history_to_market
from .paper_research import PaperResearchStore, collect_paper_quote
from .setup_wizard import run_setup_wizard
from .soak import SoakStore
from .soak_report import build_soak_quality_report
from .soak_runner import collect_market_snapshot_batch, run_soak_loop
from .specialist_model import audit_database
from .sync import sync_kalshi_outcomes
from .telemetry_report import build_telemetry_report
from .venues.kalshi import KalshiVenue
from .venues.kalshi_history import KalshiHistory
from .venues.kalshi_stream import KalshiStream


async def _agent_once(db: str) -> None:
    config = AgentConfig.from_env()
    config = AgentConfig(
        db_path=db,
        cycle_interval_seconds=config.cycle_interval_seconds,
        heartbeat_interval_seconds=config.heartbeat_interval_seconds,
        max_radar_rows=config.max_radar_rows,
        max_markets_per_cycle=config.max_markets_per_cycle,
        max_event_checks_per_cycle=config.max_event_checks_per_cycle,
        outcome_sync_interval_seconds=config.outcome_sync_interval_seconds,
        max_outcomes_per_sync=config.max_outcomes_per_sync,
        evm_rpc_url=config.evm_rpc_url,
        evm_address=config.evm_address,
    )
    config.validate()
    status = await run_cycle(cycle_id=1, config=config, runtime_running=False)
    print(json.dumps(asdict(status), sort_keys=True, default=str))


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


def _economy_init(db: str, capital: Decimal) -> None:
    ledger = EconomicLedger(db)
    if ledger.latest_snapshot() is not None:
        raise RuntimeError("economic ledger is already initialized")
    snapshot = bootstrap_economy(capital)
    ledger.append_snapshot(snapshot)
    print(json.dumps({key: str(value) for key, value in asdict(snapshot).items()}, sort_keys=True))


def _economy_show(db: str) -> None:
    print(json.dumps(build_economic_overview(db), sort_keys=True))


def _ecosystem_show(db: str) -> None:
    print(json.dumps(build_ecosystem_overview(db), sort_keys=True, default=str))


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


async def _paper_quote(db: str, ticker: str, contracts: Decimal) -> None:
    venue = KalshiVenue()
    try:
        try:
            result = await collect_paper_quote(
                venue, PaperResearchStore(db), ticker, contracts=contracts,
            )
            print(json.dumps(result.as_dict(), sort_keys=True))
        except (httpx.HTTPError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            print(json.dumps({"status": "unavailable", "ticker": ticker,
                              "detail": f"paper quote failed: {type(exc).__name__}"},
                             sort_keys=True))
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
    load_local_env()
    parser = argparse.ArgumentParser(prog="noema")
    sub = parser.add_subparsers(dest="command", required=True)

    markets = sub.add_parser("markets")
    markets.add_argument("--limit", type=int, default=20)

    stream = sub.add_parser("stream")
    stream.add_argument("tickers", nargs="*")

    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--db", default="data/noema.db")

    compare = sub.add_parser("compare")
    compare.add_argument("--db", default="data/noema.db")

    model_audit = sub.add_parser("model-audit")
    model_audit.add_argument("--db", default="data/noema.db")

    paper_quote = sub.add_parser("paper-quote")
    paper_quote.add_argument("ticker")
    paper_quote.add_argument("--db", default="data/noema.db")
    paper_quote.add_argument("--contracts", type=Decimal, default=Decimal(1))

    paper_audit = sub.add_parser("paper-audit")
    paper_audit.add_argument("--db", default="data/noema.db")

    sub.add_parser("account")
    sub.add_parser("check-config")
    sub.add_parser("telemetry")
    sub.add_parser("setup")
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--db", default="data/noema.db")

    ladder = sub.add_parser("ladder")
    ladder.add_argument("--db", default="data/noema.db")

    agent_once = sub.add_parser("agent-once")
    agent_once.add_argument("--db", default="data/noema.db")

    economy_init = sub.add_parser("economy-init")
    economy_init.add_argument("--db", default="data/noema.db")
    economy_init.add_argument("--capital", type=Decimal, required=True)

    economy_show = sub.add_parser("economy-show")
    economy_show.add_argument("--db", default="data/noema.db")

    ecosystem_show = sub.add_parser("ecosystem-show")
    ecosystem_show.add_argument("--db", default="data/noema.db")

    bill_config = sub.add_parser("bill-config")
    bill_config.add_argument("--db", default="data/noema.db")
    bill_config.add_argument("--hosting", type=Decimal, required=True)
    bill_config.add_argument("--other", type=Decimal, default=Decimal(0))
    bill_config.add_argument("--model-budget", type=Decimal, default=Decimal(0))
    bill_config.add_argument("--owner-limit", type=Decimal, required=True)

    bill_entry = sub.add_parser("bill-entry")
    bill_entry.add_argument("--db", default="data/noema.db")
    bill_entry.add_argument("--kind", choices=("receipt", "expense"), required=True)
    bill_entry.add_argument("--amount", type=Decimal, required=True)
    bill_entry.add_argument("--source", required=True)
    bill_entry.add_argument("--reference", required=True)

    bill_show = sub.add_parser("bill-show")
    bill_show.add_argument("--db", default="data/noema.db")

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
    elif args.command == "compare":
        print(json.dumps(compare_history_to_market(args.db).as_dict(), sort_keys=True))
    elif args.command == "model-audit":
        print(json.dumps(audit_database(args.db), sort_keys=True))
    elif args.command == "paper-quote":
        asyncio.run(_paper_quote(args.db, args.ticker, args.contracts))
    elif args.command == "paper-audit":
        print(json.dumps(PaperResearchStore(args.db).audit(), sort_keys=True))
    elif args.command == "setup":
        run_setup_wizard()
    elif args.command == "doctor":
        print(json.dumps(doctor_report(args.db), sort_keys=True))
    elif args.command == "ladder":
        print(json.dumps(build_ladder_report(args.db), sort_keys=True))
    elif args.command == "agent-once":
        asyncio.run(_agent_once(args.db))
    elif args.command == "economy-init":
        _economy_init(args.db, args.capital)
    elif args.command == "economy-show":
        _economy_show(args.db)
    elif args.command == "ecosystem-show":
        _ecosystem_show(args.db)
    elif args.command == "bill-config":
        tracker = BillTracker(args.db)
        tracker.configure(hosting_usd=args.hosting, other_usd=args.other,
                          owner_limit_usd=args.owner_limit,
                          model_budget_usd=args.model_budget)
        print(json.dumps(tracker.overview(), sort_keys=True))
    elif args.command == "bill-entry":
        tracker = BillTracker(args.db)
        tracker.record(kind=args.kind, amount_usd=args.amount,
                       source=args.source, reference=args.reference)
        print(json.dumps(tracker.overview(), sort_keys=True))
    elif args.command == "bill-show":
        print(json.dumps(BillTracker(args.db).overview(), sort_keys=True))
    elif args.command == "check-config":
        print(json.dumps(diagnostic_dict(), sort_keys=True))
    else:
        _evaluate(args.db)


if __name__ == "__main__":
    main()
