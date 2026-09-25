from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from noema.models import Action, MarketSnapshot


class VenueAdapter(ABC):
    name: str
    supports_live_execution: bool = False

    @abstractmethod
    async def markets(self) -> AsyncIterator[MarketSnapshot]:
        raise NotImplementedError

    async def execute(self, action: Action) -> str:
        raise RuntimeError(f"{self.name} live execution is not enabled")
