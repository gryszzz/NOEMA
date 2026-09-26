from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .strategist_audit import StrategistAudit


def strategist_report(audit: StrategistAudit) -> dict[str, Any]:
    best = audit.edges.best
    return {
        "ensemble": asdict(audit.ensemble),
        "uncertainty": {
            "penalty": audit.uncertainty.penalty,
            "components": dict(audit.uncertainty.components),
        },
        "decision_quality": asdict(audit.decision_quality),
        "best_side": best.side.value if best else None,
        "best_after_cost_edge": best.after_cost_edge if best else None,
        "yes": asdict(audit.edges.yes) if audit.edges.yes else None,
        "no": asdict(audit.edges.no) if audit.edges.no else None,
        "research": (
            asdict(audit.research_verdict)
            if audit.research_verdict is not None
            else None
        ),
    }
