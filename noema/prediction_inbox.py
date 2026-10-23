"""Prediction Bot drop inbox -> TEV trading room (PAPER ONLY, read-only marks, never trades).

Prediction Bot (another agent on the same box) has no NOEMA credentials, endpoints or keys. It drops one JSON
file per post into ``NOEMA_PREDICTION_INBOX`` (default ``/workspace/noema-run/prediction-inbox``); this loop
validates each file strictly, stores posts + picks in ``data/prediction_bot.db`` (own file: no extra lock
contention on noema.db), tracks every pick as a paper position and publishes the ``feed:prediction_bot``
heartbeat. Same pattern as the X inbox (``x_signal.py``).

Post file (``*.json``, <= 64 KB), schema ``tev.prediction_post.v1``::

    {"schema": "tev.prediction_post.v1", "id": "pb-20261022-01", "posted_at": "2026-10-22T22:10:00-04:00",
     "author": "Prediction Bot", "title": "...", "summary": "...", "report_path": "/workspace/... (optional)",
     "picks": [{"venue": "kalshi|polymarket", "market": "...", "market_id": "TICKER (optional)",
                "url": "https://kalshi.com/... (optional)", "side": "YES|NO", "price": 0.40, "fair": 0.45,
                "confidence": "low|medium|high", "resolves": "2026-11-03 (optional)", "note": "(optional)"}]}

``price`` and ``fair`` are for the side picked (a NO pick at 0.40 means NO costs 40c). Unknown keys, wrong
types, out-of-range numbers, oversized text, symlinks and files over 64 KB are rejected (moved to
``rejected/YYYYMMDD/`` with the reason logged); accepted files move to ``archive/YYYYMMDD/``.

SECURITY: every string is untrusted plain text. It is stored verbatim (control characters stripped), never
interpreted, never sent to a model, never followed (urls/report_path are shown as text only) and the room
renders it with textContent. Posts are evidence for Tony to read: nothing here places orders or touches flags.

Paper tracking: entry = posted price; mark = mid of NOEMA's own latest stored quote for that market (Kalshi:
market_snapshots / fs_quotes; Polymarket US: market_snapshots) when NOEMA already has one (<= 6h old);
settled at 1/0 when NOEMA has the venue's resolution (Kalshi fs_markets.result). P&L is per $1 contract.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import heartbeats
from .sqlite_util import connect as sqlite_connect

SCHEMA_ID = "tev.prediction_post.v1"
AUTHOR = "Prediction Bot"
DEFAULT_INBOX = "/workspace/noema-run/prediction-inbox"
HEARTBEAT = "feed:prediction_bot"
CADENCE_S = 300
MAX_FILE_BYTES = 64 * 1024
MAX_PICKS = 25
MARK_MAX_AGE_S = 6 * 3600
VENUES = {"kalshi": ("kalshi:production",), "polymarket": ("polymarket-us",)}
SIDES = ("YES", "NO")
CONFIDENCE = ("low", "medium", "high")
URL_HOSTS = ("kalshi.com", "polymarket.com")
TOP_KEYS = {"schema", "id", "posted_at", "author", "title", "summary", "picks", "report_path"}
PICK_KEYS = {"venue", "market", "market_id", "url", "side", "price", "fair", "confidence", "resolves", "note"}
TEXT_LIMITS = {"title": 160, "summary": 800, "market": 200, "note": 400, "report_path": 300}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_MID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u202a-\u202e\u2066-\u2069]")

SCHEMA = """
CREATE TABLE IF NOT EXISTS prediction_posts (post_id TEXT PRIMARY KEY, posted_at TEXT NOT NULL, author TEXT NOT NULL,
    title TEXT NOT NULL, summary TEXT NOT NULL, report_path TEXT, n_picks INTEGER NOT NULL, file_sha256 TEXT,
    ingested_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS prediction_picks (pick_id TEXT PRIMARY KEY, post_id TEXT NOT NULL, idx INTEGER NOT NULL,
    venue TEXT NOT NULL, market TEXT NOT NULL, market_id TEXT, url TEXT, side TEXT NOT NULL, entry_price REAL NOT NULL,
    fair REAL NOT NULL, confidence TEXT NOT NULL, resolves TEXT, note TEXT, opened_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', mark_price REAL, mark_at TEXT, mark_source TEXT,
    settled_at TEXT, outcome TEXT, settle_price REAL, pnl REAL);
CREATE INDEX IF NOT EXISTS prediction_picks_post ON prediction_picks(post_id, idx);
CREATE TABLE IF NOT EXISTS prediction_ingest_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
    files INTEGER, accepted INTEGER, rejected INTEGER, duplicates INTEGER, marked INTEGER, settled INTEGER,
    detail TEXT);
