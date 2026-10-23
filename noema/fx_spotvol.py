"""Pre-registered paper test of the Kalshi FX 15-minute spot-vol agents (docs/research/fx-spotvol-prereg-2026-10-22.md).

PAPER ONLY. Reads public Kalshi market data (no auth, no keys) and public Yahoo 5m bars; never places orders,
never reads signer material, never touches a live flag. Writes only its own DB (data/fx_spotvol.db) so it adds
no lock contention to noema.db / pm_train.db (pm_train.db is opened read-only).

  python -m noema.fx_spotvol --loop            # forward paper run (start.sh)
  python -m noema.fx_spotvol --redteam         # historical re-score under the pre-registered rules
  python -m noema.fx_spotvol --status          # daily check: n, mean, 97.5% CI, per-day, skips, verdict
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .pm_models import Bars, clip, logit, sigmoid, spotvol_prob
from .sqlite_util import connect as sqlite_connect
from .sqlite_util import is_busy, retry_write

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/"
ET = ZoneInfo("America/New_York")

# ---- frozen pre-registered parameters (changing any of these voids the run) ----
SERIES = {"KXGBPUSD15M": "GBPUSD=X", "KXAUDUSD15M": "AUDUSD=X", "KXUSDCAD15M": "CAD=X"}
AGENTS = {"fx-spotvol": "pm-other-spotvol-2d3462", "fx-model_cal": "pm-other-model_cal-364537"}
MODEL_CAL_W = (-0.23540, 0.56220)
LEAD_S = 450                 # decision at close - 450s (training snapshot lead for 15m markets)
SIGNAL_MAX_AGE_S = 60
LIVE_SIGNAL_GRACE_S = 20     # live: the signal read must happen within 20s of t_d (else skip)
FILL_DELAY_S, FILL_MAX_DELAY_S = 60, 120
MAX_SPREAD = 0.20
SPOT_MAX_AGE_S = 900
SLIPPAGE = 0.01
CONTRACTS = 10
WINDOW = (datetime(2026, 10, 23, tzinfo=ET), datetime(2026, 10, 30, tzinfo=ET))
MIN_N, MIN_POS_DAYS, DAY_MIN_TRADES = 500, 5, 30
CI_LEVEL = 0.975             # Bonferroni over 2 agents
KILL_AFTER_N, KILL_PNL_USD = 200, -300.0
BAR_S = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS fx_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT NOT NULL,
    ticker TEXT NOT NULL, series TEXT NOT NULL, close_time TEXT NOT NULL, decision_at TEXT NOT NULL,
    signal_quote_at TEXT, signal_bid REAL, signal_ask REAL, spot_bar_at TEXT, spot REAL, strike REAL, prob REAL,
    side TEXT, fill_quote_at TEXT, fill_bid REAL, fill_ask REAL, fill_price REAL, fee REAL, contracts INTEGER,
    status TEXT NOT NULL, skip_reason TEXT, result INTEGER, pnl REAL, pnl_per_contract REAL, settled_at TEXT,
    created_at TEXT NOT NULL, UNIQUE (agent, ticker));
CREATE INDEX IF NOT EXISTS fx_decisions_status ON fx_decisions(status);
CREATE TABLE IF NOT EXISTS fx_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, summary_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fx_hist_candles (ticker TEXT NOT NULL, end_ts INTEGER NOT NULL, bid REAL, ask REAL,
    PRIMARY KEY (ticker, end_ts));
CREATE TABLE IF NOT EXISTS fx_hist_fetched (ticker TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS fx_reports (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL,
    report_json TEXT NOT NULL);
"""


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def _ts(s: str) -> int:
    return int(datetime.fromisoformat(s).timestamp())


def _f(v: Any) -> float | None:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def fee_per_contract(price: float, contracts: int = CONTRACTS) -> float:
    """Kalshi taker fee 0.07*P*(1-P) per contract, rounded up to the cent per order."""
    return math.ceil(round(100 * 0.07 * contracts * price * (1 - price), 6)) / 100 / contracts


