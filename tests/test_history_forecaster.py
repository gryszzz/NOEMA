from datetime import UTC, datetime, timedelta

from noema.baseline_recording import record_market_baseline
from noema.history_forecaster import MODEL_VERSION, record_history_candidate
from noema.ledger import ForecastLedger
from noema.models import MarketSnapshot
from noema.opportunity_radar import build_radar
from noema.outcomes import OutcomeStore
from noema.paired_evaluation import compare_history_to_market
from noema.provenance import EvidenceStore


def _history(store, now, *, count=30, shared_event=False, seen_late=False):
    for i in range(count):
        store.upsert(
            venue="kalshi:demo", market_id=f"KXTEST-OLD{i}-A", outcome_yes=i % 2,
            resolved_at=(now - timedelta(days=2)).isoformat(),
            seen_at=now + timedelta(seconds=1) if seen_late else now - timedelta(days=1),
            raw={"event_ticker": "KXTEST-OLD" if shared_event else f"KXTEST-OLD{i}"},
        )


def _market(now):
    return MarketSnapshot(
        "kalshi:demo", "KXTEST-NEW-A", "Independent research candidate?",
        .4, .5, .5, .6, 10000, None, "Clear rules", captured_at=now,
    )


def _paired_history(store, now, *, complementary=True):
    for i in range(30):
        for side in ("A", "B"):
            store.upsert(
                venue="kalshi:demo", market_id=f"KXTEST-OLD{i}-{side}",
                outcome_yes=int(side == "A") if complementary else 0,
                resolved_at=(now - timedelta(days=2)).isoformat(),
                seen_at=now - timedelta(days=1),
                raw={"event_ticker": f"KXTEST-OLD{i}"},
            )


def test_candidate_is_grounded_paper_only_and_compared_on_same_market(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    now = datetime.now(UTC)
    outcomes, ledger, evidence = OutcomeStore(db), ForecastLedger(db), EvidenceStore(db)
    _history(outcomes, now)
    market = _market(now)
    record_market_baseline(market, ledger)
    verified = frozenset({market.market_id})
    assert record_history_candidate(
        market, outcomes=outcomes, ledger=ledger, evidence=evidence,
        verified_market_ids=verified,
    )
    assert not record_history_candidate(
        market, outcomes=outcomes, ledger=ledger, evidence=evidence,
        verified_market_ids=verified,
    )
    row = build_radar(db)[0]
    assert row.model_version == MODEL_VERSION
    assert row.attention_score is None
    assert row.decision == "pass"
    assert row.evidence_ids and evidence.verify_integrity(row.evidence_ids[0])

    record_market_baseline(market, ledger)  # Repeated polling must not inflate the comparison.
    outcomes.upsert(
        venue="kalshi:demo", market_id=market.market_id, outcome_yes=1,
        resolved_at=(now + timedelta(days=1)).isoformat(),
        raw={"event_ticker": "KXTEST-NEW"},
    )
    result = compare_history_to_market(db)
    assert result.distinct_resolved_markets == 1
    assert result.status == "collect_more"
    assert result.live_eligible is False
    assert result.candidate_brier is not None
    assert result.market_baseline_brier is not None


def test_history_known_only_after_capture_cannot_train_candidate(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    now = datetime.now(UTC)
    outcomes, ledger, evidence = OutcomeStore(db), ForecastLedger(db), EvidenceStore(db)
    _history(outcomes, now, seen_late=True)
    assert not record_history_candidate(
        _market(now), outcomes=outcomes, ledger=ledger, evidence=evidence,
        verified_market_ids=frozenset({_market(now).market_id}),
    )


def test_demo_outcomes_cannot_train_a_production_forecast(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    now = datetime.now(UTC)
    outcomes, ledger, evidence = OutcomeStore(db), ForecastLedger(db), EvidenceStore(db)
    _history(outcomes, now)
    market = MarketSnapshot(
        "kalshi:production", "KXTEST-NEW-A", "Production market?",
        .4, .5, .5, .6, 10000, None, "Clear rules", captured_at=now,
    )
    assert not record_history_candidate(
        market, outcomes=outcomes, ledger=ledger, evidence=evidence,
        verified_market_ids=frozenset({market.market_id}),
    )


def test_multiple_markets_per_event_rejects_family_frequency(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    now = datetime.now(UTC)
    outcomes, ledger, evidence = OutcomeStore(db), ForecastLedger(db), EvidenceStore(db)
    _history(outcomes, now, shared_event=True)
    assert not record_history_candidate(
        _market(now), outcomes=outcomes, ledger=ledger, evidence=evidence,
        verified_market_ids=frozenset({_market(now).market_id}),
    )


def test_two_sided_events_with_one_yes_each_form_paper_frequency(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    now = datetime.now(UTC)
    outcomes, ledger, evidence = OutcomeStore(db), ForecastLedger(db), EvidenceStore(db)
    _paired_history(outcomes, now)
    market = _market(now)
    record_market_baseline(market, ledger)
    assert record_history_candidate(
        market, outcomes=outcomes, ledger=ledger, evidence=evidence,
        current_event_size=2,
        verified_market_ids=frozenset({market.market_id, "KXTEST-NEW-B"}),
    )
    assert build_radar(db)[0].probability_yes == .5
    assert build_radar(db)[0].attention_score is None


def test_two_sided_events_without_complementary_results_are_rejected(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    now = datetime.now(UTC)
    outcomes, ledger, evidence = OutcomeStore(db), ForecastLedger(db), EvidenceStore(db)
    _paired_history(outcomes, now, complementary=False)
    assert not record_history_candidate(
        _market(now), outcomes=outcomes, ledger=ledger, evidence=evidence,
        current_event_size=2,
        verified_market_ids=frozenset({_market(now).market_id, "KXTEST-NEW-B"}),
    )


def test_settled_before_snapshot_never_forms_a_paired_score(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    now = datetime.now(UTC)
    outcomes, ledger, evidence = OutcomeStore(db), ForecastLedger(db), EvidenceStore(db)
    _history(outcomes, now)
    market = _market(now)
    record_market_baseline(market, ledger)
    assert record_history_candidate(
        market, outcomes=outcomes, ledger=ledger, evidence=evidence,
        verified_market_ids=frozenset({market.market_id}),
    )
    outcomes.upsert(
        venue="kalshi:demo", market_id=market.market_id, outcome_yes=1,
        resolved_at=(now - timedelta(seconds=1)).isoformat(),
        raw={"event_ticker": "KXTEST-NEW"},
    )
    assert compare_history_to_market(db).distinct_resolved_markets == 0
