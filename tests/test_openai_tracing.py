from __future__ import annotations

import sys
from contextlib import contextmanager
from types import SimpleNamespace

from noema.openai_tracing import openai_trace, safe_trace_metadata


def test_trace_metadata_is_a_strict_allowlist() -> None:
    clean = safe_trace_metadata({
        "mission_id": "unassigned",
        "decision_id": "decision-1",
        "specialist": "market-cognition",
        "research_experiment": "none-registered",
        "provider": "openai",
        "financial_mode": "research-only",
        "authority_state": "no-execution-authority",
        "api_key": "never-export-this",
        "prompt": "private prompt",
        "wallet": "private wallet",
    })
    assert clean == {
        "mission_id": "unassigned",
        "decision_id": "decision-1",
        "specialist": "market-cognition",
        "research_experiment": "none-registered",
        "provider": "openai",
        "financial_mode": "research-only",
        "authority_state": "no-execution-authority",
    }


def test_tracing_outage_does_not_block_the_cognition_call(monkeypatch) -> None:
    records: list[tuple[str, object]] = []

    class FakeTrace:
        trace_id = "trace_00000000000000000000000000000001"

        def __enter__(self):
            records.append(("trace_enter", None))
            return self

        def __exit__(self, *args):
            records.append(("trace_exit", None))
            return False

    @contextmanager
    def fake_span(name, data=None):
        records.append((name, data))
        yield

    monkeypatch.setitem(sys.modules, "agents", SimpleNamespace(
        trace=lambda name, metadata=None: (
            records.append((name, metadata)) or FakeTrace()
        ),
        custom_span=fake_span,
        flush_traces=lambda: (_ for _ in ()).throw(RuntimeError("offline")),
    ))
    metadata = {"decision_id": "decision-1", "provider": "openai", "api_key": "secret"}
    with openai_trace(metadata) as run:
        assert run.trace_id is not None
        assert run.status == "recording"
        records.append(("cognition", "continues"))
    assert run.status == "export_failed"
    assert ("cognition", "continues") in records
    assert all("secret" not in repr(record) for record in records)


def test_disabled_tracing_does_not_load_exporter() -> None:
    with openai_trace({}, enabled=False) as run:
        assert run.trace_id is None
        assert run.status == "disabled"
