from datetime import UTC, datetime, timedelta

import pytest

from noema.solana_research import JupiterTrenchResearchClient, SolanaRpcResearchClient
from noema.trench_collector import (
    DueObservation,
    TrenchCollectorStore,
    collect_trench_cycle,
    update_trench_research_from_observations,
)
from noema.trench_models import LaunchTick, TokenControlState
from noema.trench_store import TrenchResearchStore


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
