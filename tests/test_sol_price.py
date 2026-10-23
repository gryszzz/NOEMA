import asyncio
import json
from datetime import UTC, datetime

from noema import sol_price as sp


def _run(c):
    return asyncio.run(c)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _mk(sources, clock, beats):
    return sp.SolPrice(sources=sources, clock=clock, beat=lambda *a, **k: beats.append(a))


def _src(v):
    async def f():
        if isinstance(v, Exception):
            raise v
        return v
    return f


def test_first_good_source_and_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("NOEMA_SOL_USD_FILE", str(tmp_path / "none.json"))
    calls = []

    async def dex():
        calls.append(1)
        return 103.5
    c, beats = Clock(), []
    s = _mk([("dex", dex), ("cb", _src(999.0))], c, beats)
    assert _run(s.get()) == 103.5 and s.source == "dex"
    c.t += 30
    assert _run(s.get()) == 103.5 and len(calls) == 1
    assert beats[0][:2] == ("feed:sol_usd", True)


def test_falls_through_to_backup(monkeypatch, tmp_path):
    monkeypatch.setenv("NOEMA_SOL_USD_FILE", str(tmp_path / "none.json"))
    s = _mk([("dex", _src(RuntimeError("x"))), ("cb", _src(None)), ("jup", _src(104.0))], Clock(), [])
    assert _run(s.get()) == 104.0 and s.source == "jup"


def _write(path, asof):
    path.write_text(json.dumps({"price": 101.0, "asof_utc": datetime.fromtimestamp(asof, UTC).isoformat()}))


def test_file_last_resort_only_if_fresh(monkeypatch, tmp_path):
    f = tmp_path / "latest.json"
    monkeypatch.setenv("NOEMA_SOL_USD_FILE", str(f))
    c = Clock()
    _write(f, c.t - 60)
    s = _mk([("dex", _src(RuntimeError("down")))], c, [])
    assert _run(s.get()) == 101.0 and s.source == "latest.json"
    _write(f, c.t - 300)
    s2 = _mk([("dex", _src(RuntimeError("down")))], c, [])
    assert _run(s2.get()) is None


def test_nothing_fresh_returns_none_no_constant(monkeypatch, tmp_path):
    monkeypatch.setenv("NOEMA_SOL_USD_FILE", str(tmp_path / "none.json"))
    c, beats = Clock(), []
    s = _mk([("dex", _src(90.0))], c, beats)
    assert _run(s.get()) == 90.0
    s.sources = [("dex", _src(RuntimeError("down")))]
    c.t += 90  # cache expired but last price still under MAX_AGE_S
    assert _run(s.get()) == 90.0
    c.t += 60  # now 150s old
    assert _run(s.get()) is None
    assert beats[-1][:2] == ("feed:sol_usd", False)


def test_insane_price_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("NOEMA_SOL_USD_FILE", str(tmp_path / "none.json"))
    assert _run(_mk([("dex", _src(0.0)), ("cb", _src(1e9))], Clock(), []).get()) is None


def test_no_hardcoded_fallback_in_runtime():
    import inspect

    from noema import agent_runtime
    assert "NOEMA_SOL_USD_FALLBACK" not in inspect.getsource(agent_runtime)
