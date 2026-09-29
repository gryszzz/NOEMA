import asyncio

from noema import prediction_venues
from noema.canonical_comparability import (
    PROPOSITION_FAMILIES,
    EconomicQuote,
    SettlementTerms,
    assess_economic_comparability,
    assess_settlement_equivalence,
    identify_explicit_proposition,
    identify_kalshi_btc_numeric_bucket,
    identify_kalshi_eth_price_at_timestamp,
    identify_kalshi_rain_occurrence_by_deadline,
    identify_tesla_q3_deliveries_threshold,
)
from noema.canonical_market_identity import (
    compare_contract_identities,
    identify_mlb_world_series_champion,
    identify_structured_contract,
    registered_resolvers,
)
from noema.ledger import ForecastLedger


def _identity(venue: str, contract_id: str, season: int, text: str, rules: str):
    return identify_mlb_world_series_champion(
        venue=venue,
        contract_id=contract_id,
        season=season,
        title=text,
        outcome_text=text,
        resolution_rules=rules,
    )


def test_full_team_name_and_ticker_map_to_same_canonical_proposition():
    kalshi = _identity("kalshi", "KXMLB-26LAD", 2026, "Los Angeles Dodgers", "Rule A")
    polymarket = _identity(
        "polymarket-us", "world-series-champion-los-angeles-dodgers", 2026,
        "Will the Dodgers win?", "Rule B",
    )

    assert kalshi.outcome_id == polymarket.outcome_id == "LAD"
    comparison = compare_contract_identities(kalshi, polymarket)
    assert comparison["semantic_match"] == "confirmed"
    assert comparison["canonical_proposition_id"] == "sports:mlb:2026:world-series-champion:winner:LAD"
    assert comparison["settlement_equivalence"] == "unverified"
    assert comparison["settlement_rule_hashes_equal"] is False
    assert comparison["executable_comparison"] == "unavailable"


def test_same_rules_hash_does_not_claim_settlement_equivalence():
    left = _identity("kalshi", "KXMLB-26NYY", 2026, "New York Yankees", "Same text")
    right = _identity("polymarket-us", "ws-champion-yankees", 2026, "Yankees", "Same text")

    result = compare_contract_identities(left, right)

    assert result["semantic_match"] == "confirmed"
    assert result["settlement_rule_hashes_equal"] is True
    assert result["settlement_equivalence"] == "unverified"
    assert result["levels"]["same_event"] == "confirmed"
    assert result["levels"]["same_outcome"] == "confirmed"
    assert result["levels"]["economic_comparability"] == "unavailable"


def test_structured_identity_supports_new_resolvers_without_fuzzy_title_matching():
    left = identify_structured_contract(
        venue="kalshi", contract_id="K1", topic_id="economics:rates",
        canonical_event_id="economics:fed:2026-11", proposition_type="threshold",
        canonical_outcome_id="cut:25bp", resolution_rules="Rule A",
    )
    right = identify_structured_contract(
        venue="polymarket-us", contract_id="P1", topic_id="economics:rates",
        canonical_event_id="economics:fed:2026-11", proposition_type="threshold",
        canonical_outcome_id="cut:25bp", resolution_rules="Rule B",
    )
    comparison = compare_contract_identities(left, right)

    assert comparison["semantic_match"] == "confirmed"
    assert comparison["canonical_proposition_id"] == "economics:fed:2026-11:threshold:cut:25bp"
    assert comparison["levels"]["settlement_equivalence"] == "unverified"
    assert ("sports:baseball", "world_series_champion") in registered_resolvers()


def test_structured_identity_does_not_infer_event_or_outcome_from_text():
    unresolved = identify_structured_contract(
        venue="kalshi", contract_id="K2", topic_id="sports:baseball",
        canonical_event_id=None, proposition_type="winner",
        canonical_outcome_id=None,
    )
    assert unresolved.identity_status == "unresolved"
    assert unresolved.canonical_proposition_id is None


def test_matching_ids_under_different_topics_do_not_confirm_semantics():
    a = identify_structured_contract(
        venue="kalshi", contract_id="K1", topic_id="sports:baseball",
        canonical_event_id="event:2026", proposition_type="winner",
        canonical_outcome_id="team:ATL",
    )
    b = identify_structured_contract(
        venue="polymarket-us", contract_id="P1", topic_id="sports:football",
        canonical_event_id="event:2026", proposition_type="winner",
        canonical_outcome_id="team:ATL",
    )
    assert compare_contract_identities(a, b)["semantic_match"] == "unverified"


