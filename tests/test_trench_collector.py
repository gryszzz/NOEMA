import json
from datetime import UTC, datetime, timedelta

import pytest

from noema.solana_research import (
    DexScreenerTrenchPriceClient,
    JupiterTrenchResearchClient,
    ProviderFailure,
    SolanaRpcResearchClient,
)
from noema.trench_collector import (
    DueObservation,
    TrenchCollectorStore,
    collect_trench_cycle,
    update_trench_research_from_observations,
)
from noema.trench_dashboard import build_trench_overview
from noema.trench_models import LaunchTick, TokenControlState
from noema.trench_store import TrenchResearchStore
from noema.trench_survival_model import load_verified_examples


class FakeJupiter(JupiterTrenchResearchClient):
    def __init__(self, token):
        self.token = token

    async def recent_tradeable_tokens(self):
        return [self.token]

    async def tokens_by_mint(self, mints):
        return {mint: self.token for mint in mints}


class FakeSolana(SolanaRpcResearchClient):
    async def top_account_supply_shares(self, mint):
        return (0.08, 0.06, 0.04, 0.03, 0.02)


class FakeDexScreener(DexScreenerTrenchPriceClient):
    def __init__(self, pair):
        self.pair = pair

    async def tokens_by_mint(self, mints):
        return {mint: self.pair for mint in mints} if self.pair else {}


def token_at(first_pool: datetime, *, price: float = 0.002):
    return {
        "id": "mint-a",
        "tokenProgram": "Token",
        "usdPrice": price,
        "liquidity": 15000,
        "organicScore": 70,
        "firstPool": {"createdAt": first_pool.isoformat()},
        "stats24h": {
            "buyVolume": 5000,
            "sellVolume": 1000,
            "buyOrganicVolume": 2500,
            "sellOrganicVolume": 500,
            "numOrganicBuyers": 50,
            "numTraders": 100,
            "numNetBuyers": 35,
        },
        "audit": {
            "mintAuthorityDisabled": True,
            "freezeAuthorityDisabled": True,
            "devBalancePercentage": 3,
        },
    }


def test_legacy_rpc_enrichment_failures_backfill_canonical_health(tmp_path):
    db = str(tmp_path / "legacy-rpc-health.db")
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE trench_collection_attempts (id INTEGER PRIMARY KEY, mint TEXT, "
        "horizon_seconds INTEGER, attempted_at TEXT, status TEXT, detail TEXT, raw_json TEXT)"
    )
    conn.executemany(
        "INSERT INTO trench_collection_attempts(attempted_at,detail,status) VALUES (?,?,?)",
        [
            ("2026-09-29T02:00:00+00:00", "holder enrichment unavailable: solana_rpc:rate_limited", "recorded"),
            ("2026-09-29T03:00:00+00:00", "holder enrichment unavailable: solana_rpc:rate_limited", "recorded"),
        ],
    )
    conn.commit()
    conn.close()

    store = TrenchCollectorStore(db)
    health = store.provider_states()["solana_rpc:primary"]
    assert health["state"] == "degraded"
    assert health["last_attempt_at"] == "2026-09-29T03:00:00+00:00"
    assert health["last_failure_at"] == "2026-09-29T03:00:00+00:00"
    assert health["last_success_at"] is None
    assert health["consecutive_failures"] == 2
    assert health["last_error_class"] == "rate_limited"
    store.close()

    # Reopening does not rewrite newer canonical health from the migration.
    store = TrenchCollectorStore(db)
    store.record_provider_health(
        "solana_rpc:primary", success=True,
        attempted_at=datetime(2026, 9, 30, tzinfo=UTC),
    )
    store.close()
    store = TrenchCollectorStore(db)
    assert store.provider_states()["solana_rpc:primary"]["state"] == "healthy"
    store.close()


