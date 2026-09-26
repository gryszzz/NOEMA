from noema.arrival_toxicity import TradeArrival, assess_arrival_toxicity


def test_persistent_one_sided_arrivals_score_higher() -> None:
    balanced = [
        TradeArrival("buy" if i % 2 == 0 else "sell", float(i))
        for i in range(20)
    ]
    toxic = [TradeArrival("buy", float(i) / 10) for i in range(20)]

    assert (
        assess_arrival_toxicity(toxic).persistence_score
        > assess_arrival_toxicity(balanced).persistence_score
    )
