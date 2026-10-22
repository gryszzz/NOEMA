"""Central connection heartbeat registry - READ ONLY, paper only, never reads or emits secrets.

Every connection TEV depends on (wallet/venue balance sources, API + market feeds, social crawlers, smart wallets,
agent loops) gets one record::

    {name, group, last_success_at, last_attempt_at, last_error, cadence_s, age_s, status: fresh|amber|red}

amber = last success older than 2x cadence (or the latest attempt failed), red = older than 6x cadence / never.

Evidence comes from two places, neither of which writes to noema.db (no extra lock contention):
  * observed: read-only probes of the newest row each feed already writes (sqlite ``mode=ro``) / file mtimes / pids.
  * explicit: ``beat(name, ok, error)`` writes a tiny JSON file per connection in ``NOEMA_HEARTBEAT_DIR``
    (atomic rename). When present it supplies last_attempt_at / last_error on top of the observed success time.

``snapshot()`` builds the registry, ``write_json()`` publishes it to ``NOEMA_HEARTBEATS_JSON``
(default /workspace/noema-run/heartbeats.json) so bots can check freshness without the UI, and
``stale_sources()`` / ``gate()`` let paper agents skip decisions that depend on a red source.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
from datetime import UTC, datetime
from typing import Any

SCHEMA = "tev.heartbeats.v1"
AMBER_X, RED_X = 2.0, 6.0
RUN_DIR = os.getenv("NOEMA_RUN_DIR", "/workspace/noema-run")


# pm_crypto snapshots pm_near once per loop (start.sh: --interval 900, PR 185).
NEAR_CLOSE_CADENCE_S = int(__import__("os").getenv("NOEMA_PM_CRYPTO_INTERVAL", "900"))


def hb_dir() -> str:
    return os.getenv("NOEMA_HEARTBEAT_DIR", os.path.join(RUN_DIR, "heartbeats"))


def json_path() -> str:
    return os.getenv("NOEMA_HEARTBEATS_JSON", os.path.join(RUN_DIR, "heartbeats.json"))


def skips_path() -> str:
    return os.path.join(hb_dir(), "skips.jsonl")


_SECRETISH = [
    (re.compile(r"(?i)(api[-_]?key|token|secret|password|auth|signature|sig|key)=([^&\s\"']+)"), r"\1=***"),
    (re.compile(r"(?i)(bearer|basic)\s+[A-Za-z0-9._~+/=-]+"), r"\1 ***"),
    (re.compile(r"https?://[^\s\"']*"), lambda m: m.group(0).split("?")[0]),
    (re.compile(r"[A-Za-z0-9_-]{40,}"), "***"),
]


def sanitize(err: Any, limit: int = 160) -> str | None:
    """Errors are shown on the dashboard: strip query strings, key=..., bearer tokens and long opaque strings."""
    if err is None or err == "":
        return None
    s = str(err)
    for rx, rep in _SECRETISH:
        s = rx.sub(rep, s)
    return s[:limit]


def to_epoch(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        return x / 1000 if x > 1e11 else x
    s = str(v).strip()
    try:
        return to_epoch(float(s))
    except ValueError:
        pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=UTC)).timestamp()


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat() if ts else None


def status_for(age_s: float | None, cadence_s: float, failed_since_success: bool = False) -> str:
    if age_s is None or age_s > RED_X * cadence_s:
        return "red"
    if age_s > AMBER_X * cadence_s or failed_since_success:
        return "amber"
    return "fresh"


# ---- explicit beats (file based, no db) ---------------------------------------------------------
def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.:-]", "_", name)[:80]


def beat(name: str, ok: bool = True, error: Any = None, *, cadence_s: float | None = None,
         now: float | None = None) -> None:
    """Record an attempt for ``name``. Never raises (a heartbeat must not break the loop it measures)."""
    try:
        d = hb_dir()
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, _safe(name) + ".json")
        now = now or time.time()
        try:
            with open(p) as f:
                prev = json.load(f)
        except (OSError, ValueError):
            prev = {}
        rec = {"name": name, "last_attempt_at": _iso(now),
               "last_success_at": _iso(now) if ok else prev.get("last_success_at"),
               "last_error": None if ok else sanitize(error) or "failed",
               "last_error_at": prev.get("last_error_at") if ok else _iso(now),
               "cadence_s": cadence_s or prev.get("cadence_s")}
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".hb")
        with os.fdopen(fd, "w") as f:
            json.dump(rec, f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, p)
    except Exception:  # noqa: BLE001, S110 - a heartbeat must never break the loop it measures
        pass


def read_beats() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    d = hb_dir()
    if not os.path.isdir(d):
        return out
    for fn in os.listdir(d):
        if fn.endswith(".json") and not fn.startswith("."):
            try:
                with open(os.path.join(d, fn)) as f:
                    r = json.load(f)
                out[r["name"]] = r
            except (OSError, ValueError, KeyError):
                continue
    return out


# ---- observed probes -----------------------------------------------------------------------------
def _ro(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
    c.execute("PRAGMA query_only=1")
    return c


def _q(path: str, sql: str, args: tuple = ()) -> list[tuple]:
    if not os.path.isfile(path):
        raise FileNotFoundError(os.path.basename(path) + " missing")
    c = _ro(path)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def _max(path: str, sql: str, args: tuple = ()) -> float | None:
    r = _q(path, sql, args)
    return to_epoch(r[0][0]) if r else None


def _rec(name: str, group: str, cadence: float, success: float | None, *, attempt: float | None = None,
         error: Any = None, detail: str | None = None, depends: str | None = None) -> dict[str, Any]:
    return {"name": name, "group": group, "cadence_s": cadence, "success": success,
            "attempt": attempt if attempt is not None else success, "error": error, "detail": detail,
            "depends": depends}


def _probe(fn: Any, name: str, group: str, cadence: float) -> list[dict[str, Any]]:
    try:
        r = fn()
        return r if isinstance(r, list) else [r]
    except Exception as exc:  # noqa: BLE001 - a broken probe is itself a red heartbeat, never a crash
        return [_rec(name, group, cadence, None, attempt=time.time(), error=f"probe: {type(exc).__name__}: {exc}")]


def _wallets(main: str) -> list[dict[str, Any]]:
    rows = _q(main, "SELECT at, sources_json FROM equity_feed_readings ORDER BY rowid DESC LIMIT 400")
    if not rows:
        return [_rec("wallet:none", "wallet", 60, None, error="no treasury readings yet")]
    cur = json.loads(rows[0][1] or "{}")
    out = []
    for venue, v in cur.items():
        if v.get("status") == "excluded":
            continue
        attempt = to_epoch(v.get("read_at") or rows[0][0])
        succ, err = (attempt, None) if v.get("status") == "ok" else (None, v.get("error") or v.get("status"))
        if succ is None:
            for at, sj in rows[1:]:
                pv = json.loads(sj or "{}").get(venue) or {}
                if pv.get("status") == "ok":
                    succ = to_epoch(pv.get("read_at") or at)
                    break
        out.append(_rec(f"wallet:{venue}", "wallet", 60, succ, attempt=attempt, error=err,
                        detail=f"${v['equity_usd']:.2f}" if isinstance(v.get("equity_usd"), (int, float)) else None))
    return out


def _launch_sources(main: str) -> list[dict[str, Any]]:
    got = {s: to_epoch(t) for s, t in _q(
        main, "SELECT source, MAX(received_at) FROM (SELECT source, received_at FROM launch_trades "
              "ORDER BY rowid DESC LIMIT 20000) GROUP BY source")}
    label = {"helius": "helius (pump.fun trades via logsSubscribe, free)", "pumpportal": "pumpportal (keyed trades, paid)"}
    srcs = ("helius", "pumpportal") if os.getenv("NOEMA_STREAM_TRADES", "0") == "1" else ("helius",)
    out = [_rec(f"feed:{s}", "feed", 30, got.get(s), detail=label[s]) for s in srcs]
    if len(srcs) == 1:  # keyed trades off: PumpPortal only supplies free creates/migrations
        cr = {s: to_epoch(t) for s, t in _q(main, "SELECT source, MAX(received_at) FROM launch_stream GROUP BY source")}
        out.append(_rec("feed:pumpportal", "feed", 120, cr.get("pumpportal"),
                        detail="pumpportal (free new-token + migration stream; trades from helius)"))
    return out


def _provider_health(main: str) -> list[dict[str, Any]]:
    out = []
    for prov, _st, _upd, ok_at, fail_at, _n, _cool, reason, *_ in _q(main, "SELECT * FROM trench_provider_health"):
        succ, fail = to_epoch(ok_at), to_epoch(fail_at)
        err = reason if fail and (not succ or fail >= succ) else None
        out.append(_rec(f"feed:{prov}", "feed", 120, succ, attempt=max(x for x in (succ, fail, 0) if x is not None) or None,
                        error=err, detail="trench provider"))
    return out


def _culture_sources(main: str) -> list[dict[str, Any]]:
    out = []
    for src, _cat, _url, code, _n, at in _q(main, "SELECT * FROM culture_source_state"):
        if src.startswith(("dexscreener", "pumpfun", "kalshi", "polymarket", "jupiter")):
            ok = str(code).startswith(("2", "3"))
            name = {"kalshi_markets": "crawl:kalshi", "polymarket_volume": "crawl:polymarket",
                    "pumpfun_live": "feed:pump.fun", "dexscreener_boosts_top": "feed:dexscreener"}.get(src)
            if name:
                t = to_epoch(at)
                out.append(_rec(name, "feed", 600, t if ok else None, attempt=t, error=None if ok else f"HTTP {code}",
                                detail=src))
    return out


def _news(main: str) -> list[dict[str, Any]]:
    rows = _q(main, "SELECT url, status, fetched_at FROM news_fetch_state")
    rss = [(s, to_epoch(t)) for u, s, t in rows if u.startswith("http") and "gdelt" not in u]
    gd = [(s, to_epoch(t)) for u, s, t in rows if "gdeltproject" in u]
    ok = lambda xs: max([t for s, t in xs if t and str(s)[:1] in "23"], default=None)
    last = lambda xs: max([t for _, t in xs if t], default=None)
    gd_err = next((f"HTTP {s}" for s, t in sorted(gd, key=lambda x: -(x[1] or 0)) if str(s)[:1] not in "23"), None)
    return [_rec("social:news_rss", "social", 300, ok(rss), attempt=last(rss), detail=f"{len(rss)} feeds"),
            _rec("social:gdelt", "social", 3600, ok(gd), attempt=last(gd),
                 error=gd_err if (last(gd) or 0) > (ok(gd) or 0) else None, detail="GDELT doc API")]


def _x_inbox(main: str) -> dict[str, Any]:
    run = _max(main, "SELECT MAX(at) FROM x_ingest_runs")
    inbox = os.getenv("NOEMA_X_INBOX", os.path.join(RUN_DIR, "x-inbox"))
    newest = None
    for root, _d, files in os.walk(inbox):
        for fn in files:
            if fn.endswith(".json"):
                newest = max(newest or 0, os.path.getmtime(os.path.join(root, fn)))
    return _rec("social:x_inbox", "social", 600, max(x for x in (run, newest, 0) if x is not None) or None,
                detail="X signal routine -> x-inbox")


LOOPS = {  # connection -> (process cmdline marker, cadence_s, log file in RUN_DIR)
    "agent:noema-agent": ("bin/noema-agent", 120, "agent.log"),
    "agent:dashboard": ("bin/noema-dashboard", 60, "dashboard.log"),
    "agent:proxy": ("room_proxy:app", 3600, "proxy.log"),
    "agent:treasury_updater": ("noema.equity_feed", 60, "equity_feed.log"),
    "agent:launch_stream": ("noema.launch_stream", 60, "launch_stream.log"),
    "agent:timing": ("noema.timing_agent", 120, "timing.log"),
    "agent:smart_wallets": ("noema.smart_wallets", 600, "smart_wallets.log"),
    "agent:strategies": ("noema.edge_strategies", 600, "strategies.log"),
    "agent:meta-launch": ("noema.meta_launch", 600, "meta.log"),
    "agent:avenues": ("noema.edge_avenues", 900, "avenues.log"),
    "agent:lifecycle": ("noema.agent_lifecycle", 900, "lifecycle.log"),
    "agent:pm_models": ("noema.pm_models", 21600, "pm_models.log"),
    "agent:pm_weak": ("noema.pm_weak", 21600, "pm_weak.log"),
    "agent:pm_crypto": ("noema.pm_crypto", 900, "pm_crypto.log"),
    "agent:fast_settle": ("noema.fast_settle", 300, "fast_settle.log"),
    "agent:cross_venue": ("noema.xv_match", 900, "xv.log"),
    "agent:pm_history": ("noema.pmus_history", 3600, "pmus_history.log"),
    "agent:news": ("noema.news_crawler", 300, "news.log"),
    "agent:culture": ("noema.culture_crawler", 600, "culture.log"),
}


def _cmdlines() -> list[str]:
    out = []
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    out.append(f.read().replace(b"\0", b" ").decode(errors="replace"))
            except OSError:
                continue
    return out


def _loops() -> list[dict[str, Any]]:
    """A loop is healthy while its process runs AND its log keeps moving (a hung loop goes amber/red by age)."""
    if not os.path.isdir(RUN_DIR):
        return []
    cmds = _cmdlines()
    out = []
    for name, (marker, cad, log) in LOOPS.items():
        alive = any(marker in c for c in cmds)
        lp = os.path.join(RUN_DIR, log)
        mt = os.path.getmtime(lp) if os.path.isfile(lp) else None
        succ = (mt or time.time()) if alive else None
        out.append(_rec(name, "agent", cad, succ, attempt=mt, error=None if alive else "process not running",
                        detail="process alive · log activity"))
    return out


def _live_checkout_branch(main_db: str) -> dict[str, Any]:
    """Red if the live checkout (repo containing the main DB) is not on main: agents must use git worktrees."""
    import subprocess
    repo = os.path.dirname(os.path.dirname(os.path.abspath(main_db)))
    now = time.time()
    br = subprocess.run(["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True,
                        text=True, timeout=5, check=False).stdout.strip()
    if br == "main":
        return _rec("system:live_checkout_branch", "system", 60, now, detail="live checkout on main")
    return _rec("system:live_checkout_branch", "system", 60, None, attempt=now,
                error=f"live checkout on '{br or '?'}', not main - use git worktrees", detail="live checkout branch")


def observe(main_db: str) -> list[dict[str, Any]]:
    dd = os.path.dirname(os.path.abspath(main_db))
    j = lambda f: os.path.join(dd, f)
    recs: list[dict[str, Any]] = []
    P = recs.extend
    P(_probe(lambda: _wallets(main_db), "wallet:treasury", "wallet", 60))
    P(_probe(lambda: _launch_sources(main_db), "feed:launch_trades", "feed", 30))
    P(_probe(lambda: _rec("feed:launch_stream", "feed", 60, _max(main_db, "SELECT MAX(received_at) FROM launch_stream"),
                          detail="new pump.fun / LaunchLab creates"), "feed:launch_stream", "feed", 60))
    P(_probe(lambda: _provider_health(main_db), "feed:trench_providers", "feed", 120))
    P(_probe(lambda: _rec("feed:geckoterminal", "feed", 300, _max(j("timing.db"), "SELECT MAX(fetched_at) FROM pools"),
                          detail="GeckoTerminal pools (timing)"), "feed:geckoterminal", "feed", 300))
    P(_probe(lambda: _rec("feed:candles", "feed", 120, _max(j("timing.db"), "SELECT MAX(ts) FROM candles"),
                          detail="launch candles"), "feed:candles", "feed", 120))
    P(_probe(lambda: _culture_sources(main_db), "feed:culture_sources", "feed", 600))
    P(_probe(lambda: _rec("feed:kalshi_crawl", "feed", 300, _max(main_db, "SELECT MAX(at) FROM fs_runs"),
                          detail="forward sampler (fs_runs)"), "feed:kalshi_crawl", "feed", 300))
    P(_probe(lambda: _rec("feed:near_close", "feed", NEAR_CLOSE_CADENCE_S, _max(j("pm_train.db"), "SELECT MAX(snap_ts) FROM pm_near"),
                          detail="near-close crypto"), "feed:near_close", "feed", NEAR_CLOSE_CADENCE_S))
    P(_probe(lambda: _rec("feed:kalshi_train", "feed", 3600, _max(j("pm_train.db"), "SELECT MAX(fetched_at) FROM pm_train"),
                          detail="Kalshi history"), "feed:kalshi_train", "feed", 3600))
    P(_probe(lambda: _rec("feed:cross_venue", "feed", 900, _max(main_db, "SELECT MAX(at) FROM xv_gap_history"),
                          detail="PM <-> Kalshi gaps"), "feed:cross_venue", "feed", 900))
    P(_probe(lambda: _rec("feed:pm_history", "feed", 3600, _max(j("pmus_history.db"), "SELECT MAX(at) FROM pmus_quotes"),
                          detail="Polymarket US history"), "feed:pm_history", "feed", 3600))
    P(_probe(lambda: _rec("feed:smart_wallets", "feed", 300, _max(j("smart_wallets.db"), "SELECT MAX(at) FROM wallet_trades"),
                          detail="vetted wallet moves"), "feed:smart_wallets", "feed", 300))
    P(_probe(lambda: _x_inbox(main_db), "social:x_inbox", "social", 600))
    P(_probe(lambda: _news(main_db), "social:news", "social", 300))
    P(_probe(lambda: _rec("social:culture", "social", 600,
                          _max(main_db, "SELECT MAX(started_at) FROM crawl_sweeps WHERE crawler='culture' AND errors<requests"),
                          detail="culture crawler sweep"), "social:culture", "social", 600))
    P(_probe(lambda: _rec("agent:noema-agent:cycle", "agent", 60, _max(main_db, "SELECT MAX(created_at) FROM agent_heartbeats"),
                          detail="agent_heartbeats"), "agent:noema-agent:cycle", "agent", 60))
    P(_probe(lambda: _rec("agent:trench_desks", "agent", 120, _max(main_db, "SELECT MAX(ts) FROM trench_desk_events"),
                          detail="desk cycle events"), "agent:trench_desks", "agent", 120))
    P(_probe(lambda: _rec("agent:samplers", "agent", 900, _max(j("edge_avenues.db"), "SELECT MAX(at) FROM avenue_reports"),
                          detail="edge avenue samplers"), "agent:samplers", "agent", 900))
    P(_probe(_loops, "agent:loops", "agent", 60))
    P(_probe(lambda: _live_checkout_branch(main_db), "system:live_checkout_branch", "system", 60))
    return recs


def snapshot(main_db: str, now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    beats = read_beats()
    recs = {r["name"]: r for r in observe(main_db)}
    for name, b in beats.items():  # explicit beats add attempt/error and may be the only evidence
        r = recs.setdefault(name, _rec(name, name.split(":")[0], float(b.get("cadence_s") or 60), None))
        bs, ba = to_epoch(b.get("last_success_at")), to_epoch(b.get("last_attempt_at"))
        r["success"] = max(x for x in (r["success"], bs, 0) if x is not None) or None
        if ba and (not r["attempt"] or ba >= r["attempt"]):
            r["attempt"], r["error"] = ba, b.get("last_error")
        if b.get("cadence_s"):
            r["cadence_s"] = float(b["cadence_s"])
    out = []
    for r in sorted(recs.values(), key=lambda r: (r["group"], r["name"])):
        age = now - r["success"] if r["success"] else None
        failed = bool(r["error"]) and (r["attempt"] or 0) >= (r["success"] or 0)
        out.append({"name": r["name"], "group": r["group"], "detail": r["detail"],
                    "last_success_at": _iso(r["success"]), "last_attempt_at": _iso(r["attempt"]),
                    "last_error": sanitize(r["error"]) if failed or not r["success"] else None,
                    "cadence_s": r["cadence_s"], "age_s": round(age, 1) if age is not None else None,
                    "status": status_for(age, r["cadence_s"], failed)})
    counts = {s: sum(1 for x in out if x["status"] == s) for s in ("fresh", "amber", "red")}
    return {"schema": SCHEMA, "paper_only": True, "at": _iso(now), "as_of": _iso(now), "cadence_s": 15,
            "rule": "amber > 2x cadence (or last attempt failed), red > 6x cadence or never",
            "counts": counts, "total": len(out), "connections": out}


def write_json(snap: dict[str, Any], path: str | None = None) -> None:
    path = path or json_path()
    try:
        d = os.path.dirname(path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".heartbeats")
        with os.fdopen(fd, "w") as f:
            json.dump(snap, f, indent=1)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError:
        pass


# ---- freshness gate -------------------------------------------------------------------------------
def load(path: str | None = None, max_file_age_s: float = 120) -> dict[str, Any] | None:
    """The published registry, or None if missing/too old (then gates fail OPEN to probes? no: callers decide)."""
    p = path or json_path()
    try:
        if time.time() - os.path.getmtime(p) > max_file_age_s:
            return None
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def stale_sources(names: list[str], snap: dict[str, Any] | None = None) -> list[str]:
    """Names (exact or prefix like 'wallet:') whose status is red. Unknown registry -> [] (gate fails open only
    when the registry itself is unavailable, which the dashboard then shows as its own red heartbeat)."""
    snap = snap if snap is not None else load()
    if not snap:
        return []
    conns = snap.get("connections") or []
    bad = []
    for n in names:
        hit = [c for c in conns if c["name"] == n or (n.endswith(":") and c["name"].startswith(n))]
        bad += [c["name"] for c in hit if c["status"] == "red"]
    return sorted(set(bad))


def record_skip(agent: str, stale: list[str], subject: str = "decision", now: float | None = None) -> None:
    try:
        os.makedirs(hb_dir(), exist_ok=True)
        with open(skips_path(), "a") as f:
            f.write(json.dumps({"agent": agent, "at": _iso(now or time.time()), "subject": subject,
                                "decision": "skip", "reason": "skip: stale " + ", ".join(stale)}) + "\n")
        if os.path.getsize(skips_path()) > 512_000:
            with open(skips_path()) as f:
                lines = f.readlines()[-500:]
            with open(skips_path(), "w") as f:
                f.writelines(lines)
    except OSError:
        pass


def gate(agent: str, depends_on: list[str], subject: str = "decision") -> list[str]:
    """Return the red sources ``agent`` depends on; if any, the skip is logged for the 'what each agent decided' panel."""
    bad = stale_sources(depends_on)
    if bad:
        record_skip(agent, bad, subject)
    return bad


def recent_skips(limit: int = 12) -> list[dict[str, Any]]:
    try:
        with open(skips_path()) as f:
            lines = f.readlines()[-limit * 4:]
    except OSError:
        return []
    out, seen = [], set()
    for ln in reversed(lines):
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        k = (e.get("agent"), e.get("reason"))
        if k in seen:
            continue
        seen.add(k)
        e.setdefault("ref", None)
        e.setdefault("features", {})
        out.append(e)
        if len(out) >= limit:
            break
    return out
