"""One trench-desk cycle: discover pools, filter, judge, and run the desks.

Paper fills are the default. Entries and exits go to the isolated Solana signer only
when NOEMA_SOLANA_SIGNER_ENABLED, NOEMA_ALLOW_LIVE_ORDERS, NOEMA_SOLANA_OWNER_REVIEWED,
and a solana live venue are all set. This module never reads the signer secret.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .accuracy_config import AccuracyConfig
from .accuracy_influence import apply_influence, influences_for
from .accuracy_ledger import record_forecast, resolve_token_marks
from .solana_live_gate import DeskSwapRejected, master_halt_active, solana_desk_live_enabled
from .solana_research import (
    DexScreenerTrenchPriceClient,
    GeckoTerminalPoolClient,
    PoolCandidate,
    ProviderFailure,
    PumpFunLaunchClient,
    SolanaRpcResearchClient,
    XChatterSource,
)
from .trench_desks import CostModel, Mark, PaperDeskEngine, TrenchDeskStore, desk_overview
from .trench_judgement import judge_candidates
from .trench_risk import DeskEntryPolicy, evaluate_desk_entry

LIVE_FLAGS = ("NOEMA_EXECUTION_GATEWAY_ENABLED", "NOEMA_ALLOW_LIVE_ORDERS",
              "NOEMA_TREASURY_ACTIONS_ENABLED", "NOEMA_SOLANA_SIGNER_ENABLED")


@dataclass(frozen=True)
class DeskRuntimeConfig:
    enabled: bool = False
    desk_count: int = 5
    interval_seconds: float = 120.0
    holder_enrichment_limit: int = 4
    judge_top_n: int = 3
    paper_capital_usd: float | None = None
    chatter_enabled: bool = False
    pumpfun_enabled: bool = True

    @classmethod
    def from_env(cls) -> DeskRuntimeConfig:
        capital = os.getenv("NOEMA_TRENCH_DESK_PAPER_CAPITAL_USD")
        return cls(
            enabled=os.getenv("NOEMA_TRENCH_DESKS_ENABLED", "0") == "1",
            desk_count=int(os.getenv("NOEMA_TRENCH_DESK_COUNT", "5")),
            interval_seconds=float(os.getenv("NOEMA_TRENCH_DESK_INTERVAL_SECONDS", "120")),
            holder_enrichment_limit=int(os.getenv("NOEMA_TRENCH_DESK_HOLDER_LIMIT", "4")),
            judge_top_n=int(os.getenv("NOEMA_TRENCH_DESK_JUDGE_TOP_N", "3")),
            paper_capital_usd=float(capital) if capital else None,
            chatter_enabled=os.getenv("NOEMA_TRENCH_CHATTER_ENABLED", "0") == "1",
            pumpfun_enabled=os.getenv("NOEMA_TRENCH_PUMPFUN_ENABLED", "1") == "1",
        )


class _HaltedDeskExecutor:
    def buy(self, **_kwargs: Any) -> Any:
        raise DeskSwapRejected("owner master halt is active")

    def sell(self, **_kwargs: Any) -> Any:
        raise DeskSwapRejected("owner master halt is active")


def paper_capital(db_path: str, config: DeskRuntimeConfig) -> float:
    if config.paper_capital_usd is not None:
        return config.paper_capital_usd
    try:
        from .economic_ledger import EconomicLedger

        snapshot = EconomicLedger(db_path).latest_snapshot()
        if snapshot is not None and snapshot.strategy_capital_usd > 0:
            return float(snapshot.strategy_capital_usd)
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        return 400.0
    return 400.0


def _dedupe(pools: list[PoolCandidate]) -> list[PoolCandidate]:
    best: dict[str, PoolCandidate] = {}
    for pool in pools:
        prior = best.get(pool.mint)
        if prior is None or pool.liquidity_usd > prior.liquidity_usd:
            best[pool.mint] = pool
    return list(best.values())


async def run_desk_cycle(
    db_path: str, *, config: DeskRuntimeConfig,
    gecko: GeckoTerminalPoolClient, solana: SolanaRpcResearchClient,
    dex: DexScreenerTrenchPriceClient, pumpfun: PumpFunLaunchClient | None = None,
    chatter: XChatterSource | None = None, policy: DeskEntryPolicy | None = None,
    now: datetime | None = None, executor: Any | None = None,
) -> dict[str, Any]:
    live = solana_desk_live_enabled()
    if not live:
        executor = None
    elif executor is None and master_halt_active():
        executor = _HaltedDeskExecutor()
    elif executor is None:
        from .solana_execution import JupiterDeskExecutor

        executor = JupiterDeskExecutor(db_path)
    now = now or datetime.now(UTC)
    policy = policy or DeskEntryPolicy.from_env()
    summary: dict[str, Any] = {"provider_failures": []}

    pools: list[PoolCandidate] = []
    for fetch in (gecko.trending_pools, gecko.new_pools):
        try:
            pools.extend(await fetch())
        except ProviderFailure as exc:
            summary["provider_failures"].append(str(exc))
    pools = _dedupe(pools)
    summary["pools_seen"] = len(pools)

    if pumpfun is not None and config.pumpfun_enabled:
        try:
            summary["pumpfun_new_launches"] = len(await pumpfun.newest(20))
        except ProviderFailure as exc:
            summary["provider_failures"].append(str(exc))

    # Cheap pre-filter without holder data, then spend RPC calls only on survivors.
    pre: list[tuple[PoolCandidate, float | None]] = []
    rejections: dict[str, int] = {}
    feed: dict[str, dict[str, Any]] = {}
    for pool in pools:
        age = (now - pool.pool_created_at).total_seconds() if pool.pool_created_at else None
        ok, reasons, _ = evaluate_desk_entry(
            liquidity_usd=pool.liquidity_usd, pool_age_seconds=age, holder_shares=None,
            price_change_m5=pool.price_change_m5, price_change_h1=pool.price_change_h1,
            policy=DeskEntryPolicy(**{**policy.__dict__, "require_holder_data": False}),
        )
        if ok:
            pre.append((pool, age))
        for reason in reasons:
            rejections[reason] = rejections.get(reason, 0) + 1
        feed[pool.mint] = {
            "mint": pool.mint, "symbol": pool.symbol, "source": pool.source,
            "liquidity_usd": round(pool.liquidity_usd, 2), "price_usd": pool.price_usd,
            "pool_age_hours": round(age / 3600, 2) if age is not None else None,
            "price_change_m5": pool.price_change_m5,
            "pool_address": pool.pool_address,
            "fdv_usd": pool.fdv_usd, "volume_h1_usd": pool.volume_h1_usd, "trades_h1": pool.trades_h1,
            "stage": "filters" if ok else "rejected",
            "reason": "; ".join(reasons) or ("passed filters; queued for holder check" if ok else None),
        }
    pre.sort(key=lambda item: item[0].liquidity_usd, reverse=True)

    passed: list[dict[str, Any]] = []
    for pool, age in pre[: config.holder_enrichment_limit]:
        # GeckoTerminal aggregates holder distribution; the free public Solana RPC
        # throttles getTokenLargestAccounts (HTTP 429), so it is the fallback.
        top10: float | None = None
        shares: tuple[float, ...] = ()
        try:
            top10 = await gecko.token_top10_fraction(pool.mint)
        except (ProviderFailure, AttributeError) as exc:
            summary["provider_failures"].append(f"holders_gecko:{exc}")
        if top10 is None:
            try:
                shares = await solana.top_account_supply_shares(pool.mint)
            except (ProviderFailure, ValueError, RuntimeError) as exc:
                summary["provider_failures"].append(f"holders_rpc:{exc}")
        ok, reasons, metrics = evaluate_desk_entry(
            liquidity_usd=pool.liquidity_usd, pool_age_seconds=age, holder_shares=shares,
            top10_fraction=top10,
            price_change_m5=pool.price_change_m5, price_change_h1=pool.price_change_h1,
            policy=policy,
        )
        for reason in reasons:
            rejections[reason] = rejections.get(reason, 0) + 1
        if pool.mint in feed:
            feed[pool.mint].update(
                stage="holders" if ok else "rejected", reason="; ".join(reasons) or None,
                top10_holder_fraction=metrics.get("top10_holder_fraction"))
        if ok:
            context = await chatter.chatter(pool.symbol, pool.mint) if chatter else None
            passed.append({
                "mint": pool.mint, "symbol": pool.symbol, "price_usd": pool.price_usd,
                "liquidity_usd": pool.liquidity_usd, "fdv_usd": pool.fdv_usd,
                "price_change_m5": pool.price_change_m5, "price_change_h1": pool.price_change_h1,
                "trades_m5": pool.trades_m5, "trades_h1": pool.trades_h1,
                "volume_h1_usd": pool.volume_h1_usd,
                "pool_age_hours": round((age or 0) / 3600, 2),
                "top10_holder_fraction": metrics["top10_holder_fraction"],
                "spike_acceleration": metrics["spike_acceleration"],
                "chatter_status": (context or {}).get("status", "disabled"),
            })
    # Real candles for a few filter-passing pools so the room's crawler can flip through charts.
    ohlcv_budget = int(os.getenv("NOEMA_TRENCH_OHLCV_PER_CYCLE", "6"))
    if hasattr(gecko, "pool_ohlcv"):
        for pool, _age in pre[:ohlcv_budget]:
            if not pool.pool_address:
                continue
            try:
                candles = await gecko.pool_ohlcv(pool.pool_address)
            except ProviderFailure as exc:
                summary["provider_failures"].append(f"ohlcv:{exc}")
                continue
            if candles and pool.mint in feed:
                feed[pool.mint]["candles"] = candles
    summary["prefilter_passed"] = len(pre)
    summary["holder_checked"] = min(len(pre), config.holder_enrichment_limit)
    summary["entry_passed"] = len(passed)
    summary["rejections"] = rejections

    top = sorted(passed, key=lambda c: c["liquidity_usd"], reverse=True)[: config.judge_top_n]
    judgements, judge_status = await judge_candidates(db_path, top)
    summary["judgement"] = judge_status
    judged_picks = [j for j in judgements if j.trade]
    picks, chief_log = chief_gate(judged_picks)
    accuracy = AccuracyConfig.from_env()
    if accuracy.influence_enabled:
        weight_conn = sqlite3.connect(db_path, timeout=30)
        try:
            weights = influences_for(weight_conn, accuracy)
        finally:
            weight_conn.close()
        picks, chief_log = apply_influence(
            picks, chief_log, weights, min_probability=CHIEF_MIN_PROBABILITY,
            base_rate=accuracy.base_rate,
        )
    summary["chief"] = {"approved": [c["symbol"] for c in chief_log if c["approved"]],
                        "vetoed": [c["symbol"] for c in chief_log if not c["approved"]],
                        "min_probability": CHIEF_MIN_PROBABILITY, "min_confidence": CHIEF_MIN_CONFIDENCE}
    summary["social"] = {"status": "unavailable",
                         "detail": "recycled-X-account check not wired (X search needs login); research only"}
    for j in judgements:
        if j.mint in feed:
            feed[j.mint].update(stage="desk" if j.trade else "rejected",
                                probability=round(j.probability, 4), judge=j.source,
                                reason=None if j.trade else f"{j.source} pass p={j.probability:.2f}")
    for c in chief_log:
        if c["mint"] in feed:
            feed[c["mint"]].update(chief="approve" if c["approved"] else "veto", chief_reason=c["reason"],
                                   confidence=c["confidence"])
            if not c["approved"]:
                feed[c["mint"]]["stage"] = "chief_veto"
    summary["feed"] = sorted(feed.values(), key=lambda r: (
        ["desk", "chief_veto", "holders", "filters", "rejected"].index(r["stage"]), -r["liquidity_usd"]))[:60]
    by_mint = {c["mint"]: c for c in passed}
    _record_chief_forecasts(db_path, chief_log, by_mint)

    store = TrenchDeskStore(db_path)
    try:
        store.ensure_desks(config.desk_count, paper_capital(db_path, config))
        engine = PaperDeskEngine(store, costs=CostModel.from_env(), executor=executor)
        names = {m: f.get("symbol") for m, f in feed.items()}
        for c in chief_log:
            store.event(None, "chief_decision", **{**c, "symbol": names.get(c["mint"])})
        held = {p["mint"] for p in store.open_positions()}
        wanted = list(held | {j.mint for j in picks})
        marks: dict[str, Mark] = {}
        for start in range(0, len(wanted), 30):
            try:
                pairs = await dex.tokens_by_mint(wanted[start:start + 30])
            except ProviderFailure as exc:
                summary["provider_failures"].append(str(exc))
                pairs = {}
            for mint, pair in pairs.items():
                price = float(pair.get("priceUsd") or 0)
                liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
                if price > 0:
                    marks[mint] = Mark(price, liquidity, "dexscreener")
        for mint in wanted:
            if mint not in marks and mint in by_mint:
                c = by_mint[mint]
                marks[mint] = Mark(c["price_usd"], c["liquidity_usd"], "geckoterminal")
        resolve_conn = sqlite3.connect(db_path, timeout=30)
        try:
            resolve_token_marks(
                resolve_conn, {mint: mark.price_usd for mint, mark in marks.items()},
                now=now, db_path=db_path, config=accuracy,
            )
            resolve_conn.commit()
        finally:
            resolve_conn.close()
        entries = fired = 0
        for index, desk in enumerate(store.active_desks()):
            engine.manage_positions(desk["desk_id"], marks, now=now)
            if picks:
                ordered = picks[index % len(picks):] + picks[: index % len(picks)]
                for pick in ordered:
                    mark = marks.get(pick.mint)
                    if mark and engine.try_enter(
                            desk["desk_id"], pick.mint, by_mint[pick.mint]["symbol"], mark,
                            f"{pick.source} pick p={pick.probability:.2f}"):
                        entries += 1
                        break
            if engine.fire_if_needed(desk["desk_id"], marks):
                fired += 1
        store.event(None, "desk_cycle", **{k: v for k, v in summary.items()
                                           if k != "provider_failures"},
                    entries=entries, fired=fired, picks=[j.mint for j in picks])
        engine.commit()
    finally:
        store.close()
    _record_equity_snapshot(db_path)
    summary.pop("feed", None)
    summary.update(entries=entries, fired=fired, marks=len(marks),
                   live_execution=live and not master_halt_active())
    return summary


CHIEF_MIN_PROBABILITY = float(os.getenv("NOEMA_CHIEF_MIN_PROBABILITY", "0.60"))
CHIEF_MIN_CONFIDENCE = float(os.getenv("NOEMA_CHIEF_MIN_CONFIDENCE", "0.55"))


def chief_gate(picks: list[Any]) -> tuple[list[Any], list[dict[str, Any]]]:
    """CHIEF seat: approve a judged pick only if p >= min and confidence >= min."""
    approved, log = [], []
    for j in picks:
        conf = getattr(j, "confidence", None)
        if j.probability < CHIEF_MIN_PROBABILITY:
            ok, why = False, f"p={j.probability:.2f} < {CHIEF_MIN_PROBABILITY:.2f}"
        elif conf is None:
            ok, why = False, "no confidence signal"
        elif conf < CHIEF_MIN_CONFIDENCE:
            ok, why = False, f"confidence={conf:.2f} < {CHIEF_MIN_CONFIDENCE:.2f}"
        else:
            ok, why = True, f"p={j.probability:.2f} conf={conf:.2f}"
        log.append({"mint": j.mint, "symbol": None, "approved": ok, "reason": why,
                    "probability": j.probability, "confidence": conf})
        if ok:
            approved.append(j)
    return approved, log


def _record_chief_forecasts(
    db_path: str, chief_log: list[dict[str, Any]], by_mint: dict[str, dict[str, Any]],
) -> None:
    if not chief_log:
        return
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        for entry in chief_log:
            mint = entry.get("mint")
            probability = entry.get("shrunk_probability", entry.get("probability"))
            if not isinstance(mint, str) or not isinstance(probability, (int, float)):
                continue
            if entry.get("shadow"):
                decision = "shadow"
            elif entry.get("approved"):
                decision = "trade"
            else:
                decision = "veto"
            raw = by_mint.get(mint) or {}
            price = raw.get("price_usd")
            confidence = entry.get("confidence")
            record_forecast(
                conn, agent_id="chief", venue="solana", market_id=mint,
                symbol=raw.get("symbol"), predicted_probability=float(probability),
                confidence=confidence if isinstance(confidence, (int, float)) else None,
                market_price=float(price) if isinstance(price, (int, float)) else None,
                reasoning={
                    "reason": entry.get("reason"), "weight": entry.get("weight"),
                    "approved": bool(entry.get("approved")),
                },
                decision=decision, horizon="1h", metadata={"kind": "token_up"},
            )
        conn.commit()
    finally:
        conn.close()


def _record_equity_snapshot(db_path: str) -> None:
    """Persist aggregate paper equity so the trading room can chart P&L over time."""
    overview = desk_overview(db_path)
    desks = overview["desks"]
    store = TrenchDeskStore(db_path)
    try:
        store.event(None, "equity_snapshot",
                    equity_usd=round(sum(d["equity_usd"] for d in desks), 2),
                    starting_usd=round(sum(d["starting_bankroll_usd"] for d in desks), 2),
                    pnl_after_costs_usd=overview.get("total_pnl_after_costs_usd", 0.0),
                    active=sum(1 for d in desks if d["status"] == "active"))
        store.conn.commit()
    finally:
        store.close()
