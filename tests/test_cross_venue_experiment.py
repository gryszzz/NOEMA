from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from noema.cross_venue_experiment import (
    evaluate_candidate,
    extract_clause_evidence,
    mature_paper_pairs,
    persist_evaluation,
    recent_evaluations,
)
from noema.outcomes import OutcomeStore


def _candidate(now: datetime) -> dict:
    clauses = {
        name: {
            "kalshi": f"verified:{name}", "polymarket_us": f"verified:{name}",
            "citations": {"kalshi": "fixture://kalshi/rules", "polymarket_us": "fixture://pm/rules"},
        }
        for name in (
            "resolution_condition", "outcome_definition", "settlement_authority",
            "event_timing", "postponement", "cancellation", "void_refund", "edge_cases",
            "rule_revisions", "settlement_rounding", "exceptional_conditions",
        )
    }
    common_fees = {
        "verified": True,
        "schedule_version": "fixture-fees-v1",
        "maker_taker_assumption": "taker",
        "rounding": "per-contract fixture amount",
        "terms": {"type": "flat_per_contract", "amount_usd": "0.01"},
        "unknown_fields": [],
    }
    kalshi_depth = {
        "yes_asks": [{"price": "0.55", "contracts": "3"}],
        "yes_bids": [{"price": "0.50", "contracts": "3"}],
    }
    polymarket_depth = {
        "yes_asks": [{"price": "0.35", "contracts": "3"}],
        "yes_bids": [{"price": "0.30", "contracts": "3"}],
    }
    timestamp = (now - timedelta(seconds=1)).isoformat()
    return {
        "observed_at": timestamp,
        "canonical_identity": {
            "comparison": {
                "semantic_match": "confirmed",
                "canonical_event_id": "fixture:event:1",
                "canonical_proposition_id": "fixture:event:1:yes",
                "settlement_equivalence": "unverified",
            },
            "contracts": [
                {"venue": "kalshi", "contract_id": "K-FIXTURE",
                 "canonical_event_id": "fixture:event:1",
                 "canonical_proposition_id": "fixture:event:1:yes", "active_at_observation": True},
                {"venue": "polymarket_us", "contract_id": "P-FIXTURE",
                 "canonical_event_id": "fixture:event:1",
                 "canonical_proposition_id": "fixture:event:1:yes", "active_at_observation": True},
            ],
        },
        "contract_clauses": {**clauses, "_source_text": {
            "kalshi": "Official rules: The designated champion wins.",
            "polymarket_us": "Official rules: The designated champion wins.",
        }},
        "evidence_citations": [{"source": "test fixture", "identifier": "fixture://contract-rules"}],
        "kalshi": {
            "market_id": "K-FIXTURE", "yes_bid": 0.50, "yes_ask": 0.55,
            "quote_observed_at": timestamp, "source_timestamp": timestamp,
            "source_timestamp_semantics": "documented_quote_event_time",
            "fees": {**common_fees, "venue": "kalshi"},
            "normalized_depth": kalshi_depth, "capacity": 3,
        },
        "polymarket_us": {
            "market_id": "P-FIXTURE", "yes_bid": 0.30, "yes_ask": 0.35,
            "quote_observed_at": timestamp, "source_timestamp": timestamp,
            "source_timestamp_semantics": "documented_quote_event_time",
            "fees": {**common_fees, "venue": "polymarket_us"},
            "normalized_depth": polymarket_depth, "capacity": 3,
        },
    }


def test_equivalent_fresh_fee_known_depth_pair_is_simulated_paper_only():
    now = datetime.now(UTC)
    result = evaluate_candidate(_candidate(now), now=now)

    assert result["contract_equivalence"]["status"] == "verified"
    assert result["freshness"]["status"] == "verified"
    assert result["depth"]["capacity"] == "unknown"
    assert result["depth"]["one_contract_eligible"] is True
    assert result["verdict"] == "PAPER_RESULT_POSITIVE"
    assert result["paper_simulation"]["paired_fill"]["target_contracts"] == "1"
    assert result["paper_simulation"]["paired_fill"]["net_result_usd"] == "0.13"
    assert result["orders_created"] is False
    assert result["live_execution_enabled"] is False
    assert result["chronological_validation"]["diagnostics_invoked"] is False
    assert result["chronological_validation"]["status"] == "not_run_insufficient_forward_outcomes"


