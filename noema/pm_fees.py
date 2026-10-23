"""Per-market Polymarket (international) taker fees, read from Gamma and cached (PAPER ONLY, read-only HTTP).

Polymarket international charges ``fee = C x rate x (p x (1 - p))^exponent`` with a rate set per market
(docs.polymarket.com/trading/fees and /market-data/market-details): Gamma's ``feesEnabled`` and
``feeSchedule {rate, exponent, takerOnly, rebateRate}``. Many markets are fee-free (``feeType: zero_fees``,
rate 0, or ``feesEnabled: false``); crypto is 0.07, sports 0.03-0.05, and so on. Polymarket US is a different
exchange with one exchange-wide coefficient (0.0695, see ``edge_strategies.pmus_fee``) and is not handled here.

The cache is its own SQLite file (``NOEMA_PM_FEE_DB``, default ``pm_fees.db`` next to ``NOEMA_DB_PATH``).
Lookups never touch the network; only ``refresh()`` (called from the Prediction Bot inbox loop) fetches.
Known schedules are refreshed after ``NOEMA_PM_FEE_TTL_S`` (6 h) and trusted for up to 7 days; failed or
unknown lookups are retried after 10 min. Unknown -> the caller falls back to the flat rate and marks the
pick 'fee assumed'.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from typing import Any

GAMMA = "https://gamma-api.polymarket.com"
TRUST_S = 7 * 86400
RETRY_S = 600
_NUM = re.compile(r"^\d{1,12}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,159}$")
_memo: dict[str, tuple[float, dict[str, Any] | None]] = {}
_MEMO_S = 60.0


def ttl_s() -> float:
    try:
        return max(300.0, float(os.getenv("NOEMA_PM_FEE_TTL_S", "21600")))
    except ValueError:
        return 21600.0


def db_path() -> str:
    main = os.getenv("NOEMA_DB_PATH", "data/noema.db")
    return os.getenv("NOEMA_PM_FEE_DB", os.path.join(os.path.dirname(os.path.abspath(main)), "pm_fees.db"))


def _conn(path: str | None = None) -> sqlite3.Connection:
    c = sqlite3.connect(path or db_path(), timeout=5)
    c.execute("CREATE TABLE IF NOT EXISTS pm_fee_cache (market_id TEXT PRIMARY KEY, known INTEGER NOT NULL, "
              "fees_enabled INTEGER, rate REAL, exponent REAL, fee_type TEXT, gamma_id TEXT, fetched_at REAL NOT NULL)")
    return c


def valid_id(mid: Any) -> bool:
    return isinstance(mid, str) and bool(_NUM.match(mid) or _SLUG.match(mid))


def parse(m: dict[str, Any]) -> dict[str, Any]:
    """Gamma market -> schedule. feesEnabled false -> rate 0; feeSchedule.rate present -> that rate;
    anything else (fees enabled but no schedule) -> unknown."""
    en, sch = m.get("feesEnabled"), m.get("feeSchedule")
    out: dict[str, Any] = {"known": False, "fees_enabled": en, "rate": None, "exponent": 1.0,
                           "fee_type": m.get("feeType"), "gamma_id": str(m.get("id") or "") or None}
    if en is False:
        out.update(known=True, rate=0.0)
    elif isinstance(sch, dict) and sch.get("rate") is not None:
        try:
            out.update(known=True, rate=float(sch["rate"]), exponent=float(sch.get("exponent") or 1))
        except (TypeError, ValueError):
            pass
    return out


def fee(sched: dict[str, Any], price: float) -> float:
    return float(sched["rate"]) * (price * (1 - price)) ** float(sched.get("exponent") or 1)


def lookup(market_id: Any, path: str | None = None) -> dict[str, Any] | None:
    """Cached schedule for a Polymarket international market id or slug; None when unknown or stale."""
    if not valid_id(market_id):
        return None
    now = time.time()
    hit = _memo.get(market_id)
    if hit and now - hit[0] < _MEMO_S and path is None:
        return hit[1]
    p = path or db_path()
    res = None
    if os.path.exists(p):
        try:
            c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2)
            try:
                r = c.execute("SELECT known, fees_enabled, rate, exponent, fee_type, fetched_at FROM pm_fee_cache "
                              "WHERE market_id=?", (market_id,)).fetchone()
            finally:
                c.close()
            if r and r[0] and now - r[5] < TRUST_S:
                res = {"rate": r[2], "exponent": r[3], "fees_enabled": None if r[1] is None else bool(r[1]),
                       "fee_type": r[4], "fetched_at": r[5]}
        except sqlite3.Error:
            res = None
    if path is None:
        _memo[market_id] = (now, res)
    return res


def _get(url: str, timeout: float) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "noema-paper/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # fixed https host
        return json.loads(r.read(2_000_000))


def fetch(market_id: str, timeout: float = 5.0) -> dict[str, Any]:
    if _NUM.match(market_id):
        m = _get(f"{GAMMA}/markets/{market_id}", timeout)
    else:
        rows = _get(f"{GAMMA}/markets?slug={urllib.parse.quote(market_id)}", timeout)
        m = rows[0] if isinstance(rows, list) and rows else {}
    return parse(m if isinstance(m, dict) else {})


def due(ids: list[str], path: str | None = None, now: float | None = None) -> list[str]:
    now = now or time.time()
    c = _conn(path)
    try:
        have = {r[0]: (r[1], r[2]) for r in c.execute("SELECT market_id, known, fetched_at FROM pm_fee_cache")}
    finally:
        c.close()
    out = []
    for m in dict.fromkeys(i for i in ids if valid_id(i)):
        k = have.get(m)
        if k is None or now - k[1] >= (ttl_s() if k[0] else RETRY_S):
            out.append(m)
    return out


def refresh(ids: list[str], *, path: str | None = None, limit: int = 40, fetcher: Any = None) -> dict[str, int]:
    """Fetch the schedules that are due (bounded per call); store known and unknown results."""
    fetcher = fetcher or fetch
    todo = due(ids, path)[:limit]
    stats = {"due": len(todo), "known": 0, "unknown": 0, "errors": 0}
    rows = []
    for mid in todo:
        if stats["errors"] >= 3:  # Gamma unreachable: stop for this cycle, keep what is cached
            break
        try:
            s = fetcher(mid)
        except Exception:  # noqa: BLE001 - network is best-effort; unknown -> flat rate, retried later
            stats["errors"] += 1
            if lookup(mid, path or db_path()) is not None:
                continue  # keep the last known schedule (trusted up to 7 days) rather than forgetting it
            s = {"known": False, "fees_enabled": None, "rate": None, "exponent": 1.0, "fee_type": None,
                 "gamma_id": None}
        stats["known" if s["known"] else "unknown"] += 1
        rows.append((mid, int(bool(s["known"])), None if s["fees_enabled"] is None else int(bool(s["fees_enabled"])),
                     s["rate"], s["exponent"], s["fee_type"], s["gamma_id"], time.time()))
    if rows:
        c = _conn(path)
        try:
            c.executemany("INSERT OR REPLACE INTO pm_fee_cache VALUES (?,?,?,?,?,?,?,?)", rows)
            c.commit()
        finally:
            c.close()
        _memo.clear()
    return stats