def test_unknown_or_different_season_outcomes_do_not_match():
    unknown = _identity("polymarket-us", "market-1", 2026, "A contender", "Rules")
    identified = _identity("kalshi", "KXMLB-27LAD", 2027, "Los Angeles Dodgers", "Rules")
    old_season = _identity("kalshi", "KXMLB-26LAD", 2026, "Los Angeles Dodgers", "Rules")

    assert unknown.identity_status == "unknown_outcome"
    assert compare_contract_identities(unknown, old_season)["semantic_match"] == "unverified"
    assert compare_contract_identities(identified, old_season)["semantic_match"] == "unverified"


def test_canonical_pair_observation_is_append_only_and_deduplicated(tmp_path):
    kalshi = _identity("kalshi", "KXMLB-26LAD", 2026, "Los Angeles Dodgers", "Rule A")
    polymarket = _identity("polymarket-us", "ws-champion-dodgers", 2026, "Dodgers", "Rule B")
    comparison = compare_contract_identities(kalshi, polymarket)
    observation = {
        "observed_at": "2026-09-28T12:00:00+00:00",
        "canonical_identity": {
            "comparison": comparison,
            "contracts": [kalshi.to_dict(), polymarket.to_dict()],
        },
        "kalshi": {"yes_bid": 0.16, "yes_ask": 0.17},
        "polymarket_us": {"yes_bid": 0.16, "yes_ask": 0.168},
    }
    ledger = ForecastLedger(str(tmp_path / "predictions.db"))

    assert ledger.append_canonical_pair_observation(observation) is True
    assert ledger.append_canonical_pair_observation(observation) is False
    rows = ledger.conn.execute(
        "SELECT canonical_event_id, canonical_proposition_id, settlement_equivalence "
        "FROM canonical_pair_observations"
    ).fetchall()
    graph_counts = {
        table: ledger.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("canonical_events", "canonical_propositions", "venue_contracts",
                      "contract_observations", "identity_assessments", "settlement_assessments")
    }
    ledger.conn.close()
    assert rows == [(
        "sports:mlb:2026:world-series-champion",
        "sports:mlb:2026:world-series-champion:winner:LAD",
        "unverified",
    )]
    assert graph_counts == {
        "canonical_events": 1, "canonical_propositions": 1, "venue_contracts": 2,
        "contract_observations": 2, "identity_assessments": 1,
        "settlement_assessments": 1,
    }


def test_venue_status_persists_canonical_candidate_in_configured_ledger(monkeypatch, tmp_path):
    kalshi = _identity("kalshi", "KXMLB-26LAD", 2026, "Los Angeles Dodgers", "Rule A")
    polymarket = _identity("polymarket-us", "ws-champion-dodgers", 2026, "Dodgers", "Rule B")
    candidate = {
        "observed_at": "2026-09-28T12:00:00+00:00",
        "canonical_identity": {
            "comparison": compare_contract_identities(kalshi, polymarket),
            "contracts": [kalshi.to_dict(), polymarket.to_dict()],
        },
        "settlement_assessment": {"status": "unverified", "unknown_fields": ["cancellation_policy"]},
        "economic_comparability": {"status": "unavailable", "unknown_fields": ["fee_model"]},
        "kalshi": {"market_id": kalshi.contract_id, "yes_ask": 0.17},
        "polymarket_us": {"market_id": polymarket.contract_id, "yes_ask": 0.168},
    }

    async def kalshi_status():
        return {"venue": "Kalshi", "market_data": {"status": "connected"}}

    async def polymarket_status():
        return {"venue": "Polymarket US", "market_data": {"status": "connected"}}

    async def cross_venue_candidate():
        return {"status": "candidate_only", "matches": [candidate]}

    monkeypatch.setenv("NOEMA_DB_PATH", str(tmp_path / "runtime.db"))
    monkeypatch.setattr(prediction_venues, "_kalshi_status", kalshi_status)
    monkeypatch.setattr(prediction_venues, "_polymarket_us_status", polymarket_status)
    monkeypatch.setattr(prediction_venues, "_cross_venue_candidate", cross_venue_candidate)
    monkeypatch.setattr(prediction_venues, "_registered_quote_pair", None)
    monkeypatch.setattr(prediction_venues, "_registered_identity_review_at", 0.0)

    payload = asyncio.run(prediction_venues.build_prediction_venue_status(force=True))

    assert payload["cross_venue_comparison"]["persistence_status"] == "recorded"
    identity_status = payload["canonical_market_identity"]
    assert {item["family"] for item in identity_status["proposition_families"]} == set(PROPOSITION_FAMILIES)
    assert identity_status["coverage"]["slow_path"].startswith("discovery")
    assert "world_series_champion" in identity_status["coverage"]["live_resolver_families"][0]
    loaded_pair = prediction_venues._load_registered_quote_pair()
    assert loaded_pair is not None
    assert [item["venue"] for item in loaded_pair[0]["canonical_identity"]["contracts"]] == [
        "kalshi", "polymarket-us",
    ]
    import sqlite3

    connection = sqlite3.connect(tmp_path / "runtime.db")
    persisted = connection.execute("SELECT count(*) FROM canonical_pair_observations").fetchone()[0]
    evidence_counts = (
        connection.execute("SELECT count(*) FROM economic_comparability_assessments").fetchone()[0],
        connection.execute("SELECT count(*) FROM canonical_quote_observations").fetchone()[0],
        connection.execute("SELECT count(*) FROM economic_events WHERE event_type='canonical_market_observation'").fetchone()[0],
    )
    connection.close()
    assert persisted == 1
    assert evidence_counts == (1, 1, 1)


