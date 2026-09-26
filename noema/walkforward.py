from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class WalkForwardFold:
    train_start: int
    train_end: int
    test_start: int
    test_end: int


def expanding_walk_forward[T](
    rows: Sequence[T],
    *,
    min_train: int,
    test_size: int,
) -> list[WalkForwardFold]:
    if min_train <= 0:
        raise ValueError("min_train must be > 0")
    if test_size <= 0:
        raise ValueError("test_size must be > 0")

    folds: list[WalkForwardFold] = []
    train_end = min_train
    n = len(rows)

    while train_end < n:
        test_end = min(train_end + test_size, n)
        folds.append(
            WalkForwardFold(
                train_start=0,
                train_end=train_end,
                test_start=train_end,
                test_end=test_end,
            )
        )
        train_end = test_end

    return folds
