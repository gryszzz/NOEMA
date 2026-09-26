from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CognitionPacket:
    market_id: str
    thesis: str
    confidence: float
    attention_reason: str
    counterarguments: tuple[str, ...]
    unknowns: tuple[str, ...]
    requested_research: tuple[str, ...]
    recommended_mode: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class CognitionResult:
    status: str
    packet: CognitionPacket | None = None
    detail: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
