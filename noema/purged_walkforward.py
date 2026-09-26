from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TimedLabel:
    observed_at: datetime
    label_end_at: datetime

    def __post_init__(self) -> None:
        if self.label_end_at < self.observed_at:
            raise ValueError("label_end_at cannot precede observed_at")


@dataclass(frozen=True)
class PurgedFold:
    train_indices: tuple[int, ...]
    test_indices: tuple[int, ...]
    purged_indices: tuple[int, ...]


def purged_expanding_walk_forward(
    rows: Sequence[TimedLabel],
    *,
    min_train: int,
    test_size: int,
) -> list[PurgedFold]:
    """Expanding walk-forward with overlap purging at each train/test boundary.

    Training observations whose future label window reaches into the test period are removed.
    This is the leakage mode that matters for next-N-minute launch outcomes near a fold boundary.
    """

    if min_train <= 0:
        raise ValueError("min_train must be > 0")
    if test_size <= 0:
        raise ValueError("test_size must be > 0")
    if any(rows[i].observed_at > rows[i + 1].observed_at for i in range(len(rows) - 1)):
        raise ValueError("rows must be ordered by observed_at")

    folds: list[PurgedFold] = []
    train_end = min_train
    n = len(rows)

    while train_end < n:
        test_end = min(train_end + test_size, n)
        test_indices = tuple(range(train_end, test_end))
        test_start = rows[train_end].observed_at

        kept: list[int] = []
        purged: list[int] = []
        for index in range(train_end):
            if rows[index].label_end_at >= test_start:
                purged.append(index)
            else:
                kept.append(index)

        folds.append(
            PurgedFold(
                train_indices=tuple(kept),
                test_indices=test_indices,
                purged_indices=tuple(purged),
            )
        )
        train_end = test_end

    return folds
