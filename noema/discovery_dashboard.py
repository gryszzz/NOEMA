"""Read-only projection of persisted public discovery evidence for the desk."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .provenance import canonical_payload_hash

_DISCOVERY_TYPES = {"public_paid_work_discovery", "public_repository_metadata"}


def _connect_readonly(path: str) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=1)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _public_issue(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    url = item.get("url") or item.get("html_url")
    title = item.get("title")
    if not isinstance(url, str) or not url.startswith("https://github.com/"):
        return None
    if not isinstance(title, str):
        return None
    labels = item.get("labels")
    safe_labels = []
    for label in labels[:12] if isinstance(labels, list) else []:
        if isinstance(label, str):
            safe_labels.append(label[:80])
        elif isinstance(label, dict) and isinstance(label.get("name"), str):
            safe_labels.append(label["name"][:80])
    return {
        "title": title[:300],
        "url": url[:500],
        "repository": str(item.get("repository") or "")[:200],
        "state": str(item.get("state") or "unknown")[:20],
        "labels": safe_labels,
        "updated_at": item.get("updated_at") if isinstance(item.get("updated_at"), str) else None,
        "assignee_count": item.get("assignee_count") if type(item.get("assignee_count")) is int else None,
        "comments": item.get("comments") if type(item.get("comments")) is int else None,
    }


def _project_record(row: sqlite3.Row) -> dict[str, Any] | None:
    evidence_id, source, source_type, observed_at, retrieved_at, payload_hash, payload_json = row
    if source_type not in _DISCOVERY_TYPES:
        return None
    try:
        payload = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError):
        payload = None
    if not isinstance(payload, dict):
        payload = {}
    integrity = canonical_payload_hash(payload) == payload_hash
    raw_issues = payload.get("issues")
    issues = [projected for item in raw_issues[:10] if (projected := _public_issue(item))] \
        if isinstance(raw_issues, list) else []
    try:
        total_count = int(payload["total_count"])
        if total_count < 0:
            total_count = None
    except (KeyError, TypeError, ValueError, OverflowError):
        total_count = None
    return {
        "evidence_id": str(evidence_id),
        "source": str(source)[:120],
        "source_type": str(source_type),
        "observed_at": str(observed_at),
        "retrieved_at": str(retrieved_at),
        "payload_hash": str(payload_hash),
        "integrity_verified": integrity,
        "query": str(payload.get("query") or "")[:300],
        "total_count": total_count,
        "result_count": len(issues),
        "issues": issues,
        "demand_verified": payload.get("demand_verified") is True,
        "payment_verified": payload.get("payment_verified") is True,
        "issue_bodies_included": payload.get("issue_bodies_included") is True,
    }


def build_discovery_overview(paths: tuple[str, ...]) -> dict[str, Any]:
    """Read only allowlisted evidence fields; never expose arbitrary payload text."""
    by_id: dict[str, dict[str, Any]] = {}
    source_errors = 0
    for path in dict.fromkeys(paths):
        if not Path(path).is_file():
            continue
        conn = None
        try:
            conn = _connect_readonly(path)
            tables = {str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if "evidence_records" not in tables:
                continue
            rows = conn.execute(
                "SELECT evidence_id,source,source_type,observed_at,retrieved_at,payload_hash,payload_json "
                "FROM evidence_records WHERE source_type IN (?,?) ORDER BY retrieved_at DESC LIMIT 50",
                tuple(sorted(_DISCOVERY_TYPES)),
            )
            for row in rows:
                item = _project_record(row)
                if item is None:
                    continue
                previous = by_id.get(item["evidence_id"])
                if previous is None or item["retrieved_at"] > previous["retrieved_at"]:
                    by_id[item["evidence_id"]] = item
        except (sqlite3.Error, OSError, ValueError):
            source_errors += 1
        finally:
            if conn is not None:
                conn.close()
    records = sorted(by_id.values(), key=lambda row: row["retrieved_at"], reverse=True)[:30]
    return {
        "status": "available" if records else "no_persisted_discovery" if source_errors == 0 else "source_unavailable",
        "source_errors": source_errors,
        "records": records,
        "record_count": len(records),
        "coverage": "Current public MCP discovery records only; not a general web crawler.",
        "limitations": [
            "Current paid-work search is fixed to public GitHub issue metadata.",
            "Issue listings are leads, not verified buyer demand or payment.",
            "Issue bodies are intentionally excluded from persisted evidence.",
        ],
    }
