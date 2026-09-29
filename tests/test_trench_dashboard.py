import sqlite3
from datetime import UTC, datetime, timedelta

from noema.trench_collector import DueObservation, TrenchCollectorStore
from noema.trench_dashboard import build_trench_overview
from noema.trench_models import LaunchTick, TokenControlState
from noema.trench_store import TrenchResearchStore


def test_trench_overview_is_safe_without_database(tmp_path) -> None:
    overview = build_trench_overview(str(tmp_path / "missing.db"))
    assert overview["database_present"] is False
    assert overview["counts"]["launches"] == 0


def test_trench_overview_is_read_only_and_exposes_qualification_progress(tmp_path) -> None:
    path = tmp_path / "noema.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sentinel(value TEXT)")
    conn.execute("INSERT INTO sentinel VALUES ('unchanged')")
    conn.commit()
    conn.close()

    overview = build_trench_overview(str(path))

    assert overview["progress"]["observations_collected"] is None
    assert overview["progress"]["launch_distinct_observations"] is None
    assert overview["progress"]["labels_pending"] is None
    assert overview["progress"]["labels_matured"] is None
    assert overview["progress"]["training_labels"] is None
    assert overview["progress"]["walk_forward_labels"] is None
    assert overview["progress"]["threshold_remaining"] == {
        "training": None, "walk_forward": None, "total": None,
    }
    assert overview["progress"]["collection_mode"] == "read_only"
    assert overview["progress"]["wallet_authority"] == "none_from_research_capability"
    check = sqlite3.connect(path)
    tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"sentinel"}
    assert check.execute("SELECT value FROM sentinel").fetchone()[0] == "unchanged"
    check.close()


def test_progress_counts_only_valid_persisted_observations_and_keeps_70_label_gate(monkeypatch, tmp_path) -> None:
    path = tmp_path / "collector.db"
    now = datetime.now(UTC)
    first_pool = now - timedelta(seconds=120)
    collector = TrenchCollectorStore(str(path))
    research = TrenchResearchStore(str(path))
    collector.register_recent(
        [{"id": "synthetic-test-mint", "firstPool": {"createdAt": first_pool.isoformat()}}],
        now=now,
    )
    due = DueObservation("synthetic-test-mint", first_pool, 30, first_pool + timedelta(seconds=30))
    collector.record_observation(
        due,
        tick=LaunchTick(due.scheduled_at, 1.0, 1000.0, 10.0, 2.0),
        control=TokenControlState(),
        raw_token={"id": "synthetic-test-mint", "test_fixture": True},
        holder_shares=(),
    )
    collector.record_attempt(due, status="recorded", attempted_at=now)
    monkeypatch.setenv("NOEMA_TRENCH_ENABLED", "1")
    monkeypatch.setenv("NOEMA_RESEARCH_WORK_ENABLED", "1")
    monkeypatch.setenv("NOEMA_SOLANA_RPC_FALLBACK_URL", "https://fallback.example/rpc")

    progress = build_trench_overview(str(path))["progress"]

    assert progress["valid_observations"] == 1
    assert progress["launch_distinct_observations"] == 1
    assert progress["invalid_observations"] == 0
    assert progress["labels_matured"] == 0
    assert progress["threshold_remaining"]["total"] == 70
    assert progress["mission_eligible"] is False
    assert progress["solana_rpc_fallback_configured"] is True
    research.conn.close()
    collector.conn.close()
