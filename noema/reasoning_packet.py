from __future__ import annotations

from dataclasses import dataclass

from .provenance import EvidenceStore


@dataclass(frozen=True)
class GroundedClaim:
    claim: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReasoningPacket:
    thesis: str
    claims: tuple[GroundedClaim, ...]
    counterarguments: tuple[GroundedClaim, ...]
    unknowns: tuple[str, ...]


@dataclass(frozen=True)
class PacketValidation:
    valid: bool
    reasons: tuple[str, ...]


def validate_reasoning_packet(
    packet: ReasoningPacket,
    store: EvidenceStore,
) -> PacketValidation:
    reasons: list[str] = []

    if not packet.thesis.strip():
        reasons.append("missing thesis")
    if not packet.claims:
        reasons.append("packet contains no factual claims")

    for section_name, claims in (
        ("claim", packet.claims),
        ("counterargument", packet.counterarguments),
    ):
        for index, claim in enumerate(claims):
            if not claim.claim.strip():
                reasons.append(f"empty {section_name} {index}")
            if not claim.evidence_ids:
                reasons.append(f"ungrounded {section_name} {index}")
                continue
            for evidence_id in claim.evidence_ids:
                if store.get(evidence_id) is None:
                    reasons.append(
                        f"missing evidence {evidence_id} for {section_name} {index}"
                    )
                elif not store.verify_integrity(evidence_id):
                    reasons.append(
                        f"corrupt evidence {evidence_id} for {section_name} {index}"
                    )

    return PacketValidation(valid=not reasons, reasons=tuple(reasons))