def test_unverified_contract_rules_take_precedence_and_unknown_fees_never_become_zero():
    now = datetime.now(UTC)
    candidate = _candidate(now)
    candidate.pop("contract_clauses")
    candidate["polymarket_us"]["fees"] = {"verified": False, "unknown_fields": ["fee_terms"]}

    result = evaluate_candidate(candidate, now=now)

    assert result["verdict"] == "REJECTED_RULES_UNVERIFIED"
    assert "settlement equivalence is unverified" in " ".join(result["rejection_reasons"])
    assert "venue fee schedule" in " ".join(result["rejection_reasons"])
    assert result["quotes"]["polymarket_us"]["fee_amount_or_terms"] is None
    assert result["paper_simulation"]["status"] == "not_run"

    fees_only = _candidate(now)
    fees_only["polymarket_us"]["fees"] = {"verified": False, "unknown_fields": ["fee_terms"]}
    assert evaluate_candidate(fees_only, now=now)["verdict"] == "REJECTED_FEES_UNKNOWN"


def test_clause_extraction_retains_citations_and_ambiguous_language_blocks_equivalence():
    rules = "If the event is canceled, this market may settle to fair market price."
    extracted = extract_clause_evidence(
        "polymarket_us", "P-1", rules, "https://api.polymarket.us/v1/market/slug/P-1",
    )
    clause = extracted["cancellation"]
    assert clause["status"] == "unresolved"
    assert clause["citation"]["excerpt"] == rules
    assert clause["citation"]["text_sha256"]
    assert clause["citation"]["source_url"].endswith("P-1")

    now = datetime.now(UTC)
    candidate = _candidate(now)
    candidate["contract_clauses"]["cancellation"]["polymarket_us"] = {
        "text": clause["text"], "status": clause["status"],
        "source_evidence": clause["citation"],
    }
    candidate["contract_clauses"]["cancellation"]["citations"]["polymarket_us"] = clause["citation"]
    assert evaluate_candidate(candidate, now=now)["verdict"] == "REJECTED_RULES_UNVERIFIED"


def test_mismatch_staleness_missing_depth_and_missing_quotes_are_rejected():
    now = datetime.now(UTC)
    mismatch = _candidate(now)
    mismatch["contract_clauses"]["void_refund"]["polymarket_us"] = "different"
    mismatch["contract_clauses"]["void_refund"]["comparison_status"] = "mismatch"
    assert evaluate_candidate(mismatch, now=now)["verdict"] == "REJECTED_RULE_MISMATCH"

    stale = _candidate(now)
    stale["kalshi"]["source_timestamp"] = None
    assert evaluate_candidate(stale, now=now)["verdict"] == "REJECTED_STALE_DATA"

    receipt_only = _candidate(now)
    receipt_only["polymarket_us"]["source_timestamp_semantics"] = "NOEMA receipt time only"
    assert evaluate_candidate(receipt_only, now=now)["verdict"] == "REJECTED_STALE_DATA"

    no_depth = _candidate(now)
    no_depth["kalshi"]["normalized_depth"] = None
    no_depth["kalshi"]["capacity"] = "unknown"
    assert evaluate_candidate(no_depth, now=now)["verdict"] == "REJECTED_MISSING_DEPTH"

    partial_depth = _candidate(now)
    partial_depth["polymarket_us"]["normalized_depth"]["yes_asks"][0]["contracts"] = "0.5"
    partial_depth["polymarket_us"]["capacity"] = 0.5
    failed = evaluate_candidate(partial_depth, now=now)
    assert failed["verdict"] == "REJECTED_MISSING_DEPTH"
    assert failed["paper_simulation"]["status"] == "not_run"

    no_quotes = _candidate(now)
    no_quotes["kalshi"]["yes_ask"] = None
    assert evaluate_candidate(no_quotes, now=now)["verdict"] == "REJECTED_MISSING_QUOTES"

    cost_erased = _candidate(now)
    cost_erased["kalshi"]["yes_bid"] = 0.1
    cost_erased["kalshi"]["yes_ask"] = 0.8
    cost_erased["polymarket_us"]["yes_bid"] = 0.1
    cost_erased["polymarket_us"]["yes_ask"] = 0.7
    cost_erased["kalshi"]["normalized_depth"]["yes_bids"][0]["price"] = "0.1"
    cost_erased["kalshi"]["normalized_depth"]["yes_asks"][0]["price"] = "0.8"
    cost_erased["polymarket_us"]["normalized_depth"]["yes_asks"][0]["price"] = "0.7"
    cost_erased["polymarket_us"]["normalized_depth"]["yes_bids"][0]["price"] = "0.1"
    assert evaluate_candidate(cost_erased, now=now)["verdict"] == "REJECTED_COSTS_ERASE_EDGE"


