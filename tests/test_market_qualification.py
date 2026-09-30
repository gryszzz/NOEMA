import sqlite3
from types import SimpleNamespace

from noema import market_qualification


def _create_history_schema(connection):
    connection.execute(
        "CREATE TABLE forecast_ledger "
        "(id INTEGER PRIMARY KEY, venue TEXT, market_id TEXT, forecast_json TEXT)"
    )
    connection.executemany(
        "INSERT INTO forecast_ledger (venue,market_id,forecast_json) VALUES (?,?,?)",
        [
            ("kalshi:production", "EVENT-A-YES", '{"model_version":"market-baseline-v1"}'),
            ("kalshi:production", "EVENT-A-YES", '{"model_version":"market-baseline-v1"}'),
            ("kalshi:production", "EVENT-B-YES", '{"model_version":"series-frequency-v1"}'),
        ],
    )
    connection.execute(
        "CREATE TABLE outcomes (id INTEGER PRIMARY KEY, venue TEXT, market_id TEXT)"
    )
    connection.execute(
        "INSERT INTO outcomes (venue,market_id) VALUES (?,?)",
        ("kalshi:production", "EVENT-A-YES"),
    )


def test_historical_corpus_counts_are_separate_from_eligible_pairs(tmp_path, monkeypatch):
    database = tmp_path / "qualification.db"
    with sqlite3.connect(database) as connection:
        _create_history_schema(connection)

    monkeypatch.setattr(market_qualification, "market_data_quality", lambda _path: {
        "status": "research_only", "observations": 0, "valid_markets": 0,
        "markets_with_rules": 0, "markets_with_two_sided_quotes": 0,
    })
    monkeypatch.setattr(market_qualification, "compare_history_to_market", lambda _path: SimpleNamespace(
        as_dict=lambda: {
            "status": "no_pairs", "distinct_resolved_markets": 0,
            "distinct_resolved_events": 0, "candidate_brier": None,
            "market_baseline_brier": None,
        }
    ))

    result = market_qualification.build_market_data_qualification(str(database))

    assert result["historical_corpus"] == {
        "status": "recorded",
        "forecast_records": 3,
        "outcome_records": 1,
        "forecasts_by_model": {"market-baseline-v1": 2, "series-frequency-v1": 1},
        "forecast_outcome_joins_by_model": {"market-baseline-v1": 2},
        "scope": "All persisted forecast_ledger and outcomes rows; not restricted to paired-evaluation eligibility.",
    }
    assert result["validation"]["distinct_resolved_markets"] == 0
    assert result["validation"]["distinct_resolved_events"] == 0


def test_historical_corpus_stays_unknown_when_required_tables_are_missing(tmp_path, monkeypatch):
    database = tmp_path / "partial-schema.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE forecast_ledger (id INTEGER PRIMARY KEY)")

    monkeypatch.setattr(market_qualification, "market_data_quality", lambda _path: {
        "status": "research_only", "observations": 0, "valid_markets": 0,
        "markets_with_rules": 0, "markets_with_two_sided_quotes": 0,
    })
    monkeypatch.setattr(market_qualification, "compare_history_to_market", lambda _path: SimpleNamespace(
        as_dict=lambda: {"status": "no_pairs", "distinct_resolved_markets": 0,
                         "distinct_resolved_events": 0}
    ))

    result = market_qualification.build_market_data_qualification(str(database))

    assert result["historical_corpus"]["status"] == "incomplete_schema"
    assert result["historical_corpus"]["forecast_records"] is None
    assert result["historical_corpus"]["outcome_records"] is None


def test_historical_corpus_survives_market_snapshot_pipeline_failure(tmp_path, monkeypatch):
    database = tmp_path / "snapshot-failure.db"
    with sqlite3.connect(database) as connection:
        _create_history_schema(connection)

    def fail_quality(_path):
        raise sqlite3.OperationalError("snapshot store unavailable")

    monkeypatch.setattr(market_qualification, "market_data_quality", fail_quality)

    result = market_qualification.build_market_data_qualification(str(database))

    assert result["status"] == "unavailable"
    assert result["historical_corpus"]["status"] == "recorded"
    assert result["historical_corpus"]["forecast_records"] == 3
    assert result["historical_corpus"]["outcome_records"] == 1
