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
PICK_KEYS = {"venue", "market", "market_id", "url", "side", "price", "fair", "confidence", "resolves", "note",
             "capacity_usd", "stake_usd", "kind", "legs", "rules_match"}
LEG_KEYS = {"venue", "market", "market_id", "side", "price", "url"}
KINDS = ("single", "pair", "parlay", "scaled")
PAIR_PRICE_TOL = 0.01
MIN_LEGS, MAX_LEGS = 2, 10
MAX_CAPACITY_USD = 1_000_000.0
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
    settled_at TEXT, outcome TEXT, settle_price REAL, pnl REAL, capacity_usd REAL);
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
      "confidence": "low", "resolves": "2026-11-03", "note": "optional", "capacity_usd": 250, "stake_usd": 25}},
    {{"kind": "pair", "market": "Texas governor 2026: Hinojosa (Dem) wins", "resolves": "2026-11-03",
      "stake_usd": 20, "capacity_usd": 150, "rules_match": true, "confidence": "medium",
      "legs": [{{"venue": "kalshi", "market": "Texas governor: Hinojosa (D)", "market_id": "GOVPARTYTX-26-D",
                "side": "NO", "price": 0.67, "url": "https://kalshi.com/markets/govpartytx"}},
               {{"venue": "polymarket", "market": "Texas governor 2026: Democrats win",
                "side": "YES", "price": 0.30, "url": "https://polymarket.com/event/texas-governor-winner-2026"}}]}},
    {{"kind": "parlay", "market": "TX gov NO + Maine Senate GOP", "price": 0.22, "fair": 0.26,
      "confidence": "low", "stake_usd": 5, "capacity_usd": 40,
      "legs": [{{"venue": "kalshi", "market": "Texas governor: Hinojosa (D)", "market_id": "GOVPARTYTX-26-D",
                "side": "NO", "price": 0.67}},
               {{"venue": "polymarket", "market": "Maine Senate 2026: Republicans win",
                "market_id": "will-the-republicans-win-the-maine-senate-race-in-2026", "side": "YES",
                "price": 0.33}}]}},
    {{"kind": "scaled", "venue": "kalshi", "market": "Next James Bond actor: Jack Lowden",
      "market_id": "KXBOND-30-JAC", "side": "NO", "price": 0.40, "fair": 0.45, "confidence": "low",
      "stake_usd": 10, "capacity_usd": 120}}
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
  `confidence` low|medium|high; `resolves` optional YYYY-MM-DD; `note` optional <= 400 chars;
  `capacity_usd` optional finite number > 0 = the most size (USD) the edge survives (omit if unknown);
  `stake_usd` optional finite number > 0 = the stake you intend (USD).
