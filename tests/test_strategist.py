import pytest

from noema.strategist import BayesianEnsembler, ModelBelief


def test_ensemble_shrinks_toward_market_when_sample_is_small() -> None:
    result = BayesianEnsembler().combine(
        market_probability=0.50,
        beliefs=[
            ModelBelief("tiny", 0.90, reliability=1.0, sample_size=1),
        ],
    )
    assert 0.50 < result.probability_yes < 0.90
    assert result.effective_models > 0


def test_ensemble_disagreement_widens_interval() -> None:
    ensembler = BayesianEnsembler()
    close = ensembler.combine(
        market_probability=0.50,
        beliefs=[
            ModelBelief("a", 0.60, 1.0, 500),
            ModelBelief("b", 0.62, 1.0, 500),
        ],
    )
    split = ensembler.combine(
        market_probability=0.50,
        beliefs=[
            ModelBelief("a", 0.30, 1.0, 500),
            ModelBelief("b", 0.90, 1.0, 500),
        ],
    )
    assert split.upper_bound - split.lower_bound > close.upper_bound - close.lower_bound


def test_bad_probability_rejected() -> None:
    with pytest.raises(ValueError):
        BayesianEnsembler().combine(
            market_probability=0.5,
            beliefs=[ModelBelief("bad", 1.2, 1.0, 100)],
        )
