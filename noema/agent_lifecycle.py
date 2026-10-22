"""AGENT LIFECYCLE (hire/fire). Paper/shadow only — nothing here can place an order or touch flags.

Roster of rule agents with a genome (entry filters + exit params). Each agent is scored
PROSPECTIVELY: only pool-sweep samples captured after its hire time count. Fire rules:
  * n >= MIN_N and the 95% upper bound of mean net return < 0 (losing after costs, with confidence)
  * n >= 3*MIN_N and mean net < 0
Forecast agents from the accuracy ledger are also scored (Brier vs the market price, paired);
for them a "fire" is an advisory roster verdict (weight 0), their code is not changed.
On a fire: a post-mortem (losses by category) is written and a replacement is hired —
a mutation of the best surviving agent plus the post-mortem fix. New hires start in shadow and
earn weight only when n >= MIN_N with a 95% lower bound > 0. Population is capped.
Own DB: data/agent_lifecycle.db.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sqlite3
import time
from datetime import UTC, datetime
from typing import Any

from . import edge_mining as em

MIN_N = int(os.getenv("NOEMA_LIFECYCLE_MIN_N", "30"))
POP_CAP = int(os.getenv("NOEMA_LIFECYCLE_POP_CAP", "12"))
POP_MIN = 6
DEFAULT_EXIT = {"tp1": 0.30, "tp1_sell": 0.5, "runner": 0.25, "trail_atr": 2.5, "stop": -0.15,
                "time_stop_h": 24, "extend_h": 24}

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (agent_id TEXT PRIMARY KEY, name TEXT, kind TEXT, generation INTEGER,
  parent_id TEXT, status TEXT, weight REAL DEFAULT 0, genome_json TEXT, hired_at TEXT, fired_at TEXT,
  fire_reason TEXT, origin TEXT, stats_json TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS postmortems (agent_id TEXT PRIMARY KEY, at TEXT, summary TEXT, by_category_json TEXT,
  fix_json TEXT);
CREATE TABLE IF NOT EXISTS lifecycle_events (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, agent_id TEXT,
  event TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS edge_reports (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, report_json TEXT);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    c = sqlite3.connect(path, timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def _event(c: sqlite3.Connection, aid: str, ev: str, detail: str) -> None:
    c.execute("INSERT INTO lifecycle_events (at, agent_id, event, detail) VALUES (?,?,?,?)", (_now(), aid, ev, detail))


def _aid(seed: str) -> str:
    return hashlib.sha1(seed.encode()).hexdigest()[:10]


def hire(c: sqlite3.Connection, genome: dict[str, Any], *, generation: int, parent: str | None,
         origin: str, name: str | None = None, kind: str | None = None) -> str:
    kind = kind or genome.get("kind", "trench_rule")
    aid = _aid(json.dumps(genome, sort_keys=True) + _now() + str(random.random()))
    name = name or f"{origin.upper()[:5]}-g{generation}-{aid[:4]}"
    c.execute("INSERT INTO agents (agent_id,name,kind,generation,parent_id,status,genome_json,hired_at,origin)"
              " VALUES (?,?,?,?,?,'shadow',?,?,?)",
              (aid, name, kind, generation, parent, json.dumps(genome), _now(), origin))
    _event(c, aid, "hired", f"{origin}; parent={parent}; rule={em.rule_name(genome['filters'])}")
    return aid


def mutate(genome: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    g = json.loads(json.dumps(genome))
    for f in g["filters"]:
        f["v"] = f["v"] * rng.uniform(0.8, 1.25) if f["v"] else rng.uniform(0, 0.05)
    ex = g.setdefault("exit", dict(DEFAULT_EXIT))
    for k, lo, hi in (("tp1", 0.15, 0.8), ("runner", 0.2, 0.3), ("trail_atr", 1.5, 4.0), ("stop", -0.3, -0.06)):
        ex[k] = round(min(hi, max(lo, ex.get(k, DEFAULT_EXIT[k]) * rng.uniform(0.85, 1.18))), 4)
    if rng.random() < 0.3 and len(g["filters"]) > 1:
        g["filters"].pop(rng.randrange(len(g["filters"])))
    return g


def _bands(s: dict[str, Any]) -> dict[str, str]:
    liq = s["liq"] or 0
    age = s["x"].get("age_seconds") or 0
    if s["disposition"] == "meta":
        lb = "<1k" if liq < 1e3 else "1k-5k" if liq < 5e3 else "5k-25k" if liq < 2.5e4 else ">25k"
        return {"liquidity": lb, "stage": "early" if s["x"].get("stage_early") else "late",
                "rank": "first" if s["x"].get("first") else "copycat"}
    return {"liquidity": "<1k" if liq < 1e3 else "1k-5k" if liq < 5e3 else "5k-25k" if liq < 2.5e4 else ">25k",
            "age": "<5m" if age < 300 else "5-30m" if age < 1800 else ">30m",
            "disposition": s["disposition"]}


def postmortem(samples: list[dict[str, Any]]) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    by: dict[str, dict[str, list[float]]] = {}
    for s in samples:
        for cat, band in _bands(s).items():
            by.setdefault(cat, {}).setdefault(band, []).append(s["net"])
    table = {cat: {b: {"n": len(v), "mean_net": round(em.mean(v), 4), "loss_total": round(sum(x for x in v if x < 0), 3)}
                   for b, v in d.items()} for cat, d in by.items()}
    worst = min(((cat, b, st) for cat, d in table.items() for b, st in d.items() if st["n"] >= 5),
                key=lambda t: t[2]["loss_total"], default=None)
    fix = None
    if worst:
        cat, band, st = worst
        if cat == "liquidity":
            floors = {"<1k": 1e3, "1k-5k": 5e3, "5k-25k": 2.5e4}
            fix = {"f": "liquidity_usd", "op": ">=", "v": floors.get(band, 1e3)} if band in floors else None
        elif cat == "age":
            fix = {"f": "age_seconds", "op": ">=", "v": 300.0} if band == "<5m" else \
                  {"f": "age_seconds", "op": "<=", "v": 1800.0}
        elif cat == "stage" and band == "late":
            fix = {"f": "stage_early", "op": ">=", "v": 1.0}
        elif cat == "rank" and band == "copycat":
            fix = {"f": "first", "op": ">=", "v": 1.0}
        elif cat == "disposition" and band == "quarantine":
            fix = {"f": "observe", "op": ">=", "v": 1.0}
        summary = (f"lost most in {cat}={band} (n={st['n']}, mean net {st['mean_net']:+.1%}); "
                   f"fix: {em.rule_name([fix]) if fix else 'none found'}")
    else:
        summary = "too few samples per category to locate the loss"
    return summary, table, fix


def score_trench(agent: sqlite3.Row, samples: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    g = json.loads(agent["genome_json"])
    mine = [s for s in samples if s["at"] >= agent["hired_at"] and em.rule_match(s["x"], g["filters"])]
    nets = [s["net"] for s in mine]
    lo, hi, _ = em.bootstrap(nets) if len(nets) >= 2 else (None, None, 1.0)
    return {"n": len(nets), "mean_net": round(em.mean(nets), 4) if nets else None,
            "ci95": [round(lo, 4), round(hi, 4)] if lo is not None else None,
            "win_rate": round(sum(v > 0 for v in nets) / len(nets), 3) if nets else None}, mine


def score_forecasters(db: str) -> list[dict[str, Any]]:
    c = em._ro(db)
    try:
        if not em._has(c, "accuracy_resolutions"):
            return []
        rows = c.execute("SELECT f.agent_id, f.venue, f.decision, f.predicted_probability p, f.market_price m,"
                         " r.outcome o, r.ev_after_cost ev FROM accuracy_forecasts f JOIN accuracy_resolutions r"
                         " ON r.forecast_id=f.id WHERE f.market_price BETWEEN 0 AND 1").fetchall()
    finally:
        c.close()
    by: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by.setdefault(r["agent_id"], []).append(r)
    out = []
    for aid, rs in sorted(by.items()):
        diff = [(r["p"] - r["o"]) ** 2 - (r["m"] - r["o"]) ** 2 for r in rs]   # >0 = worse than market
        lo, hi, _ = em.bootstrap(diff) if len(diff) >= 2 else (None, None, 1.0)
        pnl = sum(r["ev"] or 0 for r in rs)
        verdict = "learning"
        if len(rs) >= MIN_N and lo is not None and lo > 0:
            verdict = "fire: Brier worse than market (95% CI excludes 0)"
        elif len(rs) >= MIN_N and hi is not None and hi < 0:
            verdict = "beats market"
        out.append({"agent": aid, "n": len(rs),
                    "brier": round(em.mean([(r["p"] - r["o"]) ** 2 for r in rs]), 4),
                    "market_brier": round(em.mean([(r["m"] - r["o"]) ** 2 for r in rs]), 4),
                    "brier_minus_market_ci95": [round(lo, 4), round(hi, 4)] if lo is not None else None,
                    "paper_pnl_after_cost": round(pnl, 3), "verdict": verdict})
    return out


def seed(c: sqlite3.Connection, report: dict[str, Any]) -> None:
    """Generation 0: edge-mining survivors first, then the least-bad train rules as hypotheses to watch."""
    if c.execute("SELECT COUNT(*) FROM agents").fetchone()[0]:
        return
    tr = report["families"][0]
    picks = [(s["filters"], "edge_survivor") for s in tr.get("survivors", [])]
    for b in tr.get("best_train_rules", []):
        filt = [{"f": p.split(" ")[0], "op": p.split(" ")[1], "v": float(p.split(" ")[2])}
                for p in b["rule"].split(" & ")] if b["rule"] != "all" else []
        picks.append((filt, "hypothesis"))
    picks.append(([{"f": "liquidity_usd", "op": ">=", "v": 5000.0}, {"f": "observe", "op": ">=", "v": 1.0}], "hypothesis"))
    for filt, origin in picks[:POP_MIN]:
        hire(c, {"filters": filt, "horizon_s": em.HORIZON, "exit": dict(DEFAULT_EXIT)}, generation=0,
             parent=None, origin=origin)


META_SEEDS = [
    ("shield pass & first", [{"f": "shield_ok", "op": ">=", "v": 1.0}, {"f": "first", "op": ">=", "v": 1.0}]),
    ("shield pass & early", [{"f": "shield_ok", "op": ">=", "v": 1.0}, {"f": "stage_early", "op": ">=", "v": 1.0}]),
    ("shield pass & liq>=5k", [{"f": "shield_ok", "op": ">=", "v": 1.0}, {"f": "liquidity_usd", "op": ">=", "v": 5000.0}]),
    ("shield pass & X cross", [{"f": "shield_ok", "op": ">=", "v": 1.0}, {"f": "cross", "op": ">=", "v": 1.0}]),
]


def seed_meta(c: sqlite3.Connection) -> None:
    """Meta-launch genomes join the roster as shadow agents (once)."""
    if c.execute("SELECT 1 FROM agents WHERE kind='meta_launch'").fetchone():
        return
    from .meta_launch import META_EXIT
    for _, filt in META_SEEDS:
        hire(c, {"kind": "meta_launch", "filters": filt, "exit": dict(META_EXIT)}, generation=0, parent=None,
             origin="meta", kind="meta_launch")


AVENUE_SEEDS = {"funding_carry": "carry: spot long / perp short when trailing funding clears costs+buffer",
                "kalshi_mm": "kalshi passive MM 1c inside spread, fills only on trades through",
                "news_lag": "news/X lag continuation, 2c move at +5m, exit +60m",
                "pm_weather": "kalshi weather: market + Open-Meteo prior-run forecast stack, 08:00 LST entry",
                "pm_thin": "wide-spread (>=3c) markets: walk-forward calibration / favourite-longshot correction",
                "pm_politics": "long-dated politics: calibration + 14d drift mean-reversion at 30d to close"}


def seed_avenues(c: sqlite3.Connection) -> None:
    """Paper agents from noema.edge_avenues join the roster as shadow agents (once per kind)."""
    for kind, desc in AVENUE_SEEDS.items():
        if not c.execute("SELECT 1 FROM agents WHERE kind=?", (kind,)).fetchone():
            hire(c, {"kind": kind, "filters": [], "desc": desc}, generation=0, parent=None, origin="avenue",
                 name=f"{kind.upper().replace('_', '-')}-g0", kind=kind)


def load_meta_samples(meta_db: str | None) -> list[dict[str, Any]]:
    if not meta_db or not os.path.isfile(meta_db):
        return []
    c = sqlite3.connect(f"file:{meta_db}?mode=ro", uri=True, timeout=10)
    try:
        rows = c.execute("SELECT mint, launched_at, shield_ok, features_json, outcome_json FROM meta_matches").fetchall()
    except sqlite3.Error:
        return []
    finally:
        c.close()
    out = []
    for mint, at, ok, fj, oj in rows:
        oc = json.loads(oj or "{}")
        if oc.get("net") is None:
            continue
        x = dict(json.loads(fj or "{}"), shield_ok=float(ok))
        out.append({"id": mint, "at": at, "x": x, "liq": x.get("liquidity_usd", 0), "disposition": "meta",
                    "net": oc["net"], "gross": oc["net"]})
    return sorted(out, key=lambda s: s["at"])


def cycle(db: str, life_db: str, feed_db: str | None = None, rng: random.Random | None = None,
          meta_db: str | None = None, avenues_db: str | None = None) -> dict[str, Any]:
    rng = rng or random.Random()
    report = em.mine_all(db, feed_db)
    by_kind = {"trench_rule": em.load_trench(db), "meta_launch": load_meta_samples(meta_db)}
    if avenues_db:
        from .edge_avenues import load_samples
        by_kind.update(load_samples(avenues_db))
    c = connect(life_db)
    try:
        c.execute("INSERT INTO edge_reports (at, report_json) VALUES (?,?)", (report["at"], json.dumps(report)))
        c.execute("DELETE FROM edge_reports WHERE id NOT IN (SELECT id FROM edge_reports ORDER BY id DESC LIMIT 48)")
        seed(c, report)
        if meta_db:
            seed_meta(c)
        if avenues_db:
            seed_avenues(c)
        live = c.execute("SELECT * FROM agents WHERE status IN ('shadow','earning')").fetchall()
        fired = []
        scored: dict[str, dict[str, Any]] = {}
        for a in live:
            st, mine = score_trench(a, by_kind.get(a["kind"] or "trench_rule", []))
            scored[a["agent_id"]] = st
            reason = None
            if st["n"] >= MIN_N and st["ci95"] and st["ci95"][1] < 0:
                reason = f"losing after costs with confidence (n={st['n']}, 95% CI {st['ci95']})"
            elif st["n"] >= 3 * MIN_N and (st["mean_net"] or 0) < 0:
                reason = f"negative after costs over {st['n']} samples"
            if reason:
                summary, table, fix = postmortem(mine)
                c.execute("UPDATE agents SET status='fired', weight=0, fired_at=?, fire_reason=?, stats_json=? WHERE agent_id=?",
                          (_now(), reason, json.dumps(st), a["agent_id"]))
                c.execute("INSERT OR REPLACE INTO postmortems VALUES (?,?,?,?,?)",
                          (a["agent_id"], _now(), summary, json.dumps(table), json.dumps(fix)))
                _event(c, a["agent_id"], "fired", reason + " | " + summary)
                fired.append((a, fix))
                continue
            earning = st["n"] >= MIN_N and st["ci95"] and st["ci95"][0] > 0
            c.execute("UPDATE agents SET status=?, stats_json=? WHERE agent_id=?",
                      ("earning" if earning else "shadow", json.dumps(st), a["agent_id"]))
        # weights: only earning agents, proportional to their lower bound (paper allocation only)
        earn = c.execute("SELECT agent_id, stats_json FROM agents WHERE status='earning'").fetchall()
        tot = sum(max(0.0, json.loads(r["stats_json"])["ci95"][0]) for r in earn) or 1.0
        c.execute("UPDATE agents SET weight=0 WHERE status!='earning'")
        for r in earn:
            c.execute("UPDATE agents SET weight=? WHERE agent_id=?",
                      (round(json.loads(r["stats_json"])["ci95"][0] / tot, 4), r["agent_id"]))
        # replacements
        survivors = c.execute("SELECT * FROM agents WHERE status IN ('shadow','earning')").fetchall()
        def fitness(r: sqlite3.Row) -> float:
            st = scored.get(r["agent_id"]) or json.loads(r["stats_json"] or "{}")
            return st.get("mean_net") if st.get("mean_net") is not None else -1.0
        for a, fix in fired:
            same = [r for r in survivors if r["kind"] == a["kind"]]
            if len(same) >= POP_CAP:
                continue
            parent = max(same, key=fitness) if same else a
            g = mutate(json.loads(parent["genome_json"]), rng)
            if fix and not any(f["f"] == fix["f"] for f in g["filters"]):
                g["filters"].append(fix)
            g["postmortem_of"] = a["agent_id"]
            hire(c, g, generation=max(a["generation"], parent["generation"]) + 1, parent=parent["agent_id"],
                 origin="replacement", kind=a["kind"])
            survivors = c.execute("SELECT * FROM agents WHERE status IN ('shadow','earning')").fetchall()
        c.commit()
        return {"fired": len(fired), "active": len(survivors), "verdict": report["verdict"]}
    finally:
        c.close()


def roster(life_db: str, db: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"schema": "tev.roster.v1", "mode": "paper/shadow only", "min_n": MIN_N,
                           "population_cap": POP_CAP, "agents": [], "events": [], "forecasters": []}
    if os.path.isfile(life_db):
        c = sqlite3.connect(f"file:{life_db}?mode=ro", uri=True, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            pm = {r["agent_id"]: dict(r) for r in c.execute("SELECT * FROM postmortems")}
            for r in c.execute("SELECT * FROM agents ORDER BY status='fired', generation DESC, hired_at DESC"):
                g = json.loads(r["genome_json"])
                p = pm.get(r["agent_id"])
                out["agents"].append({
                    "id": r["agent_id"], "name": r["name"], "kind": r["kind"], "status": r["status"], "generation": r["generation"],
                    "parent": r["parent_id"], "origin": r["origin"], "weight": r["weight"],
                    "rule": em.rule_name(g["filters"]), "exit": g.get("exit"), "hired_at": r["hired_at"],
                    "fired_at": r["fired_at"], "fire_reason": r["fire_reason"],
                    "stats": json.loads(r["stats_json"] or "{}"),
                    "postmortem": {"summary": p["summary"], "fix": json.loads(p["fix_json"])} if p else None})
            out["events"] = [dict(r) for r in c.execute("SELECT at, agent_id, event, detail FROM lifecycle_events ORDER BY id DESC LIMIT 30")]
            rep = c.execute("SELECT report_json FROM edge_reports ORDER BY id DESC LIMIT 1").fetchone()
            out["edge_mining"] = json.loads(rep[0]) if rep else None
        finally:
            c.close()
    if db and os.path.isfile(db):
        out["forecasters"] = score_forecasters(db)
    out["counts"] = {s: sum(a["status"] == s for a in out["agents"]) for s in ("shadow", "earning", "fired")}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.getenv("NOEMA_DB_PATH", "data/noema.db"))
    ap.add_argument("--life-db", default=os.getenv("NOEMA_LIFECYCLE_DB", "data/agent_lifecycle.db"))
    ap.add_argument("--feed-db", default="data/opportunity_feed.db")
    ap.add_argument("--meta-db", default=os.getenv("NOEMA_META_DB", "data/meta_launch.db"))
    ap.add_argument("--avenues-db", default=os.getenv("NOEMA_AVENUES_DB", "data/edge_avenues.db"))
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=int(os.getenv("NOEMA_LIFECYCLE_INTERVAL_SECONDS", "900")))
    a = ap.parse_args()
    while True:
        try:
            print(_now(), json.dumps(cycle(a.db, a.life_db, a.feed_db, meta_db=a.meta_db, avenues_db=a.avenues_db)), flush=True)
        except Exception as e:  # noqa: BLE001 - keep the loop alive; the next cycle retries
            print(_now(), "cycle error:", repr(e), flush=True)
        if not a.loop:
            break
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