"""

README = f"""# Prediction Bot -> TEV trading room drop folder

Drop ONE JSON file per post here (`*.json`, max 64 KB, UTF-8). NOEMA picks it up within ~2 minutes,
shows it in the TEV trading room "Prediction Bot" panel and tracks every pick as a PAPER position.
No credentials, endpoints or keys are needed or accepted. Nothing here can place a trade.

Write atomically: write `name.json.tmp` first, then rename to `name.json` (only `*.json` is read).

```json
{{
  "schema": "{SCHEMA_ID}",
  "id": "pb-20261022-01",
  "posted_at": "2026-10-22T22:10:00-04:00",
  "author": "{AUTHOR}",
  "title": "Edge scan: 4 picks",
  "summary": "Short plain-text summary.",
  "report_path": "/workspace/predictions/out/REPORT_20261022.md",
  "picks": [
    {{"venue": "kalshi", "market": "Texas governor: Hinojosa (D)", "market_id": "GOVPARTYTX-26-D",
      "url": "https://kalshi.com/markets/govpartytx", "side": "NO", "price": 0.67, "fair": 0.70,
      "confidence": "low", "resolves": "2026-11-03", "note": "optional"}}
  ]
}}
```

Rules (anything else is rejected and moved to `rejected/YYYYMMDD/`):
- `schema` = `{SCHEMA_ID}`, `author` = `{AUTHOR}`, `id` unique (letters, digits, `._:-`, max 80).
- `posted_at` ISO 8601 WITH a UTC offset, not in the future.
- `title` <= 160 chars, `summary` <= 800, `report_path` optional (shown as text only, never opened).
- `picks`: 1-{MAX_PICKS} objects. `venue` kalshi|polymarket; `market` <= 200 chars; `market_id` optional
  (Kalshi ticker / Polymarket slug); `url` optional, https on kalshi.com or polymarket.com only;
  `side` YES|NO; `price` and `fair` are numbers strictly between 0 and 1 for the side picked;
  `confidence` low|medium|high; `resolves` optional YYYY-MM-DD; `note` optional <= 400 chars.
- No other keys. All text is treated as untrusted plain text (rendered escaped, no HTML/markdown).

