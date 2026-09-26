from datetime import UTC, datetime, timedelta

from noema.purged_walkforward import TimedLabel, purged_expanding_walk_forward


def test_overlapping_future_labels_are_purged() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        TimedLabel(
            observed_at=start + timedelta(minutes=index),
            label_end_at=start + timedelta(minutes=index + 3),
        )
        for index in range(10)
    ]

    folds = purged_expanding_walk_forward(rows, min_train=5, test_size=2)

    assert folds[0].test_indices == (5, 6)
    assert folds[0].purged_indices == (2, 3, 4)
    assert folds[0].train_indices == (0, 1)
