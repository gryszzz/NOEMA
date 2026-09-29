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


def test_real_agents_sdk_trace_api_records_only_safe_custom_span() -> None:
    from agents.tracing import get_trace_provider, set_trace_processors

    captured = []

    class Recorder:
        def on_trace_start(self, trace):
            captured.append(("trace", trace.name, trace.metadata))

        def on_trace_end(self, _trace):
            pass

        def on_span_start(self, span):
            data = span.span_data
            captured.append(("span", data.name, data.data))

        def on_span_end(self, _span):
            pass

        def force_flush(self):
            pass

        def shutdown(self):
            pass

    provider = get_trace_provider()
    original_processors = list(provider._multi_processor._processors)
    set_trace_processors([Recorder()])
    try:
        with openai_trace({
            "decision_id": "decision-1",
            "provider": "openai",
            "prompt": "must not be exported",
            "api_key": "must not be exported",
        }) as run:
            assert run.status == "recording"
            assert run.trace_id
        assert run.status == "submitted"
    finally:
        set_trace_processors(original_processors)

    assert captured[0] == ("trace", "NOEMA", {
        "decision_id": "decision-1", "provider": "openai",
    })
    assert captured[1] == ("span", "noema.openai.responses_cognition", {
        "decision_id": "decision-1", "provider": "openai",
    })
    assert "must not be exported" not in repr(captured)


def test_tracing_failure_does_not_block_cognition(monkeypatch) -> None:
    records: list[tuple[str, object]] = []

    class FakeTrace:
        trace_id = "trace_00000000000000000000000000000001"

        def __enter__(self):
            records.append(("trace_enter", None))
            return self

        def __exit__(self, *_args):
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
    with openai_trace({"decision_id": "decision-1", "provider": "openai"}) as run:
        assert run.status == "recording"
        records.append(("cognition", "continues"))
    assert run.status == "export_failed"
    assert ("cognition", "continues") in records
    assert "offline" not in repr(records)


def test_disabled_tracing_does_not_load_exporter(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "agents", None)
    with openai_trace({}, enabled=False) as run:
        assert run.trace_id is None
        assert run.status == "disabled"
