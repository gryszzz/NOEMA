import pytest

from noema.edge import CostAssumptions, Side, compare_binary_edges


def test_edge_math_is_symmetric() -> None:
    result = compare_binary_edges(
        probability_yes=0.70,
        yes_ask=0.60,
        no_ask=0.35,
        costs=CostAssumptions(fee_fraction=0.01),
    )
    assert result.yes is not None
    assert result.no is not None
    assert result.yes.after_cost_edge == pytest.approx(0.09)
    assert result.no.after_cost_edge == pytest.approx(-0.06)
    assert result.best is not None
    assert result.best.side is Side.YES


def test_no_side_can_be_best() -> None:
    result = compare_binary_edges(
        probability_yes=0.30,
        yes_ask=0.40,
        no_ask=0.60,
        costs=CostAssumptions(),
    )
    assert result.best is not None
    assert result.best.side is Side.NO