def test_duplicate_launch_refreshes_last_seen_without_creating_new_identity(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    store = TrenchCollectorStore(db)
    first_pool = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
    assert store.register_recent([token_at(first_pool)], now=first_pool + timedelta(seconds=1)) == 1
    duplicate = token_at(first_pool + timedelta(seconds=10), price=0.5)
    assert store.register_recent([duplicate], now=first_pool + timedelta(seconds=20)) == 0
    row = store.conn.execute(
        "SELECT COUNT(*),first_pool_at,last_seen_at,discovery_json FROM trench_launches WHERE mint='mint-a'"
    ).fetchone()
    assert row[0] == 1
    assert row[1] == first_pool.isoformat()
    assert row[2] == (first_pool + timedelta(seconds=20)).isoformat()
    assert '"usdPrice":0.002' in row[3]


@pytest.mark.asyncio
async def test_collector_marks_missed_early_horizons_and_records_current_one(tmp_path) -> None:
    now = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
    first_pool = now - timedelta(seconds=300)
    db = str(tmp_path / "noema.db")

    summary = await collect_trench_cycle(
        db_path=db,
        jupiter=FakeJupiter(token_at(first_pool)),
        solana=FakeSolana(),
        now=now,
        due_limit=10,
        enrichment_limit=1,
        request_pause_seconds=0,
    )

    assert summary.discovered == 1
    assert summary.due == 1
    assert summary.recorded == 1

    store = TrenchCollectorStore(db)
    observations = store.observations("mint-a")
    assert [horizon for horizon, _, _ in observations] == [300]

    missed = store.conn.execute(
        """
        SELECT horizon_seconds FROM trench_collection_attempts
        WHERE mint = 'mint-a' AND status = 'missed'
        ORDER BY horizon_seconds
        """
    ).fetchall()
    assert [row[0] for row in missed] == [30, 60, 120]


@pytest.mark.asyncio
async def test_discovery_disabled_preserves_existing_forward_observations(tmp_path) -> None:
    start = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
    db = str(tmp_path / "noema.db")
    store = TrenchCollectorStore(db)
    store.register_recent([token_at(start)], now=start + timedelta(seconds=10))

    class NoDiscoveryJupiter(FakeJupiter):
        async def recent_tradeable_tokens(self):
            raise AssertionError("quarantined research must not discover new launches")

    summary = await collect_trench_cycle(
        db_path=db,
        jupiter=NoDiscoveryJupiter(token_at(start, price=0.0001)),
        solana=FakeSolana(),
        now=start + timedelta(seconds=3600),
        due_limit=10,
        enrichment_limit=0,
        discover_new=False,
    )
    assert summary.discovered == 0
    assert summary.due == 1
    assert summary.recorded == 1
    observations = store.observations("mint-a")
    assert observations[0][0] == 3600
    assert observations[0][1].price_usd == 0.0001


@pytest.mark.asyncio
async def test_discovery_disabled_without_existing_obligations_makes_no_requests(tmp_path) -> None:
    class NoRequestJupiter(FakeJupiter):
        async def recent_tradeable_tokens(self):
            raise AssertionError("discovery must be skipped")

        async def tokens_by_mint(self, mints):
            raise AssertionError("no existing observations are due")

    summary = await collect_trench_cycle(
        db_path=str(tmp_path / "noema.db"),
        jupiter=NoRequestJupiter({}),
        solana=FakeSolana(),
        discover_new=False,
    )
    assert summary.discovered == summary.due == summary.recorded == 0


@pytest.mark.asyncio
async def test_discovery_rate_limit_does_not_block_existing_forward_observation(tmp_path) -> None:
    now = datetime(2026, 9, 26, 20, 5, tzinfo=UTC)
    first_pool = now - timedelta(seconds=300)
    db = str(tmp_path / "discovery-limited.db")
    existing = TrenchCollectorStore(db)
    existing.register_recent([token_at(first_pool)], now=first_pool + timedelta(seconds=1))
    existing.close()

    class DiscoveryLimitedJupiter(FakeJupiter):
        async def recent_tradeable_tokens(self):
            raise ProviderFailure("jupiter", "recent", "rate_limited")

    summary = await collect_trench_cycle(
        db_path=db,
        jupiter=DiscoveryLimitedJupiter(token_at(first_pool)), solana=FakeSolana(), now=now,
    )

    assert summary.provider_failures == ("jupiter:rate_limited",)
    assert summary.recorded == 1
    assert summary.failed == 0


@pytest.mark.asyncio
async def test_solana_rate_limit_keeps_jupiter_observation_and_persists_safe_classification(
    tmp_path,
) -> None:
    start = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
    now = start + timedelta(seconds=300)

    class RateLimitedSolana(SolanaRpcResearchClient):
        async def top_account_supply_shares(self, _mint):
            raise ProviderFailure("solana_rpc", "getTokenSupply", "rate_limited")

    db = str(tmp_path / "rpc-rate-limit.db")
    summary = await collect_trench_cycle(
        db_path=db, jupiter=FakeJupiter(token_at(start)), solana=RateLimitedSolana(), now=now,
    )
    store = TrenchCollectorStore(db)
    attempt = store.conn.execute(
        "SELECT status,detail FROM trench_collection_attempts "
        "WHERE horizon_seconds=300 ORDER BY id DESC LIMIT 1",
    ).fetchone()
    provenance_raw = store.conn.execute(
        "SELECT provider_provenance_json FROM trench_observations "
        "WHERE mint='mint-a' AND horizon_seconds=300",
    ).fetchone()[0]

    assert summary.recorded == 1
    assert summary.provider_failures == ("solana_rpc:rate_limited",)
    assert "solana_rpc:primary=degraded" in summary.provider_health
    assert attempt == ("recorded", "holder enrichment unavailable: solana_rpc:rate_limited")
    assert '"status":"unavailable"' in provenance_raw
    assert '"error_class":"rate_limited"' in provenance_raw


@pytest.mark.asyncio
async def test_dexscreener_current_price_fallback_is_provenanced_and_jupiter_raw_is_retained(tmp_path):
    start = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
    token = token_at(start, price=0)
    token["liquidity"] = None
    pair = {
        "chainId": "solana", "dexId": "fixture-dex", "pairAddress": "pool-1",
        "baseToken": {"address": "mint-a"}, "priceUsd": "0.004",
        "liquidity": {"usd": 12000}, "txns": {"m5": {"buys": 2, "sells": 1}},
    }
    db = str(tmp_path / "dex-fallback.db")
    summary = await collect_trench_cycle(
        db_path=db, jupiter=FakeJupiter(token), solana=FakeSolana(),
        price_fallback=FakeDexScreener(pair), now=start + timedelta(seconds=300),
    )
    collector = TrenchCollectorStore(db)
    row = collector.conn.execute(
        "SELECT tick_json,raw_token_json,provider_provenance_json FROM trench_observations WHERE mint='mint-a' AND horizon_seconds=300"
    ).fetchone()
    tick, raw, provenance = (json.loads(value) for value in row)
    assert summary.recorded == 1
    assert tick["price_usd"] == 0.004
    assert tick["liquidity_usd"] == 12000
    assert raw["usdPrice"] == 0
    assert raw["_noema_market_data_fallback"]["pairAddress"] == "pool-1"
    assert raw["_noema_normalized_price_usd"] == 0.004
    assert provenance["fields"]["price_usd"] == "dexscreener.priceUsd"
    assert provenance["fields"]["liquidity_usd"] == "dexscreener.liquidity.usd"
    assert provenance["market_fallback"]["minimum_recent_trades"] == 1
    assert collector.provider_states()["dexscreener_market"]["state"] == "healthy"


@pytest.mark.asyncio
async def test_jupiter_market_outage_does_not_block_independent_dexscreener_observation(tmp_path):
    start = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)

    class JupiterMarketDown(FakeJupiter):
        async def tokens_by_mint(self, _mints):
            raise ProviderFailure("jupiter", "tokens_by_mint", "rate_limited", http_status=429)

    pair = {
        "chainId": "solana", "dexId": "fixture-dex", "pairAddress": "pool-1",
        "baseToken": {"address": "mint-a"}, "priceUsd": "0.004",
        "liquidity": {"usd": 12000}, "txns": {"m5": {"buys": 2, "sells": 1}},
    }
    db = str(tmp_path / "jupiter-outage.db")
    summary = await collect_trench_cycle(
        db_path=db, jupiter=JupiterMarketDown(token_at(start)), solana=FakeSolana(),
        price_fallback=FakeDexScreener(pair), now=start + timedelta(seconds=300),
    )
    store = TrenchCollectorStore(db)
    assert summary.recorded == 1
    assert "jupiter:rate_limited" in summary.provider_failures
    assert store.conn.execute(
        "SELECT provider_provenance_json FROM trench_observations WHERE mint='mint-a' AND horizon_seconds=300"
    ).fetchone()[0]
    assert store.provider_states()["jupiter_market"]["state"] == "degraded"
    assert store.provider_states()["dexscreener_market"]["state"] == "healthy"


