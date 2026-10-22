"""Weaker prediction-market categories, PAPER ONLY (no orders, no flags, no keys).

Walk-forward, no look-ahead: a row's model is fitted only on rows whose market CLOSED before the row's
snapshot time; every feature is observable at the snapshot. CIs resample whole events.
Trades pass the fee-plus-buffer gate (edge_strategies.clears_fee_buffer) with the Kalshi taker fee.

1. weather  - Kalshi daily high/low at the NWS station coordinates Kalshi settles on (pm_history.CITIES).
   Open-Meteo Previous-Runs hourly temperature: `previous_day2` for a snapshot at 20:00 LST the evening
   before (every value issued >= 48h before its hour, so before the snapshot) and `previous_day1` for a
   snapshot at 08:00 LST on the day (issued >= 24h before its hour). Market quote = Kalshi hourly
   candlestick closed at/before the snapshot. Error SD learned walk-forward per site.
2. thin     - favourite-longshot / calibration correction (logistic on logit(mid)) fitted on past
   low-volume rows only (volume below the past-only 33rd percentile), all pm_train categories.
3. politics - long-dated Kalshi Politics markets (open >= 45 days): daily candle at close-30d and
   close-90d; calibration (FLB) and 14-day drift mean-reversion. P&L also annualised for lock-up.
Economic releases: see report (consensus/nowcast data not wired - too few settled CPI/NFP markets).
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import sqlite3
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .edge_strategies import clears_fee_buffer
from .fast_settle import KALSHI, Api, _f
from .pm_history import CITIES, weather_site
from .pm_models import (
    brier,
    clip,
    cluster_ci,
    fit_logistic,
    kalshi_fee,
    logit,
    logloss,
    phi,
    sigmoid,
)

STD_OFFSET_H = {"NY": -5, "NYC": -5, "PHIL": -5, "BOS": -5, "DC": -5, "MIA": -5, "CHI": -6, "AUS": -6, "HOU": -6,
                "DEN": -7, "PHX": -7, "LAX": -8}
WX_LEADS = {"eve": ("temperature_2m_previous_day2", -4), "morn": ("temperature_2m_previous_day1", 8)}
SCHEMA = """
CREATE TABLE IF NOT EXISTS pw_wx_hourly (site TEXT, var TEXT, ts INTEGER, temp REAL, PRIMARY KEY (site, var, ts));
CREATE TABLE IF NOT EXISTS pw_snap (ticker TEXT, lead TEXT, snap_ts INTEGER, bid REAL, ask REAL, mid REAL,
  mid_prev REAL, PRIMARY KEY (ticker, lead));
CREATE TABLE IF NOT EXISTS pw_pol (ticker TEXT PRIMARY KEY, series TEXT, event TEXT, open_time TEXT, close_time TEXT,
  result INTEGER, volume REAL);
CREATE TABLE IF NOT EXISTS pw_pol_src (ticker TEXT PRIMARY KEY, series TEXT, event TEXT, open_time TEXT,
  close_time TEXT, result TEXT, volume REAL);