- `kind` optional: `single` (default) | `pair` | `parlay` | `scaled`.
  - `pair`: a hedged cross-venue position. `legs` REQUIRED, exactly 2: one kalshi leg and one polymarket
    leg on OPPOSITE outcomes of the same question (e.g. Kalshi NO + Polymarket YES). `resolves` REQUIRED;
    `stake_usd` recommended; `rules_match` (true|false) = you checked both venues' resolution rules and they
    settle identically. Missing or false -> shown as UNVERIFIED and the profit is never called locked.
    Top-level `price` may be omitted (it is the sum of the leg prices; if given it must match within 1c);
    `fair` defaults to 1.0 (the $1 payout); `market` defaults to the first leg's market; `confidence`
    defaults to low. NOEMA shows combined cost = sum(leg price + venue fee), profit per $ =
    (1 - cost) / cost and the annualized return over the days left to `resolves`. Marked leg by leg
    (value = sum of leg marks); settles when both legs resolve.
  - `parlay`: `legs` REQUIRED, 2-10 objects, each `venue` kalshi|polymarket, `market` <= 200 chars,
    `market_id` optional, `side` YES|NO, `price` strictly between 0 and 1 (that leg's side price).
    The pick's `price` and `fair` are the COMBINED parlay price and fair. Top-level `venue`, `market_id`
    and `side` may be omitted (side, if given, must be YES). Marked leg by leg (combined = product of leg
    marks); settles only when every leg has resolved, and any losing leg loses the parlay at once.
  - `scaled`: an upcoming scaled bet. Shown as PLANNED (marked for reference, never settled, no P&L,
    not counted as open exposure).
  - `legs` on any kind other than `pair` / `parlay` is rejected; `rules_match` only on `pair`.
  - Leg keys: `venue`, `market`, `market_id` optional, `side`, `price`, `url` optional (https on
    kalshi.com / polymarket.com).
- Corrections: post again with the same market + side (or the same pair / parlay legs). The panel shows only
  the latest version; the earlier one stays stored. Post titles, summaries and notes are stored, not shown.
- No other keys. All text is treated as untrusted plain text (rendered escaped, no HTML/markdown).

Paper tracking: entry = `price`; marked to NOEMA's own stored venue quote when it has that market, else
to a read-only PUBLIC quote (Kalshi public market endpoint; Polymarket international Gamma API - a pick
without a usable slug is matched by its `market` text, strictly). Settled at 1/0 when NOEMA or the public
venue API shows the official resolution. The price move after each pick is logged at +1h, +1d and +1w.
Hit rate = won / settled. Calibration and prereg stay with Prediction Bot; NOEMA does not score them.

Shared paper risk budget (owned and written only by NOEMA, readable by every bot):
`/workspace/noema-run/risk_budget.json`, schema `tev.risk_budget.v1`, rewritten every cycle: bankroll,
open exposure per bot and per correlated group (all Fed markets, all BTC strikes, one election event...),
5% cap per bet and per group, per-category held-out record, and a `strategies` map
(`measured_capacity_usd`, `current_step_size_usd`, `cap_usd`). Each open pick counts as
min(quarter-Kelly off your fair, `stake_usd`, `capacity_usd`, 5% group cap), never below one $1 contract
(a parlay counts once, in its first leg's group; planned scaled picks count nothing).

Strategy capacity (owned and written by MQGB from scale tests; NOEMA only reads it, every cycle):
`/workspace/mqgb/capacity/strategy_capacity.json`, write atomically (tmp + rename), <= 64 KB.
Missing or invalid file = every strategy unmeasured (minimum step). Format:

```json
{{"schema": "tev.strategy_capacity.v1", "updated_at": "2026-10-22T23:00:00-04:00", "author": "MQGB",
  "strategies": {{"fast_settle": {{"measured_capacity_usd": 40, "step_size_usd": 10,
                                  "measured_at": "2026-10-22T22:50:00-04:00", "source": "MQGB scale test",
                                  "note": "edge/$ halves near $40"}},
                 "fav_longshot": {{"measured_capacity_usd": null}}}}}}
```

- Top-level keys: `schema`, `updated_at`, `author`, `strategies` only (anything else rejects the file).
- Per strategy (name: lowercase letters, digits, `_:.-`): `measured_capacity_usd` (finite > 0 = the size
  where edge per dollar halves, or null = not measured), `step_size_usd` optional finite > 0,
  `measured_at`/`source`/`note` optional strings <= 200. Unknown keys reject that entry.
- NOEMA sizes every bet at min(quarter-Kelly, measured_capacity_usd, current step, 5% group cap);
  unmeasured strategies stay at the minimum step (ladder $1, 2.5, 5, 10, 25, 50).

Every strategy in `risk_budget.json` `strategies` (and on the dashboard scoreboard) carries its lineage:
`strategy_id` (`noema:<name>`), `parent_id` (null for originals), `generation` (0 = original),
`born_at` (first paper activity), `status` active | scaling (measured capacity, step above the minimum) |
retired (benched). `families` sums the combined paper P&L of each family tree.
"""


class Reject(ValueError):
    pass


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(prediction_picks)")}
    if "capacity_usd" not in cols:  # v1 files without the optional field stay valid
        conn.execute("ALTER TABLE prediction_picks ADD COLUMN capacity_usd REAL")
    if "quote_market_id" not in cols:  # public market the read-only quote resolved to (slug / ticker)
        conn.execute("ALTER TABLE prediction_picks ADD COLUMN quote_market_id TEXT")
    for col, typ in (("kind", "TEXT NOT NULL DEFAULT 'single'"), ("stake_usd", "REAL"), ("legs_json", "TEXT"),
                     ("legs_mark_json", "TEXT"), ("rules_match", "INTEGER"),
                     ("hidden", "INTEGER NOT NULL DEFAULT 0")):
        if col not in cols:
            conn.execute(f"ALTER TABLE prediction_picks ADD COLUMN {col} {typ}")
    from .paper_risk import MOVES_SCHEMA
    conn.executescript(MOVES_SCHEMA)


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


def _usd(v: Any, field: str) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) \
            or not 0 < float(v) <= MAX_CAPACITY_USD:
        raise Reject(f"{field}: must be a finite number > 0 (USD)")
    return round(float(v), 2)


def _market_id(v: Any, field: str) -> str | None:
    if v is not None and (not isinstance(v, str) or not _MID.match(v)):
        raise Reject(f"{field}: invalid")
    return v


def validate_leg(leg: Any, field: str) -> dict[str, Any]:
    if not isinstance(leg, dict):
        raise Reject(f"{field}: not an object")
    extra = set(leg) - LEG_KEYS
    if extra:
        raise Reject(f"{field}: unknown keys {sorted(extra)[:5]}")
    if leg.get("venue") not in VENUES:
        raise Reject(f"{field}.venue: must be kalshi or polymarket")
    if leg.get("side") not in SIDES:
        raise Reject(f"{field}.side: must be YES or NO")
    try:
        market = _text(leg.get("market"), "market")
    except Reject as exc:
        raise Reject(f"{field}.{exc}") from exc
    try:
        url = _url(leg.get("url"))
    except Reject as exc:
        raise Reject(f"{field}.{exc}") from exc
    out = {"venue": leg["venue"], "market": market, "market_id": _market_id(leg.get("market_id"),
                                                                            f"{field}.market_id"),
           "side": leg["side"], "price": _prob(leg.get("price"), f"{field}.price")}
    if url:
        out["url"] = url
    return out


def validate_pick(p: Any, i: int) -> dict[str, Any]:
    f = f"picks[{i}]"
    if not isinstance(p, dict):
        raise Reject(f"{f}: not an object")
    extra = set(p) - PICK_KEYS
    if extra:
        raise Reject(f"{f}: unknown keys {sorted(extra)[:5]}")
    kind = p.get("kind", "single")
    if kind not in KINDS:
        raise Reject(f"{f}.kind: must be single, pair, parlay or scaled")
    if "rules_match" in p and kind != "pair":
        raise Reject(f"{f}.rules_match: only allowed when kind is pair")
    legs, rules_match = None, None
    if kind in ("parlay", "pair"):
        raw = p.get("legs")
        lo, hi = (2, 2) if kind == "pair" else (MIN_LEGS, MAX_LEGS)
        if not isinstance(raw, list) or not lo <= len(raw) <= hi:
            raise Reject(f"{f}.legs: a {kind} needs {lo if lo == hi else f'{lo}-{hi}'} legs")
        legs = [validate_leg(x, f"{f}.legs[{j}]") for j, x in enumerate(raw)]
        venues = {x["venue"] for x in legs}
        if p.get("venue") is not None and p.get("venue") not in VENUES:
            raise Reject(f"{f}.venue: must be kalshi or polymarket")
        if p.get("side", "YES") != "YES":
            raise Reject(f"{f}.side: omit it (each leg carries its own side)")
        venue = p.get("venue") or (venues.pop() if len(venues) == 1 else "mixed")
        side = "YES"
        if kind == "pair":
            if {x["venue"] for x in legs} != {"kalshi", "polymarket"} or legs[0]["side"] == legs[1]["side"]:
                raise Reject(f"{f}.legs: a pair is one kalshi leg and one polymarket leg on opposite outcomes")
            rm = p.get("rules_match")
            if rm is not None and not isinstance(rm, bool):
                raise Reject(f"{f}.rules_match: must be true or false")
            rules_match = rm
            venue = "mixed"
    else:
        if "legs" in p:
            raise Reject(f"{f}.legs: only allowed when kind is pair or parlay")
        if p.get("venue") not in VENUES:
            raise Reject(f"{f}.venue: must be kalshi or polymarket")
        if p.get("side") not in SIDES:
            raise Reject(f"{f}.side: must be YES or NO")
        venue, side = p["venue"], p["side"]
    if kind == "pair":  # combined price = sum of leg prices; fair defaults to the $1 payout
        combo = round(sum(x["price"] for x in legs), 4)
        if p.get("price") is not None:
            given = p["price"]
            if isinstance(given, bool) or not isinstance(given, (int, float)) or abs(given - combo) > PAIR_PRICE_TOL:
                raise Reject(f"{f}.price: a pair's price is the sum of its leg prices ({combo})")
        p = {**p, "price": combo, "market": p.get("market") or legs[0]["market"],
             "confidence": p.get("confidence", "low")}
        if not p.get("resolves"):
            raise Reject(f"{f}.resolves: required on a pair (YYYY-MM-DD)")
    if p.get("confidence") not in CONFIDENCE:
        raise Reject(f"{f}.confidence: must be low, medium or high")
    mid = _market_id(p.get("market_id"), f"{f}.market_id")
    res = p.get("resolves")
    if res is not None:
        if not isinstance(res, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", res):
            raise Reject(f"{f}.resolves: must be YYYY-MM-DD")
        try:
            date.fromisoformat(res)
        except ValueError as exc:
            raise Reject(f"{f}.resolves: not a date") from exc
    cap = _usd(p.get("capacity_usd"), f"{f}.capacity_usd")
    stake = _usd(p.get("stake_usd"), f"{f}.stake_usd")
    try:
        url = _url(p.get("url"))
        market = _text(p.get("market"), "market")
        note = _text(p.get("note"), "note", required=False)
    except Reject as exc:
        raise Reject(f"{f}.{exc}") from exc
    return {"venue": venue, "market": market, "market_id": mid, "url": url, "side": side,
            "price": p["price"] if kind == "pair" else _prob(p.get("price"), f"{f}.price"),
            "fair": (1.0 if p.get("fair") is None else _pair_fair(p["fair"], f)) if kind == "pair"
            else _prob(p.get("fair"), f"{f}.fair"),
            "confidence": p["confidence"], "resolves": res, "note": note,
            "capacity_usd": cap, "stake_usd": stake, "kind": kind, "legs": legs, "rules_match": rules_match}


def _pair_fair(v: Any, f: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v <= 1:
        raise Reject(f"{f}.fair: must be a number in (0, 1]")
    return float(v)


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


def _leg_state(main: sqlite3.Connection | None, quotes: Any, venue: str, mid: str | None, side: str,
               market: str, now: datetime) -> dict[str, Any]:
    """One market: {'result': won|lost|None, 'price': side price|None, 'source', 'at', 'quote_id'}."""
    res, how, q = lookup_resolution(main, venue, mid), "venue resolution", None
    if quotes is not None and not res:
        try:
            q = quotes.quote(venue, mid, market)
        except Exception:  # noqa: BLE001 - public quotes are best-effort, never fatal
            q = None
        if q is not None and q.result in ("yes", "no"):
            res, how = q.result, f"{q.source} resolution"
    qid = q.market_id if q is not None and q.market_id else None
    if res:
        won = (res == "yes") == (side == "YES")
        return {"result": "won" if won else "lost", "price": 1.0 if won else 0.0, "source": how,
                "at": now.isoformat(), "quote_id": qid}
    px, at, src = lookup_mark(main, venue, mid, side, now.timestamp()) if venue in VENUES else (None, None, None)
    if px is None and q is not None and q.side_price(side) is not None:
        px, at = q.side_price(side), now.isoformat()
        src = f"{q.source} {q.market_id}"[:160] if not mid or q.market_id != mid else q.source
    return {"result": None, "price": px, "source": src, "at": at, "quote_id": qid}


def _parlay_state(legs: list[dict[str, Any]], states: list[dict[str, Any]]) -> dict[str, Any]:
    """Any losing leg loses the parlay; it wins only when every leg has resolved in its favour."""
    if any(st["result"] == "lost" for st in states):
        return {"result": "lost", "price": 0.0}
    if all(st["result"] == "won" for st in states):
        return {"result": "won", "price": 1.0}
    if any(st["price"] is None for st in states):
        return {"result": None, "price": None}
    px = 1.0
    for st in states:
        px *= st["price"]
    return {"result": None, "price": round(px, 4)}


def _pair_state(states: list[dict[str, Any]]) -> dict[str, Any]:
    """Hedged pair: value = sum of leg marks; settles only when both legs resolve (payout = legs won)."""
    if all(st["result"] for st in states):
        won = sum(st["result"] == "won" for st in states)
        return {"result": "won" if won >= 1 else "lost", "price": float(won)}
    if any(st["price"] is None for st in states):
        return {"result": None, "price": None}
    return {"result": None, "price": round(sum(st["price"] for st in states), 4)}


class _PickQuotes:
    """Quotes adapter for move logging: venue 'parlay' -> combined leg-by-leg price of that pick."""

    def __init__(self, quotes: Any, main: sqlite3.Connection | None, legs: dict[str, list[dict[str, Any]]],
                 now: datetime, pairs: set[str] | None = None):
        self.quotes, self.main, self.legs, self.now, self.pairs = quotes, main, legs, now, pairs or set()

    def quote(self, venue: str, market_id: str | None, text: str = "") -> Any:
        if venue != "parlay":
            return self.quotes.quote(venue, market_id, text)
        legs = self.legs.get(market_id or "")
        if not legs:
            return None
        from .public_quotes import Quote
        states = [_leg_state(self.main, self.quotes, x["venue"], x.get("market_id"), x["side"], x["market"],
                             self.now) for x in legs]
        st = _pair_state(states) if len(legs) == 2 and {x["venue"] for x in legs} == {"kalshi", "polymarket"} \
            and legs[0]["side"] != legs[1]["side"] and market_id in self.pairs else _parlay_state(legs, states)
        res = None if market_id in self.pairs else {"won": "yes", "lost": "no"}.get(st["result"] or "")
        return Quote("parlay", market_id or "", None, None, st["price"], res, "legs", text[:200],
                     time.time(), "parlay legs")


def update_positions(conn: sqlite3.Connection, main_db: str | None, now: datetime,
                     quotes: Any = None) -> tuple[int, int]:
    """Mark/settle picks: NOEMA's own stored quotes first, then (when `quotes` is given) read-only public
    Kalshi / Polymarket-international quotes. Parlays are marked leg by leg (combined = product of leg prices)
    and settle only when every leg resolves (any losing leg loses). Scaled picks are planned, never settled.
    Also logs each open pick's price move at +1h/+1d/+1w."""
    from . import paper_risk
    main = _ro(main_db) if main_db else None
    marked = settled = 0
    cols = {r[1] for r in conn.execute("PRAGMA table_info(prediction_picks)")}
    kcol = "kind" if "kind" in cols else "'single'"
    lcol = "legs_json" if "legs_json" in cols else "NULL"
    try:
        rows = conn.execute(f"SELECT pick_id, venue, market_id, side, entry_price, market, {kcol}, {lcol}, status "
                            "FROM prediction_picks WHERE status IN ('open','planned')").fetchall()
        for pick_id, venue, mid, side, entry, market, kind, legs_json, status in rows:
            if kind in ("parlay", "pair") and legs_json:
                legs = json.loads(legs_json)
                states = [_leg_state(main, quotes, x["venue"], x.get("market_id"), x["side"], x["market"], now)
                          for x in legs]
                st = _pair_state(states) if kind == "pair" else _parlay_state(legs, states)
                conn.execute("UPDATE prediction_picks SET legs_mark_json=? WHERE pick_id=?",
                             (json.dumps([{k: v[k] for k in ("result", "price", "source")} for v in states]),
                              pick_id))
                st.update(source="parlay legs", at=now.isoformat(), quote_id=None)
            else:
                st = _leg_state(main, quotes, venue, mid, side, market, now)
            if st.get("quote_id"):
                conn.execute("UPDATE prediction_picks SET quote_market_id=? WHERE pick_id=?", (st["quote_id"], pick_id))
            if st["result"] and status == "open":
                sp = st["price"]
                conn.execute("UPDATE prediction_picks SET status='settled', settled_at=?, outcome=?, settle_price=?, "
                             "mark_price=?, mark_at=?, mark_source=?, pnl=? WHERE pick_id=?",
                             (now.isoformat(), st["result"], sp, sp, now.isoformat(), st["source"],
                              round(sp - entry, 4), pick_id))
                settled += 1
                continue
            if st["price"] is not None:
                conn.execute("UPDATE prediction_picks SET mark_price=?, mark_at=?, mark_source=?, pnl=? "
                             "WHERE pick_id=?", (st["price"], st["at"], st["source"],
                                                 None if status == "planned" else round(st["price"] - entry, 4),
                                                 pick_id))
                marked += 1
        if quotes is not None:
            since = (now - timedelta(days=8)).isoformat()
            bets, parlay_legs, pairs = [], {}, set()
            for pid, v, mid, market, side, entry, opened, kind, legs_json in conn.execute(
                    f"SELECT pick_id, venue, market_id, market, side, entry_price, opened_at, {kcol}, {lcol} "
                    "FROM prediction_picks WHERE opened_at >= ? AND status != 'planned'", (since,)).fetchall():
                if kind in ("parlay", "pair") and legs_json:
                    parlay_legs[pid] = json.loads(legs_json)
                    v, mid = "parlay", pid
                    if kind == "pair":
                        pairs.add(pid)
                bets.append(paper_risk.Bet("prediction_bot", pid, v, mid, market, side, float(entry),
                                           _dt_iso(opened)))
            paper_risk.schedule_moves(conn, bets)
            conn.commit()
            paper_risk.capture_moves(conn, now, _PickQuotes(quotes, main, parlay_legs, now, pairs),
                                     known=paper_risk.noema_resolution(main) if main is not None else None)
    finally:
        if main is not None:
            main.close()
    return marked, settled


def _dt_iso(v: str) -> datetime:
    d = datetime.fromisoformat(v)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


# ---------- ingestion ----------

def _move(f: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / f.name
    if target.exists():
        target = dest / f"{f.stem}-{int(time.time() * 1000)}{f.suffix}"
    shutil.move(str(f), str(target))


def ingest(db_path: str | None = None, inbox: Path | str | None = None, *, main_db: str | None = None,
           now: datetime | None = None, quotes: Any = None) -> dict[str, Any]:
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
                             "side, entry_price, fair, confidence, resolves, note, opened_at, capacity_usd, stake_usd, "
                             "kind, legs_json, status, rules_match) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (f"{post['id']}#{i}", post["id"], i, p["venue"], p["market"], p["market_id"], p["url"],
                              p["side"], p["price"], p["fair"], p["confidence"], p["resolves"], p["note"],
                              post["posted_at"].isoformat(), p.get("capacity_usd"), p.get("stake_usd"),
                              p.get("kind", "single"), json.dumps(p["legs"]) if p.get("legs") else None,
                              "planned" if p.get("kind") == "scaled" else "open",
                              None if p.get("rules_match") is None else int(p["rules_match"])))
            stats["accepted"] += 1
            moves.append((f, inbox_p / "archive" / day))
        conn.commit()  # picks are durable before any (rate-limited) public quote call
        stats["marked"], stats["settled"] = update_positions(conn, main_db, now, quotes)
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

def _moves(conn: sqlite3.Connection, pick_id: str) -> dict[str, float | None]:
    try:
        rows = conn.execute("SELECT offset_s, move FROM paper_bet_moves WHERE bot='prediction_bot' AND bet_id=? "
                            "AND captured_at IS NOT NULL", (pick_id,)).fetchall()
    except sqlite3.Error:
        return {}
    names = {3600: "1h", 86400: "1d", 604800: "1w"}
    return {names[o]: m for o, m in rows if o in names}


def pair_metrics(legs: list[dict[str, Any]], resolves: str | None, rules_match: Any,
                 today: date | None = None) -> dict[str, Any]:
    """Hedged pair economics per $1 payout: cost = sum(leg price + venue fee); locked profit per $ =
    (1 - cost) / cost; annualized over the days left to `resolves`. Only 'locked' when rules_match is true."""
    from .paper_risk import fee
    cost = sum(float(x["price"]) + fee(x["venue"], float(x["price"])) for x in legs)
    per = (1 - cost) / cost if cost > 0 else None
    days = None
    if resolves:
        try:
            days = max(1, (date.fromisoformat(resolves) - (today or datetime.now(UTC).date())).days)
        except ValueError:
            days = None
    ann = per * 365 / days if per is not None and days else None
    verified = rules_match in (1, True)
    return {"combined_cost": round(cost, 4), "profit_per_usd": None if per is None else round(per, 4),
            "annualized": None if ann is None else round(ann, 4), "days_to_resolve": days,
            "rules_match": verified, "verification": "rules match" if verified else "unverified",
            "locked": bool(verified and per is not None and per > 0)}


PICK_COLS = ("pick_id", "post_id", "opened_at", "venue", "market", "market_id", "url", "side", "entry_price", "fair",
             "confidence", "resolves", "note", "status", "mark_price", "mark_at", "mark_source", "outcome", "pnl",
             "capacity_usd", "stake_usd", "kind", "legs_json", "legs_mark_json", "quote_market_id", "rules_match",
             "hidden")


def room_view(db_path: str | None = None, *, limit_posts: int = 6) -> dict[str, Any]:
    """Picks for the room panel: newest version of each pick only (a later post with the same market + side, or
    the same pair / parlay legs, replaces the earlier one), hidden picks omitted. Post text stays stored."""
    from . import paper_risk
    db_path = db_path or default_db()
    out: dict[str, Any] = {"paper": True, "status": "no data", "posts": [], "picks": [], "stats": None,
                           "last_run_at": None, "role": "Prediction Bot picks · paper tracking only · never trades"}
    conn = _ro(db_path)
    if conn is None:
        out["note"] = "no Prediction Bot posts ingested yet"
        return out
    try:
        run = conn.execute("SELECT at, files, accepted, rejected FROM prediction_ingest_runs ORDER BY id DESC "
                           "LIMIT 1").fetchone()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(prediction_picks)")}
        sel = ", ".join(c if c in cols else f"NULL AS {c}" for c in PICK_COLS)
        raw = [dict(zip(PICK_COLS, r)) for r in conn.execute(
            f"SELECT {sel} FROM prediction_picks ORDER BY opened_at DESC, idx")]
        visible = paper_risk.latest_picks([r for r in raw if not r["hidden"]])
        order = {pid: i for i, pid in enumerate(r[0] for r in conn.execute(
            "SELECT post_id FROM prediction_posts ORDER BY posted_at DESC"))}
        visible.sort(key=lambda r: (order.get(r["post_id"], 1 << 30), r["pick_id"]))
        rows = []
        for r in visible:
            d = {"id": r["pick_id"], "post_id": r["post_id"], "venue": r["venue"], "market": r["market"],
                 "market_id": r["market_id"], "url": r["url"], "side": r["side"], "price": r["entry_price"],
                 "fair": r["fair"], "confidence": r["confidence"], "resolves": r["resolves"], "status": r["status"],
                 "current": r["mark_price"], "current_at": r["mark_at"], "current_source": r["mark_source"],
                 "outcome": r["outcome"], "pnl": r["pnl"], "capacity_usd": r["capacity_usd"],
                 "posted_stake_usd": r["stake_usd"], "kind": r["kind"] or "single"}
            legs = json.loads(r["legs_json"]) if r["legs_json"] else []
            marks = json.loads(r["legs_mark_json"]) if r["legs_mark_json"] else []
            for j, leg in enumerate(legs):
                m = marks[j] if j < len(marks) else {}
                leg.update(current=m.get("price"), result=m.get("result"), current_source=m.get("source"))
            d["legs"] = legs
            if d["kind"] == "pair" and len(legs) == 2:
                d["pair"] = pair_metrics(legs, r["resolves"], r["rules_match"])
                d["stake_usd"] = round(max(min(r["stake_usd"] or 1.0, r["capacity_usd"] or float("inf"),
                                               paper_risk.cap_pct() * paper_risk.bankroll()), 1.0), 2)
            else:
                d["stake_usd"] = paper_risk.pick_stake(d["venue"], d["fair"], d["price"], d["capacity_usd"],
                                                       stake_usd=r["stake_usd"])
            d["moves"] = _moves(conn, r["pick_id"])
            rows.append(d)
        out["picks"] = rows
        by_post: dict[str, list[dict[str, Any]]] = {}
        for d in rows:
            by_post.setdefault(d["post_id"], []).append(d)
        for pid, at, author, title, summary, rp in conn.execute(
                "SELECT post_id, posted_at, author, title, summary, report_path FROM prediction_posts "
                "ORDER BY posted_at DESC LIMIT ?", (limit_posts,)).fetchall():
            if by_post.get(pid):
                out["posts"].append({"id": pid, "posted_at": at, "author": author, "title": title,
                                     "summary": summary, "report_path": rp, "picks": by_post[pid]})
    except sqlite3.Error:
        out["note"] = "no Prediction Bot posts ingested yet"
        return out
    finally:
        conn.close()
    live = [d for d in rows if d["status"] != "planned"]
    settled = [d for d in live if d["status"] == "settled"]
    won = sum(d["outcome"] == "won" for d in settled)
    opn = [d for d in live if d["status"] == "open"]
    out["status"] = "active" if rows else "no data"
    out["last_run_at"] = run[0] if run else None
    out["last_run"] = dict(zip(("at", "files", "accepted", "rejected"), run)) if run else None
    out["stats"] = {"picks": len(rows), "open": len(opn), "settled": len(settled), "won": won,
                    "hit_rate": round(won / len(settled), 3) if settled else None,
                    "marked_open": sum(d["current"] is not None for d in opn),
                    "in_the_money_open": sum((d["pnl"] or 0) > 0 for d in opn),
                    "paper_pnl_per_contract": round(sum(d["pnl"] or 0 for d in live), 4),
                    "planned": len(rows) - len(live), "superseded_or_hidden": len(raw) - len(rows)}
    return out
    try:
        run = conn.execute("SELECT at, files, accepted, rejected FROM prediction_ingest_runs ORDER BY id DESC "
                           "LIMIT 1").fetchone()
        posts = conn.execute("SELECT post_id, posted_at, author, title, summary, report_path, n_picks "
                             "FROM prediction_posts ORDER BY posted_at DESC LIMIT ?", (limit_posts,)).fetchall()
        agg = conn.execute("SELECT COUNT(*), SUM(status='open'), SUM(status='settled'), SUM(outcome='won'), "
                           "SUM(mark_price IS NOT NULL AND status='open'), SUM(CASE WHEN status!='planned' "
                           "THEN pnl END), "
                           "SUM(CASE WHEN status='open' AND mark_price IS NOT NULL AND pnl>0 THEN 1 ELSE 0 END), "
                           "SUM(status='planned') FROM prediction_picks").fetchone()
        out["posts"] = []
        cols = {r[1] for r in conn.execute("PRAGMA table_info(prediction_picks)")}

        def col(name: str) -> str:
            return name if name in cols else "NULL"

        for pid, at, author, title, summary, rp, n in posts:
            picks = conn.execute("SELECT venue, market, market_id, url, side, entry_price, fair, confidence, resolves, "
                                 "note, status, mark_price, mark_at, mark_source, outcome, pnl, "
                                 f"{col('capacity_usd')}, {col('stake_usd')}, {col('kind')}, {col('legs_json')}, "
                                 f"{col('legs_mark_json')}, pick_id "
                                 "FROM prediction_picks WHERE post_id=? ORDER BY idx", (pid,)).fetchall()
            rows = []
            for r in picks:
                d = dict(zip(("venue", "market", "market_id", "url", "side", "price", "fair", "confidence",
                              "resolves", "note", "status", "current", "current_at", "current_source",
                              "outcome", "pnl", "capacity_usd", "posted_stake_usd", "kind"), r))
                d["kind"] = d["kind"] or "single"
                d["stake_usd"] = paper_risk.pick_stake(d["venue"], d["fair"], d["price"], d["capacity_usd"],
                                                       stake_usd=d["posted_stake_usd"])
                legs = json.loads(r[19]) if r[19] else []
                marks = json.loads(r[20]) if r[20] else []
                for j, leg in enumerate(legs):
                    m = marks[j] if j < len(marks) else {}
                    leg.update(current=m.get("price"), result=m.get("result"), current_source=m.get("source"))
                d["legs"] = legs
                d["moves"] = _moves(conn, r[21])
                rows.append(d)
            out["posts"].append({"id": pid, "posted_at": at, "author": author, "title": title, "summary": summary,
                                 "report_path": rp, "picks": rows})
    except sqlite3.Error:
        out["note"] = "no Prediction Bot posts ingested yet"
        return out
    finally:
        conn.close()
    total, open_, settled, won, marked, pnl, green, planned = (x or 0 for x in agg)
    out["status"] = "active" if total else "no data"
    out["last_run_at"] = run[0] if run else None
    out["last_run"] = dict(zip(("at", "files", "accepted", "rejected"), run)) if run else None
    out["stats"] = {"picks": total, "open": open_, "settled": settled, "won": won,
                    "hit_rate": round(won / settled, 3) if settled else None, "marked_open": marked,
                    "in_the_money_open": green, "paper_pnl_per_contract": round(pnl, 4),
                    "planned": planned}
    return out


def run_loop(interval: float = 120, db_path: str | None = None) -> None:
    init_inbox()
    while True:
        try:
            q = None
            if os.getenv("NOEMA_PUBLIC_QUOTES", "1") != "0":
                from .public_quotes import shared
                q = shared()
                q.new_cycle()
            s = ingest(db_path, quotes=q)
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
