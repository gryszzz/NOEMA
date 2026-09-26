from datetime import UTC, datetime

from noema.provenance import EvidenceStore
from noema.reasoning_packet import (
    GroundedClaim,
    ReasoningPacket,
    validate_reasoning_packet,
)


def test_packet_requires_real_evidence(tmp_path) -> None:
    store = EvidenceStore(str(tmp_path / "noema.db"))
    packet = ReasoningPacket(
        thesis="Market is mispriced.",
        claims=(GroundedClaim("Price is 0.55.", ("missing",)),),
        counterarguments=(),
        unknowns=("future information",),
    )
    assert validate_reasoning_packet(packet, store).valid is False


def test_grounded_packet_passes(tmp_path) -> None:
    store = EvidenceStore(str(tmp_path / "noema.db"))
    store.append(
        evidence_id="e1",
        source="kalshi",
        source_type="market_data",
        observed_at=datetime.now(UTC),
        payload={"yes_ask": 0.55},
    )
    packet = ReasoningPacket(
        thesis="Observed quote is documented.",
        claims=(GroundedClaim("YES ask is 0.55.", ("e1",)),),
        counterarguments=(),
        unknowns=(),
    )
    assert validate_reasoning_packet(packet, store).valid is True