def test_distinct_proposition_structures_require_all_explicit_identity_fields():
    # Live catalog inspection found distinct structures across the venues,
    # but only the championship winner currently has a vetted shared source
    # mapping. Without source-mapped fields the other shapes stay out.
    assert set(PROPOSITION_FAMILIES) == {
        "winner", "occurrence_by_deadline", "numeric_threshold", "numeric_bucket",
        "asset_price_at_time", "economic_release_bucket",
    }
    for family in PROPOSITION_FAMILIES:
        identity = identify_explicit_proposition(
            venue="kalshi", contract_id=f"observed-{family}", topic_id=f"test:{family}",
            event_id=f"event:{family}:2026", proposition_type=family, fields={},
        )
        assert identity.identity_status == "unresolved"
        assert identity.canonical_proposition_id is None
        ladder = compare_contract_identities(identity, identity)
        assert list(ladder["levels"]) == [
            "same_topic", "same_event", "same_proposition", "same_outcome",
            "semantic_match", "settlement_equivalence", "economic_comparability",
            "executable_comparability",
        ]
        assert ladder["semantic_match"] == "unverified"


def test_current_live_milwaukee_championship_pair_resolves_but_not_settlement():
    # Contract identifiers observed from current public Kalshi and Polymarket
    # US catalogs on 2026-09-29. Their venue-specific rule texts remain distinct.
    kalshi = identify_mlb_world_series_champion(
        venue="kalshi", contract_id="KXMLB-26-MIL", season=2026,
        title="Will Milwaukee win the 2026 Pro Baseball Championship?",
        outcome_text="MIL", resolution_rules="If Milwaukee wins the 2026 Pro Baseball Championship, resolves Yes.",
    )
    polymarket = identify_mlb_world_series_champion(
        venue="polymarket-us", contract_id="tec-mlb-champ-2026-09-27-mil", season=2026,
        title="Milwaukee Brewers", outcome_text="tec-mlb-champ-2026-09-27-mil",
        resolution_rules="Will Milwaukee Brewers win the 2026 MLB World Series?",
    )
    comparison = compare_contract_identities(kalshi, polymarket)
    assert comparison["semantic_match"] == "confirmed"
    assert comparison["settlement_equivalence"] == "unverified"
    assert comparison["executable_comparison"] == "unavailable"