def raw_fee(price: float) -> float:
    return 0.07 * price * (1 - price)


def agent_prob(agent: str, p_spotvol: float) -> float:
    if agent == "fx-spotvol":
        return p_spotvol
    return clip(sigmoid(MODEL_CAL_W[0] + MODEL_CAL_W[1] * logit(p_spotvol)))


def decide(p: float, bid: float, ask: float) -> str | None:
    """Unchanged backtest rule, evaluated on the signal quote only."""
    if p - ask > raw_fee(ask):
        return "yes"
    if (1 - p) - (1 - bid) > raw_fee(1 - bid):
        return "no"
    return None


def quote_ok(bid: float | None, ask: float | None) -> bool:
    return bid is not None and ask is not None and 0 < bid < ask < 1 and ask - bid <= MAX_SPREAD


def fill(side: str, bid: float, ask: float, slippage: float = SLIPPAGE) -> float:
    px = ask if side == "yes" else 1 - bid
    return min(0.99, round(px + slippage, 4))


def pnl_per_contract(side: str, price: float, result: int) -> float:
    win = result if side == "yes" else 1 - result
    return win - price - fee_per_contract(price)


def aligned(bars: list[tuple[int, float]], before: int) -> Bars:
    """Only completed, start-stamped 5m bars (start + 300 <= decision) are usable."""
    return Bars([(t, p) for t, p in bars if t % BAR_S == 0 and t + BAR_S <= before])


def spot_check(b: Bars, at: int) -> tuple[float, int] | None:
    s = b.spot(at)
    if not s or at - (s[1] + BAR_S) > SPOT_MAX_AGE_S:
        return None
    return s


# ---- statistics ----
def cluster_ci(vals: list[tuple[str, float]], level: float = CI_LEVEL, n_boot: int = 10000,
               seed: int = 11) -> tuple[float, float, float]:
    by: dict[str, list[float]] = defaultdict(list)
    for k, v in vals:
        by[k].append(v)
    sums = [(sum(v), len(v)) for v in by.values()]
    n = sum(m for _, m in sums)
    mean = sum(s for s, _ in sums) / n if n else 0.0
    if len(sums) < 5:
        return mean, -math.inf, math.inf
    rng = random.Random(seed)
    k = len(sums)
    boots = []
    for _ in range(n_boot):
        s = m = 0.0
        for _ in range(k):
            a, b = sums[rng.randrange(k)]
            s += a
            m += b
        boots.append(s / m)
    boots.sort()
    a = (1 - level) / 2
    return mean, boots[int(a * n_boot)], boots[min(n_boot - 1, int((1 - a) * n_boot))]


def evaluate_trades(trades: list[dict[str, Any]], n_boot: int = 10000) -> dict[str, Any]:
    """trades: dicts with close_time, decision_at, pnl_per_contract."""
    if not trades:
        return {"n": 0, "verdict": "no trades"}
    mean, lo, hi = cluster_ci([(t["close_time"], t["pnl_per_contract"]) for t in trades], n_boot=n_boot)
    days: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        days[datetime.fromisoformat(t["decision_at"]).astimezone(ET).date().isoformat()].append(t["pnl_per_contract"])
    per_day = {d: {"n": len(v), "mean": round(sum(v) / len(v), 4)} for d, v in sorted(days.items())}
    qual = {d: x for d, x in per_day.items() if x["n"] >= DAY_MIN_TRADES}
    pos = sum(1 for x in qual.values() if x["mean"] > 0)
    total = sum(t["pnl_per_contract"] for t in trades) * CONTRACTS
    checks = {"n>=500": len(trades) >= MIN_N, "mean>0": mean > 0, "ci97.5_excludes_0": lo > 0,
              "positive_days>=5": pos >= MIN_POS_DAYS}
    kill = (len(trades) >= KILL_AFTER_N and hi < 0) or total <= KILL_PNL_USD
    return {"n": len(trades), "mean_per_contract": round(mean, 4), "ci97_5": [round(lo, 4), round(hi, 4)],
            "total_pnl_usd_10ct": round(total, 2), "per_day": per_day, "qualifying_days": len(qual),
            "positive_days": pos, "checks": checks, "kill": kill,
            "verdict": "KILL" if kill else ("PASS" if all(checks.values()) else "not passed (yet)")}


