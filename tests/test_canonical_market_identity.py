import asyncio

from noema import prediction_venues
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

    payload = asyncio.run(prediction_venues.build_prediction_venue_status(force=True))

    assert payload["cross_venue_comparison"]["persistence_status"] == "recorded"
    import sqlite3

    connection = sqlite3.connect(tmp_path / "runtime.db")
    persisted = connection.execute("SELECT count(*) FROM canonical_pair_observations").fetchone()[0]
    connection.close()
    assert persisted == 1