def test_persisted_evaluation_registers_trial_run_and_append_only_evidence(tmp_path):
    now = datetime.now(UTC)
    candidate = _candidate(now)
    candidate.pop("contract_clauses")
    result = evaluate_candidate(candidate, now=now)
    path = str(tmp_path / "cross-venue.db")

    first = persist_evaluation(path, candidate, result)
    second = persist_evaluation(path, candidate, result)

    assert first["persistence_status"] == "recorded"
    assert second["persistence_status"] == "already_recorded"
    assert first["trial_id"] == second["trial_id"]
    assert first["run_id"] == second["run_id"]
    history = recent_evaluations(path)
    assert len(history) == 1
    assert history[0]["trial_id"] == first["trial_id"]
    assert history[0]["research_run_id"] == first["run_id"]
    assert history[0]["experiment_evaluation"]["verdict"] == "REJECTED_RULES_UNVERIFIED"

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM canonical_pair_observations").fetchone()[0] == 1
        trial = conn.execute("SELECT family,status,params_json FROM research_trials").fetchone()
        assert trial[0:2] == ("prediction_markets_cross_venue_paper", "rejected")
        assert '"variant_id":"one_contract_taker_pair_v1"' in trial[2]
        run = conn.execute(
            "SELECT kind,status,compute_cost_usd,result_json FROM autonomous_research_runs"
        ).fetchone()
        assert run[0:3] == ("cross_venue_paper_experiment", "completed", None)
        assert '"verdict":"REJECTED_RULES_UNVERIFIED"' in run[3]
    finally:
        conn.close()


def test_simulated_pair_is_recorded_as_unsettled_non_cash_economic_event(tmp_path):
    now = datetime.now(UTC)
    candidate = _candidate(now)
    result = evaluate_candidate(candidate, now=now)
    path = str(tmp_path / "paper-pair.db")

    persist_evaluation(path, candidate, result)
    persist_evaluation(path, candidate, result)

    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT event_type,amount_usd,payload_json FROM economic_events"
        ).fetchall()
        assert len(rows) == 2
        simulation = next(row for row in rows if row[0] == "paper_cross_venue_simulation")
        canonical_result = next(row for row in rows if row[0] == "paper_result")
        assert simulation[1] is None
        assert canonical_result[1] == "0.13"
        payload = json.loads(simulation[2])
        assert payload["settled"] is False
        assert payload["live_execution"] is False
        assert payload["simulated_net_if_settled_usd"] == "0.13"
        row = conn.execute(
            "SELECT value_state,capital_class,reconciliation_state FROM economic_events "
            "WHERE event_type='paper_result'"
        ).fetchone()
        assert row == ("paper", "none", "OBSERVED")
    finally:
        conn.close()


def test_paper_pair_matures_only_from_prospective_matching_venue_outcomes(tmp_path):
    decision_at = datetime.now(UTC)
    candidate = _candidate(decision_at)
    result = evaluate_candidate(candidate, now=decision_at)
    path = str(tmp_path / "pair-maturity.db")
    persisted = persist_evaluation(path, candidate, result)
    outcomes = OutcomeStore(path)
    resolved_at = (decision_at + timedelta(seconds=10)).isoformat()
    seen_at = (decision_at + timedelta(seconds=11)).isoformat()
    outcomes.upsert(venue="kalshi", market_id="K-FIXTURE", outcome_yes=1,
                    resolved_at=resolved_at, raw={"result": "yes", "settlement_ts": resolved_at,
                                                  "finality": "official_kalshi_settlement"},
                    source="kalshi_official_market_api",
                    canonical_proposition_id="fixture:event:1:yes",
                    seen_at=datetime.fromisoformat(seen_at))
    outcomes.upsert(venue="polymarket-us", market_id="P-FIXTURE", outcome_yes=1,
                    resolved_at=None, raw={"finality": "official_closed_market_and_settlement_endpoint",
                                           "market": {"closed": True},
                                           "settlement": {"slug": "P-FIXTURE", "settlement": 1},
                                           "venue_resolved_at_available": False},
                    source="polymarket_us_official_settlement_endpoint",
                    canonical_proposition_id="fixture:event:1:yes",
                    seen_at=datetime.fromisoformat(seen_at))
    outcomes.conn.close()

    maturity = mature_paper_pairs(path, now=decision_at + timedelta(seconds=12))
    history = recent_evaluations(path)
    conn = sqlite3.connect(path)
    try:
        event = conn.execute(
            "SELECT amount_usd,payload_json FROM economic_events "
            "WHERE event_type='paper_cross_venue_settlement'"
        ).fetchone()
    finally:
        conn.close()

    assert maturity["matured"] == 1
    assert maturity["walk_forward"]["status"] == "insufficient_independent_matured_events"
    assert event[0] is None
    event_payload = json.loads(event[1])
    assert event_payload["realized_net_if_paper_usd"] == "0.13"
    assert all(item["evidence_sha256"] for item in event_payload["settled_venues"].values())
    assert event_payload["settled_venues"]["polymarket_us"]["resolved_at"] is None
    assert history[0]["paper_settlement"]["evidence_hash"] == persisted["evidence_hash"]


