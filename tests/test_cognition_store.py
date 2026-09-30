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
    assert latest["trace_id"].startswith("trace_")
    assert latest["trace_status"] == "submitted"
    assert store.calls_last_hour() == 1
