from noema.treasury import label_deposits


def test_bonus_split_from_first_deposit(monkeypatch):
    monkeypatch.setenv("NOEMA_TREASURY_BONUSES", "polymarket_us=15")
    out = label_deposits([{"source": "polymarket_us", "usd": 40.0}, {"source": "kalshi", "usd": 14.7},
                          {"source": "polymarket_us", "usd": 5.0}])
    kinds = [(d["source"], d["kind"], d["usd"]) for d in out]
    assert kinds == [("polymarket_us", "bonus", 15.0), ("polymarket_us", "deposit", 25.0),
                     ("kalshi", "deposit", 14.7), ("polymarket_us", "deposit", 5.0)]


def test_no_bonus_configured(monkeypatch):
    monkeypatch.delenv("NOEMA_TREASURY_BONUSES", raising=False)
    assert [d["kind"] for d in label_deposits([{"source": "kalshi", "usd": -2.0}])] == ["withdrawal"]
