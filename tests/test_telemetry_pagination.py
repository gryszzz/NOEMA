import asyncio

from noema.kalshi_telemetry import KalshiTelemetry


def test_kalshi_telemetry_reads_all_pages_and_deduplicates_repeated_rows():
    telemetry = KalshiTelemetry.__new__(KalshiTelemetry)
    telemetry.coverage = {}
    calls = []

    async def get(endpoint, *, params=None, retries=4):
        calls.append((endpoint, params))
        if params.get("cursor") is None:
            return {"orders": [{"order_id": "order-a", "ticker": "MKT-A",
                                "status": "resting", "initial_count_fp": "2",
                                "fill_count_fp": "1", "remaining_count_fp": "1"}],
                    "cursor": "next"}
        return {"orders": [{"order_id": "order-a", "ticker": "MKT-A",
                            "status": "resting", "initial_count_fp": "2",
                            "fill_count_fp": "1", "remaining_count_fp": "1"},
                           {"order_id": "order-b", "ticker": "MKT-B",
                            "status": "filled", "initial_count_fp": "1",
                            "fill_count_fp": "1", "remaining_count_fp": "0"}],
                "cursor": ""}

    telemetry._get = get
    result = asyncio.run(telemetry.orders())
    assert [order.order_id for order in result] == ["order-a", "order-b"]
    assert telemetry.coverage["orders"] == {"complete": True, "pages": 2, "records": 2}
    assert calls[1][1]["cursor"] == "next"
