import pytest

from noema.orderbook_state import BinaryOrderBook, BookLevel


def test_binary_book_derives_asks_from_opposite_bids() -> None:
    book = BinaryOrderBook("M")
    book.apply_snapshot(
        sequence=10,
        yes_bids=[BookLevel(0.55, 20)],
        no_bids=[BookLevel(0.40, 15)],
    )
    assert book.best_yes_bid == 0.55
    assert book.best_yes_ask == pytest.approx(0.60)
    assert book.best_no_ask == pytest.approx(0.45)
    assert book.spread_yes == pytest.approx(0.05)


def test_sequence_gap_desynchronizes_book() -> None:
    book = BinaryOrderBook("M")
    book.apply_snapshot(sequence=10, yes_bids=[], no_bids=[])
    with pytest.raises(RuntimeError, match="sequence gap"):
        book.apply_delta(sequence=12, side="yes", price=0.5, size_delta=1)
    assert book.synchronized is False