# ---- Kalshi / Yahoo I/O ----
class Http:
    def __init__(self) -> None:
        import httpx
        self.c = httpx.Client(timeout=20, headers={"accept": "application/json", "User-Agent": "Mozilla/5.0"})
        self.last = 0.0

    def get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        wait = 0.25 - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(3):
            self.last = time.monotonic()
            r = self.c.get(url, params=params)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("rate limited")


def yahoo_bars(h: Http, sym: str, rng: str = "2d") -> list[tuple[int, float]]:
    res = h.get(YAHOO + sym, {"interval": "5m", "range": rng})["chart"]["result"][0]
    return [(int(t), float(v)) for t, v in zip(res["timestamp"], res["indicators"]["quote"][0]["close"],
                                                strict=False) if v]


# ---- historical red-team ----
def _hist_candles(h: Http, out: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    have = {r[0] for r in out.execute("SELECT ticker FROM fx_hist_fetched")}
    groups: dict[int, list[str]] = defaultdict(list)
    for r in rows:
        if r["ticker"] not in have:
            groups[_ts(r["close_time"]) - LEAD_S].append(r["ticker"])
    for i, (td, tickers) in enumerate(sorted(groups.items())):
        try:
            d = h.get(f"{KALSHI}/markets/candlesticks", {"market_tickers": ",".join(tickers), "start_ts": td - 180,
                                                         "end_ts": td + 180, "period_interval": 1})
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"candles_error": str(exc)[:120], "td": td}), flush=True)
            continue
        for x in d.get("markets") or []:
            for cs in x.get("candlesticks") or []:
                out.execute("INSERT OR REPLACE INTO fx_hist_candles VALUES (?,?,?,?)", (
                    x["market_ticker"], int(cs["end_period_ts"]), _f((cs.get("yes_bid") or {}).get("close_dollars")),
                    _f((cs.get("yes_ask") or {}).get("close_dollars"))))
        out.executemany("INSERT OR IGNORE INTO fx_hist_fetched VALUES (?)", [(t,) for t in tickers])
        if i % 50 == 0:
            out.commit()
            print(json.dumps({"candle_groups_done": i, "of": len(groups)}), flush=True)
    out.commit()


