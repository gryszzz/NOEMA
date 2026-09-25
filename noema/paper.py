from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .models import Action, Decision


@dataclass(frozen=True)
class PaperFill:
    venue: str
    market_id: str
    side: str
    price: float
    stake_usd: float
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class PaperBroker:
    def __init__(self) -> None:
        self.fills: list[PaperFill] = []

    def execute(self, action: Action) -> PaperFill | None:
        if action.decision not in {Decision.PAPER_BUY_YES, Decision.PAPER_BUY_NO}:
            return None
        if action.max_price is None or action.stake_usd <= 0:
            return None

        fill = PaperFill(
            venue=action.venue,
            market_id=action.market_id,
            side="yes" if action.decision is Decision.PAPER_BUY_YES else "no",
            price=action.max_price,
            stake_usd=action.stake_usd,
        )
        self.fills.append(fill)
        return fill