def test_console_maturation_reads_worker_outcomes_but_writes_only_durable_console_state(tmp_path):
    import os
    import stat

    decision_at = datetime.now(UTC)
    candidate = _candidate(decision_at)
    state_path = str(tmp_path / "console-state.db")
    worker_path = tmp_path / "worker-snapshot.db"
    persisted = persist_evaluation(state_path, candidate, evaluate_candidate(candidate, now=decision_at))
    outcomes = OutcomeStore(str(worker_path))
    resolved_at = (decision_at + timedelta(seconds=10)).isoformat()
    seen_at = decision_at + timedelta(seconds=11)
    outcomes.upsert(venue="kalshi", market_id="K-FIXTURE", outcome_yes=1,
                    resolved_at=resolved_at, raw={"result": "yes", "settlement_ts": resolved_at,
                                                   "finality": "official_kalshi_settlement"},
                    source="kalshi_official_market_api",
                    canonical_proposition_id="fixture:event:1:yes", seen_at=seen_at)
    outcomes.upsert(venue="polymarket-us", market_id="P-FIXTURE", outcome_yes=1,
                    resolved_at=None, raw={"finality": "official_closed_market_and_settlement_endpoint",
                                           "market": {"closed": True},
                                           "settlement": {"slug": "P-FIXTURE", "settlement": 1},
                                           "venue_resolved_at_available": False},
                    source="polymarket_us_official_settlement_endpoint",
                    canonical_proposition_id="fixture:event:1:yes", seen_at=seen_at)
    outcomes.conn.close()
    os.chmod(worker_path, 0o444)
    snapshot_before = worker_path.read_bytes()

    maturity = mature_paper_pairs(
        state_path, outcome_path=str(worker_path), now=decision_at + timedelta(seconds=12),
    )
    history = recent_evaluations(state_path)

    assert maturity["matured"] == 1
    assert history[0]["paper_settlement"]["evidence_hash"] == persisted["evidence_hash"]
    assert worker_path.read_bytes() == snapshot_before
    assert stat.S_IMODE(worker_path.stat().st_mode) == 0o444
    with sqlite3.connect(state_path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM economic_events WHERE event_type='paper_cross_venue_settlement'"
        ).fetchone()[0] == 1
    with sqlite3.connect(worker_path.as_uri() + "?mode=ro", uri=True) as conn:
        assert conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='economic_events'"
        ).fetchone()[0] == 0


def test_outcomes_resolved_before_observation_do_not_backfill_paper_results(tmp_path):
    decision_at = datetime.now(UTC)
    candidate = _candidate(decision_at)
    persist_evaluation(str(tmp_path / "lookahead.db"), candidate,
                       evaluate_candidate(candidate, now=decision_at))
    path = str(tmp_path / "lookahead.db")
    outcomes = OutcomeStore(path)
    outcomes.upsert(venue="kalshi", market_id="K-FIXTURE", outcome_yes=1,
                    resolved_at=(decision_at - timedelta(seconds=1)).isoformat(),
                    raw={"result": "yes", "settlement_ts": (decision_at - timedelta(seconds=1)).isoformat(),
                         "finality": "official_kalshi_settlement"}, source="kalshi_official_market_api",
                    canonical_proposition_id="fixture:event:1:yes",
                    seen_at=decision_at + timedelta(seconds=1))
    outcomes.upsert(venue="polymarket-us", market_id="P-FIXTURE", outcome_yes=1,
                    resolved_at=(decision_at - timedelta(seconds=1)).isoformat(),
                    raw={"finality": "official_closed_market_and_settlement_endpoint",
                         "market": {"closed": True},
                         "settlement": {"slug": "P-FIXTURE", "settlement": 1}},
                    source="polymarket_us_official_settlement_endpoint",
                    canonical_proposition_id="fixture:event:1:yes",
                    seen_at=decision_at + timedelta(seconds=1))
    outcomes.conn.close()

    maturity = mature_paper_pairs(path, now=decision_at + timedelta(seconds=2))

    assert maturity["matured"] == 0
    assert maturity["pending"] == 1