def redteam(train_db: str, out_db: str, fetch: bool = True) -> dict[str, Any]:
    from .pm_models import design, fit_logistic, stack_prior
    src = sqlite3.connect(f"file:{train_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    rows = [dict(r) for r in src.execute("SELECT * FROM pm_train WHERE category='other' AND strike_type="
                                         "'greater_or_equal' AND floor_strike IS NOT NULL ORDER BY close_time")]
    src.row_factory = None
    allbars = {s: sorted(src.execute("SELECT ts, close FROM pm_ref_bars WHERE symbol=?", (s,)).fetchall())
               for s in SERIES.values()}
    out = sqlite_connect(out_db)
    out.executescript(SCHEMA)
    if fetch:
        _hist_candles(Http(), out, rows)
    cand: dict[str, dict[int, tuple[float | None, float | None]]] = defaultdict(dict)
    for t, e, b, a in out.execute("SELECT ticker, end_ts, bid, ask FROM fx_hist_candles"):
        cand[t][e] = (b, a)
    bars_all = {s: Bars([(t, p) for t, p in v if t % BAR_S == 0]) for s, v in allbars.items()}
    # model probabilities at the decision time (spot strictly from completed bars)
    for r in rows:
        td = _ts(r["close_time"]) - LEAD_S
        r["td"] = td
        b = bars_all[SERIES[r["series"]]]
        s = spot_check(b, td)
        r["p_sv"] = spotvol_prob(b, td, _ts(r["close_time"]), "greater_or_equal", r["floor_strike"], None) if s else None
        r["day"] = r["close_time"][:10]
    # walk-forward model_cal (same as pm_models: fit on strictly earlier days on the snapshot quote rows)
    wf: dict[str, list[float] | None] = {}
    for d in sorted({r["day"] for r in rows}):
        xs = [(design("model_cal", 0.5, r["p_sv"]), r["result"]) for r in rows if r["day"] < d and r["p_sv"] is not None]
        wf[d] = fit_logistic([x for x, _ in xs], [y for _, y in xs], 1.0, stack_prior("model_cal")) if len(xs) >= 150 else None

    def q_at(tk: str, lo: int, hi: int, first: bool) -> tuple[int, float, float] | None:
        ks = sorted(e for e in cand.get(tk, {}) if lo <= e <= hi)
        for e in (ks if first else ks[::-1]):
            b, a = cand[tk][e]
            if quote_ok(b, a):
                return e, b, a  # type: ignore[return-value]
        return None

    variants = {"original_snapshot_no_delay": {}, "prereg_ask+1m+1c": {}, "ask+1m_0c": {}, "ask+1m_2c": {},
                "ask+30s+1c": {}, "recheck_edge_at_fill": {}}
    skips: Counter = Counter()
    res: dict[str, dict[str, list[dict[str, Any]]]] = {v: {a: [] for a in AGENTS} for v in variants}
    by_pair: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r["p_sv"] is None:
            skips["stale_spot"] += 1
            continue
        for ag in AGENTS:
            if ag == "fx-spotvol":
                p = r["p_sv"]
            else:
                w = wf.get(r["day"])
                if w is None:
                    continue
                p = clip(sigmoid(w[0] + w[1] * logit(r["p_sv"])))
            # original backtest view: pm_train snapshot quote, no delay/slip, raw fee
            if quote_ok(r["snap_bid"], r["snap_ask"]):
                side = decide(p, r["snap_bid"], r["snap_ask"])
                if side:
                    px = r["snap_ask"] if side == "yes" else 1 - r["snap_bid"]
                    win = r["result"] if side == "yes" else 1 - r["result"]
                    res["original_snapshot_no_delay"][ag].append({"close_time": r["close_time"], "decision_at": _iso(r["td"]),
                                                                  "pnl_per_contract": win - px - raw_fee(px)})
            sig = q_at(r["ticker"], r["td"] - SIGNAL_MAX_AGE_S, r["td"], first=False)
            if not sig:
                skips[f"{ag}:stale_signal"] += 1
                continue
            side = decide(p, sig[1], sig[2])
            if not side:
                continue
            for v, (dlo, dhi, slip, recheck) in {"prereg_ask+1m+1c": (60, 120, 0.01, False), "ask+1m_0c": (60, 120, 0.0, False),
                                                  "ask+1m_2c": (60, 120, 0.02, False), "ask+30s+1c": (30, 90, 0.01, False),
                                                  "recheck_edge_at_fill": (60, 120, 0.01, True)}.items():
                fq = q_at(r["ticker"], r["td"] + dlo, r["td"] + dhi, first=True)
                if not fq:
                    if v == "prereg_ask+1m+1c":
                        skips[f"{ag}:stale_fill"] += 1
                    continue
                if recheck and decide(p, fq[1], fq[2]) != side:
                    continue
                px = fill(side, fq[1], fq[2], slip)
                win = r["result"] if side == "yes" else 1 - r["result"]
                pp = win - px - fee_per_contract(px)
                res[v][ag].append({"close_time": r["close_time"], "decision_at": _iso(r["td"]), "pnl_per_contract": pp,
                                   "series": r["series"], "side": side, "move": (fq[2] + fq[1]) / 2 - (sig[2] + sig[1]) / 2})
                if v == "prereg_ask+1m+1c":
                    by_pair[f"{ag}:{r['series']}"].append(pp)
    report: dict[str, Any] = {"at": _iso(time.time()), "rows": len(rows), "date_range": [rows[0]["close_time"], rows[-1]["close_time"]],
                              "skips": dict(skips), "variants": {}}
    for v in variants:
        report["variants"][v] = {}
        for ag in AGENTS:
            e = evaluate_trades(res[v][ag], n_boot=2000)
            e.pop("per_day", None)
            report["variants"][v][ag] = e
    report["by_pair_prereg"] = {k: {"n": len(v), "mean": round(sum(v) / len(v), 4)} for k, v in sorted(by_pair.items())}
    # how much did the quote move toward our side in the 1-2 minutes after the signal? (stale-quote artifact)
    mv = [t["move"] * (1 if t["side"] == "yes" else -1) for t in res["prereg_ask+1m+1c"]["fx-spotvol"]]
    report["mid_move_toward_signal_side_1m"] = round(sum(mv) / len(mv), 4) if mv else None
    out.execute("INSERT INTO fx_reports (at, kind, report_json) VALUES (?,?,?)", (report["at"], "redteam", json.dumps(report)))
    out.commit()
    out.close()
    return report


