"""Offline, bounded evidence review for Meridian; never a forecast or trade input.

The payload hash checks transfer integrity, not publisher authenticity or truth.
Source excerpts are data, never instructions. No URLs or models are called here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

LIMIT = 65_536
REQUEST_SCHEMA = "meridian.noema.request.v1"
REVIEW_SCHEMA = "meridian.noema.review.v1"
PROVENANCE = {
    "live", "delayed", "stale-cache", "offline-cache", "public-unauthenticated",
    "public-disclosure", "official-api", "media-observation", "rss-public", "verified",
}


def _object(value: object, keys: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("unsupported packet fields")
    return value


def _text(value: object, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError("invalid packet text")
    return value


def _timestamp(value: object) -> datetime:
    text = _text(value, 64)
    try:
        timestamp = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("invalid packet timestamp") from exc
    if timestamp.tzinfo is None:
        raise ValueError("packet timestamps require a timezone")
    return timestamp.astimezone(UTC)


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _json(text: str) -> object:
    def invalid_constant(_: str) -> None:
        raise ValueError("non-finite JSON number")

    try:
        return json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=invalid_constant)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("invalid packet JSON") from exc


def validate_request(text: str, *, now: datetime | None = None) -> tuple[dict, str]:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("review clock requires a timezone")
    if len(text.encode("utf-8")) > LIMIT:
        raise ValueError("packet exceeds transfer limit")
    envelope = _object(_json(text), {"schema", "payload", "sha256"})
    if envelope["schema"] != REQUEST_SCHEMA:
        raise ValueError("unsupported request schema")
    payload = _text(envelope["payload"], LIMIT)
    digest = _text(envelope["sha256"], 64)
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("invalid transfer digest")
    if hashlib.sha256(payload.encode("utf-8")).hexdigest() != digest:
        raise ValueError("transfer integrity check failed")
    body = _object(_json(payload), {"requestId", "createdAt", "question", "evidence"})
    if not re.fullmatch(r"[a-zA-Z0-9-]{1,64}", _text(body["requestId"], 64)):
        raise ValueError("invalid request identity")
    created = _timestamp(body["createdAt"])
    if created > now:
        raise ValueError("request timestamp is in the future")
    _text(body["question"], 2000)
    evidence = _object(body["evidence"], {
        "eventId", "title", "summary", "sourceId", "sourceUrl", "sourceProvenance",
        "eventAt", "sourceRetrievedAt", "upstreamPayloadHash", "freshness",
    })
    for key in ("eventId", "sourceId", "upstreamPayloadHash"):
        _text(evidence[key], 256)
    _text(evidence["title"], 2000)
    _text(evidence["summary"], 12000, empty=True)
    provenance = _text(evidence["sourceProvenance"], 64)
    if provenance not in PROVENANCE:
        raise ValueError("ineligible source provenance")
    event_at = _timestamp(evidence["eventAt"])
    if event_at > created or event_at.timestamp() < 0:
        raise ValueError("event timestamp is outside request history")
    # V1 deliberately cannot infer retrieval time or domain-specific freshness.
    if evidence["sourceRetrievedAt"] is not None or evidence["freshness"] != "unassessed":
        raise ValueError("unsupported freshness assertion")
    source_url = _text(evidence["sourceUrl"], 2048)
    if any(character.isspace() or ord(character) < 32 for character in source_url):
        raise ValueError("invalid source URL")
    url = urlsplit(source_url)
    if (url.scheme != "https" or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment
            or "?" in source_url or "#" in source_url or "\\" in source_url):
        raise ValueError("source URL must be public HTTPS without credentials, query, or fragment")
    return body, digest


def review_request(text: str, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    body, digest = validate_request(text, now=now)
    evidence = body["evidence"]
    unknowns = [
        "Source retrieval time is absent; current freshness has not been established.",
        ("The upstream payload hash is a reference only; the original payload was not transferred "
         "or independently verified."),
        "No independent corroboration or documented causal relationship was supplied.",
        "This review has not answered the research question through additional collection.",
    ]
    if evidence["sourceProvenance"] in {"stale-cache", "offline-cache"}:
        unknowns.insert(0, "The source is explicitly cached/stale; refresh it before current use.")
    if evidence["sourceProvenance"] in {"media-observation", "rss-public"}:
        unknowns.insert(0, "A media/feed observation does not establish the underlying event as fact.")
    result = {
        "requestId": body["requestId"],
        "requestSha256": digest,
        "eventId": evidence["eventId"],
        "generatedAt": now.isoformat(),
        "method": "deterministic-evidence-review-v1",
        "basis": "analysis",
        "status": "needs-more-evidence",
        "findings": [{
            "text": (
                f"Meridian supplied an excerpt attributed to {evidence['sourceId']} "
                f"with provenance {evidence['sourceProvenance']}: "
                f"{evidence['title']}\n{evidence['summary']}"
            ),
            "evidenceIds": [evidence["eventId"]],
        }],
        "unknowns": unknowns,
        "nextChecks": [
            "Inspect the original source and record its publication and retrieval timestamps.",
            "Collect an independent primary source addressing the research question.",
            "Document competing explanations and the evidence that could invalidate each.",
            "Verify entity identifiers and any claimed relationship before linking systems.",
        ],
        "nonClaims": [
            "This is an offline evidence checklist, not a model investigation or forecast.",
            "A valid transfer hash does not authenticate the author or prove source truth.",
            "No market probability, causal impact, trade, or spending instruction is produced.",
            "This analysis must not re-enter Meridian as independent source corroboration.",
        ],
    }
    payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    envelope = {
        "schema": REVIEW_SCHEMA,
        "payload": payload,
        "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }
    if len(json.dumps(envelope, ensure_ascii=False).encode("utf-8")) > LIMIT:
        raise ValueError("review exceeds transfer limit")
    return envelope


def save_review(text: str, db_path: str, *, now: datetime | None = None) -> dict:
    """Idempotent local audit trail; separate from EvidenceStore and ForecastLedger."""
    now = now or datetime.now(UTC)
    body, digest = validate_request(text, now=now)
    review = review_request(text, now=now)
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS meridian_reviews (
            request_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL,
            imported_at TEXT NOT NULL, request_json TEXT NOT NULL, review_json TEXT NOT NULL
        )""")
        conn.execute(
            "INSERT OR IGNORE INTO meridian_reviews VALUES (?, ?, ?, ?, ?)",
            (body["requestId"], digest, now.isoformat(), text, json.dumps(review, ensure_ascii=False)),
        )
        row = conn.execute(
            "SELECT request_sha256, review_json FROM meridian_reviews WHERE request_id = ?",
            (body["requestId"],),
        ).fetchone()
        if row[0] != digest:
            raise ValueError("request identity already exists with different evidence")
        return json.loads(row[1])


def main() -> None:
    parser = argparse.ArgumentParser(description="Review a Meridian excerpt locally, without a model")
    parser.add_argument("request", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--db", default="data/noema.db")
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("output already exists; choose a new output path")
        with args.request.open("rb") as stream:
            raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError("packet exceeds transfer limit")
        review = save_review(raw.decode("utf-8"), args.db)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(review, stream, ensure_ascii=False, indent=2)
    except (ValueError, TypeError, OSError, sqlite3.Error) as exc:
        # Do not echo source text, paths, SQL parameters, or upstream exception messages.
        parser.exit(2, f"Evidence review failed ({type(exc).__name__}); check packet and output path.\n")
    print("Evidence checklist saved. No new collection, model call, forecast, or trade performed.")


if __name__ == "__main__":
    main()
