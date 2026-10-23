"""Box-suspend guard for paper results (read-only over ``/workspace/marketdata/suspends.csv``).

While the box is suspended nothing marks, exits or stops out a paper position, so a paper trade
that was open across a suspend window carries a frozen-clock result: it can neither be a win nor
count toward a size-up. GOMD's suspend monitor appends every detected window (``loop_step`` live,
``retro_logs`` afterwards) to ``suspends.csv``; SUSPENDS.md defines the rule this module applies:
any interval overlapping a window, widened by the measuring feed's padding, is INVALID.

* ``windows()`` - parsed windows (UTC), cached by file mtime; a missing/unreadable file yields
  ``[]`` and ``status()`` says so (no file is never silently treated as "verified clean").
* ``straddle(opened_at, closed_at)`` - the first window the interval overlaps, else ``None``.
* ``void_reason(...)`` - short operator string for a voided trade, else ``None``.

Paths/padding: ``NOEMA_SUSPENDS_CSV`` (default /workspace/marketdata/suspends.csv) and
``NOEMA_SUSPEND_PAD_S`` (default 60 s; paper marks refresh on a sub-minute cadence).
This module never writes to marketdata and never touches live or owner flags.
"""
from __future__ import annotations

import csv
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULT_PATH = "/workspace/marketdata/suspends.csv"
DEFAULT_PAD_S = 60.0


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    detection: str

    def label(self) -> str:
        return f"{self.start.isoformat()}..{self.end.isoformat()} ({self.detection or 'unknown'})"


_lock = threading.Lock()
_cache: dict[str, tuple[float, list[Window]]] = {}
_last_status: dict[str, str] = {}


def csv_path() -> str:
    return os.getenv("NOEMA_SUSPENDS_CSV", DEFAULT_PATH)


def pad_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("NOEMA_SUSPEND_PAD_S", DEFAULT_PAD_S)))
    except ValueError:
        return DEFAULT_PAD_S


def parse_time(value: Any) -> datetime | None:
    """ISO text (``Z`` or offset; naive is read as UTC), epoch seconds, or datetime -> aware UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), UTC)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    return (dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt).astimezone(UTC)


def _load(path: str) -> list[Window]:
    out: list[Window] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            start = parse_time(row.get("start_utc") or row.get("start_et"))
            end = parse_time(row.get("end_utc") or row.get("end_et"))
            if start is None or end is None:
                continue
            if end < start:
                start, end = end, start
            out.append(Window(start, end, (row.get("detection") or "").strip()))
    out.sort(key=lambda w: w.start)
    return out


def windows(path: str | None = None) -> list[Window]:
    path = path or csv_path()
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        _last_status[path] = "missing"
        return []
    with _lock:
        hit = _cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        try:
            parsed = _load(path)
        except (OSError, csv.Error, UnicodeDecodeError):
            _last_status[path] = "unreadable"
            return hit[1] if hit else []
        _cache[path] = (mtime, parsed)
        _last_status[path] = "ok"
        return parsed


def status(path: str | None = None) -> dict[str, Any]:
    path = path or csv_path()
    found = windows(path)
    return {"path": path, "status": _last_status.get(path, "missing"), "windows": len(found),
            "pad_s": pad_seconds(), "last_end": found[-1].end.isoformat() if found else None}


def straddle(opened_at: Any, closed_at: Any = None, *, path: str | None = None,
             pad_s: float | None = None) -> Window | None:
    """First suspend window overlapping [opened_at, closed_at] (closed_at None = still open, now)."""
    start = parse_time(opened_at)
    if start is None:
        return None
    end = parse_time(closed_at) or datetime.now(UTC)
    if end < start:
        start, end = end, start
    pad = timedelta(seconds=pad_seconds() if pad_s is None else max(0.0, pad_s))
    for w in windows(path):
        if w.start - pad < end and w.end + pad > start:
            return w
    return None


def void_reason(opened_at: Any, closed_at: Any = None, **kw: Any) -> str | None:
    w = straddle(opened_at, closed_at, **kw)
    return None if w is None else f"open across box suspend {w.label()}"