# ---- forward paper run ----
class Live:
    def __init__(self, db: str, train_db: str, http: Http | None = None) -> None:
        self.db, self.train_db = db, train_db
        self.h = http or Http()
        self.c = sqlite_connect(db)
        self.c.executescript(SCHEMA)
        self.markets: dict[str, dict[str, Any]] = {}
        self.hist: dict[str, list[tuple[int, float]]] = {}
        self.fresh: dict[str, tuple[float, list[tuple[int, float]]]] = {}
        self.hist_at = self.disc_at = self.settle_at = 0.0
        self.errors: Counter = Counter()

    def _w(self, sql: str, args: tuple) -> bool:
        """Own-DB write with busy retry; on persistent lock skip-and-log (never crash the loop)."""
        def go() -> None:
            self.c.execute(sql, args)
            self.c.commit()
        try:
            retry_write(go, conn=self.c)
            return True
        except sqlite3.OperationalError as exc:
            if not is_busy(exc):
                raise
            self.errors["db_busy_skipped"] += 1
            return False

    def load_hist(self, now: float) -> None:
        if now - self.hist_at < 6 * 3600 and self.hist:
            return
        src = sqlite3.connect(f"file:{self.train_db}?mode=ro", uri=True, timeout=10)
        try:
            for s in SERIES.values():
                self.hist[s] = src.execute("SELECT ts, close FROM pm_ref_bars WHERE symbol=? AND ts>=?",
                                           (s, int(now) - 32 * 86400)).fetchall()
        finally:
            src.close()
        self.hist_at = now

    def bars(self, sym: str, now: float) -> list[tuple[int, float]]:
        at, rows = self.fresh.get(sym, (0.0, []))
        if now - at > 45:
            try:
                rows = yahoo_bars(self.h, sym)
                self.fresh[sym] = (now, rows)
            except Exception:  # noqa: BLE001 - stale spot is handled by the freshness gate
                self.errors["yahoo_error"] += 1
        merged = dict(self.hist.get(sym, []))
        merged.update(dict(rows))
        return sorted(merged.items())

    def discover(self, now: float) -> None:
        if now - self.disc_at < 60:
            return
        self.disc_at = now
        for series in SERIES:
            try:
                d = self.h.get(f"{KALSHI}/markets", {"series_ticker": series, "status": "open", "limit": 100})
            except Exception:  # noqa: BLE001
                self.errors["discover_error"] += 1
                continue
            for m in d.get("markets") or []:
                ct = _ts(m["close_time"])
                if ct > now:
                    self.markets[m["ticker"]] = {"series": series, "close_ts": ct, "event": m.get("event_ticker")}
        for t in [t for t, m in self.markets.items() if m["close_ts"] < now - 3600]:
            self.markets.pop(t, None)

    def quote(self, ticker: str) -> tuple[float, dict[str, Any]] | None:
        try:
            m = self.h.get(f"{KALSHI}/markets/{ticker}")["market"]
        except Exception:  # noqa: BLE001
            self.errors["quote_error"] += 1
            return None
        return time.time(), m

    def _done(self, ticker: str) -> bool:
        return self.c.execute("SELECT 1 FROM fx_decisions WHERE ticker=? LIMIT 1", (ticker,)).fetchone() is not None

    def signal(self, now: float) -> int:
        n = 0
        for tk, m in list(self.markets.items()):
            td = m["close_ts"] - LEAD_S
            if not (td <= now < td + SIGNAL_MAX_AGE_S) or self._done(tk):
                continue
            q = self.quote(tk)
            base = {"ticker": tk, "series": m["series"], "close_time": _iso(m["close_ts"]), "decision_at": _iso(td)}
            reason, qa, bid, ask, strike = None, None, None, None, None
            if not q:
                reason = "quote_error"
            else:
                qa, mk = q
                bid, ask, strike = _f(mk.get("yes_bid_dollars")), _f(mk.get("yes_ask_dollars")), _f(mk.get("floor_strike"))
                if qa - td > LIVE_SIGNAL_GRACE_S:
                    reason = "stale_signal"
                elif not quote_ok(bid, ask):
                    reason = "bad_quote"
                elif mk.get("strike_type") != "greater_or_equal" or not strike:
                    reason = "no_strike"
            sym = SERIES[m["series"]]
            b = aligned(self.bars(sym, now), td)
            s = spot_check(b, td) if not reason else None
            if not reason and not s:
                reason = "stale_spot"
            psv = spotvol_prob(b, td, m["close_ts"], "greater_or_equal", strike, None) if not reason else None
            if not reason and psv is None:
                reason = "stale_spot"
            for ag in AGENTS:
                p = agent_prob(ag, psv) if psv is not None else None
                side = decide(p, bid, ask) if p is not None else None  # type: ignore[arg-type]
                status = "skip" if reason else ("pending_fill" if side else "no_trade")
                self._w("INSERT OR IGNORE INTO fx_decisions (agent, ticker, series, close_time, decision_at, signal_quote_at,"
                        " signal_bid, signal_ask, spot_bar_at, spot, strike, prob, side, contracts, status, skip_reason,"
                        " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ag, tk, base["series"], base["close_time"], base["decision_at"], _iso(qa) if qa else None, bid, ask,
                         _iso(s[1] + BAR_S) if s else None, s[0] if s else None, strike, p, side,
                         CONTRACTS if side else None, status, reason, _iso(time.time())))
                n += 1
        return n

    def fills(self, now: float) -> int:
        n = 0
        pend = self.c.execute("SELECT DISTINCT ticker, decision_at FROM fx_decisions WHERE status='pending_fill'").fetchall()
        for tk, da in pend:
            td = _ts(da)
            if now < td + FILL_DELAY_S:
                continue
            q = self.quote(tk) if now <= td + FILL_MAX_DELAY_S else None
            qa = q[0] if q else None
            bid = _f(q[1].get("yes_bid_dollars")) if q else None
            ask = _f(q[1].get("yes_ask_dollars")) if q else None
            ok = q is not None and td + FILL_DELAY_S <= qa <= td + FILL_MAX_DELAY_S and quote_ok(bid, ask)  # type: ignore[operator]
            for aid, side in self.c.execute("SELECT id, side FROM fx_decisions WHERE ticker=? AND status='pending_fill'",
                                            (tk,)).fetchall():
                if not ok:
                    self._w("UPDATE fx_decisions SET status='skip', skip_reason='stale_fill', fill_quote_at=?, fill_bid=?,"
                            " fill_ask=? WHERE id=?", (_iso(qa) if qa else None, bid, ask, aid))
                    continue
                px = fill(side, bid, ask)  # type: ignore[arg-type]
                self._w("UPDATE fx_decisions SET status='open', fill_quote_at=?, fill_bid=?, fill_ask=?, fill_price=?, fee=?"
                        " WHERE id=?", (_iso(qa), bid, ask, px, round(fee_per_contract(px) * CONTRACTS, 2), aid))  # type: ignore[arg-type]
                n += 1
        return n

    def settle(self, now: float) -> int:
        if now - self.settle_at < 60:
            return 0
        self.settle_at = now
        n = 0
        for (tk,) in self.c.execute("SELECT DISTINCT ticker FROM fx_decisions WHERE status='open' AND close_time<?",
                                    (_iso(now - 30),)).fetchall():
            q = self.quote(tk)
            res = (q[1].get("result") if q else None) or ""
            if res not in ("yes", "no"):
                continue
            y = 1 if res == "yes" else 0
            for aid, side, px in self.c.execute("SELECT id, side, fill_price FROM fx_decisions WHERE ticker=? AND status='open'",
                                                (tk,)).fetchall():
                pp = pnl_per_contract(side, px, y)
                self._w("UPDATE fx_decisions SET status='settled', result=?, pnl_per_contract=?, pnl=?, settled_at=? WHERE id=?",
                        (y, round(pp, 6), round(pp * CONTRACTS, 4), _iso(now), aid))
                n += 1
        return n

    def cycle(self) -> dict[str, Any]:
        now = time.time()
        self.load_hist(now)
        self.discover(now)
        out = {"signals": self.signal(now), "fills": self.fills(time.time()), "settled": self.settle(time.time()),
               "tracked": len(self.markets), "errors": dict(self.errors)}
        return out


