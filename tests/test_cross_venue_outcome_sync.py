from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta

from test_cross_venue_experiment import _candidate

from noema import agent_runtime
from noema.agent_config import AgentConfig
from noema.config import KalshiConfig
from noema.cross_venue_experiment import (
    evaluate_candidate,
    mature_paper_pairs,
    persist_evaluation,
)
from noema.outcomes import OutcomeStore
from noema.sync import sync_polymarket_us_outcomes


class FakePolymarketUS:
    def __init__(self, *, closed=True, settlement=1, resolved_at=None):
        self.closed = closed
        self.settlement_value = settlement
        self.resolved_at = resolved_at
        self.calls = []

    async def market_by_slug(self, slug):
        self.calls.append(("market", slug))
        return {"slug": slug, "active": not self.closed, "closed": self.closed}

    async def settlement(self, slug):
        self.calls.append(("settlement", slug))
        result = {"slug": slug, "settlement": self.settlement_value}
        if self.resolved_at:
            result["settledAt"] = self.resolved_at
        return result

    def close(self):
        pass


def test_polymarket_official_settlement_enters_shared_outcomes_schema(tmp_path):
    path = str(tmp_path / "outcomes.db")
    store = OutcomeStore(path)
    venue = FakePolymarketUS()
    result = asyncio.run(sync_polymarket_us_outcomes(
        store, venue, {"fixed-market": "canonical:proposition:yes"},
    ))

    row = store.conn.execute(
        "SELECT venue,market_id,outcome_yes,resolved_at,first_seen_at,raw_json,"
        "canonical_proposition_id,source,source_url FROM outcomes"
    ).fetchone()
    assert (result.scanned, result.imported, result.skipped) == (1, 1, 0)
    assert row[:4] == ("polymarket-us", "fixed-market", 1, None)
    assert row[4] and row[6] == "canonical:proposition:yes"
    assert row[7] == "polymarket_us_official_settlement_endpoint"
    assert row[8].endswith("/fixed-market/settlement")
    evidence = json.loads(row[5])
    assert evidence["finality"] == "official_closed_market_and_settlement_endpoint"
    assert evidence["outcome_semantics"].startswith("official binary settlement")
    assert evidence["venue_resolved_at_available"] is False
    audit = store.conn.execute("SELECT COUNT(*) FROM outcome_observations").fetchone()[0]
    assert audit == 1
    store.conn.close()


def test_polymarket_open_or_nonbinary_settlement_stays_pending(tmp_path):
    store = OutcomeStore(str(tmp_path / "pending.db"))
    opened = asyncio.run(sync_polymarket_us_outcomes(
        store, FakePolymarketUS(closed=False), {"open-market": "prop"},
    ))
    fractional = asyncio.run(sync_polymarket_us_outcomes(
        store, FakePolymarketUS(closed=True, settlement=0.5), {"void-market": "prop"},
    ))
    assert opened.imported == fractional.imported == 0
    assert opened.skipped == fractional.skipped == 1
    assert store.conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0
    store.conn.close()


def test_outcome_store_preserves_distinct_authoritative_raw_evidence(tmp_path):
    store = OutcomeStore(str(tmp_path / "audit.db"))
    seen = datetime(2026, 9, 29, tzinfo=UTC)
    common = {
        "venue": "polymarket-us", "market_id": "market", "outcome_yes": 1,
        "resolved_at": None, "canonical_proposition_id": "prop",
        "source": "polymarket_us_official_settlement_endpoint",
        "source_url": "https://api.polymarket.us/v1/markets/market/settlement",
    }
    store.upsert(**common, raw={"settlement": {"settlement": 1, "slug": "market"}}, seen_at=seen)
    store.upsert(**common, raw={"settlement": {"settlement": 1, "slug": "market"}},
                 seen_at=datetime(2026, 9, 29, 0, 1, tzinfo=UTC))
    store.upsert(**common, raw={"settlement": {"settlement": 1, "slug": "market", "revision": 2}},
                 seen_at=datetime(2026, 9, 29, 0, 2, tzinfo=UTC))
    assert store.conn.execute("SELECT COUNT(*) FROM outcome_observations").fetchone()[0] == 2
    assert store.conn.execute("SELECT first_seen_at FROM outcomes").fetchone()[0] == seen.isoformat()
    store.conn.close()


