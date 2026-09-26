from noema.autonomy_transition import transition_autonomy
from noema.economic_models import AutonomyLevel


def test_promotion_is_one_level_at_a_time() -> None:
    result = transition_autonomy(
        AutonomyLevel.PAPER,
        AutonomyLevel.SELF_FUNDED,
    )
    assert result.next_level is AutonomyLevel.DEMO
    assert result.promoted is True


def test_demotion_is_immediate() -> None:
    result = transition_autonomy(
        AutonomyLevel.PROVEN,
        AutonomyLevel.PAPER,
    )
    assert result.next_level is AutonomyLevel.PAPER
    assert result.demoted is True
