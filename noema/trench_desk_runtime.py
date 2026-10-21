"""One paper-desk cycle: discover pools, filter, judge, and run paper desks.

PAPER ONLY. This module never imports a signer or swap builder and refuses to run if
any live-execution flag is enabled.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .solana_research import (
    DexScreenerTrenchPriceClient,
    GeckoTerminalPoolClient,
    PoolCandidate,
    ProviderFailure,
    PumpFunLaunchClient,
    SolanaRpcResearchClient,
    XChatterSource,
)
from .trench_desks import CostModel, Mark, PaperDeskEngine, TrenchDeskStore
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


def assert_paper_only() -> None:
    enabled = [flag for flag in LIVE_FLAGS if os.getenv(flag, "0") == "1"]
    if enabled:
        raise RuntimeError("trench desks are paper-only; live flags enabled: " + ",".join(enabled))


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
    now: datetime | None = None,
) -> dict[str, Any]:
    assert_paper_only()
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
    pre.sort(key=lambda item: item[0].liquidity_usd, reverse=True)

    passed: list[dict[str, Any]] = []
    for pool, age in pre[: config.holder_enrichment_limit]:
        try:
            shares = await solana.top_account_supply_shares(pool.mint)
        except (ProviderFailure, ValueError, RuntimeError) as exc:
            summary["provider_failures"].append(f"holders:{type(exc).__name__}")
            shares = ()
        ok, reasons, metrics = evaluate_desk_entry(
            liquidity_usd=pool.liquidity_usd, pool_age_seconds=age, holder_shares=shares,
            price_change_m5=pool.price_change_m5, price_change_h1=pool.price_change_h1,
            policy=policy,
        )
        for reason in reasons:
            rejections[reason] = rejections.get(reason, 0) + 1
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
    summary["prefilter_passed"] = len(pre)
    summary["holder_checked"] = min(len(pre), config.holder_enrichment_limit)
    summary["entry_passed"] = len(passed)
    summary["rejections"] = rejections

    top = sorted(passed, key=lambda c: c["liquidity_usd"], reverse=True)[: config.judge_top_n]
    judgements, judge_status = await judge_candidates(db_path, top)
    summary["judgement"] = judge_status
    picks = [j for j in judgements if j.trade]
    by_mint = {c["mint"]: c for c in passed}

    store = TrenchDeskStore(db_path)
    try:
        store.ensure_desks(config.desk_count, paper_capital(db_path, config))
        engine = PaperDeskEngine(store, costs=CostModel.from_env())
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
    summary.update(entries=entries, fired=fired, marks=len(marks))
    return summary