@pytest.mark.asyncio
async def test_price_unavailable_has_durable_bounded_retry_state(tmp_path):
    start = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
    db = str(tmp_path / "price-retry.db")
    summary = await collect_trench_cycle(
        db_path=db, jupiter=FakeJupiter(token_at(start, price=0)), solana=FakeSolana(),
        now=start + timedelta(seconds=300),
    )
    assert summary.unavailable == 1
    store = TrenchCollectorStore(db)
    attempt = store.conn.execute(
        "SELECT retry_at FROM trench_collection_attempts WHERE mint='mint-a' AND horizon_seconds=300 AND status='unavailable'"
    ).fetchone()
    retry_at = datetime.fromisoformat(attempt[0])
    store.close()
    reopened = TrenchCollectorStore(db)
    assert reopened.due_observations(now=retry_at - timedelta(milliseconds=1)) == []
    assert reopened.due_observations(now=retry_at + timedelta(milliseconds=1)) == [
        DueObservation("mint-a", start, 300, start + timedelta(seconds=300))
    ]
    reopened.close()


def test_missed_horizon_accounting_is_idempotent(tmp_path):
    start = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
    store = TrenchCollectorStore(str(tmp_path / "missed-idempotent.db"))
    store.register_recent([token_at(start)], now=start)
    due = DueObservation("mint-a", start, 30, start + timedelta(seconds=30))
    first = store.record_attempt(due, status="missed", attempted_at=start + timedelta(seconds=90))
    second = store.record_attempt(due, status="missed", attempted_at=start + timedelta(seconds=120))
    assert first == second
    assert store.conn.execute("SELECT COUNT(*) FROM trench_collection_attempts WHERE status='missed'").fetchone()[0] == 1


