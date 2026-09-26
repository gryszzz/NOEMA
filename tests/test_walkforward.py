from noema.walkforward import expanding_walk_forward


def test_walk_forward_preserves_time_order() -> None:
    rows = list(range(100))
    folds = expanding_walk_forward(rows, min_train=40, test_size=20)

    assert len(folds) == 3
    assert folds[0].train_end == folds[0].test_start
    assert folds[-1].test_end == 100
    assert all(fold.train_end <= fold.test_start for fold in folds)