def test_current_live_tesla_delivery_threshold_pair_uses_strict_ids_and_rules():
    kalshi = identify_tesla_q3_deliveries_threshold(
        venue="kalshi", contract_id="KXTSLA-26OCTDELIV-520000",
        resolution_rules=(
            "If Tesla Inc. reports Above 520000 total deliveries in Q3 2026, "
            "then the market resolves to Yes."
        ),
    )
    polymarket = identify_tesla_q3_deliveries_threshold(
        venue="polymarket-us", contract_id="kpic-tsla-dlvrs-2026-q3-above-520k",
        resolution_rules=(
            "This market will settle to Yes if Tesla reports total deliveries greater than "
            "520000 for the 3rd fiscal quarter of 2026. Outcome sourced from Tesla."
        ),
    )
    comparison = compare_contract_identities(kalshi, polymarket)
    assert comparison["semantic_match"] == "confirmed"
    assert comparison["canonical_proposition_id"] == polymarket.canonical_proposition_id
    assert comparison["settlement_equivalence"] == "unverified"
    assert comparison["executable_comparison"] == "unavailable"
    wrong_threshold = identify_tesla_q3_deliveries_threshold(
        venue="polymarket-us", contract_id="kpic-tsla-dlvrs-2026-q3-above-510k",
        resolution_rules=(
            "This market will settle to Yes if Tesla reports total deliveries greater than "
            "520000 for the 3rd fiscal quarter of 2026. Outcome sourced from Tesla."
        ),
    )
    assert wrong_threshold.identity_status == "unresolved"
    assert compare_contract_identities(kalshi, wrong_threshold)["semantic_match"] == "unverified"
    assert ("company:automotive", "q3_deliveries_numeric_threshold") in registered_resolvers()


def test_live_occurrence_and_timestamped_asset_contracts_resolve_from_source_fields():
    rain = identify_kalshi_rain_occurrence_by_deadline(
        venue="kalshi", contract_id="KXRAIN-26SEP29-NYC",
        resolution_rules=(
            "If the total precipitation at CLINYC in New York City in Sep 29, 2026 "
            "is strictly greater than 0 inches, then the market resolves to Yes."
        ), close_time="2026-09-30T05:00:00Z",
    )
    eth = identify_kalshi_eth_price_at_timestamp(
        venue="kalshi", contract_id="KXETH-26SEP2901-T1905",
        resolution_rules=(
            "If the simple average of the sixty seconds of CF Benchmarks' Ethereum Real-Time "
            "Index (ERTI) before 1 AM EDT is below 1905 at 1 AM EDT on Sep 29, 2026, "
            "then the market resolves to Yes."
        ), close_time="2026-09-29T05:00:00Z",
    )
    assert rain.identity_status == "identified"
    assert rain.proposition_type == "occurrence_by_deadline"
    assert eth.identity_status == "identified"
    assert eth.proposition_type == "asset_price_at_time"
    assert ("weather:precipitation", "daily_station_precipitation_occurrence") in registered_resolvers()
    assert ("asset:ethereum:price", "eth_price_at_timestamp") in registered_resolvers()


def test_live_bitcoin_bucket_with_ambiguous_endpoints_stays_unresolved():
    identity = identify_kalshi_btc_numeric_bucket(
        venue="kalshi", contract_id="KXBTC-26SEP2901-B73250",
        resolution_rules=(
            "If the simple average of the sixty seconds of CF Benchmarks' Bitcoin Real-Time "
            "Index (BRTI) before 1 AM EDT is between 73200-73299.99 at 1 AM EDT on Sep 29, "
            "2026, then the market resolves to Yes."
        ), close_time="2026-09-29T05:00:00Z",
    )
    assert identity.identity_status == "unresolved"
    assert identity.canonical_proposition_id is None


def test_settlement_assessment_is_evidence_complete_and_revocable():
    shared = SettlementTerms(
        authoritative_resolution_source="official league final results",
        measurement_publication_source="official league final results",
        cutoff_timestamp_utc="2026-11-01T23:59:59Z", timezone="UTC",
        preliminary_or_final="final", revision_correction_policy="final publication",
        cancellation_policy="void if event canceled", postponement_policy="reschedule",
        rescheduling_policy="reschedule", tie_dead_heat_policy="dead heat split",
        rounding_policy="none", exceptional_conditions="standard conditions only",
        expiration_policy="event completion", dispute_handling="venue rulebook process",
        evidence_ref="official-rule-page", evidence_sha256="a" * 64,
        rule_version="v1", observed_at="2026-09-29T12:00:00Z",
    )
    assert assess_settlement_equivalence(shared, shared)["status"] == "confirmed"
    revised = SettlementTerms(**{**shared.__dict__, "cancellation_policy": "settle last fair value", "rule_version": "v2"})
    assessment = assess_settlement_equivalence(shared, revised)
    assert assessment["status"] == "mismatch"
    assert assessment["differences"] == ["cancellation_policy"]
    assert assess_settlement_equivalence(SettlementTerms(), shared)["status"] == "unverified"


