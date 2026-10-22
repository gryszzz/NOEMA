"""1-minute crypto index mirror, near-close training set and walk-forward models (paper only).

Kalshi's crypto series (KXBTC*, KXETH*, KXSOL*, KXXRP*, KXDOGE*) all settle on the
simple average of the 60 seconds of a CF Benchmarks Real-Time Index (BRTI, ERTI,
SOLUSD_RTI, XRPUSD_RTI, DOGEUSD_RTI) before the close time; 15-minute markets
compare that average to the same average 15 minutes earlier. CF's RTIs aggregate
order books of constituent exchanges (Coinbase, Bitstamp, Kraken, Gemini, ...).
We mirror them with the cross-venue median of public 1-minute candles from
Coinbase and Bitstamp (the two with deep public 1m history); settlement is mirrored
by the mean typical price (o+h+l+c)/4 of the final minute.

No look-ahead: every bar is keyed by its CLOSE time (start + 60 s) and a value at
time t only uses bars with close_ts <= t. Kalshi candlesticks are keyed by
end_period_ts with the same rule. 15-minute markets are only sampled at leads
shorter than their window, because their strike is set at window open.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import sqlite3
import statistics
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import httpx

from .fast_settle import KALSHI, Api, _f
from .pm_models import bh, clip, evaluate, fit_logistic, logit, phi, sigmoid
from .sqlite_util import connect as sqlite_connect

ASSETS = {"KXBTC": ("BTC-USD", "btcusd"), "KXETH": ("ETH-USD", "ethusd"), "KXSOL": ("SOL-USD", "solusd"),
          "KXXRP": ("XRP-USD", "xrpusd"), "KXDOGE": ("DOGE-USD", "dogeusd")}
LEADS = (300, 900, 1800, 3600)  # seconds before close
VENUES = ("coinbase", "bitstamp")

SCHEMA = """
CREATE TABLE IF NOT EXISTS pm_ref_1m (asset TEXT NOT NULL, venue TEXT NOT NULL, close_ts INTEGER NOT NULL,
    o REAL, h REAL, l REAL, c REAL, PRIMARY KEY (asset, venue, close_ts));
CREATE TABLE IF NOT EXISTS pm_near (ticker TEXT NOT NULL, lead_s INTEGER NOT NULL, snap_ts INTEGER NOT NULL,
    bid REAL, ask REAL, mid REAL, quote_age_s INTEGER, vol10 REAL, dmid10 REAL, PRIMARY KEY (ticker, lead_s));
CREATE TABLE IF NOT EXISTS pm_crypto_reports (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
    report_json TEXT NOT NULL);
