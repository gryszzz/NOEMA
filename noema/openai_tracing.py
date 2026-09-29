"""Privacy-minimal traces for NOEMA-owned OpenAI workflows."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass

_ALLOWED_METADATA = {
    "mission_id",
    "decision_id",
    "specialist",
    "research_experiment",
    "provider",
    "financial_mode",
    "authority_state",
}


@dataclass
class OpenAITraceRun:
    trace_id: str | None = None
    status: str = "disabled"


def safe_trace_metadata(metadata: dict[str, str]) -> dict[str, str]:
    """Keep exported trace metadata to short, explicitly allowed labels."""
    safe: dict[str, str] = {}
    for key in _ALLOWED_METADATA:
        value = metadata.get(key)
        if not isinstance(value, str):
            continue
        clean = value.strip()
        if clean and len(clean) <= 128:
            safe[key] = clean
    return safe


@contextmanager
def openai_trace(
    metadata: dict[str, str], *, enabled: bool = True,
) -> Iterator[OpenAITraceRun]:
    """Record a metadata-only custom span; exporter failures never block cognition.

    NOEMA sends model requests directly through its existing Responses client.
    This trace deliberately contains no request body, prompt, market text,
    response content, credentials, or wallet identifiers.
    """
    run = OpenAITraceRun(status="disabled" if not enabled else "unavailable")
    if not enabled:
        yield run
        return

    try:
        from agents import custom_span, flush_traces, trace
    except Exception:  # noqa: BLE001 - tracing is optional and must fail open
        yield run
        return

    safe = safe_trace_metadata(metadata)
    if not safe:
        run.status = "not_requested"
        yield run
        return

    trace_context = None
    span_context = None
    try:
        trace_context = trace("NOEMA", metadata=safe)
        trace_context.__enter__()
        span_context = custom_span("noema.openai.responses_cognition", data=safe)
        span_context.__enter__()
    except Exception:  # noqa: BLE001 - tracing is optional and must fail open
        for context in (span_context, trace_context):
            if context is not None:
                with suppress(Exception):
                    context.__exit__(None, None, None)
        run.status = "unavailable"
        yield run
        return

    run.trace_id = trace_context.trace_id
    run.status = "recording"
    try:
        yield run
    except BaseException as exc:
        for context in (span_context, trace_context):
            try:
                context.__exit__(type(exc), exc, exc.__traceback__)
            except Exception:  # noqa: BLE001 - exporter errors cannot block cognition
                run.status = "export_failed"
        try:
            flush_traces()
        except Exception:  # noqa: BLE001 - exporter errors cannot block cognition
            run.status = "export_failed"
        else:
            if run.status != "export_failed":
                run.status = "submitted"
        raise
    else:
        try:
            span_context.__exit__(None, None, None)
            trace_context.__exit__(None, None, None)
            flush_traces()
            run.status = "submitted"
        except Exception:  # noqa: BLE001 - exporter errors cannot block cognition
            run.status = "export_failed"