def test_economic_comparability_stays_unavailable_until_every_required_input_is_known():
    partial_left = EconomicQuote(bid="0.3", ask="0.4", denomination="USD", quote_timestamp="t")
    partial_right = EconomicQuote(bid="0.3", ask="0.4", denomination="USD", quote_timestamp="t")
    result = assess_economic_comparability(partial_left, partial_right, settlement_status="confirmed")
    assert result["status"] == "unavailable"
    assert result["executable_comparability"] == "unavailable"
    complete = EconomicQuote(
        fee_model="verified:v1", denomination="USD", bid="0.3", ask="0.4",
        quote_timestamp="2026-09-29T12:00:00+00:00", venue_timestamp="2026-09-29T12:00:00+00:00", freshness_seconds=1,
        available_depth="10", minimum_size="1", maximum_usable_size="10",
        expected_slippage="0", capital_lock_seconds=60,
        evidence_ref="captured-bbo", observed_at="2026-09-29T12:00:01+00:00",
    )
    assert assess_economic_comparability(complete, complete, settlement_status="confirmed")["status"] == "comparable"
    assert assess_economic_comparability(complete, complete, settlement_status="unverified")["status"] == "unavailable"
    malformed = EconomicQuote(**{**complete.__dict__, "bid": "0.8", "ask": "0.2"})
    malformed_assessment = assess_economic_comparability(
        malformed, complete, settlement_status="confirmed",
    )
    assert malformed_assessment["status"] == "unavailable"
    assert "left.bid_ask_order" in malformed_assessment["invalid_fields"]


def test_settlement_and_economic_assessments_append_without_overwriting(tmp_path):
    ledger = ForecastLedger(str(tmp_path / "canonical.db"))
    old = {"status": "confirmed", "evidence_ref": "rules-v1"}
    changed = {"status": "mismatch", "evidence_ref": "rules-v2"}
    assert ledger.append_settlement_assessment("event:prop", old, observed_at="t1")
    assert ledger.append_settlement_assessment("event:prop", changed, observed_at="t2")
    assert ledger.append_economic_comparability_assessment(
        "event:prop", {"status": "unavailable", "unknown_fields": ["fee_model"]}, observed_at="t2",
    )
    assert ledger.conn.execute(
        "SELECT status FROM settlement_assessments ORDER BY id"
    ).fetchall() == [("confirmed",), ("mismatch",)]
    assert ledger.conn.execute(
        "SELECT status FROM economic_comparability_assessments"
    ).fetchall() == [("unavailable",)]
    ledger.conn.close()
    reopened = ForecastLedger(str(tmp_path / "canonical.db"))
    assert reopened.conn.execute(
        "SELECT status FROM settlement_assessments ORDER BY id"
    ).fetchall() == [("confirmed",), ("mismatch",)]
    reopened.conn.close()


def test_registered_quote_fast_path_does_not_rerun_identity_resolver(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"market": {"yes_bid_dollars": "0.31", "yes_ask_dollars": "0.34"}}

    class KalshiClient:
        async def get(self, _path):
            return Response()

        async def aclose(self):
            return None

    class FakeKalshi:
        def __init__(self, *_args, **_kwargs):
            self.client = KalshiClient()

        async def close(self):
            return None

    class PMClient:
        class Markets:
            def bbo(self, slug):
                return {"marketData": {"marketSlug": slug,
                                       "bestBid": {"value": "0.30"},
                                       "bestAsk": {"value": "0.35"}}}

        markets = Markets()

    class FakePolymarket:
        def __init__(self, *_args, **_kwargs):
            self.client = PMClient()

        def close(self):
            return None

    monkeypatch.setattr(prediction_venues, "KalshiVenue", FakeKalshi)
    monkeypatch.setattr(prediction_venues, "PolymarketUSVenue", FakePolymarket)
    monkeypatch.setattr(
        prediction_venues, "identify_mlb_world_series_champion",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("slow resolver called")),
    )
    previous = {
        "canonical_identity": {"contracts": [
            {"venue": "kalshi", "contract_id": "live-kalshi-id"},
            {"venue": "polymarket-us", "contract_id": "live-pm-id"},
        ]},
        "settlement_assessment": {"status": "unverified"},
        "kalshi": {}, "polymarket_us": {},
    }
    refreshed = asyncio.run(prediction_venues._refresh_registered_pair(previous))
    assert refreshed["quote_path"] == "fast_quote_refresh"
    assert refreshed["kalshi"]["yes_bid"] == 0.31
    assert refreshed["polymarket_us"]["yes_ask"] == 0.35
    assert refreshed["economic_comparability"]["status"] == "unavailable"
