from noema.cognition_models import CognitionPacket
from noema.cognition_store import CognitionStore


def test_cognition_packet_roundtrip(tmp_path) -> None:
    store = CognitionStore(str(tmp_path / "noema.db"))
    packet = CognitionPacket(
        market_id="M",
        thesis="inspect",
        confidence=0.7,
        attention_reason="edge",
        counterarguments=("spread",),
        unknowns=("external facts",),
        requested_research=("verify source",),
        recommended_mode="investigate",
        evidence_ids=("e1",),
    )
    store.append(
        deployment="astra",
        packet=packet,
        response_id="r1",
        input_tokens=10,
        output_tokens=20,
        total_tokens=30,
        decision_id="decision-1",
        trace_id="trace_00000000000000000000000000000001",
        trace_status="submitted",
    )
    latest = store.latest()
    assert latest is not None
    assert latest["packet"]["thesis"] == "inspect"
    assert latest["decision_id"] == "decision-1"
    assert latest["trace_id"] == "trace_00000000000000000000000000000001"
    assert latest["trace_status"] == "submitted"
    assert store.calls_last_hour() == 1


def test_cognition_store_adds_trace_columns_to_existing_database(tmp_path) -> None:
    import sqlite3

    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """CREATE TABLE cognition_packets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
            market_id TEXT NOT NULL, deployment TEXT NOT NULL, response_id TEXT,
            packet_json TEXT NOT NULL, input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL
        )"""
    )
    connection.commit()
    connection.close()

    store = CognitionStore(str(db_path))
    columns = {
        row[1] for row in store.conn.execute("PRAGMA table_info(cognition_packets)")
    }
    assert {"decision_id", "trace_id", "trace_status"} <= columns
