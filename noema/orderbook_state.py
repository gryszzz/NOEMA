from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass
class BinaryOrderBook:
    """Deterministic local binary book reconstructed from snapshot + deltas."""

    ticker: str
    sequence: int | None = None
    yes_bids: dict[float, float] = field(default_factory=dict)
    no_bids: dict[float, float] = field(default_factory=dict)
    synchronized: bool = False

    def apply_snapshot(
        self,
        *,
        sequence: int,
        yes_bids: list[BookLevel],
        no_bids: list[BookLevel],
    ) -> None:
        if sequence < 0:
            raise ValueError("sequence must be non-negative")
        self.yes_bids = {level.price: level.size for level in yes_bids if level.size > 0}
        self.no_bids = {level.price: level.size for level in no_bids if level.size > 0}
        self.sequence = sequence
        self.synchronized = True
        self._validate_prices()

    def apply_delta(
        self,
        *,
        sequence: int,
        side: str,
        price: float,
        size_delta: float,
    ) -> None:
        if not self.synchronized or self.sequence is None:
            raise RuntimeError("book must be initialized with a snapshot")
        if sequence != self.sequence + 1:
            self.synchronized = False
            raise RuntimeError(
                f"orderbook sequence gap: expected {self.sequence + 1}, got {sequence}"
            )
        if not 0 <= price <= 1:
            raise ValueError("price must be in [0, 1]")
        if side not in {"yes", "no"}:
            raise ValueError("side must be yes or no")

        book = self.yes_bids if side == "yes" else self.no_bids
        new_size = book.get(price, 0.0) + size_delta
        if new_size <= 0:
            book.pop(price, None)
        else:
            book[price] = new_size
        self.sequence = sequence

    @property
    def best_yes_bid(self) -> float | None:
        return max(self.yes_bids, default=None)

    @property
    def best_no_bid(self) -> float | None:
        return max(self.no_bids, default=None)

    @property
    def best_yes_ask(self) -> float | None:
        no_bid = self.best_no_bid
        return None if no_bid is None else 1 - no_bid

    @property
    def best_no_ask(self) -> float | None:
        yes_bid = self.best_yes_bid
        return None if yes_bid is None else 1 - yes_bid

    @property
    def midpoint_yes(self) -> float | None:
        bid = self.best_yes_bid
        ask = self.best_yes_ask
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2

    @property
    def spread_yes(self) -> float | None:
        bid = self.best_yes_bid
        ask = self.best_yes_ask
        if bid is None or ask is None:
            return None
        return ask - bid

    def top_depth_imbalance(self, levels: int = 5) -> float | None:
        if levels <= 0:
            raise ValueError("levels must be positive")
        yes_sizes = [
            self.yes_bids[price]
            for price in sorted(self.yes_bids, reverse=True)[:levels]
        ]
        no_sizes = [
            self.no_bids[price]
            for price in sorted(self.no_bids, reverse=True)[:levels]
        ]
        yes_total = sum(yes_sizes)
        no_total = sum(no_sizes)
        total = yes_total + no_total
        if total <= 0:
            return None
        return (yes_total - no_total) / total

    def _validate_prices(self) -> None:
        for price in (*self.yes_bids, *self.no_bids):
            if not 0 <= price <= 1:
                self.synchronized = False
                raise ValueError("book price must be in [0, 1]")