"""


def asset_of(series: str) -> str | None:
    return next((k for k in ASSETS if series.startswith(k)), None)


def ensure_schema(c: sqlite3.Connection) -> None:
    c.executescript(SCHEMA)


# ---------------- 1-minute reference backfill ----------------

def _coinbase(client: httpx.Client, product: str, start: int, end: int) -> list[tuple[int, float, float, float, float]]:
    r = client.get(f"https://api.exchange.coinbase.com/products/{product}/candles",
                   params={"granularity": 60, "start": start, "end": end})
    r.raise_for_status()
    # Coinbase rows: [start, low, high, open, close, volume]; key by close time = start + 60.
    return [(int(x[0]) + 60, float(x[3]), float(x[2]), float(x[1]), float(x[4])) for x in r.json()]


def _bitstamp(client: httpx.Client, pair: str, start: int) -> list[tuple[int, float, float, float, float]]:
    r = client.get(f"https://www.bitstamp.net/api/v2/ohlc/{pair}/", params={"step": 60, "limit": 1000, "start": start})
    r.raise_for_status()
    return [(int(x["timestamp"]) + 60, float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"]))
            for x in r.json()["data"]["ohlc"]]


def backfill_1m(c: sqlite3.Connection, days: int, client: httpx.Client | None = None, now: int | None = None) -> dict[str, int]:
    ensure_schema(c)
    client = client or httpx.Client(timeout=30)
    now = now or int(time.time())
    out: dict[str, int] = {}
    for asset, (cb, bs) in ASSETS.items():
        for venue in VENUES:
            have = c.execute("SELECT MAX(close_ts) FROM pm_ref_1m WHERE asset=? AND venue=?", (asset, venue)).fetchone()[0]
            t = max(have or 0, now - days * 86400)
            n = 0
            while t < now - 60:
                try:
                    if venue == "coinbase":
                        rows = _coinbase(client, cb, t, min(t + 300 * 60, now))
                        nxt = t + 300 * 60
                    else:
                        rows = _bitstamp(client, bs, t)
                        nxt = max([r[0] for r in rows], default=t + 1000 * 60)
                        nxt = nxt if nxt > t else t + 1000 * 60
                except (httpx.HTTPError, KeyError, ValueError):
                    time.sleep(2)
                    t += 300 * 60
                    continue
                # never store a bar that has not closed yet
                rows = [r for r in rows if r[0] <= now]
                c.executemany("INSERT OR REPLACE INTO pm_ref_1m VALUES (?,?,?,?,?,?,?)",
                              [(asset, venue, *r) for r in rows])
                n += len(rows)
                t = nxt
                time.sleep(0.35)
            c.commit()
            out[f"{asset}/{venue}"] = n
    return out


class Index1m:
    """Cross-venue median of 1m closes, keyed by bar close time (strict no look-ahead)."""

    def __init__(self, c: sqlite3.Connection, asset: str) -> None:
        self.v: dict[str, tuple[list[int], list[float], list[float]]] = {}
        for venue in VENUES:
            rows = c.execute("SELECT close_ts, c, (o+h+l+c)/4 FROM pm_ref_1m WHERE asset=? AND venue=? ORDER BY close_ts",
                             (asset, venue)).fetchall()
            if rows:
                self.v[venue] = ([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows])
        self._grid: tuple[list[int], list[float]] | None = None

    def at(self, t: int, max_age: int = 180) -> float | None:
        vals = []
        for ts, cl, _ in self.v.values():
            i = bisect.bisect_right(ts, t) - 1
            if i >= 0 and t - ts[i] <= max_age:
                vals.append(cl[i])
        return statistics.median(vals) if vals else None

    def settle_mirror(self, close_t: int) -> float | None:
        """Mean typical price of the minute ending at close_t (≈ CF 60-second average)."""
        vals = []
        for ts, _, tp in self.v.values():
            i = bisect.bisect_left(ts, close_t)
            if i < len(ts) and ts[i] == close_t:
                vals.append(tp[i])
        return sum(vals) / len(vals) if vals else None

    def grid(self) -> tuple[list[int], list[float]]:
        if self._grid is None:
            base = self.v.get("coinbase") or next(iter(self.v.values()))
            self._grid = (base[0], base[1])
        return self._grid

    def vol(self, t: int) -> tuple[float, list[float]] | None:
        """Per-minute vol over the prior 2h and an hour-of-day profile from the prior 14 days (bars closed by t)."""
        ts, px = self.grid()
        i = bisect.bisect_right(ts, t)
        j = bisect.bisect_left(ts, t - 7200)
        k = bisect.bisect_left(ts, t - 14 * 86400)
        rets = [math.log(px[n] / px[n - 1]) for n in range(max(j, 1), i) if ts[n] - ts[n - 1] == 60]
        if len(rets) < 60:
            return None
        base = math.sqrt(sum(r * r for r in rets) / len(rets))
        acc: dict[int, list[float]] = defaultdict(list)
        for n in range(max(k, 1), i):
            if ts[n] - ts[n - 1] == 60:
                acc[datetime.fromtimestamp(ts[n], UTC).hour].append(math.log(px[n] / px[n - 1]) ** 2)
        allv = [x for v in acc.values() for x in v]
        m = sum(allv) / len(allv) if allv else base * base
        prof = [math.sqrt(sum(acc[h]) / len(acc[h]) / m) if len(acc[h]) >= 60 and m > 0 else 1.0 for h in range(24)]
        cur = prof[datetime.fromtimestamp(t, UTC).hour] or 1.0
        return base / cur, prof  # de-seasonalised base level


def model_prob(ix: Index1m, snap: int, close: int, strike_type: str, lo: float | None, hi: float | None) -> tuple[float, float] | None:
    """P(YES) and distance-to-strike in vol units, using only bars closed by `snap`."""
    s = ix.at(snap)
    v = ix.vol(snap)
    if s is None or v is None or close <= snap:
        return None
    base, prof = v
    var, t = 0.0, snap
    while t < close:
        step = min(60, close - t)
        var += (base * prof[datetime.fromtimestamp(t, UTC).hour]) ** 2 * step / 60
        t += step
    if close - snap >= 60:  # the 60 s settlement average keeps only 1/3 of the final minute's variance
        var -= (base * prof[datetime.fromtimestamp(close - 60, UTC).hour]) ** 2 * 2 / 3
    sd = max(math.sqrt(max(var, 1e-12)), 1e-6)

    def above(k: float) -> float:
        return phi(math.log(s / k) / sd)
    if strike_type in ("greater", "greater_or_equal") and lo:
        return clip(above(lo)), math.log(s / lo) / sd
    if strike_type in ("less", "less_or_equal") and hi:
        return clip(1 - above(hi)), math.log(hi / s) / sd
    if strike_type == "between" and lo and hi:
        mid = math.sqrt(lo * hi)
        return clip(above(lo) - above(hi)), -abs(math.log(s / mid)) / sd
    return None


# ---------------- near-close Kalshi quotes ----------------

def backfill_near(c: sqlite3.Connection, api: Api, *, max_groups: int | None = None) -> int:
    ensure_schema(c)
    rows = c.execute(
        "SELECT ticker, series, close_time FROM pm_train WHERE category='crypto' AND ticker NOT IN"
        " (SELECT ticker FROM pm_near) ORDER BY close_time DESC").fetchall()
    groups: dict[int, list[str]] = defaultdict(list)
    for t, series, close in rows:
        if asset_of(series):
            groups[int(datetime.fromisoformat(close).timestamp())].append(t)
    added = 0
    for gi, (close, tickers) in enumerate(sorted(groups.items(), reverse=True)):
        if max_groups is not None and gi >= max_groups:
            break
        for i in range(0, len(tickers), 100):
            chunk = tickers[i:i + 100]
            try:
                d = api.get(f"{KALSHI}/markets/candlesticks", {"market_tickers": ",".join(chunk),
                            "start_ts": close - max(LEADS) - 1200, "end_ts": close, "period_interval": 1})
            except (httpx.HTTPError, RuntimeError):
                continue
            for x in d.get("markets") or []:
                cs = []
                for k in x.get("candlesticks") or []:
                    b = _f((k.get("yes_bid") or {}).get("close_dollars"))
                    a = _f((k.get("yes_ask") or {}).get("close_dollars"))
                    cs.append((int(k["end_period_ts"]), b, a, _f(k.get("volume_fp")) or 0.0))
                cs.sort()
                ends = [e for e, *_ in cs]
                for lead in LEADS:
                    if "15M" in x["market_ticker"] and lead >= 900:
                        continue  # strike not yet set
                    snap = close - lead
                    j = bisect.bisect_right(ends, snap) - 1
                    while j >= 0 and not (cs[j][1] is not None and cs[j][2] is not None and cs[j][2] > cs[j][1]):
                        j -= 1
                    if j < 0 or snap - cs[j][0] > 900:
                        continue
                    _, b, a, _ = cs[j]
                    vol10 = sum(v for e, _, _, v in cs if snap - 600 < e <= snap)
                    k0 = bisect.bisect_right(ends, snap - 600) - 1
                    m0 = (cs[k0][1] + cs[k0][2]) / 2 if k0 >= 0 and cs[k0][1] is not None and cs[k0][2] is not None else None
                    c.execute("INSERT OR REPLACE INTO pm_near VALUES (?,?,?,?,?,?,?,?,?)", (
                        x["market_ticker"], lead, snap, b, a, round((b + a) / 2, 4), snap - cs[j][0], vol10,
                        None if m0 is None else (b + a) / 2 - m0))
                    added += 1
        c.commit()
    return added


# ---------------- training / evaluation ----------------

def load(c: sqlite3.Connection) -> list[dict[str, Any]]:
    c.row_factory = sqlite3.Row
    base = [dict(r) for r in c.execute(
        "SELECT n.*, t.series, t.event_ticker, t.close_time, t.strike_type, t.floor_strike, t.cap_strike, t.result,"
        " t.expiration_value FROM pm_near n JOIN pm_train t USING (ticker) WHERE n.ask - n.bid <= 0.2")]
    c.row_factory = None
    ixs = {a: Index1m(c, a) for a in ASSETS}
    out = []
    for r in base:
        a = asset_of(r["series"])
        close = int(datetime.fromisoformat(r["close_time"]).timestamp())
        mp = model_prob(ixs[a], r["snap_ts"], close, r["strike_type"] or "", r["floor_strike"], r["cap_strike"]) if a else None
        r.update({"snap_mid": r["mid"], "snap_bid": r["bid"], "snap_ask": r["ask"], "category": "crypto",
                  "day": r["close_time"][:10], "model_p": mp[0] if mp else None, "z": mp[1] if mp else None})
        out.append(r)
    return out


def x_of(model: str, r: dict[str, Any]) -> list[float] | None:
    lm = logit(r["snap_mid"])
    if model == "market_cal":
        return [1.0, lm]
    if r["model_p"] is None:
        return None
    lp = logit(r["model_p"])
    if model == "spot1m_cal":
        return [1.0, lp]
    if model == "stack":
        return [1.0, lm, lp]
    if model == "stack_flow":
        return [1.0, lm, lp, r["dmid10"] or 0.0, math.log1p(r["vol10"] or 0.0) / 10]
    return None


PRIORS = {"market_cal": [0.0, 1.0], "spot1m_cal": [0.0, 1.0], "stack": [0.0, 1.0, 0.0],
          "stack_flow": [0.0, 1.0, 0.0, 0.0, 0.0]}


def wf(rows: list[dict[str, Any]], model: str, min_train: int = 300) -> list[tuple[dict[str, Any], float]]:
    days = sorted({r["day"] for r in rows})
    preds = []
    for d in days:
        test = [r for r in rows if r["day"] == d]
        if model == "spot1m":
            preds += [(r, r["model_p"]) for r in test if r["model_p"] is not None]
            continue
        tr = [(x_of(model, r), r["result"]) for r in rows if r["day"] < d]
        tr = [(x, y) for x, y in tr if x is not None]
        if len(tr) < min_train:
            continue
        w = fit_logistic([x for x, _ in tr], [y for _, y in tr], 1.0, PRIORS[model])
        for r in test:
            x = x_of(model, r)
            if x is not None:
                preds.append((r, clip(sigmoid(sum(a * b for a, b in zip(w, x, strict=False))))))
    return preds


def mirror_check(c: sqlite3.Connection) -> dict[str, Any]:
    """How closely our index mirror reproduces Kalshi's published expiration value."""
    out = {}
    for a in ASSETS:
        ix = Index1m(c, a)
        errs = []
        for close, ev in c.execute("SELECT DISTINCT close_time, expiration_value FROM pm_train WHERE series LIKE ?"
                                   " AND expiration_value > 0", (a + "%",)):
            m = ix.settle_mirror(int(datetime.fromisoformat(close).timestamp()))
            if m:
                errs.append(abs(m / ev - 1) * 1e4)
        if errs:
            errs.sort()
            out[a] = {"n": len(errs), "median_bps": round(errs[len(errs) // 2], 2),
                      "p90_bps": round(errs[int(len(errs) * 0.9)], 2)}
    return out


def train(db: str) -> dict[str, Any]:
    c = sqlite_connect(db)
    ensure_schema(c)
    rows = load(c)
    results = []
    for lead in LEADS:
        rs = [r for r in rows if r["lead_s"] == lead]
        rs.sort(key=lambda r: r["close_time"])
        for model in ("spot1m", "market_cal", "spot1m_cal", "stack", "stack_flow"):
            e = evaluate(wf(rs, model))
            if e.get("n"):
                w = None
                if model != "spot1m":
                    tr = [(x_of(model, r), r["result"]) for r in rs]
                    tr = [(x, y) for x, y in tr if x is not None]
                    w = fit_logistic([x for x, _ in tr], [y for _, y in tr], 1.0, PRIORS[model]) if len(tr) >= 300 else None
                results.append({"lead_min": lead // 60, "model": model, "weights": w, **e})
    keep_b = bh([r["p_brier"] for r in results])
    keep_p = bh([r["p_pnl"] for r in results])
    for r, kb, kp in zip(results, keep_b, keep_p, strict=False):
        r["beats_market"] = bool(kb and r["brier_diff_ci95"][1] < 0)
        r["worse_than_market"] = bool(kb and r["brier_diff_ci95"][0] > 0)
        r["paper_trade_enabled"] = bool(r["beats_market"] and kp and r["pnl_ci95"][0] > 0)
    rep = {"schema": "tev.pm_crypto.v1", "at": datetime.now(UTC).isoformat(), "mode": "paper/forecast only",
           "rows": len(rows), "by_lead": {lead // 60: sum(r["lead_s"] == lead for r in rows) for lead in LEADS},
           "events": len({r["event_ticker"] for r in rows}),
           "mirror_vs_kalshi_expiration": mirror_check(c),
           "multiple_testing": f"Benjamini-Hochberg q=0.05 over {len(results)} tests (Brier and P&L separately)",
           "fee": "Kalshi taker 0.07*P*(1-P) per contract", "results": results}
    c.execute("INSERT INTO pm_crypto_reports (at, report_json) VALUES (?,?)", (rep["at"], json.dumps(rep)))
    c.commit()
    c.close()
    return rep


# ---------------- live (used by fast_settle) ----------------

class Live:
    def __init__(self) -> None:
        self.report: dict[str, Any] | None = None
        self.loaded = 0.0
        self.ix: dict[str, Index1m] = {}
        self.ix_at: dict[str, float] = {}
        self.client = httpx.Client(timeout=15)


LIVE = Live()


def _refresh_asset(c: sqlite3.Connection, asset: str) -> None:
    if time.monotonic() - LIVE.ix_at.get(asset, 0) < 55:
        return
    now = int(time.time())
    cb, bs = ASSETS[asset]
    rows: list[tuple[str, str, int, float, float, float, float]] = []
    try:
        rows += [(asset, "coinbase", *r) for r in _coinbase(LIVE.client, cb, now - 300 * 60, now) if r[0] <= now]
    except (httpx.HTTPError, ValueError):
        pass
    try:
        rows += [(asset, "bitstamp", *r) for r in _bitstamp(LIVE.client, bs, now - 1000 * 60) if r[0] <= now]
    except (httpx.HTTPError, KeyError, ValueError):
        pass
    c.executemany("INSERT OR REPLACE INTO pm_ref_1m VALUES (?,?,?,?,?,?,?)", rows)
    c.commit()
    LIVE.ix[asset] = Index1m(c, asset)
    LIVE.ix_at[asset] = time.monotonic()


def live_forecasts(db: str, row: Any, mid: float, bid: float, ask: float, now: datetime) -> dict[str, tuple[float, dict]]:
    """Near-close agents: forecast once inside [lead-90s, lead] before close using bars closed by now."""
    asset = asset_of(row["series"])
    if not asset or not os.path.isfile(db):
        return {}
    close = int(datetime.fromisoformat(row["close_time"]).timestamp())
    secs = close - now.timestamp()
    leads = [lead for lead in LEADS if lead - 90 <= secs <= lead and not ("15M" in row["series"] and lead >= 900)]
    if not leads:
        return {}
    c = sqlite_connect(db)
    try:
        ensure_schema(c)
        if time.monotonic() - LIVE.loaded > 600:
            r = c.execute("SELECT report_json FROM pm_crypto_reports ORDER BY id DESC LIMIT 1").fetchone()
            LIVE.report = json.loads(r[0]) if r else None
            LIVE.loaded = time.monotonic()
        if not LIVE.report:
            return {}
        _refresh_asset(c, asset)
    finally:
        c.close()
    snap = int(now.timestamp())
    mp = model_prob(LIVE.ix[asset], snap, close, row["strike_type"] or "", row["floor_strike"], row["cap_strike"])
    r = {"snap_mid": mid, "model_p": mp[0] if mp else None, "dmid10": None, "vol10": None}
    out: dict[str, tuple[float, dict]] = {}
    for res in LIVE.report["results"]:
        if res["lead_min"] * 60 not in leads or res.get("worse_than_market"):
            continue
        model = res["model"]
        if model == "spot1m":
            p = r["model_p"]
        else:
            x = x_of(model, r)
            p = None if x is None or not res.get("weights") else clip(sigmoid(sum(a * b for a, b in zip(res["weights"], x, strict=False))))
        if p is not None:
            out[f"c1m-{model}-{res['lead_min']}m"] = (p, {"rule": f"pm_crypto {model} at {res['lead_min']}m to close",
                                                          "model_p": r["model_p"], "z": mp[1] if mp else None,
                                                          "trade_enabled": bool(res.get("paper_trade_enabled"))})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("NOEMA_PM_TRAIN_DB", "data/pm_train.db"))
    ap.add_argument("--days", type=int, default=46)
    ap.add_argument("--skip-backfill", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=6 * 3600)
    a = ap.parse_args()
    while True:
        c = sqlite_connect(a.db)
        if not a.skip_backfill:
            # pm_models only tops up pm_train every 6h; pull fresh crypto settlements here so the
            # near-close set tracks the loop cadence instead of going stale between model fits.
            try:
                from .pm_history import SERIES as _hist_series
                from .pm_history import run as _hist_run
                crypto = [x for x in _hist_series if asset_of(x)]
                _hist_run(a.db, days=1, series=crypto, max_per_series=500)
            except Exception as exc:  # noqa: BLE001 - settlement top-up is best effort
                print(json.dumps({"settlement_topup_error": type(exc).__name__}), flush=True)
            print(json.dumps({"ref_1m": backfill_1m(c, a.days)}), flush=True)
            print(json.dumps({"near": backfill_near(c, Api())}), flush=True)
        c.close()
        rep = train(a.db)
        print(json.dumps({k: rep[k] for k in ("at", "rows", "by_lead", "mirror_vs_kalshi_expiration")}), flush=True)
        if not a.loop:
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