def run_loop(db: str, train_db: str, interval: float = 5.0) -> None:
    from .heartbeats import beat
    lv = Live(db, train_db)
    last_run = 0.0
    while True:
        try:
            s = lv.cycle()
            ok, err = True, None
        except Exception as exc:  # noqa: BLE001 - keep the paper loop alive; the heartbeat shows the error
            s, ok, err = {"error": f"{type(exc).__name__}: {exc}"[:200]}, False, exc
        if time.time() - last_run >= 60 or not ok:
            last_run = time.time()
            lv._w("INSERT INTO fx_runs (at, summary_json) VALUES (?,?)", (_iso(last_run), json.dumps(s)))
            beat("agent:fx_spotvol", ok, err, cadence_s=60)
            print(json.dumps({"at": _iso(last_run), **s}), flush=True)
        time.sleep(interval)


def status(db: str) -> dict[str, Any]:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    lo, hi = WINDOW[0].astimezone(UTC).isoformat(), WINDOW[1].astimezone(UTC).isoformat()
    out: dict[str, Any] = {"window_et": [WINDOW[0].isoformat(), WINDOW[1].isoformat()], "prereg":
                           "docs/research/fx-spotvol-prereg-2026-10-22.md", "agents": {}}
    for ag in AGENTS:
        tr = [dict(r) for r in c.execute("SELECT close_time, decision_at, pnl_per_contract FROM fx_decisions WHERE agent=?"
                                         " AND status='settled' AND decision_at>=? AND decision_at<?", (ag, lo, hi))]
        e = evaluate_trades(tr)
        e["status_counts"] = dict(c.execute("SELECT status||COALESCE(':'||skip_reason,''), COUNT(*) FROM fx_decisions"
                                            " WHERE agent=? AND decision_at>=? AND decision_at<? GROUP BY 1", (ag, lo, hi)).fetchall())
        out["agents"][ag] = e
    r = c.execute("SELECT at, summary_json FROM fx_runs ORDER BY id DESC LIMIT 1").fetchone()
    out["last_run"] = {"at": r["at"], **json.loads(r["summary_json"])} if r else None
    out["last_run_age_s"] = round(time.time() - _ts(r["at"]), 1) if r else None
    out["all_time_counts"] = dict(c.execute("SELECT status, COUNT(*) FROM fx_decisions GROUP BY 1").fetchall())
    out["window_complete"] = datetime.now(UTC) >= WINDOW[1]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("NOEMA_FX_SPOTVOL_DB", "data/fx_spotvol.db"))
    ap.add_argument("--train-db", default=os.environ.get("NOEMA_PM_TRAIN_DB", "data/pm_train.db"))
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--redteam", action="store_true")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    if a.redteam:
        print(json.dumps(redteam(a.train_db, a.db, fetch=not a.no_fetch), indent=1))
    elif a.status:
        print(json.dumps(status(a.db), indent=1))
    elif a.loop:
        run_loop(a.db, a.train_db)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