def test_only_one_resolved_venue_keeps_pair_pending(tmp_path):
    decision_at = datetime.now(UTC)
    candidate = _candidate(decision_at)
    path = str(tmp_path / "one-leg.db")
    persist_evaluation(path, candidate, evaluate_candidate(candidate, now=decision_at))
    store = OutcomeStore(path)
    resolved_at = decision_at + timedelta(seconds=10)
    store.upsert(
        venue="kalshi:production", market_id="K-FIXTURE", outcome_yes=1,
        resolved_at=resolved_at.isoformat(),
        raw={"result": "yes", "settlement_ts": resolved_at.isoformat(),
             "finality": "official_kalshi_settlement"},
        source="kalshi_official_market_api", canonical_proposition_id="fixture:event:1:yes",
        seen_at=resolved_at + timedelta(seconds=1),
    )
    store.conn.close()

    state = mature_paper_pairs(path, now=resolved_at + timedelta(seconds=2))

    assert state["matured"] == 0
    assert state["pending"] == 1
    assert state["walk_forward"]["diagnostics_invoked"] is False


def test_polymarket_prospective_outcome_completes_end_to_end_pair_maturation(tmp_path):
    decision_at = datetime.now(UTC) - timedelta(minutes=1)
    candidate = _candidate(decision_at)
    path = str(tmp_path / "e2e-maturity.db")
    persist_evaluation(path, candidate, evaluate_candidate(candidate, now=decision_at))
    store = OutcomeStore(path)
    kalshi_resolved = decision_at + timedelta(seconds=10)
    store.upsert(
        venue="kalshi:production", market_id="K-FIXTURE", outcome_yes=1,
        resolved_at=kalshi_resolved.isoformat(),
        raw={"result": "yes", "settlement_ts": kalshi_resolved.isoformat(),
             "finality": "official_kalshi_settlement"},
        source="kalshi_official_market_api", canonical_proposition_id="fixture:event:1:yes",
        seen_at=kalshi_resolved + timedelta(seconds=1),
    )
    sync_result = asyncio.run(sync_polymarket_us_outcomes(
        store, FakePolymarketUS(), {"P-FIXTURE": "fixture:event:1:yes"},
    ))
    store.conn.close()

    state = mature_paper_pairs(path, now=datetime.now(UTC) + timedelta(seconds=1))
    conn = sqlite3.connect(path)
    try:
        event = conn.execute(
            "SELECT payload_json FROM economic_events WHERE event_type='paper_cross_venue_settlement'"
        ).fetchone()
    finally:
        conn.close()
    assert sync_result.imported == 1
    assert state["matured"] == 1
    assert state["walk_forward"]["diagnostics_invoked"] is False
    assert json.loads(event[0])["status"] == "settled"


def test_persistent_agent_sync_wires_both_official_outcome_adapters(tmp_path, monkeypatch):
    decision_at = datetime.now(UTC) - timedelta(minutes=1)
    candidate = _candidate(decision_at)
    path = str(tmp_path / "runtime-sync.db")
    persist_evaluation(path, candidate, evaluate_candidate(candidate, now=decision_at))
    kalshi_resolved = decision_at + timedelta(seconds=10)

    class FakeHistory:
        config = KalshiConfig(environment="production")

        async def settled_page(self, partition, *, cursor, limit):
            rows = ([{"ticker": "K-FIXTURE", "result": "yes",
                      "settlement_ts": kalshi_resolved.isoformat()}]
                    if partition == "live" else [])
            return rows, None

        async def close(self):
            pass

    class FakeVenue(FakePolymarketUS):
        pass

    monkeypatch.setattr(agent_runtime, "KalshiHistory", lambda _config: FakeHistory())
    monkeypatch.setattr(agent_runtime, "PolymarketUSVenue", FakeVenue)

    asyncio.run(agent_runtime._sync_outcomes(AgentConfig(db_path=path, max_outcomes_per_sync=10)))

    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT venue,market_id,canonical_proposition_id,source FROM outcomes "
            "ORDER BY venue"
        ).fetchall()
        settled = conn.execute(
            "SELECT payload_json FROM economic_events WHERE event_type='paper_cross_venue_settlement'"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [
        ("kalshi:production", "K-FIXTURE", "fixture:event:1:yes", "kalshi_official_market_api"),
        ("polymarket-us", "P-FIXTURE", "fixture:event:1:yes",
         "polymarket_us_official_settlement_endpoint"),
    ]
    assert len(settled) == 1
    assert json.loads(settled[0][0])["status"] == "settled"