CREATE TABLE IF NOT EXISTS pw_pol_done (series TEXT PRIMARY KEY, at TEXT);
CREATE TABLE IF NOT EXISTS pw_hist_tried (ticker TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS pw_reports (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, report_json TEXT);
"""
MONTHS = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def ensure(c: sqlite3.Connection) -> None:
    c.executescript(SCHEMA)


def _ts(s: str) -> int:
    return int(datetime.fromisoformat(s).timestamp())


def event_day(event_ticker: str) -> datetime | None:
    try:
        d = event_ticker.split("-")[1]
        return datetime(2000 + int(d[:2]), MONTHS[d[2:5]], int(d[5:7]), tzinfo=UTC)
    except (IndexError, KeyError, ValueError):
        return None


# ----------------------------------------------------------------------------- evaluation
def evaluate(rows: list[dict[str, Any]], key: str = "p") -> dict[str, Any]:
    """rows: event, y, bid, ask, mid, p (+ days). Brier/logloss vs mid, gated taker trades, annualised P&L."""
    rows = [r for r in rows if r.get(key) is not None]
    if not rows:
        return {"n": 0}
    db = [(r["event"], brier(r[key], r["y"]) - brier(r["mid"], r["y"])) for r in rows]
    dl = [(r["event"], logloss(r[key], r["y"]) - logloss(r["mid"], r["y"])) for r in rows]
    mb, blo, bhi, _ = cluster_ci(db)
    ml, llo, lhi, _ = cluster_ci(dl)
    tr, ann = [], []
    for r in rows:
        p, a, b = r[key], r["ask"], r["bid"]
        if a is not None and 0 < a < 1 and clears_fee_buffer(p - a - kalshi_fee(a)):
            cost, pnl = a + kalshi_fee(a), r["y"] - a - kalshi_fee(a)
        elif b is not None and 0 < b < 1 and clears_fee_buffer((1 - p) - (1 - b) - kalshi_fee(1 - b)):
            cost, pnl = (1 - b) + kalshi_fee(1 - b), (1 - r["y"]) - (1 - b) - kalshi_fee(1 - b)
        else:
            continue
        tr.append((r["event"], pnl))
        if r.get("days"):
            ann.append((r["event"], pnl / cost * 365 / max(r["days"], 1)))
    tm, tlo, thi, _ = cluster_ci(tr) if tr else (0.0, 0.0, 0.0, 1.0)
    out = {"n": len(rows), "events": len({r["event"] for r in rows}),
           "brier": round(sum(brier(r[key], r["y"]) for r in rows) / len(rows), 4),
           "market_brier": round(sum(brier(r["mid"], r["y"]) for r in rows) / len(rows), 4),
           "logloss": round(sum(logloss(r[key], r["y"]) for r in rows) / len(rows), 4),
           "market_logloss": round(sum(logloss(r["mid"], r["y"]) for r in rows) / len(rows), 4),
           "brier_diff": round(mb, 5), "brier_diff_ci95": [round(blo, 5), round(bhi, 5)],
           "logloss_diff": round(ml, 5), "logloss_diff_ci95": [round(llo, 5), round(lhi, 5)],
           "paper_trades": len(tr), "pnl_per_contract": round(tm, 4), "pnl_ci95": [round(tlo, 4), round(thi, 4)]}
    if ann:
        am, alo, ahi, _ = cluster_ci(ann)
        out["annualised_return"] = round(am, 4)
        out["annualised_ci95"] = [round(alo, 4), round(ahi, 4)]
    return out


def walk_forward(rows: list[dict[str, Any]], feats: Any, *, min_train: int = 200, refit: int = 100,
                 train_filter: Any = None) -> None:
    """Sets r['p_wf'] using a logistic fitted on rows with close_ts < r['snap_ts'] (strict)."""
    rows.sort(key=lambda r: r["snap_ts"])
    by_close = sorted(rows, key=lambda r: r["close_ts"])
    closes = [r["close_ts"] for r in by_close]
    w, fitted_at = None, -1
    for r in rows:
        k = bisect.bisect_left(closes, r["snap_ts"])
        if w is None or k - fitted_at >= refit:
            tr = [x for x in by_close[:k] if (train_filter is None or train_filter(x, r)) and feats(x) is not None]
            if len(tr) >= min_train:
                w, fitted_at = fit_logistic([feats(x) for x in tr], [x["y"] for x in tr], 1.0), k
        x = feats(r)
        r["p_wf"] = clip(sigmoid(sum(a * b for a, b in zip(w, x, strict=False)))) if w and x else None


# ----------------------------------------------------------------------------- 1. weather
def fetch_wx(c: sqlite3.Connection, start: str, end: str) -> int:
    n = 0
    with httpx.Client(timeout=60) as cl:
        for site, (lat, lon) in CITIES.items():
            if site == "NYC":
                continue
            try:
                j = cl.get("https://previous-runs-api.open-meteo.com/v1/forecast", params={
                    "latitude": lat, "longitude": lon, "temperature_unit": "fahrenheit", "timezone": "GMT",
                    "hourly": ",".join(v for v, _ in WX_LEADS.values()), "start_date": start, "end_date": end}).json()
            except (httpx.HTTPError, ValueError):
                continue
            h = j.get("hourly") or {}
            for var, _ in WX_LEADS.values():
                rows = [(site, var, _ts(t + ":00+00:00"), v) for t, v in zip(h.get("time", []), h.get(var, []), strict=False)
                        if v is not None]
                c.executemany("INSERT OR REPLACE INTO pw_wx_hourly VALUES (?,?,?,?)", rows)
                n += len(rows)
    c.commit()
    return n


def wx_markets(c: sqlite3.Connection) -> list[dict[str, Any]]:
    out = []
    for t, s, ev, close, st, lo, hi, y, vol in c.execute(
            "SELECT ticker, series, event_ticker, close_time, strike_type, floor_strike, cap_strike, result, volume"
            " FROM pm_train WHERE category='weather'"):
        ws = weather_site(s)
        day = event_day(ev or "")
        if not ws or not day:
            continue
        kind, code, _ = ws
        site = "NY" if code == "NYC" else code
        d0 = int(day.timestamp()) - STD_OFFSET_H[site] * 3600  # 00:00 LST of the climate day, in UTC
        for lead, (var, h) in WX_LEADS.items():
            out.append({"ticker": t, "event": ev, "site": site, "kind": kind, "var": var, "lead": lead, "day0": d0,
                        "snap_ts": d0 + h * 3600, "close_ts": _ts(close), "strike_type": st, "lo": lo, "hi": hi, "y": y,
                        "volume": vol})
    return out


def backfill_snaps(c: sqlite3.Connection, api: Api, items: list[tuple[str, str, int]], period: int,
                   window_s: int, prev_s: int) -> int:
    """items: (ticker, lead, snap_ts). Quote = last candle with end <= snap (bid<ask), within window."""
    have = {(t, ld) for t, ld in c.execute("SELECT ticker, lead FROM pw_snap")}
    todo = [x for x in items if (x[0], x[1]) not in have]
    groups: dict[tuple[str, int], list[str]] = defaultdict(list)
    for t, ld, s in todo:
        groups[(ld, s)].append(t)
    n = 0
    for (ld, snap), ticks in groups.items():
        for i in range(0, len(ticks), 100):
            chunk = ticks[i:i + 100]
            try:
                d = api.get(f"{KALSHI}/markets/candlesticks", {"market_tickers": ",".join(chunk),
                            "start_ts": snap - window_s - prev_s, "end_ts": snap, "period_interval": period})
            except (httpx.HTTPError, RuntimeError):
                continue
            for x in d.get("markets") or []:
                cs = []
                for k in x.get("candlesticks") or []:
                    b, a = _f((k.get("yes_bid") or {}).get("close_dollars")), _f((k.get("yes_ask") or {}).get("close_dollars"))
                    if b is not None and a is not None and a > b and int(k["end_period_ts"]) <= snap:
                        cs.append((int(k["end_period_ts"]), b, a))
                cs.sort()
                if not cs or snap - cs[-1][0] > window_s:
                    continue
                _, b, a = cs[-1]
                ends = [e for e, *_ in cs]
                j = bisect.bisect_right(ends, snap - prev_s) - 1
                prev = (cs[j][1] + cs[j][2]) / 2 if j >= 0 else None
                c.execute("INSERT OR REPLACE INTO pw_snap VALUES (?,?,?,?,?,?,?)",
                          (x["market_ticker"], ld, snap, b, a, round((a + b) / 2, 4), prev))
                n += 1
        c.commit()
    return n


def _band_prob(fc: float, sd: float, st: str, lo: float | None, hi: float | None) -> float | None:
    def ge(x: float) -> float:
        return 1 - phi((x - fc) / sd)
    if st in ("greater", "greater_or_equal") and lo is not None:
        return clip(ge(lo + 0.5))
    if st in ("less", "less_or_equal") and hi is not None:
        return clip(1 - ge(hi - 0.5))
    if st == "between" and lo is not None and hi is not None:
        return clip(ge(lo - 0.5) - ge(hi + 0.5))
    return None


def weather_rows(c: sqlite3.Connection) -> list[dict[str, Any]]:
    wx = defaultdict(dict)
    for site, var, ts, temp in c.execute("SELECT site, var, ts, temp FROM pw_wx_hourly"):
        wx[(site, var)][ts] = temp
    snaps = {(t, ld): (b, a, m) for t, ld, b, a, m in c.execute("SELECT ticker, lead, bid, ask, mid FROM pw_snap")}
    actual = dict(c.execute("SELECT event_ticker, MAX(expiration_value) FROM pm_train WHERE category='weather'"
                            " GROUP BY 1").fetchall())
    rows = []
    for r in wx_markets(c):
        q = snaps.get((r["ticker"], r["lead"]))
        hrs = [wx[(r["site"], r["var"])].get(r["day0"] + h * 3600) for h in range(24)]
        if not q or None in hrs:
            continue
        r["fc"] = max(hrs) if r["kind"] == "high" else min(hrs)
        r["actual"] = actual.get(r["event"])
        r["bid"], r["ask"], r["mid"] = q
        rows.append(r)
    # walk-forward error SD per site/kind/lead from events closed before the snapshot
    rows.sort(key=lambda r: r["snap_ts"])
    for r in rows:
        past = [x for x in rows if x["close_ts"] < r["snap_ts"] and x["site"] == r["site"] and x["kind"] == r["kind"]
                and x["lead"] == r["lead"] and x["actual"]]
        errs = list({x["event"]: x["fc"] - x["actual"] for x in past}.values())
        if len(errs) >= 8:
            bias = sum(errs) / len(errs)
            sd = max(1.0, math.sqrt(sum((e - bias) ** 2 for e in errs) / (len(errs) - 1)))
            r["p_raw"] = _band_prob(r["fc"] - bias, sd, r["strike_type"] or "", r["lo"], r["hi"])
        else:
            r["p_raw"] = None
    return rows


def weather_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out = {}
    for lead in WX_LEADS:
        rs = [r for r in rows if r["lead"] == lead]
        walk_forward(rs, lambda r: [1.0, logit(r["mid"]), logit(r["p_raw"])] if r.get("p_raw") is not None else None)
        out[lead] = {"forecast_bias_sd_model": evaluate(rs, "p_raw"), "stack_market_plus_forecast": evaluate(rs, "p_wf")}
    return out


# ----------------------------------------------------------------------------- 2. thin markets
def thin_report(c: sqlite3.Connection) -> dict[str, Any]:
    rows = []
    for t, cat, ev, close, snap, y, vol, b, a, m in c.execute(
            "SELECT ticker, category, event_ticker, close_time, snap_at, result, volume, snap_bid, snap_ask, snap_mid"
            " FROM pm_train WHERE snap_mid IS NOT NULL AND snap_at IS NOT NULL"):
        if m is None or not 0 < m < 1 or vol is None:
            continue
        rows.append({"ticker": t, "cat": cat, "event": ev or t, "close_ts": _ts(close), "snap_ts": _ts(snap), "y": y,
                     "volume": vol, "bid": b, "ask": a, "mid": m})
    rows.sort(key=lambda r: r["snap_ts"])
    # past-only volume tercile cut
    closed_vols: list[float] = []
    by_close = sorted(rows, key=lambda r: r["close_ts"])
    j = 0
    for r in rows:
        while j < len(by_close) and by_close[j]["close_ts"] < r["snap_ts"]:
            bisect.insort(closed_vols, by_close[j]["volume"])
            j += 1
        r["thin"] = len(closed_vols) >= 500 and r["volume"] <= closed_vols[len(closed_vols) // 3]
    out: dict[str, Any] = {}
    # A) observable thinness: quoted spread >= 3c at the snapshot (no look-ahead). Model fitted on wide-spread rows.
    wide = [dict(r) for r in rows if r["bid"] is not None and r["ask"] is not None and r["ask"] - r["bid"] >= 0.03]
    walk_forward(wide, lambda r: [1.0, logit(r["mid"])], min_train=300, refit=500)
    out["wide_spread_ge_3c"] = {"all": evaluate(wide, "p_wf"),
                                **{cat: evaluate([r for r in wide if r["cat"] == cat], "p_wf")
                                   for cat in sorted({r["cat"] for r in wide})}}
    # B) lifetime-volume tercile: LOOK-AHEAD in the bucket (volume is only known at settle) - diagnostic only
    thin = [dict(r) for r in rows if r["thin"]]
    walk_forward(thin, lambda r: [1.0, logit(r["mid"])], min_train=300, refit=500)
    out["lifetime_volume_low_tercile_LOOKAHEAD"] = evaluate(thin, "p_wf")
    out["note"] = ("B is biased: markets whose price never moved end with low volume, so selecting on lifetime "
                   "volume leaks the outcome path. Only A is a fair test; only A feeds the paper agent.")
    out["_wide_rows"] = wide
    return out


# ----------------------------------------------------------------------------- 3. politics
POL_LEADS = {"30d": 30 * 86400, "90d": 90 * 86400}


def politics_rows(c: sqlite3.Connection) -> list[dict[str, Any]]:
    snaps = {(t, ld): (b, a, m, p) for t, ld, b, a, m, p in c.execute("SELECT ticker, lead, bid, ask, mid, mid_prev FROM pw_snap")}
    rows = []
    for t, s, ev, op, close, y, vol in c.execute("SELECT * FROM pw_pol"):
        for ld, sec in POL_LEADS.items():
            q = snaps.get((t, "pol" + ld))
            if not q:
                continue
            b, a, m, prev = q
            rows.append({"ticker": t, "event": ev or t, "series": s, "lead": ld, "close_ts": _ts(close),
                         "snap_ts": _ts(close) - sec, "y": y, "bid": b, "ask": a, "mid": m, "drift": None if prev is None else m - prev,
                         "days": sec / 86400, "volume": vol})
    return rows


def politics_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out = {}
    for ld in POL_LEADS:
        rs = [r for r in rows if r["lead"] == ld and 0.01 < r["mid"] < 0.99]
        walk_forward(rs, lambda r: [1.0, logit(r["mid"])], min_train=150)
        for r in rs:
            r["p_flb"] = r.pop("p_wf")
        walk_forward(rs, lambda r: [1.0, logit(r["mid"]), r["drift"]] if r["drift"] is not None else None, min_train=150)
        out[ld] = {"flb_calibration": evaluate(rs, "p_flb"), "drift_mean_reversion": evaluate(rs, "p_wf")}
    return out


def fetch_politics_list(c: sqlite3.Connection, api: Api, src_db: str | None = None, max_series: int = 3000) -> int:
    """Settled binary markets of every Kalshi Politics series (live + historical endpoints), incremental."""
    if src_db and os.path.isfile(src_db):  # one-off import of a previously fetched list (same columns)
        s = sqlite3.connect(src_db)
        c.executemany("INSERT OR IGNORE INTO pw_pol_src VALUES (?,?,?,?,?,?,?)",
                      s.execute("SELECT ticker, series, event, open_time, close_time, result, volume FROM mk").fetchall())
        c.executemany("INSERT OR IGNORE INTO pw_pol_done VALUES (?, '')", s.execute("SELECT series FROM done").fetchall())
        s.close()
    ser = [x["ticker"] for x in api.get(f"{KALSHI}/series", {"category": "Politics"}).get("series") or []]
    stale = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    done = {r[0] for r in c.execute("SELECT series FROM pw_pol_done WHERE at='' OR at>=?", (stale,))}
    n = 0
    for sr in [x for x in ser if x not in done][:max_series]:
        for url, extra in ((f"{KALSHI}/markets", {"status": "settled"}), (f"{KALSHI}/historical/markets", {})):
            cur = None
            for _ in range(20):
                try:
                    d = api.get(url, {"series_ticker": sr, "limit": 1000, **extra, **({"cursor": cur} if cur else {})})
                except (httpx.HTTPError, RuntimeError):
                    break
                for m in d.get("markets") or []:
                    if m.get("result") in ("yes", "no") and m.get("market_type") == "binary":
                        n += c.execute("INSERT OR REPLACE INTO pw_pol_src VALUES (?,?,?,?,?,?,?)", (
                            m["ticker"], sr, m.get("event_ticker"), m.get("open_time"), m.get("close_time"), m["result"],
                            float(m.get("volume_fp") or m.get("volume") or 0))).rowcount
                cur = d.get("cursor")
                if not cur or not d.get("markets"):
                    break
        c.execute("INSERT OR REPLACE INTO pw_pol_done VALUES (?,?)", (sr, datetime.now(UTC).isoformat()))
        c.commit()
    return n


def fetch_politics(c: sqlite3.Connection, api: Api, min_days: int = 45) -> int:
    rows = [r for r in c.execute("SELECT ticker, series, event, open_time, close_time, result, volume FROM pw_pol_src")
            if r[3] and r[4] and _ts(r[4]) - _ts(r[3]) >= min_days * 86400]
    c.executemany("INSERT OR REPLACE INTO pw_pol VALUES (?,?,?,?,?,?,?)",
                  [(t, se, ev, op, cl, 1 if res == "yes" else 0, v) for t, se, ev, op, cl, res, v in rows])
    c.commit()
    items = []
    for t, _se, _ev, op, cl, _r, _v in rows:
        for ld, sec in POL_LEADS.items():
            snap = _ts(cl) - sec
            if snap - _ts(op) >= 14 * 86400:
                snap = snap // 86400 * 86400  # daily candle boundary
                items.append((t, "pol" + ld, snap))
    n = backfill_snaps(c, api, items, period=1440, window_s=3 * 86400, prev_s=14 * 86400)
    return n + backfill_hist_pol(c, api, items)


def backfill_hist_pol(c: sqlite3.Connection, api: Api, items: list[tuple[str, str, int]], cap: int = 4000) -> int:
    """Archived markets: per-ticker /historical candlesticks (fields 'close', not 'close_dollars')."""
    have = {(t, ld) for t, ld in c.execute("SELECT ticker, lead FROM pw_snap")}
    tried = {r[0] for r in c.execute("SELECT ticker FROM pw_hist_tried")}
    by: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for t, ld, snap in items:
        if (t, ld) not in have and t not in tried:
            by[t].append((ld, snap))
    n = 0
    for t, legs in list(by.items())[:cap]:
        lo, hi = min(s for _, s in legs) - 17 * 86400, max(s for _, s in legs)
        try:
            d = api.get(f"{KALSHI}/historical/markets/{t}/candlesticks", {"start_ts": lo, "end_ts": hi, "period_interval": 1440})
        except (httpx.HTTPError, RuntimeError):
            d = {}
        cs = []
        for k in d.get("candlesticks") or []:
            b = _f((k.get("yes_bid") or {}).get("close"))
            a = _f((k.get("yes_ask") or {}).get("close"))
            if b is not None and a is not None and a > b:
                cs.append((int(k["end_period_ts"]), b, a))
        cs.sort()
        ends = [e for e, *_ in cs]
        for ld, snap in legs:
            j = bisect.bisect_right(ends, snap) - 1
            if j < 0 or snap - cs[j][0] > 3 * 86400:
                continue
            _, b, a = cs[j]
            k0 = bisect.bisect_right(ends, snap - 14 * 86400) - 1
            prev = (cs[k0][1] + cs[k0][2]) / 2 if k0 >= 0 else None
            c.execute("INSERT OR REPLACE INTO pw_snap VALUES (?,?,?,?,?,?,?)", (t, ld, snap, b, a, round((a + b) / 2, 4), prev))
            n += 1
        c.execute("INSERT OR REPLACE INTO pw_hist_tried VALUES (?)", (t,))
        c.commit()
    return n


# ----------------------------------------------------------------------------- driver + roster
def record_outcomes(av_db: str, kind: str, rows: list[dict[str, Any]], key: str) -> int:
    """Gated paper trades -> edge_avenues.paper_outcomes, timestamped at close (roster counts post-hire only)."""
    from .edge_avenues import connect
    c = connect(av_db)
    n = 0
    for r in rows:
        p = r.get(key)
        if p is None:
            continue
        a, b = r["ask"], r["bid"]
        if a and clears_fee_buffer(p - a - kalshi_fee(a)):
            pnl = r["y"] - a - kalshi_fee(a)
        elif b and clears_fee_buffer(b - p - kalshi_fee(1 - b)):
            pnl = (1 - r["y"]) - (1 - b) - kalshi_fee(1 - b)
        else:
            continue
        n += c.execute("INSERT OR IGNORE INTO paper_outcomes VALUES (?,?,?,?,?)",
                       (kind, f"{r['ticker']}|{r.get('lead')}", datetime.fromtimestamp(r["close_ts"], UTC).isoformat(),
                        pnl, json.dumps({"lead": r.get("lead")}))).rowcount
    c.commit()
    c.close()
    return n


def run(db: str, av_db: str, pol_src: str | None, fetch: bool = True) -> dict[str, Any]:
    c = sqlite3.connect(db, timeout=60)
    ensure(c)
    api = Api()
    rep: dict[str, Any] = {"schema": "tev.pm_weak.v1", "at": datetime.now(UTC).isoformat(), "mode": "paper only"}
    if fetch:
        lo = c.execute("SELECT MIN(close_time) FROM pm_train WHERE category='weather'").fetchone()[0]
        start = (datetime.fromisoformat(lo) - timedelta(days=2)).date().isoformat()
        rep["wx_hours"] = fetch_wx(c, start, datetime.now(UTC).date().isoformat())
        items = [(r["ticker"], r["lead"], r["snap_ts"]) for r in wx_markets(c)]
        rep["wx_snaps"] = backfill_snaps(c, api, items, period=60, window_s=3 * 3600, prev_s=6 * 3600)
        rep["pol_markets"] = fetch_politics_list(c, api, pol_src)
        rep["pol_snaps"] = fetch_politics(c, api)
    wrows = weather_rows(c)
    rep["weather"] = weather_report(wrows)
    rep["thin"] = thin_report(c)
    trows = rep["thin"].pop("_wide_rows")
    prows = politics_rows(c)
    rep["politics"] = politics_report(prows)
    ec = dict(c.execute("SELECT series, COUNT(*) FROM pm_train WHERE category='economics' GROUP BY 1").fetchall())
    rep["economics"] = {"settled_markets_by_series": ec, "status": "not modelled",
                        "why": "only 16 CPI + 8 payrolls settled markets locally; no free archived consensus with "
                               "issue times; gas/diesel/claims market_cal already ties market (pm_models)"}
    rep["recorded"] = {"pm_weather": record_outcomes(av_db, "pm_weather", [r for r in wrows if r["lead"] == "morn"], "p_wf"),
                       "pm_thin": record_outcomes(av_db, "pm_thin", trows, "p_wf"),
                       "pm_politics": record_outcomes(av_db, "pm_politics", [r for r in prows if r["lead"] == "30d"], "p_wf")}
    c.execute("INSERT INTO pw_reports (at, report_json) VALUES (?,?)", (rep["at"], json.dumps(rep)))
    c.commit()
    c.close()
    return rep


def main() -> None:  # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/pm_train.db")
    ap.add_argument("--avenues-db", default="data/edge_avenues.db")
    ap.add_argument("--pol-src", default=None, help="optional one-off import: sqlite with tables mk, done")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=86400)
    a = ap.parse_args()
    while True:
        try:
            rep = run(a.db, a.avenues_db, a.pol_src, fetch=not a.no_fetch)
            print(json.dumps({k: rep[k] for k in ("at", "weather", "thin", "politics")})[:4000], flush=True)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"error": type(exc).__name__, "detail": str(exc)[:300]}), flush=True)
        if not a.loop:
            return
        time.sleep(a.interval)


if __name__ == "__main__":  # pragma: no cover
    main()
