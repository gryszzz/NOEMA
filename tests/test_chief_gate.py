from noema.trench_desk_runtime import chief_gate
from noema.trench_judgement import Judgement


def test_chief_gate_thresholds():
    picks = [Judgement("a", True, 0.7, "model", 0.6), Judgement("b", True, 0.55, "model", 0.9),
             Judgement("c", True, 0.8, "model", 0.4), Judgement("d", True, 0.8, "rules")]
    ok, log = chief_gate(picks)
    assert [j.mint for j in ok] == ["a"]
    assert [entry["approved"] for entry in log] == [True, False, False, False]


def test_pool_ohlcv_parses():
    import asyncio

    import httpx

    from noema.solana_research import GeckoTerminalPoolClient, SlidingWindowRateLimiter

    def handler(req):
        assert "/pools/P1/ohlcv/minute" in str(req.url)
        return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": [[2, 1, 2, 1, 2, 5], [1, 1, 1, 1, 1, 1]]}}})

    async def sleep(_):
        return None
    c = GeckoTerminalPoolClient(transport=httpx.MockTransport(handler),
                                limiter=SlidingWindowRateLimiter(10, 60, sleep=sleep))
    rows = asyncio.run(c.pool_ohlcv("P1"))
    assert rows[0][0] == 1 and len(rows) == 2