def test_recorded_observation_is_never_reclassified_as_missed(tmp_path):
    start = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
    store = TrenchCollectorStore(str(tmp_path / "recorded-not-missed.db"))
    store.register_recent([token_at(start)], now=start)
    due = DueObservation("mint-a", start, 30, start + timedelta(seconds=30))
    assert store.record_observation(
        due,
        tick=LaunchTick(observed_at=start + timedelta(seconds=31), price_usd=0.002,
                        liquidity_usd=10_000),
        control=TokenControlState(),
        raw_token={"id": "mint-a"},
        holder_shares=(),
    )

    # A later scheduler pass happens after the target's allowed observation window.
    assert store.due_observations(now=start + timedelta(seconds=61), horizons=(30,)) == []
    assert store.conn.execute(
        "SELECT COUNT(*) FROM trench_collection_attempts WHERE status='missed'"
    ).fetchone() == (0,)


def test_dashboard_missed_count_excludes_false_missed_after_recorded_observation(tmp_path):
    start = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
    db = str(tmp_path / "dashboard-missed-reconcile.db")
    store = TrenchCollectorStore(db)
    store.register_recent([token_at(start)], now=start)
    due = DueObservation("mint-a", start, 30, start + timedelta(seconds=30))
    store.record_observation(
        due,
        tick=LaunchTick(observed_at=start + timedelta(seconds=31), price_usd=0.002,
                        liquidity_usd=10_000),
        control=TokenControlState(),
        raw_token={"id": "mint-a"},
        holder_shares=(),
    )
    store.record_attempt(due, status="missed", attempted_at=start + timedelta(seconds=90))
    store.close()

    progress = build_trench_overview(db)["progress"]
    assert progress["attempt_statuses"]["missed"] == 0
    assert progress["missed_attempts"] == 0


@pytest.mark.asyncio
async def test_forward_label_schedule_survives_collector_restarts_end_to_end(tmp_path):
    start = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
    db = str(tmp_path / "forward-e2e.db")
    jupiter = FakeJupiter(token_at(start))
    solana = FakeSolana()
    # Discovery and each scheduled prospective snapshot use separate collector
    # instances, matching process restarts between polling cycles.
    await collect_trench_cycle(db_path=db, jupiter=jupiter, solana=solana, now=start)
    for horizon in (30, 60, 120, 300):
        result = await collect_trench_cycle(
            db_path=db, jupiter=jupiter, solana=solana,
            now=start + timedelta(seconds=horizon), enrichment_limit=0,
        )
        assert result.recorded == 1
    store = TrenchCollectorStore(db)
    candidate = store.conn.execute(
        "SELECT candidate_id FROM trench_candidates WHERE token_mint='mint-a'"
    ).fetchone()
    schedule = store.conn.execute(
        "SELECT target_at,state FROM trench_candidate_maturities WHERE candidate_id=?",
        candidate,
    ).fetchone()
    assert schedule == ((start + timedelta(seconds=3600)).isoformat(), "PENDING")
    store.close()

    matured = await collect_trench_cycle(
        db_path=db, jupiter=FakeJupiter(token_at(start, price=0.0018)),
        solana=solana, now=start + timedelta(seconds=3600), enrichment_limit=0,
    )
    assert matured.recorded == 1
    assert matured.counterfactuals_recorded == 1
    store = TrenchCollectorStore(db)
    assert store.conn.execute(
        "SELECT state FROM trench_candidate_maturities WHERE candidate_id=?", candidate,
    ).fetchone() == ("RECORDED",)
    assert store.conn.execute(
        "SELECT observed_at FROM trench_counterfactuals WHERE candidate_id=? AND horizon_seconds=3600",
        candidate,
    ).fetchone() == ((start + timedelta(seconds=3600)).isoformat(),)
    store.close()


