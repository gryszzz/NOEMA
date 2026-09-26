from datetime import UTC, datetime

import pytest

from noema.provenance import EvidenceStore


def test_evidence_integrity_roundtrip(tmp_path) -> None:
    store = EvidenceStore(str(tmp_path / "noema.db"))
    store.append(
        evidence_id="e1",
        source="kalshi",
        source_type="market_data",
        observed_at=datetime.now(UTC),
        payload={"price": 0.55, "size": 10},
    )
    assert store.verify_integrity("e1") is True


def test_naive_timestamp_rejected(tmp_path) -> None:
    store = EvidenceStore(str(tmp_path / "noema.db"))
    with pytest.raises(ValueError):
        store.append(
            evidence_id="e1",
            source="x",
            source_type="market_data",
            observed_at=datetime.now(),
            payload={},
        )