def test_four_independent_matured_events_invoke_existing_walk_forward_path(tmp_path):
    base = datetime.now(UTC)
    path = str(tmp_path / "walk-forward.db")
    outcomes = OutcomeStore(path)
    for index in range(4):
        decision_at = base + timedelta(seconds=index)
        candidate = _candidate(decision_at)
        event_id = f"fixture:event:{index}"
        proposition_id = f"{event_id}:yes"
        candidate["canonical_identity"]["comparison"]["canonical_event_id"] = event_id
        candidate["canonical_identity"]["comparison"]["canonical_proposition_id"] = proposition_id
        for contract in candidate["canonical_identity"]["contracts"]:
            contract["canonical_event_id"] = event_id
            contract["canonical_proposition_id"] = proposition_id
        candidate["kalshi"]["market_id"] = f"K-FIXTURE-{index}"
        candidate["polymarket_us"]["market_id"] = f"P-FIXTURE-{index}"
        candidate["kalshi"]["ticker"] = f"K-FIXTURE-{index}"
        candidate["polymarket_us"]["ticker"] = f"P-FIXTURE-{index}"
        persist_evaluation(path, candidate, evaluate_candidate(candidate, now=decision_at))
        resolved = (decision_at + timedelta(seconds=10)).isoformat()
        seen = decision_at + timedelta(seconds=11)
        outcomes.upsert(venue="kalshi", market_id=f"K-FIXTURE-{index}", outcome_yes=1,
                        resolved_at=resolved, raw={"result": "yes", "settlement_ts": resolved,
                                                   "finality": "official_kalshi_settlement"},
                        source="kalshi_official_market_api",
                        canonical_proposition_id=proposition_id, seen_at=seen)
        outcomes.upsert(venue="polymarket-us", market_id=f"P-FIXTURE-{index}", outcome_yes=1,
                        resolved_at=None, raw={"finality": "official_closed_market_and_settlement_endpoint",
                                               "market": {"closed": True},
                                               "settlement": {"slug": f"P-FIXTURE-{index}", "settlement": 1},
                                               "venue_resolved_at_available": False},
                        source="polymarket_us_official_settlement_endpoint",
                        canonical_proposition_id=proposition_id, seen_at=seen)
    outcomes.conn.close()

    audit = mature_paper_pairs(path, now=base + timedelta(seconds=20))

    assert audit["matured"] == 4
    assert audit["walk_forward"]["status"] == "walk_forward_invoked"
    assert audit["walk_forward"]["diagnostics_invoked"] is True
    assert audit["walk_forward"]["variant_count"] == 1
    assert audit["walk_forward"]["overfit_diagnostics"] == "not_applicable_single_predeclared_variant"


def test_contradictory_venue_settlements_are_recorded_as_rule_divergence(tmp_path):
    decision_at = datetime.now(UTC)
    candidate = _candidate(decision_at)
    path = str(tmp_path / "rule-divergence.db")
    persist_evaluation(path, candidate, evaluate_candidate(candidate, now=decision_at))
    outcomes = OutcomeStore(path)
    outcomes.upsert(venue="kalshi", market_id="K-FIXTURE", outcome_yes=1,
                    resolved_at=(decision_at + timedelta(seconds=10)).isoformat(),
                    raw={"result": "yes", "settlement_ts": (decision_at + timedelta(seconds=10)).isoformat(),
                         "finality": "official_kalshi_settlement"},
                    source="kalshi_official_market_api",
                    canonical_proposition_id="fixture:event:1:yes",
                    seen_at=decision_at + timedelta(seconds=11))
    outcomes.upsert(venue="polymarket-us", market_id="P-FIXTURE", outcome_yes=0,
                    resolved_at=None,
                    raw={"finality": "official_closed_market_and_settlement_endpoint",
                         "market": {"closed": True},
                         "settlement": {"slug": "P-FIXTURE", "settlement": 0},
                         "venue_resolved_at_available": False},
                    source="polymarket_us_official_settlement_endpoint",
                    canonical_proposition_id="fixture:event:1:yes",
                    seen_at=decision_at + timedelta(seconds=11))
    outcomes.conn.close()

    maturity = mature_paper_pairs(path, now=decision_at + timedelta(seconds=12))
    conn = sqlite3.connect(path)
    try:
        payload = json.loads(conn.execute(
            "SELECT payload_json FROM economic_events WHERE event_type='paper_cross_venue_settlement'"
        ).fetchone()[0])
    finally:
        conn.close()

    assert maturity["matured"] == 0
    assert maturity["rule_divergences"] == 1
    assert payload["status"] == "settlement_rule_divergence"
    assert "realized_net_if_paper_usd" not in payload