def test_transient_provider_failures_retry_until_horizon_is_missed(tmp_path) -> None:
    db = str(tmp_path / "retry.db")
    start = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
    collector = TrenchCollectorStore(db)
    collector.register_recent([token_at(start)], now=start + timedelta(seconds=1))
    due = DueObservation("mint-a", start, 3600, start + timedelta(seconds=3600))
    for attempt_at in (due.scheduled_at, due.scheduled_at + timedelta(seconds=10),
                       due.scheduled_at + timedelta(seconds=30)):
        collector.record_attempt(due, status="unavailable", detail="provider rate_limited",
                                 attempted_at=attempt_at)
    assert collector.due_observations(now=due.scheduled_at + timedelta(seconds=71)) == [due]
    assert collector.due_observations(now=due.scheduled_at + timedelta(seconds=301)) == []
    assert collector.conn.execute(
        "SELECT status FROM trench_collection_attempts WHERE status='missed'"
    ).fetchone() == ("missed",)


def test_five_minute_assessment_freezes_then_later_observation_labels_it(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    start = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
    store = TrenchCollectorStore(db)
    store.register_recent([token_at(start)], now=start + timedelta(seconds=10))

    control = TokenControlState(
        token_program="Token",
        mint_authority_present=False,
        freeze_authority_present=False,
        permanent_delegate_present=False,
        transfer_hook_present=False,
        transfer_fee_bps=0,
    )

    snapshots = [
        (
            120,
            LaunchTick(
                observed_at=start + timedelta(seconds=120),
                price_usd=0.001,
                liquidity_usd=8000,
                buy_volume_usd=800,
                sell_volume_usd=200,
                organic_net_buyers=15,
                total_traders=30,
                organic_buy_volume_usd=400,
                organic_sell_volume_usd=100,
                organic_score=55,
            ),
        ),
        (
            300,
            LaunchTick(
                observed_at=start + timedelta(seconds=300),
                price_usd=0.002,
                liquidity_usd=16000,
                buy_volume_usd=3000,
                sell_volume_usd=600,
                organic_net_buyers=60,
                total_traders=90,
                organic_buy_volume_usd=1800,
                organic_sell_volume_usd=300,
                organic_score=75,
            ),
        ),
    ]

    for horizon, tick in snapshots:
        due = DueObservation(
            "mint-a",
            start,
            horizon,
            start + timedelta(seconds=horizon),
        )
        assert store.record_observation(
            due,
            tick=tick,
            control=control,
            raw_token=token_at(start, price=tick.price_usd),
            holder_shares=(),
        )

    assessments, labels = update_trench_research_from_observations(
        db,
        mint="mint-a",
    )
    assert assessments == 1
    assert labels == 0

    later = LaunchTick(
        observed_at=start + timedelta(seconds=3600),
        price_usd=0.006,
        liquidity_usd=30000,
        buy_volume_usd=12000,
        sell_volume_usd=3000,
        organic_net_buyers=180,
        total_traders=300,
        organic_buy_volume_usd=7000,
        organic_sell_volume_usd=1600,
        organic_score=82,
    )
    due = DueObservation("mint-a", start, 3600, start + timedelta(seconds=3600))
    assert store.record_observation(
        due,
        tick=later,
        control=control,
        raw_token=token_at(start, price=later.price_usd),
        holder_shares=(),
    )

    assessments, labels = update_trench_research_from_observations(
        db,
        mint="mint-a",
    )
    assert assessments == 0
    assert labels == 1

    candidate = TrenchResearchStore(db).candidate_for_token("mint-a")
    assert candidate is not None
    candidate_id, _, _ = candidate
    row = TrenchResearchStore(db).conn.execute(
        """
        SELECT final_return_fraction, max_return_fraction
        FROM trench_counterfactuals
        WHERE candidate_id = ? AND horizon_seconds = 3600
        """,
        (candidate_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == pytest.approx(2.0)
    assert row[1] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_one_hour_label_derivation_recovers_after_restart_from_persisted_evidence(tmp_path) -> None:
    db = str(tmp_path / "restart.db")
    start = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
    control = TokenControlState(token_program="Token", suspicious_flag=False)
    collector = TrenchCollectorStore(db)
    collector.register_recent([token_at(start)], now=start + timedelta(seconds=5))
    for horizon, price, liquidity in ((120, 0.001, 8_000), (300, 0.002, 16_000)):
        tick = LaunchTick(start + timedelta(seconds=horizon), price, liquidity, 300, 80)
        due = DueObservation("mint-a", start, horizon, start + timedelta(seconds=horizon))
        assert collector.record_observation(
            due, tick=tick, control=control,
            raw_token=token_at(start, price=price), holder_shares=(),
            provider_provenance={"fields": {"price_usd": "jupiter.usdPrice"}},
        )
    assert update_trench_research_from_observations(db, mint="mint-a") == (1, 0)
    scheduled = start + timedelta(seconds=3600)
    observed = scheduled + timedelta(seconds=25)
    due = DueObservation("mint-a", start, 3600, scheduled)
    tick = LaunchTick(observed, 0.003, 18_000, 500, 90)
    assert collector.record_observation(
        due, tick=tick, control=control,
        raw_token=token_at(start, price=tick.price_usd), holder_shares=(),
        provider_provenance={"fields": {"price_usd": "jupiter.usdPrice"}},
    )
    candidate_id = TrenchResearchStore(db).candidate_for_token("mint-a")[0]
    # Simulate a process exit after the immutable observation commit and before the
    # derived counterfactual/label write.
    collector.close()

    recovered = await collect_trench_cycle(
        db_path=db, jupiter=FakeJupiter({}), solana=FakeSolana(), now=observed + timedelta(seconds=1),
        discover_new=False,
    )

    assert recovered.counterfactuals_recorded == 1
    examples = load_verified_examples(db, now=observed + timedelta(seconds=1))
    assert len(examples) == 1
    label = examples[0]
    assert label.candidate_id == candidate_id
    assert label.captured_at == start + timedelta(seconds=300)
    assert label.label_observed_at == observed
    audit = TrenchResearchStore(db)
    persisted = audit.conn.execute(
        "SELECT observed_at,final_return_fraction FROM trench_counterfactuals "
        "WHERE candidate_id=? AND horizon_seconds=3600", (candidate_id,),
    ).fetchone()
    assert persisted == (observed.isoformat(), pytest.approx(0.5))
    provenance = TrenchCollectorStore(db).conn.execute(
        "SELECT provider_provenance_json FROM trench_observations "
        "WHERE mint='mint-a' AND horizon_seconds=3600",
    ).fetchone()[0]
    assert 'jupiter.usdPrice' in provenance
    progress = build_trench_overview(db)["progress"]
    checkpoint = progress["matured_label_checkpoint"][0]
    assert checkpoint["candidate_id"] == candidate_id
    assert checkpoint["launch_identity"] == "mint-a"
    assert checkpoint["cohort_assignment"] == "training"
    assert checkpoint["scheduled_maturity_at"] == scheduled.isoformat()
    assert checkpoint["actual_maturity_at"] == observed.isoformat()


def test_stale_or_premature_forward_observation_is_rejected(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    start = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
    store = TrenchCollectorStore(db)
    store.register_recent([token_at(start)], now=start + timedelta(seconds=10))
    control = TokenControlState(token_program="Token")
    due = DueObservation("mint-a", start, 3600, start + timedelta(seconds=3600))
    for offset in (3599, 3901):
        tick = LaunchTick(
            observed_at=start + timedelta(seconds=offset), price_usd=0.1,
            liquidity_usd=1000, buy_volume_usd=0, sell_volume_usd=0,
        )
        with pytest.raises(ValueError, match="stale or premature"):
            store.record_observation(
                due, tick=tick, control=control,
                raw_token=token_at(start, price=tick.price_usd), holder_shares=(),
            )
    assert store.observations("mint-a") == []
