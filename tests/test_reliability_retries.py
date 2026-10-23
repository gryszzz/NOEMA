"""Retry/backoff, lock-degrade and batched-quote behaviour added for PM/Kalshi reliability."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from noema import edge_strategies as es
from noema import fast_settle as fs
from noema import xv_match


def _api(handler, **kw) -> fs.Api:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps: list[float] = []
    api = fs.Api(client, sleep=sleeps.append, **kw)
    api._sleeps = sleeps  # type: ignore[attr-defined]
    return api


def test_api_retries_5xx_then_succeeds_and_counts() -> None:
    seq = iter([503, 429, 200])

    def h(req):
        code = next(seq)
        return httpx.Response(code, json={"ok": True} if code == 200 else None,
                              text=None if code == 200 else "busy key=abc123", headers={"retry-after": "2"})
    api = _api(h)
    assert api.get("https://x.test/markets") == {"ok": True}
    assert api.errors == {"503": 1, "429": 1}
    assert any(s >= 1.0 for s in api._sleeps)  # backoff honoured Retry-After (jittered)


def test_api_retries_403_once_then_raises() -> None:
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(403, text="forbidden")
    with pytest.raises(httpx.HTTPStatusError):
        _api(h).get("https://x.test/m")
    assert len(calls) == 2


def test_api_404_is_not_retried() -> None:
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(404)
    with pytest.raises(httpx.HTTPStatusError):
        _api(h).get("https://x.test/m")
    assert calls == [1]


def test_api_timeouts_retry_then_raise() -> None:
    def h(req):
        raise httpx.ConnectTimeout("slow", request=req)
    api = _api(h, attempts=3)
    with pytest.raises(httpx.ConnectTimeout):
        api.get("https://x.test/m")
    assert api.errors == {"ConnectTimeout": 3}


def test_safe_body_redacts_secretish_values() -> None:
    r = httpx.Response(500, text="err token=SECRETVALUE more")
    assert "SECRETVALUE" not in fs._safe_body(r)


def test_phase_degrades_on_busy_and_keeps_other_errors(monkeypatch) -> None:
    monkeypatch.setattr(fs.time, "sleep", lambda s: None)
    c = sqlite3.connect(":memory:")
    out: dict = {}

    def busy():
        raise sqlite3.OperationalError("database is locked")
    assert fs._phase(c, out, "marks", busy, default=0) == 0
    assert out == {"marks_error": "database_busy", "lock_errors": 1}

    def other():
        raise sqlite3.OperationalError("no such table: x")
    with pytest.raises(sqlite3.OperationalError):
        fs._phase(c, out, "x", other)


def test_token_prices_uses_minute_open_and_remembers_missing(monkeypatch) -> None:
    t = datetime(2026, 10, 22, 12, 3, 30, tzinfo=UTC)
    m = int(t.timestamp()) // 60 * 60

    def h(req):
        if "NOPE-USD" in req.url.path:
            return httpx.Response(404)
        return httpx.Response(200, json=[[m, 1, 2, 1.5, 1.7, 10], [m - 60, 1, 2, 1.4, 1.5, 3]])
    api = _api(h)
    assert fs.token_prices(api, "btc", [t]) == {t: 1.5}
    assert fs.token_prices(api, "NOPE", [t]) == {}
    assert "NOPE" in fs._CB_MISSING and fs._cb_symbol("NOPE") is None
    assert fs._cb_symbol("USDC") is None and fs._cb_symbol("$$") is None


def test_edge_quotes_overlay_prefers_fresh_watch_quote(monkeypatch) -> None:
    now = datetime(2026, 10, 22, 12, tzinfo=UTC)
    from noema import pm_watch
    monkeypatch.setattr(pm_watch, "latest_many",
                        lambda slugs, max_age_s, now: {"a": (0.3, 0.32, now.timestamp()), "b": (None, 0.5, 0.0)})
    old = (now - timedelta(minutes=20)).timestamp()
    rows = [{"venue": "polymarket-us", "market_id": "a", "yes_bid": 0.1, "yes_ask": 0.2, "at": old},
            {"venue": "polymarket-us", "market_id": "a", "yes_bid": 0.1, "yes_ask": 0.2, "at": old},
            {"venue": "polymarket-us", "market_id": "b", "yes_bid": 0.1, "yes_ask": 0.2, "at": old},
            {"venue": "polymarket-us", "market_id": "c", "yes_bid": 0.1, "yes_ask": 0.2, "at": old},
            {"venue": "kalshi", "market_id": "K", "yes_bid": 0.1, "yes_ask": 0.2, "at": old}]
    out = es._overlay_pm_watch(rows, now)
    a = [q for q in out if q["market_id"] == "a"]
    assert len(a) == 1 and a[0]["yes_bid"] == 0.3 and a[0]["at"] == now.timestamp() and a[0]["source"] == "pm_watch"
    assert not [q for q in out if q["market_id"] == "b"]  # one-sided now -> dropped
    assert {q["market_id"] for q in out} == {"a", "c", "K"}


def test_xv_batch_bbo_one_call_per_100() -> None:
    calls: list[dict] = []

    class Markets:
        def list(self, params):
            calls.append(params)
            return {"markets": [{"slug": s, "bestBidQuote": {"value": "0.40"}, "bestAskQuote": {"value": "0.45"}}
                                for s in params["slug"]] + [{"slug": "zz", "bestBidQuote": {"value": "0.9"}}]}

    class Client:
        markets = Markets()
    slugs = [f"s{i}" for i in range(150)] + ["s1"]
    out = xv_match.pmus_bbo_batch(Client(), slugs)
    assert len(calls) == 2 and len(out) == 150 and "zz" not in out
    assert out["s0"]["yes_bid"] == 0.40 and out["s0"]["no_ask"] == 0.6


def test_kalshi_telemetry_retries_transient_and_logs_without_secrets(monkeypatch, capsys) -> None:
    from noema import kalshi_telemetry as kt

    async def nosleep(_s):
        return None
    monkeypatch.setattr(kt.asyncio, "sleep", nosleep)
    seq = iter(["timeout", 503, 403, 200])
    seen_headers: list[str] = []

    def h(req):
        seen_headers.append(req.headers["KALSHI-ACCESS-TIMESTAMP"])
        step = next(seq)
        if step == "timeout":
            raise httpx.ConnectTimeout("slow", request=req)
        if step == 200:
            return httpx.Response(200, json={"orders": []})
        return httpx.Response(step, text='{"error":{"code":"x","message":"signature=abc"}}')

    class Signer:
        def headers(self, method, path):
            return {"KALSHI-ACCESS-KEY": "KEYID", "KALSHI-ACCESS-TIMESTAMP": str(len(seen_headers)),
                    "KALSHI-ACCESS-SIGNATURE": "SIG"}

    t = kt.KalshiTelemetry.__new__(kt.KalshiTelemetry)
    t.signer = Signer()
    t.client = httpx.AsyncClient(base_url="https://k.test/trade-api/v2", transport=httpx.MockTransport(h))
    assert asyncio.run(t._get("/portfolio/orders")) == {"orders": []}
    assert len(seen_headers) == 4
    err = capsys.readouterr().err
    assert err.count("kalshi_http_error") == 3 and "KEYID" not in err and "SIG\"" not in err and "abc" not in err


def test_kalshi_telemetry_second_403_raises(monkeypatch) -> None:
    from noema import kalshi_telemetry as kt

    async def nosleep(_s):
        return None
    monkeypatch.setattr(kt.asyncio, "sleep", nosleep)

    class Signer:
        def headers(self, method, path):
            return {}
    t = kt.KalshiTelemetry.__new__(kt.KalshiTelemetry)
    t.signer = Signer()
    t.client = httpx.AsyncClient(base_url="https://k.test", transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(t._get("/portfolio/orders"))


def test_agent_health_checks_are_time_boxed() -> None:
    from noema import agent_runtime as ar

    async def slow():
        await asyncio.sleep(5)
        return ar.AgentConnectionState("connected", "late")

    async def fast():
        return ar.AgentConnectionState("connected", "ok")

    async def go():
        return await asyncio.gather(ar._bounded_state(slow(), "kalshi", 0.05), ar._bounded_state(fast(), "evm", 1))
    slow_state, fast_state = asyncio.run(go())
    assert slow_state.status == "degraded" and "timed out" in slow_state.detail
    assert fast_state.detail == "ok"