Paper tracking: entry = `price`; marked to NOEMA's own stored venue quote when it has that market;
settled when NOEMA sees the resolution. Hit rate = won / settled.
"""


class Reject(ValueError):
    pass


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def inbox_dir() -> Path:
    return Path(os.getenv("NOEMA_PREDICTION_INBOX", DEFAULT_INBOX))


def default_db(main_db: str | None = None) -> str:
    main = main_db or os.getenv("NOEMA_DB_PATH", "data/noema.db")
    return os.getenv("NOEMA_PREDICTION_DB", os.path.join(os.path.dirname(os.path.abspath(main)), "prediction_bot.db"))


def init_inbox(inbox: Path | None = None) -> Path:
    """Create the drop folder (same-user agents can write) and its README."""
    d = inbox or inbox_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o775)
    rd = d / "README.md"
    if not rd.exists() or rd.read_text(encoding="utf-8") != README:
        rd.write_text(README, encoding="utf-8")
        os.chmod(rd, 0o644)
    return d


# ---------- validation ----------

def _text(v: Any, field: str, *, required: bool = True) -> str | None:
    if v is None and not required:
        return None
    if not isinstance(v, str):
        raise Reject(f"{field}: not a string")
    s = _CTRL.sub("", v).strip()
    if required and not s:
        raise Reject(f"{field}: empty")
    if len(s) > TEXT_LIMITS[field]:
        raise Reject(f"{field}: longer than {TEXT_LIMITS[field]}")
    return s or None


def _prob(v: Any, field: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or math.isnan(v) or not 0 < float(v) < 1:
        raise Reject(f"{field}: must be a number strictly between 0 and 1")
    return round(float(v), 4)


def _posted_at(v: Any, now: datetime) -> datetime:
    if not isinstance(v, str) or len(v) > 40:
        raise Reject("posted_at: not an ISO string")
    try:
        ts = datetime.fromisoformat(v)
    except ValueError as exc:
        raise Reject("posted_at: not ISO 8601") from exc
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise Reject("posted_at: missing UTC offset")
    if ts - now > timedelta(minutes=10):
        raise Reject("posted_at: in the future")
    return ts


def _url(v: Any) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str) or len(v) > 300 or _CTRL.search(v):
        raise Reject("url: invalid")
    u = urlsplit(v)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or u.username or u.password or u.port or not any(
            host == h or host.endswith("." + h) for h in URL_HOSTS):
        raise Reject("url: must be https on kalshi.com or polymarket.com")
    return v


def validate_pick(p: Any, i: int) -> dict[str, Any]:
    f = f"picks[{i}]"
    if not isinstance(p, dict):
        raise Reject(f"{f}: not an object")
    extra = set(p) - PICK_KEYS
    if extra:
        raise Reject(f"{f}: unknown keys {sorted(extra)[:5]}")
    if p.get("venue") not in VENUES:
        raise Reject(f"{f}.venue: must be kalshi or polymarket")
    if p.get("side") not in SIDES:
        raise Reject(f"{f}.side: must be YES or NO")
    if p.get("confidence") not in CONFIDENCE:
        raise Reject(f"{f}.confidence: must be low, medium or high")
    mid = p.get("market_id")
    if mid is not None and (not isinstance(mid, str) or not _MID.match(mid)):
        raise Reject(f"{f}.market_id: invalid")
    res = p.get("resolves")
    if res is not None:
        if not isinstance(res, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", res):
            raise Reject(f"{f}.resolves: must be YYYY-MM-DD")
        try:
            date.fromisoformat(res)
        except ValueError as exc:
            raise Reject(f"{f}.resolves: not a date") from exc
    try:
        url = _url(p.get("url"))
        market = _text(p.get("market"), "market")
        note = _text(p.get("note"), "note", required=False)
    except Reject as exc:
        raise Reject(f"{f}.{exc}") from exc
    return {"venue": p["venue"], "market": market, "market_id": mid, "url": url, "side": p["side"],
            "price": _prob(p.get("price"), f"{f}.price"), "fair": _prob(p.get("fair"), f"{f}.fair"),
            "confidence": p["confidence"], "resolves": res, "note": note}


def validate_post(doc: Any, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    if not isinstance(doc, dict):
        raise Reject("not a JSON object")
    extra = set(doc) - TOP_KEYS
    if extra:
        raise Reject(f"unknown keys {sorted(extra)[:5]}")
    if doc.get("schema") != SCHEMA_ID:
        raise Reject("bad schema")
    if doc.get("author") != AUTHOR:
        raise Reject("bad author")
    pid = doc.get("id")
    if not isinstance(pid, str) or not _ID.match(pid):
        raise Reject("id: invalid")
    picks = doc.get("picks")
    if not isinstance(picks, list) or not 1 <= len(picks) <= MAX_PICKS:
        raise Reject(f"picks: need 1-{MAX_PICKS}")
    return {"id": pid, "posted_at": _posted_at(doc.get("posted_at"), now), "author": AUTHOR,
            "title": _text(doc.get("title"), "title"), "summary": _text(doc.get("summary"), "summary"),
            "report_path": _text(doc.get("report_path"), "report_path", required=False),
            "picks": [validate_pick(p, i) for i, p in enumerate(picks)]}


def read_post_file(path: Path, now: datetime | None = None) -> tuple[dict[str, Any], str]:
    """Strictly read one drop file. Raises Reject. Never follows symlinks."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise Reject("unreadable") from exc
    if not stat.S_ISREG(st.st_mode):
        raise Reject("not a regular file")
    if st.st_size > MAX_FILE_BYTES:
        raise Reject("over 64KB")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise Reject("unreadable") from exc
    if len(raw) > MAX_FILE_BYTES:
        raise Reject("over 64KB")
    try:
        doc = json.loads(raw.decode("utf-8"), parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    except (UnicodeDecodeError, ValueError) as exc:
        raise Reject("not valid UTF-8 JSON") from exc
    return validate_post(doc, now), hashlib.sha256(raw).hexdigest()


# ---------- marks / settlement (NOEMA's own stored quotes only; no network) ----------

def _ro(path: str) -> sqlite3.Connection | None:
    if not path or not os.path.isfile(path):
        return None
    try:
        return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    except sqlite3.Error:
        return None


def _epoch(v: Any) -> float | None:
    return heartbeats.to_epoch(v)


def _mid(bid: Any, ask: Any) -> float | None:
    try:
        if bid is not None and ask is not None and float(ask) > 0:
            return round((float(bid) + float(ask)) / 2, 4)
    except (TypeError, ValueError):
        pass
    return None


def lookup_mark(main: sqlite3.Connection | None, venue: str, market_id: str | None, side: str,
                now: float) -> tuple[float | None, str | None, str | None]:
    """(side price, at iso, source) from NOEMA's newest stored quote <= MARK_MAX_AGE_S old, else Nones."""
    if main is None or not market_id:
        return None, None, None
    best: tuple[float, float, str] | None = None  # (at, yes_mid, source)
    for v in VENUES[venue]:
        try:
            r = main.execute("SELECT captured_at, snapshot_json FROM market_snapshots WHERE venue=? AND market_id=? "
                             "ORDER BY captured_at DESC LIMIT 1", (v, market_id)).fetchone()
        except sqlite3.Error:
            r = None
        if r:
            try:
                s = json.loads(r[1] or "{}")
            except ValueError:
                s = {}
            m, at = _mid(s.get("yes_bid"), s.get("yes_ask")), _epoch(r[0])
            if m is not None and at and (best is None or at > best[0]):
                best = (at, m, f"{v} snapshot")
    if venue == "kalshi":
        try:
            r = main.execute("SELECT at, bid, ask FROM fs_quotes WHERE ticker=? ORDER BY at DESC LIMIT 1",
                             (market_id,)).fetchone()
        except sqlite3.Error:
            r = None
        if r:
            m, at = _mid(r[1], r[2]), _epoch(r[0])
            if m is not None and at and (best is None or at > best[0]):
                best = (at, m, "kalshi forward sampler")
    if best is None or now - best[0] > MARK_MAX_AGE_S:
        return None, None, None
    px = best[1] if side == "YES" else round(1 - best[1], 4)
    return px, datetime.fromtimestamp(best[0], UTC).isoformat(), best[2]


def lookup_resolution(main: sqlite3.Connection | None, venue: str, market_id: str | None) -> str | None:
    """'yes' | 'no' when NOEMA has stored the venue's resolution."""
    if main is None or not market_id or venue != "kalshi":
        return None
    try:
        r = main.execute("SELECT result FROM fs_markets WHERE ticker=?", (market_id,)).fetchone()
    except sqlite3.Error:
        return None
    res = str(r[0] or "").lower() if r else ""
    return res if res in ("yes", "no") else None


def update_positions(conn: sqlite3.Connection, main_db: str | None, now: datetime) -> tuple[int, int]:
    main = _ro(main_db) if main_db else None
    marked = settled = 0
    try:
        rows = conn.execute("SELECT pick_id, venue, market_id, side, entry_price FROM prediction_picks "
                            "WHERE status='open'").fetchall()
        for pick_id, venue, mid, side, entry in rows:
            res = lookup_resolution(main, venue, mid)
            if res:
                won = (res == "yes") == (side == "YES")
                sp = 1.0 if won else 0.0
                conn.execute("UPDATE prediction_picks SET status='settled', settled_at=?, outcome=?, settle_price=?, "
                             "mark_price=?, mark_at=?, mark_source=?, pnl=? WHERE pick_id=?",
                             (now.isoformat(), "won" if won else "lost", sp, sp, now.isoformat(),
                              "venue resolution", round(sp - entry, 4), pick_id))
                settled += 1
                continue
            px, at, src = lookup_mark(main, venue, mid, side, now.timestamp())
            if px is not None:
                conn.execute("UPDATE prediction_picks SET mark_price=?, mark_at=?, mark_source=?, pnl=? "
                             "WHERE pick_id=?", (px, at, src, round(px - entry, 4), pick_id))
                marked += 1
    finally:
        if main is not None:
            main.close()
    return marked, settled


# ---------- ingestion ----------

def _move(f: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / f.name
    if target.exists():
        target = dest / f"{f.stem}-{int(time.time() * 1000)}{f.suffix}"
    shutil.move(str(f), str(target))


def ingest(db_path: str | None = None, inbox: Path | str | None = None, *, main_db: str | None = None,
           now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    inbox_p = Path(inbox) if inbox else inbox_dir()
    main_db = main_db if main_db is not None else os.getenv("NOEMA_DB_PATH")
    db_path = db_path or default_db(main_db)
    day = now.strftime("%Y%m%d")
    stats: dict[str, Any] = {"files": 0, "accepted": 0, "rejected": 0, "duplicates": 0, "marked": 0,
                             "settled": 0, "reasons": {}}
    moves: list[tuple[Path, Path]] = []
    conn = sqlite_connect(db_path)
    try:
        ensure_schema(conn)
        files = sorted(p for p in inbox_p.glob("*.json")) if inbox_p.is_dir() else []
        for f in files[:200]:
            stats["files"] += 1
            try:
                post, sha = read_post_file(f, now)
            except Reject as exc:
                stats["rejected"] += 1
                stats["reasons"][f.name[:80]] = str(exc)[:160]
                moves.append((f, inbox_p / "rejected" / day))
                continue
            if conn.execute("SELECT 1 FROM prediction_posts WHERE post_id=?", (post["id"],)).fetchone():
                stats["duplicates"] += 1
                moves.append((f, inbox_p / "archive" / day))
                continue
            conn.execute("INSERT INTO prediction_posts (post_id, posted_at, author, title, summary, report_path, "
                         "n_picks, file_sha256, ingested_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (post["id"], post["posted_at"].isoformat(), post["author"], post["title"], post["summary"],
                          post["report_path"], len(post["picks"]), sha, now.isoformat()))
            for i, p in enumerate(post["picks"]):
                conn.execute("INSERT INTO prediction_picks (pick_id, post_id, idx, venue, market, market_id, url, "
                             "side, entry_price, fair, confidence, resolves, note, opened_at) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (f"{post['id']}#{i}", post["id"], i, p["venue"], p["market"], p["market_id"], p["url"],
                              p["side"], p["price"], p["fair"], p["confidence"], p["resolves"], p["note"],
                              post["posted_at"].isoformat()))
            stats["accepted"] += 1
            moves.append((f, inbox_p / "archive" / day))
        stats["marked"], stats["settled"] = update_positions(conn, main_db, now)
        conn.execute("INSERT INTO prediction_ingest_runs (at, files, accepted, rejected, duplicates, marked, settled, "
                     "detail) VALUES (?,?,?,?,?,?,?,?)",
                     (now.isoformat(), stats["files"], stats["accepted"], stats["rejected"], stats["duplicates"],
                      stats["marked"], stats["settled"], json.dumps(stats["reasons"])))
        conn.execute("DELETE FROM prediction_ingest_runs WHERE at < ?", ((now - timedelta(days=14)).isoformat(),))
        conn.commit()
    finally:
        conn.close()
    for f, dest in moves:
        try:
            _move(f, dest)
        except OSError:
            pass
    return stats


# ---------- read model for the room ----------

def room_view(db_path: str | None = None, *, limit_posts: int = 6) -> dict[str, Any]:
    db_path = db_path or default_db()
    out: dict[str, Any] = {"paper": True, "status": "no data", "posts": [], "stats": None, "last_run_at": None,
                           "role": "Prediction Bot picks · paper tracking only · never trades"}
    conn = _ro(db_path)
    if conn is None:
        out["note"] = "no Prediction Bot posts ingested yet"
        return out
    try:
        run = conn.execute("SELECT at, files, accepted, rejected FROM prediction_ingest_runs ORDER BY id DESC "
                           "LIMIT 1").fetchone()
        posts = conn.execute("SELECT post_id, posted_at, author, title, summary, report_path, n_picks "
                             "FROM prediction_posts ORDER BY posted_at DESC LIMIT ?", (limit_posts,)).fetchall()
        agg = conn.execute("SELECT COUNT(*), SUM(status='open'), SUM(status='settled'), SUM(outcome='won'), "
                           "SUM(mark_price IS NOT NULL AND status='open'), SUM(pnl), "
                           "SUM(CASE WHEN status='open' AND mark_price IS NOT NULL AND pnl>0 THEN 1 ELSE 0 END) "
                           "FROM prediction_picks").fetchone()
        out["posts"] = []
        for pid, at, author, title, summary, rp, n in posts:
            picks = conn.execute("SELECT venue, market, market_id, url, side, entry_price, fair, confidence, resolves, "
                                 "note, status, mark_price, mark_at, mark_source, outcome, pnl FROM prediction_picks "
                                 "WHERE post_id=? ORDER BY idx", (pid,)).fetchall()
            out["posts"].append({"id": pid, "posted_at": at, "author": author, "title": title, "summary": summary,
                                 "report_path": rp, "picks": [dict(zip(
                                     ("venue", "market", "market_id", "url", "side", "price", "fair", "confidence",
                                      "resolves", "note", "status", "current", "current_at", "current_source",
                                      "outcome", "pnl"), r)) for r in picks]})
    except sqlite3.Error:
        out["note"] = "no Prediction Bot posts ingested yet"
        return out
    finally:
        conn.close()
    total, open_, settled, won, marked, pnl, green = (x or 0 for x in agg)
    out["status"] = "active" if total else "no data"
    out["last_run_at"] = run[0] if run else None
    out["last_run"] = dict(zip(("at", "files", "accepted", "rejected"), run)) if run else None
    out["stats"] = {"picks": total, "open": open_, "settled": settled, "won": won,
                    "hit_rate": round(won / settled, 3) if settled else None, "marked_open": marked,
                    "in_the_money_open": green, "paper_pnl_per_contract": round(pnl, 4)}
    return out


def run_loop(interval: float = 120, db_path: str | None = None) -> None:
    init_inbox()
    while True:
        try:
            s = ingest(db_path)
            heartbeats.beat(HEARTBEAT, True, cadence_s=CADENCE_S)
            if s["files"] or s["settled"]:
                print(json.dumps({"at": datetime.now(UTC).isoformat(), **s}), flush=True)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive; the heartbeat shows the failure
            heartbeats.beat(HEARTBEAT, False, f"{type(exc).__name__}: {exc}", cadence_s=CADENCE_S)
            print(json.dumps({"at": datetime.now(UTC).isoformat(), "error": type(exc).__name__}), flush=True)
        time.sleep(interval)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Prediction Bot drop inbox -> paper-tracked picks (never trades)")
    ap.add_argument("--db", default=None, help="prediction_bot.db (default: next to NOEMA_DB_PATH)")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=float, default=120)
    ap.add_argument("--init", action="store_true", help="create the drop folder + README and exit")
    a = ap.parse_args()
    if a.init:
        print(init_inbox())
        return
    if a.loop:
        run_loop(a.interval, a.db)
        return
    init_inbox()
    print(json.dumps(ingest(a.db)), flush=True)


if __name__ == "__main__":
    main()
