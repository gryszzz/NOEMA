"""Shared paper bankroll, quarter-Kelly sizing, per-category record and post-bet price moves (PAPER ONLY).

One paper bankroll is shared by every bot in the room; NOEMA owns it and publishes it every cycle to
``NOEMA_RISK_BUDGET_PATH`` (default ``/workspace/noema-run/risk_budget.json``, schema ``tev.risk_budget.v1``,
written atomically, mode 0644: every bot may read it, only NOEMA writes it).

Sizing (every NOEMA binary paper bet):

* Entry gate: ``fair - (price + fee + slippage) > NOEMA_ENTRY_BUFFER`` (default 1c), side-relative. Fee:
  Kalshi 0.07*p*(1-p); Polymarket US 0.0695*p*(1-p) exchange-wide; Polymarket international per market from
  Gamma's feeSchedule (``pm_fees``), flat 0.0695 only when unknown ("fee assumed").
* Stake = quarter-Kelly of the shared bankroll: ``0.25 * (fair - c) / (1 - c) * bankroll`` with c the all-in cost.
* Hard caps: 5% of the bankroll per bet and per correlated group (all Fed markets, all BTC strikes, one
  election event ...); until a category has a held-out record (>= NOEMA_HELDOUT_MIN_N settled forward bets
  with positive P&L) the per-bet cap is NOEMA_UNPROVEN_BET_CAP_USD (default $5).
* Every stake draws from the room left in its group and in the bankroll, after all bots' open exposure
  (NOEMA's desks plus Prediction Bot's open picks from the prediction inbox).

Prediction Bot picks carry no size; they count at the stake this budget would give them (quarter-Kelly off
their own fair, category cap), never less than one $1 contract. Prediction Bot keeps its own calibration and
prereg; nothing here scores its picks.

Nothing in this module places orders, reads keys or touches live/owner flags.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_ID = "tev.risk_budget.v1"
DEFAULT_PATH = "/workspace/noema-run/risk_budget.json"
CATEGORIES = ("fx", "fed_macro", "elections", "crypto", "other")
CATEGORY_LABELS = {"fx": "FX", "fed_macro": "Fed / macro", "elections": "Elections", "crypto": "Crypto",
                   "other": "Other"}
MOVE_OFFSETS = (3600, 86400, 604800)  # 1 h, 1 d, 1 w after each paper bet


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def bankroll() -> float:
    v = os.getenv("NOEMA_SHARED_PAPER_BANKROLL_USD") or os.getenv("NOEMA_STRATEGY_PAPER_BANKROLL_USD")
    try:
        return max(1.0, float(v)) if v else 1000.0
    except ValueError:
        return 1000.0


def cap_pct() -> float:
    return min(0.05, max(0.0, _envf("NOEMA_RISK_CAP_PCT", 0.05)))


def kelly_fraction() -> float:
    return min(0.25, max(0.0, _envf("NOEMA_KELLY_FRACTION", 0.25)))


def unproven_cap_usd() -> float:
    return max(0.0, _envf("NOEMA_UNPROVEN_BET_CAP_USD", 5.0))


def heldout_min_n() -> int:
    return int(max(1, _envf("NOEMA_HELDOUT_MIN_N", 30)))


def slippage() -> float:
    return max(0.0, _envf("NOEMA_STRATEGY_SLIPPAGE", 0.005))


def budget_path() -> str:
    return os.getenv("NOEMA_RISK_BUDGET_PATH", DEFAULT_PATH)


# ------------------------------------------------------------------ strategy capacity (scale tests)

CAPACITY_SCHEMA = "tev.strategy_capacity.v1"
CAPACITY_PATH = "/workspace/mqgb/capacity/strategy_capacity.json"  # owned by MQGB; NOEMA only reads it
CAPACITY_TOP_KEYS = {"schema", "updated_at", "author", "strategies"}
CAPACITY_ENTRY_KEYS = {"measured_capacity_usd", "step_size_usd", "measured_at", "source", "note"}
CAPACITY_MAX_BYTES = 64 * 1024
_STRAT = re.compile(r"^[a-z0-9][a-z0-9_:.-]{0,63}$")


def capacity_path() -> str:
    return os.getenv("NOEMA_STRATEGY_CAPACITY_PATH", CAPACITY_PATH)


def step_ladder() -> list[float]:
    raw = os.getenv("NOEMA_STEP_LADDER_USD", "1,2.5,5,10,25,50")
    out = []
    for x in raw.split(","):
        try:
            v = float(x)
        except ValueError:
            continue
        if v > 0 and math.isfinite(v):
            out.append(v)
    return sorted(set(out)) or [1.0]


def _pos_num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if f > 0 and math.isfinite(f) else None


def load_capacity(path: str | None = None) -> dict[str, Any]:
    """Validate tev.strategy_capacity.v1 (owned and written by MQGB; read-only here). Unknown keys reject:
    top-level -> the whole file; per strategy -> that entry. Missing file -> nothing measured."""
    p = Path(path or capacity_path())
    out: dict[str, Any] = {"path": str(p), "status": "missing", "strategies": {}, "rejected": {}}
    try:
        st = os.lstat(p)
    except OSError:
        return out
    import stat as _stat
    if not _stat.S_ISREG(st.st_mode) or st.st_size > CAPACITY_MAX_BYTES:
        out["status"] = "rejected: not a regular file <= 64 KB"
        return out
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        out["status"] = "rejected: not JSON"
        return out
    if not isinstance(doc, dict) or doc.get("schema") != CAPACITY_SCHEMA:
        out["status"] = "rejected: bad schema"
        return out
    extra = set(doc) - CAPACITY_TOP_KEYS
    if extra:
        out["status"] = f"rejected: unknown keys {sorted(extra)[:5]}"
        return out
    strategies = doc.get("strategies")
    if not isinstance(strategies, dict) or len(strategies) > 100:
        out["status"] = "rejected: strategies must be an object (<= 100 entries)"
        return out
    for name, e in strategies.items():
        why = None
        if not isinstance(name, str) or not _STRAT.match(name):
            why = "bad strategy name"
        elif not isinstance(e, dict):
            why = "entry not an object"
        elif set(e) - CAPACITY_ENTRY_KEYS:
            why = f"unknown keys {sorted(set(e) - CAPACITY_ENTRY_KEYS)[:5]}"
        elif e.get("measured_capacity_usd") is not None and _pos_num(e.get("measured_capacity_usd")) is None:
            why = "measured_capacity_usd must be a finite number > 0 or null"
        elif e.get("step_size_usd") is not None and _pos_num(e.get("step_size_usd")) is None:
            why = "step_size_usd must be a finite number > 0"
        elif any(e.get(k) is not None and (not isinstance(e.get(k), str) or len(e[k]) > 200)
                 for k in ("measured_at", "source", "note")):
            why = "text fields must be strings <= 200 chars"
        if why:
            out["rejected"][str(name)[:64]] = why
            continue
        out["strategies"][name] = {"measured_capacity_usd": _pos_num(e.get("measured_capacity_usd")),
                                   "step_size_usd": _pos_num(e.get("step_size_usd")),
                                   "measured_at": e.get("measured_at"), "source": e.get("source")}
    out["status"] = "ok"
    return out


def strategy_limits(name: str, capacity: dict[str, Any], group_cap: float) -> dict[str, Any]:
    """measured capacity (null until a scale test), current step, and the cap = min of capacity, step, 5%.
    Unmeasured strategies stay at the minimum ladder step."""
    ladder = step_ladder()
    e = (capacity or {}).get("strategies", {}).get(name) or {}
    cap_m = e.get("measured_capacity_usd")
    if cap_m is None:
        step = ladder[0]
    elif e.get("step_size_usd"):
        step = min(max(ladder[0], e["step_size_usd"]), cap_m)
    else:
        step = max([x for x in ladder if x <= cap_m] or [ladder[0]])
    cap = min(step, group_cap, cap_m if cap_m is not None else float("inf"))
    return {"measured_capacity_usd": cap_m, "current_step_size_usd": round(step, 2), "cap_usd": round(cap, 2),
            "measured_at": e.get("measured_at"), "source": e.get("source")}


# ------------------------------------------------------------------ classification

_ASSETS = {"BTC": ("btc", "bitcoin"), "ETH": ("eth", "ether", "ethereum"), "SOL": ("sol", "solana"),
           "XRP": ("xrp",), "DOGE": ("doge", "dogecoin")}
_FX_PAIR = re.compile(r"(EUR|GBP|AUD|NZD)USD|USD(JPY|CAD|CHF|CNH|CNY|MXN)")
_FX_TXT = re.compile(r"\b(eur/usd|usd/jpy|gbp/usd|eurusd|usdjpy|gbpusd|forex|exchange rate|dollar index|dxy|yen|"
                     r"euro to|pound sterling)\b")
_FED_SER = re.compile(r"^(KX)?(FED|FOMC|RATECUT|RATEHIKE)")
_MACRO_SER = re.compile(r"^(KX)?(CPI|PCE|GDP|PAYROLL|NFP|JOBS|U3|UNEMP|RECESS|TNOTE|10Y|INFL|CORECPI)")
_FED_TXT = re.compile(r"\b(fed|fomc|federal reserve|interest rates?|rate cuts?|rate hikes?|powell)\b")
_MACRO_TXT = re.compile(r"\b(cpi|inflation|pce|gdp|payrolls?|unemployment|jobless|recession|treasury yields?)\b")
_ELECT_SER = re.compile(r"(ELECT|PRES|SENATE|HOUSE|GOV|MAYOR|NOMINEE|PRIMARY|POTUS|CONTROL|CONGRESS)")
_ELECT_TXT = re.compile(r"\b(elections?|elected|senate|governor|mayor|mayoral|president|presidential|nominee|"
                        r"primary|congress|parliament|prime minister|democrats?|republicans?)\b")


def _event(venue: str, market_id: str) -> str:
    mid = (market_id or "").strip()
    if venue.lower().startswith("kalshi"):
        return mid.upper().rsplit("-", 1)[0] if "-" in mid else mid.upper()
    return re.sub(r"(-\d[\w.]*)+$", "", mid.lower())[:60] or mid.lower()[:60]


def classify(venue: str, market_id: str | None, title: str | None = "") -> tuple[str, str]:
    """(category, correlated group) for a position. Groups: one per asset for crypto (all BTC strikes),
    one for all Fed/FOMC markets, one per macro series, one per FX pair, else one per event."""
    v = (venue or "").lower()
    mid = market_id or ""
    series = mid.upper().split("-")[0] if v.startswith("kalshi") else ""
    text = f"{title or ''} {mid.replace('-', ' ')}".lower()
    if v in ("solana", "token", "base", "ethereum", "evm"):
        return "crypto", f"crypto:token:{mid[:16]}"
    for asset, words in _ASSETS.items():
        if (series.startswith("KX") and asset in series[:7]) or any(re.search(rf"\b{w}\b", text) for w in words):
            return "crypto", f"crypto:{asset}"
    if re.search(r"\b(crypto|memecoin)\b", text):
        return "crypto", f"crypto:{_event(v, mid)}"
    m = _FX_PAIR.search(series) or _FX_PAIR.search(mid.upper().replace("/", ""))
    if m:
        return "fx", f"fx:{m.group(0)}"
    if _FX_TXT.search(text):
        return "fx", "fx:USD"
    if _FED_SER.search(series) or _FED_TXT.search(text):
        return "fed_macro", "fed_macro:fed"
    if _MACRO_SER.search(series) or _MACRO_TXT.search(text):
        return "fed_macro", f"fed_macro:{series or _event(v, mid)}"
    if _ELECT_SER.search(series) or _ELECT_TXT.search(text):
        return "elections", f"elections:{_event(v, mid)}"
    return "other", f"other:{_event(v, mid)}"


# ------------------------------------------------------------------ edge & Kelly

def is_pm_international(venue: str | None) -> bool:
    v = (venue or "").lower()
    return v.startswith("polymarket") and v not in ("polymarket-us", "polymarket_us", "pmus")


def fee_info(venue: str, price: float, market_id: str | None = None) -> tuple[float, bool]:
    """(taker fee per $1 contract, assumed?). Kalshi 0.07*p*(1-p); Polymarket US one exchange-wide 0.0695*p*(1-p);
    Polymarket international per market from Gamma's feeSchedule (often 0). An international market whose
    schedule is unknown falls back to the flat 0.0695 rate and is flagged as assumed."""
    from .edge_strategies import kalshi_fee, pmus_fee
    v = (venue or "").lower()
    if v.startswith("kalshi"):
        return kalshi_fee(price), False
    if not is_pm_international(v):
        return pmus_fee(price), False
    from . import pm_fees
    sched = pm_fees.lookup(market_id)
    if sched is None:
        return pmus_fee(price), True
    return pm_fees.fee(sched, price), False


def fee(venue: str, price: float, market_id: str | None = None) -> float:
    return fee_info(venue, price, market_id)[0]


def all_in_cost(venue: str, price: float, market_id: str | None = None) -> float:
    return price + fee(venue, price, market_id) + slippage()


def net_edge(venue: str, fair: float, price: float, market_id: str | None = None) -> float:
    """Side-relative expected gain per $1 contract after fee and slippage."""
    return fair - all_in_cost(venue, price, market_id)


def passes_gate(venue: str, fair: float | None, price: float | None, market_id: str | None = None) -> bool:
    from .edge_strategies import entry_buffer
    if fair is None or price is None or not (0 < price < 1):
        return False
    return net_edge(venue, fair, price, market_id) > entry_buffer()


def kelly_stake(venue: str, fair: float, price: float, bank: float, market_id: str | None = None) -> float:
    c = all_in_cost(venue, price, market_id)
    if c >= 1 or fair <= c:
        return 0.0
    return kelly_fraction() * (fair - c) / (1 - c) * bank


def legs_fee(legs: list[dict[str, Any]]) -> tuple[float, bool]:
    """Sum of per-leg entry fees (pair / parlay legs, each bought at its own price); assumed if any leg is."""
    tot, assumed = 0.0, False
    for x in legs:
        f, a = fee_info(str(x.get("venue") or ""), float(x["price"]), x.get("market_id"))
        tot, assumed = tot + f, assumed or a
    return tot, assumed


def _legs(legs_json: Any) -> list[dict[str, Any]]:
    try:
        legs = json.loads(legs_json) if isinstance(legs_json, str) else (legs_json or [])
        return [x for x in legs if isinstance(x, dict) and x.get("price") is not None]
    except (TypeError, ValueError):
        return []


def pick_fee(venue: str | None, price: float | None, market_id: str | None = None,
             legs_json: Any = None, kind: str | None = None) -> tuple[float | None, bool]:
    """Entry fee per contract for one Prediction Bot pick: a single at its market's rate; a pair / parlay as
    the sum of its legs' fees. (None, False) when there is no price."""
    legs = _legs(legs_json) if kind in ("pair", "parlay") else []
    if legs:
        return legs_fee(legs)
    if price is None or not (0 < float(price) < 1):
        return None, False
    v = "kalshi" if (venue or "").startswith("kalshi") else "polymarket"
    return fee_info(v, float(price), market_id)


# ------------------------------------------------------------------ positions & budget

@dataclass
class Position:
    bot: str
    desk: str
    venue: str
    market_id: str
    title: str
    stake_usd: float
    category: str = ""
    group: str = ""

    def __post_init__(self) -> None:
        if not self.category:
            self.category, self.group = classify(self.venue, self.market_id, self.title)


@dataclass
class Decision:
    usd: float
    reason: str
    category: str
    group: str
    edge: float | None = None
    kelly_usd: float | None = None
    cap_usd: float | None = None


def strategy_of(desk: str) -> str:
    return desk.split(":", 1)[1] if desk.startswith("strategy:") else desk


@dataclass
class Budget:
    bank: float
    positions: list[Position] = field(default_factory=list)
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    capacity: dict[str, Any] = field(default_factory=dict)
    lineage: dict[str, dict[str, Any]] = field(default_factory=dict)

    def strategy_cap(self, desk: str) -> float:
        return strategy_limits(strategy_of(desk), self.capacity, self.cap_usd())["cap_usd"]

    # -- exposure
    def total(self) -> float:
        return sum(p.stake_usd for p in self.positions)

    def group_open(self, group: str) -> float:
        return sum(p.stake_usd for p in self.positions if p.group == group)

    def cap_usd(self) -> float:
        return cap_pct() * self.bank

    def proven(self, category: str) -> bool:
        return bool(self.records.get(category, {}).get("proven"))

    def per_bet_cap(self, category: str) -> float:
        return self.cap_usd() if self.proven(category) else min(self.cap_usd(), unproven_cap_usd())

    def room(self, group: str) -> float:
        return max(0.0, min(self.cap_usd() - self.group_open(group), self.bank - self.total()))

    def _take(self, bot: str, desk: str, venue: str, market_id: str, title: str, usd: float) -> None:
        if usd > 0:
            self.positions.append(Position(bot, desk, venue, market_id, title, round(usd, 2)))

    # -- sizing
    def size(self, desk: str, venue: str, market_id: str, title: str, fair: float | None, price: float | None,
             *, bot: str = "noema", reserve: bool = True, min_usd: float = 0.5,
             capacity_usd: float | None = None, step_cap: bool = True) -> Decision:
        """Quarter-Kelly stake for a binary bet (fair/price for the side bought), drawn from the shared budget:
        min(quarter-Kelly, category per-bet cap, strategy cap = min(measured capacity, step, 5%),
        optional per-bet capacity_usd, room left in the group's 5% and in the bankroll)."""
        cat, grp = classify(venue, market_id, title)
        if fair is None or price is None or not (0 < price < 1):
            return Decision(0.0, "no_measured_edge", cat, grp)
        edge = net_edge(venue, fair, price, market_id)
        if not passes_gate(venue, fair, price, market_id):
            return Decision(0.0, "below_fee_slippage_buffer", cat, grp, edge)
        k = kelly_stake(venue, fair, price, self.bank, market_id)
        cap = self.per_bet_cap(cat)
        if step_cap:
            cap = min(cap, self.strategy_cap(desk))
        if capacity_usd is not None:
            cap = min(cap, capacity_usd)
        usd = min(k, cap, self.room(grp))
        if usd < min_usd:
            return Decision(0.0, "no_budget_room" if self.room(grp) < min_usd else "kelly_too_small", cat, grp,
                            edge, round(k, 2), round(cap, 2))
        usd = round(usd, 2)
        if reserve:
            self._take(bot, desk, venue, market_id, title, usd)
        return Decision(usd, "kelly" if usd == round(k, 2) else "capped", cat, grp, edge, round(k, 2), round(cap, 2))

    def clip(self, desk: str, venue: str, market_id: str, title: str, usd: float, *, bot: str = "noema",
             reserve: bool = True) -> Decision:
        """Cap a pre-sized stake (locked arbs, token probes) to the per-bet cap and the group/bankroll room."""
        cat, grp = classify(venue, market_id, title)
        cap = min(self.per_bet_cap(cat), self.strategy_cap(desk))
        out = round(max(0.0, min(usd, cap, self.room(grp))), 2)
        if reserve:
            self._take(bot, desk, venue, market_id, title, out)
        return Decision(out, "clipped" if out < usd else "ok", cat, grp, None, None, round(cap, 2))

    # -- published view
    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        from .edge_strategies import entry_buffer
        now = now or datetime.now(UTC)
        bots: dict[str, dict[str, Any]] = {}
        groups: dict[str, dict[str, Any]] = {}
        cats = {c: {"open_usd": 0.0, "positions": 0} for c in CATEGORIES}
        for p in self.positions:
            b = bots.setdefault(p.bot, {"open_usd": 0.0, "positions": 0, "desks": {}})
            b["open_usd"] += p.stake_usd
            b["positions"] += 1
            b["desks"][p.desk] = round(b["desks"].get(p.desk, 0.0) + p.stake_usd, 2)
            g = groups.setdefault(p.group, {"group": p.group, "category": p.category, "open_usd": 0.0,
                                            "positions": 0, "bots": {}})
            g["open_usd"] += p.stake_usd
            g["positions"] += 1
            g["bots"][p.bot] = round(g["bots"].get(p.bot, 0.0) + p.stake_usd, 2)
            cats[p.category]["open_usd"] += p.stake_usd
            cats[p.category]["positions"] += 1
        cap = round(self.cap_usd(), 2)
        for b in bots.values():
            b["open_usd"] = round(b["open_usd"], 2)
        glist = []
        for g in sorted(groups.values(), key=lambda x: -x["open_usd"]):
            g["open_usd"] = round(g["open_usd"], 2)
            g["cap_usd"] = cap
            g["room_usd"] = round(max(0.0, cap - g["open_usd"]), 2)
            g["over_cap"] = g["open_usd"] > cap + 1e-9
            glist.append(g)
        for c, v in cats.items():
            v["open_usd"] = round(v["open_usd"], 2)
            v["per_bet_cap_usd"] = round(self.per_bet_cap(c), 2)
            v["held_out"] = {k: self.records.get(c, {}).get(k) for k in ("n", "pnl_usd", "hit_rate", "brier",
                                                                          "proven")}
        total = round(self.total(), 2)
        names = [*original_strategies(), *sorted(self.lineage), *sorted(self.capacity.get("strategies", {}))]
        strategies = {}
        for n in dict.fromkeys(names):
            lim = strategy_limits(n, self.capacity, cap)
            lim["open_usd"] = round(sum(p.stake_usd for p in self.positions
                                        if p.bot == "noema" and strategy_of(p.desk) == n), 2)
            strategies[n] = lim
        for n, lim in strategies.items():
            lim.update(lineage_fields(n, self.lineage.get(n), lim))
        return {
            "schema": SCHEMA_ID, "owner": "NOEMA", "paper_only": True, "generated_at": now.isoformat(),
            "bankroll_usd": round(self.bank, 2), "open_exposure_usd": total,
            "available_usd": round(max(0.0, self.bank - total), 2),
            "cap_pct": cap_pct(), "cap_usd": cap,
            "rules": {"sizing": "min(quarter-Kelly, measured_capacity_usd, current_step_size_usd, category per-bet cap,"
                                " 5% group room, bankroll room)", "step_ladder_usd": step_ladder(),
                      "per_bet_cap_pct": cap_pct(), "per_group_cap_pct": cap_pct(), "kelly_fraction": kelly_fraction(),
                      "unproven_bet_cap_usd": unproven_cap_usd(), "heldout_min_n": heldout_min_n(),
                      "entry_buffer": entry_buffer(), "slippage": slippage(),
                      "gate": "fair - (price + fee + slippage) > entry_buffer",
                      "fees": "kalshi 0.07*p*(1-p); polymarket-us 0.0695*p*(1-p); polymarket international per-market "
                              "Gamma feeSchedule rate*p*(1-p), flat 0.0695 when unknown (fee assumed)",
                      "prediction_bot_stake": "min(quarter-Kelly, pick stake_usd, pick capacity_usd, 5% group cap),"
                                              " never below one $1 contract; planned (scaled) picks excluded"},
            "bots": bots, "groups": glist, "categories": cats, "strategies": strategies,
            "capacity_file": {k: self.capacity.get(k) for k in ("path", "status", "rejected")},
            "families": families({n: {**self.lineage.get(n, {}), **{k: strategies[n][k] for k in
                                       ("strategy_id", "parent_id", "generation")}} for n in strategies}),
        }


def _edge_strategy_names() -> list[str]:
    try:
        from .edge_strategies import STRATEGIES
        return list(STRATEGIES)
    except Exception:  # noqa: BLE001
        return []


def _ro(path: str | None) -> sqlite3.Connection | None:
    if not path or not os.path.isfile(path):
        return None
    try:
        return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    except sqlite3.Error:
        return None


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[tuple]:
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def noema_positions(conn: sqlite3.Connection) -> list[Position]:
    out = []
    for agent, ticker, price, fee_, n, title in _rows(
            conn, "SELECT t.agent_id, t.ticker, t.price, t.fee, t.contracts, COALESCE(m.title,'') FROM fs_paper_trades t"
                  " LEFT JOIN fs_markets m ON m.ticker=t.ticker WHERE t.result IS NULL"):
        out.append(Position("noema", "fast_settle", "kalshi", ticker, title, round(n * (price + fee_), 2)))
    for strategy, legs_json, size, title in _rows(
            conn, "SELECT strategy, legs_json, size_usd, COALESCE(title,'') FROM strategy_signals WHERE status='open'"):
        try:
            leg = json.loads(legs_json)[0]
        except (ValueError, IndexError, KeyError, TypeError):
            continue
        out.append(Position("noema", f"strategy:{strategy}", str(leg.get("venue") or ""),
                            str(leg.get("market_id") or ""), title, float(size or 0)))
    return out


def pick_stake(venue: str, fair: float | None, price: float | None, capacity_usd: float | None,
               bank: float | None = None, stake_usd: float | None = None, market_id: str | None = None,
               legs_json: Any = None, kind: str | None = None) -> float:
    """Exposure counted for one Prediction Bot pick: min(quarter-Kelly, posted stake_usd, capacity_usd,
    5% group cap), never below one $1 contract (the entry price)."""
    bank = bank if bank is not None else bankroll()
    if fair is None or price is None:
        return 0.0
    k = 0.0
    if 0 < price < 1:
        f, _ = pick_fee(venue, price, market_id, legs_json, kind)
        c = price + (f or 0.0) + slippage()  # parlay legs: sum of leg fees on the combined price
        from .edge_strategies import entry_buffer
        if fair - c > entry_buffer() and c < 1:
            k = kelly_fraction() * (fair - c) / (1 - c) * bank
    usd = min(k, cap_pct() * bank, capacity_usd or float("inf"), stake_usd or float("inf"))
    return round(max(usd, float(price)), 2)


def pick_key(kind: str | None, venue: str | None, market_id: str | None, quote_mid: str | None,
             market: str | None, side: str | None, legs_json: str | None) -> tuple:
    """Identity of a pick across posts: same market + side (or the same pair / parlay legs). A later post with
    the same key replaces the earlier version (corrections)."""
    kind = kind or "single"
    if kind in ("pair", "parlay") and legs_json:
        try:
            legs = json.loads(legs_json)
            return (kind, tuple(sorted((x["venue"], str(x.get("market_id") or x["market"]).strip().lower(),
                                        x["side"]) for x in legs)))
        except (ValueError, KeyError, TypeError):
            pass
    return (kind, venue, str(quote_mid or market_id or market or "").strip().lower(), side)


def latest_picks(rows: list[dict]) -> list[dict]:
    """Keep only the newest version of each pick_key (rows need opened_at/post order + key fields)."""
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: (str(r.get("opened_at") or ""), str(r.get("pick_id") or "")), reverse=True):
        k = pick_key(r.get("kind"), r.get("venue"), r.get("market_id"), r.get("quote_market_id"), r.get("market"),
                     r.get("side"), r.get("legs_json"))
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


_NOTE_STAKE = re.compile(r"stake \$([0-9,.]+)", re.IGNORECASE)


def note_stake(note: str | None) -> float | None:
    """Stake parsed from a pick note ("stake $4.78, cap $184"); None when absent or not positive."""
    m = _NOTE_STAKE.search(note or "")
    try:
        v = float(m.group(1).replace(",", "").rstrip(".")) if m else None
    except ValueError:
        return None
    return v if v is not None and v > 0 else None


def prediction_positions(pred_db: str | None, budget: Budget) -> list[Position]:
    """Open Prediction Bot picks: latest version of each pick only, hidden and planned (scaled) picks excluded.
    A pair / parlay counts once, grouped with its first leg's market (its correlated exposure)."""
    k = _ro(pred_db)
    if k is None:
        return []
    out = []
    try:
        cols = {r[1] for r in _rows(k, "PRAGMA table_info(prediction_picks)")}
        names = ("pick_id", "opened_at", "venue", "market", "market_id", "entry_price", "fair", "side",
                 "capacity_usd", "stake_usd", "legs_json", "kind", "quote_market_id", "note")
        sel = ", ".join(n if n in cols else f"NULL AS {n}" for n in names)
        hid = " AND hidden=0" if "hidden" in cols else ""
        rows = [dict(zip(names, r)) for r in _rows(
            k, f"SELECT {sel} FROM prediction_picks WHERE status IN ('open','settled','planned','void'){hid}")]
        for r in rows:  # older posts carry the stake only in the note ("stake $4.78, cap $184")
            if r["stake_usd"] is None:
                r["stake_usd"] = note_stake(r["note"])
        k2 = latest_picks(rows)
        open_ids = {r[0] for r in _rows(k, "SELECT pick_id FROM prediction_picks WHERE status='open'")}
        for r in k2:
            if r["pick_id"] not in open_ids:
                continue
            v = "kalshi" if r["venue"] == "kalshi" else "polymarket"
            gv, gmid, gtitle = v, r["quote_market_id"] or r["market_id"] or r["market"], r["market"]
            if r["legs_json"]:
                try:
                    leg = json.loads(r["legs_json"])[0]
                    gv = "kalshi" if leg["venue"] == "kalshi" else "polymarket"
                    gmid, gtitle = leg.get("market_id") or leg["market"], leg["market"]
                except (ValueError, KeyError, IndexError, TypeError):
                    pass
            if r["kind"] == "pair":  # hedged: counted at the posted stake (capped), not Kelly
                usd = min(r["stake_usd"] or 1.0, r["capacity_usd"] or float("inf"), cap_pct() * budget.bank)
                stake = round(max(usd, 1.0), 2)
            else:
                stake = pick_stake(v, r["fair"], r["entry_price"], r["capacity_usd"], budget.bank, r["stake_usd"],
                                   r["quote_market_id"] or r["market_id"], r["legs_json"], r["kind"])
            out.append(Position("prediction_bot", "picks", gv, gmid, gtitle, stake))
    finally:
        k.close()
    return out


def category_records(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Held-out (forward, settled) record of NOEMA's binary paper bets per category."""
    acc: dict[str, dict[str, Any]] = {c: {"n": 0, "wins": 0, "pnl_usd": 0.0, "staked_usd": 0.0, "brier_sum": 0.0,
                                          "brier_n": 0} for c in CATEGORIES}

    def add(cat: str, pnl: float, staked: float, brier: float | None) -> None:
        a = acc[cat]
        a["n"] += 1
        a["wins"] += 1 if pnl > 0 else 0
        a["pnl_usd"] += pnl
        a["staked_usd"] += staked
        if brier is not None:
            a["brier_sum"] += brier
            a["brier_n"] += 1

    for ticker, title, prob, result, pnl, price, fee_, n in _rows(
            conn, "SELECT t.ticker, COALESCE(m.title,''), t.prob, t.result, t.pnl, t.price, t.fee, t.contracts"
                  " FROM fs_paper_trades t LEFT JOIN fs_markets m ON m.ticker=t.ticker"
                  " WHERE t.result IN ('yes','no') AND t.pnl IS NOT NULL"):
        y = 1.0 if result == "yes" else 0.0
        add(classify("kalshi", ticker, title)[0], float(pnl), n * (price + fee_), (float(prob) - y) ** 2)
    for legs_json, title, pnl, size, brier in _rows(
            conn, "SELECT s.legs_json, COALESCE(s.title,''), s.pnl_usd, s.size_usd, r.brier FROM strategy_signals s"
                  " LEFT JOIN accuracy_resolutions r ON r.forecast_id=s.forecast_id"
                  " WHERE s.status='closed' AND s.pnl_usd IS NOT NULL"):
        try:
            leg = json.loads(legs_json)[0]
        except (ValueError, IndexError, KeyError, TypeError):
            continue
        if leg.get("side") not in ("yes", "no"):
            continue  # token probes are not prediction bets
        add(classify(str(leg.get("venue") or ""), str(leg.get("market_id") or ""), title)[0], float(pnl),
            float(size or 0), None if brier is None else float(brier))
    out = {}
    for c, a in acc.items():
        out[c] = {"category": c, "label": CATEGORY_LABELS[c], "n": a["n"], "pnl_usd": round(a["pnl_usd"], 2),
                  "staked_usd": round(a["staked_usd"], 2),
                  "roi": round(a["pnl_usd"] / a["staked_usd"], 4) if a["staked_usd"] else None,
                  "hit_rate": round(a["wins"] / a["n"], 4) if a["n"] else None,
                  "brier": round(a["brier_sum"] / a["brier_n"], 4) if a["brier_n"] else None,
                  "proven": a["n"] >= heldout_min_n() and a["pnl_usd"] > 0}
    return out


def load_budget(conn: sqlite3.Connection, pred_db: str | None = None, capacity: dict[str, Any] | None = None
                ) -> Budget:
    b = Budget(bankroll(), [], category_records(conn), capacity if capacity is not None else load_capacity())
    b.positions = noema_positions(conn)
    b.positions += prediction_positions(pred_db if pred_db is not None else default_pred_db(), b)
    b.lineage = lineage_rows(conn)
    return b


# ------------------------------------------------------------------ strategy lineage (family tree)

LINEAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_lineage (strategy_id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,
    parent_id TEXT, generation INTEGER NOT NULL DEFAULT 0, born_at TEXT NOT NULL, retired_at TEXT, note TEXT);
"""
LIFECYCLE = ("active", "scaling", "retired")


def strategy_id(name: str) -> str:
    return f"noema:{name}"


def original_strategies() -> list[str]:
    return ["fast_settle", *_edge_strategy_names()]


def _first_activity(conn: sqlite3.Connection) -> dict[str, str]:
    out = {r[0]: r[1] for r in _rows(conn, "SELECT strategy, MIN(at) FROM strategy_signals GROUP BY strategy")}
    fs = _rows(conn, "SELECT MIN(at) FROM fs_paper_trades")
    if fs and fs[0][0]:
        out["fast_settle"] = fs[0][0]
    return out


def register_strategy(conn: sqlite3.Connection, name: str, parent: str | None = None, *,
                      born_at: str | None = None, note: str | None = None) -> dict[str, Any]:
    """Add a strategy to the family tree (idempotent). A variant names its parent strategy; generation = parent + 1."""
    conn.executescript(LINEAGE_SCHEMA)
    gen, pid = 0, None
    if parent:
        row = conn.execute("SELECT strategy_id, generation FROM strategy_lineage WHERE name=?", (parent,)).fetchone()
        if row is None:
            raise ValueError(f"unknown parent strategy {parent!r}")
        pid, gen = row[0], int(row[1]) + 1
    conn.execute("INSERT OR IGNORE INTO strategy_lineage (strategy_id, name, parent_id, generation, born_at, note) "
                 "VALUES (?,?,?,?,?,?)", (strategy_id(name), name, pid, gen,
                                          born_at or datetime.now(UTC).isoformat(), note))
    return {"strategy_id": strategy_id(name), "parent_id": pid, "generation": gen}


def seed_lineage(conn: sqlite3.Connection, now: datetime | None = None) -> None:
    """Writers (NOEMA only) record originals: parent null, generation 0, born at first paper activity."""
    now = now or datetime.now(UTC)
    first = _first_activity(conn)
    for n in original_strategies():
        register_strategy(conn, n, born_at=first.get(n) or now.isoformat())


def _pnl_by_strategy(conn: sqlite3.Connection) -> dict[str, tuple[float, int]]:
    out = {r[0]: (float(r[1] or 0), int(r[2])) for r in _rows(
        conn, "SELECT strategy, SUM(pnl_usd), COUNT(*) FROM strategy_signals WHERE status='closed' "
              "AND pnl_usd IS NOT NULL GROUP BY strategy")}
    fs = _rows(conn, "SELECT SUM(pnl), COUNT(*) FROM fs_paper_trades WHERE pnl IS NOT NULL")
    if fs and fs[0][1]:
        out["fast_settle"] = (float(fs[0][0] or 0), int(fs[0][1]))
    return out


def lineage_rows(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Read-only: stored lineage + derived rows for originals not yet recorded, with paper P&L and allocator status."""
    first = _first_activity(conn)
    alloc = {r[0]: (r[1], r[2]) for r in _rows(conn, "SELECT strategy, status, reason FROM strategy_alloc")}
    pnl = _pnl_by_strategy(conn)
    out: dict[str, dict[str, Any]] = {}
    for sid, name, pid, gen, born, retired in _rows(
            conn, "SELECT strategy_id, name, parent_id, generation, born_at, retired_at FROM strategy_lineage"):
        out[name] = {"strategy_id": sid, "parent_id": pid, "generation": int(gen), "born_at": born,
                     "retired_at": retired}
    for n in original_strategies():
        out.setdefault(n, {"strategy_id": strategy_id(n), "parent_id": None, "generation": 0,
                           "born_at": first.get(n), "retired_at": None})
    for n, r in out.items():
        r["alloc_status"], r["alloc_reason"] = alloc.get(n, (None, None))
        r["pnl_usd"], r["n"] = round(pnl.get(n, (0.0, 0))[0], 2), pnl.get(n, (0.0, 0))[1]
    return out


def lineage_fields(name: str, row: dict[str, Any] | None, limits: dict[str, Any] | None = None) -> dict[str, Any]:
    """strategy_id, parent_id, generation, born_at, status (active | scaling | retired)."""
    row = row or {"strategy_id": strategy_id(name), "parent_id": None, "generation": 0, "born_at": None}
    limits = limits or {}
    if row.get("retired_at") or row.get("alloc_status") == "benched":
        status, why = "retired", row.get("alloc_reason") or "retired"
    elif limits.get("measured_capacity_usd") is not None and \
            (limits.get("current_step_size_usd") or 0) > step_ladder()[0]:
        status, why = "scaling", f"step ${limits['current_step_size_usd']} (measured capacity)"
    else:
        status, why = "active", "minimum step until capacity is measured"
    return {"strategy_id": row["strategy_id"], "parent_id": row.get("parent_id"),
            "generation": int(row.get("generation") or 0), "born_at": row.get("born_at"),
            "status": status, "status_reason": why}


def families(lineage: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Combined paper P&L per family tree (root original + every descendant)."""
    by_id = {r["strategy_id"]: (n, r) for n, r in lineage.items()}

    def root(sid: str) -> str:
        seen = set()
        while sid in by_id and by_id[sid][1].get("parent_id") and sid not in seen:
            seen.add(sid)
            sid = by_id[sid][1]["parent_id"]
        return sid

    fam: dict[str, dict[str, Any]] = {}
    for r in lineage.values():
        rid = root(r["strategy_id"])
        f = fam.setdefault(rid, {"family_id": rid, "root": by_id.get(rid, (rid,))[0], "members": [],
                                 "generations": 0, "pnl_usd": 0.0, "n": 0})
        f["members"].append(r["strategy_id"])
        f["generations"] = max(f["generations"], int(r.get("generation") or 0) + 1)
        f["pnl_usd"] += r.get("pnl_usd") or 0.0
        f["n"] += r.get("n") or 0
    for f in fam.values():
        f["pnl_usd"] = round(f["pnl_usd"], 2)
        f["members"].sort()
    return sorted(fam.values(), key=lambda f: -f["pnl_usd"])


def default_pred_db() -> str | None:
    try:
        from .prediction_inbox import default_db
        return default_db(os.getenv("NOEMA_DB_PATH"))
    except Exception:  # noqa: BLE001
        return None


def category_table(conn: sqlite3.Connection, budget: Budget | None = None) -> list[dict[str, Any]]:
    budget = budget or load_budget(conn)
    snap = budget.snapshot()
    rows = []
    for c in CATEGORIES:
        r = dict(budget.records.get(c) or category_records(conn)[c])
        r["open_usd"] = snap["categories"][c]["open_usd"]
        r["open_positions"] = snap["categories"][c]["positions"]
        r["per_bet_cap_usd"] = snap["categories"][c]["per_bet_cap_usd"]
        rows.append(r)
    return rows


def write_budget(budget: Budget, path: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Atomic tmp+rename, mode 0644 (world-readable; only NOEMA writes it)."""
    snap = budget.snapshot(now)
    p = Path(path or budget_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".risk_budget.", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(snap, f, indent=1, sort_keys=False)
        os.chmod(tmp, 0o644)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return snap


def read_budget(path: str | None = None) -> dict[str, Any] | None:
    try:
        return json.loads(Path(path or budget_path()).read_text())
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------ post-bet price moves

MOVES_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_bet_moves (bot TEXT NOT NULL, bet_id TEXT NOT NULL, venue TEXT NOT NULL,
    market_id TEXT, label TEXT, side TEXT NOT NULL, entry_price REAL NOT NULL, opened_at TEXT NOT NULL,
    offset_s INTEGER NOT NULL, due_at TEXT NOT NULL, captured_at TEXT, price REAL, move REAL, status TEXT,
    source TEXT, PRIMARY KEY (bot, bet_id, offset_s));
CREATE INDEX IF NOT EXISTS paper_bet_moves_due ON paper_bet_moves(captured_at, due_at);
"""


@dataclass
class Bet:
    bot: str
    bet_id: str
    venue: str
    market_id: str | None
    label: str
    side: str  # YES | NO
    entry_price: float  # price paid for that side
    opened_at: datetime


def _dt(v: str) -> datetime:
    d = datetime.fromisoformat(str(v))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def schedule_moves(conn: sqlite3.Connection, bets: Iterable[Bet]) -> int:
    conn.executescript(MOVES_SCHEMA)
    n = 0
    for b in bets:
        opened = (b.opened_at if b.opened_at.tzinfo else b.opened_at.replace(tzinfo=UTC)).astimezone(UTC)
        b = Bet(b.bot, b.bet_id, b.venue, b.market_id, b.label, b.side, b.entry_price, opened)
        for off in MOVE_OFFSETS:
            cur = conn.execute(
                "INSERT OR IGNORE INTO paper_bet_moves (bot, bet_id, venue, market_id, label, side, entry_price,"
                " opened_at, offset_s, due_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (b.bot, b.bet_id, b.venue, b.market_id, b.label[:200], b.side.upper(), b.entry_price,
                 b.opened_at.isoformat(), off, (b.opened_at + timedelta(seconds=off)).isoformat()))
            n += cur.rowcount
    return n


def _late_ok(off: int, late_s: float) -> bool:
    return late_s <= max(900.0, 0.05 * off)


Resolver = Callable[[str, str | None], tuple[str, datetime] | None]


def capture_moves(conn: sqlite3.Connection, now: datetime, quotes: Any, *, known: Resolver | None = None,
                  limit: int = 200) -> dict[str, int]:
    """Fill due 1h/1d/1w marks. A market already resolved by the due time marks at 1/0 ('settled');
    otherwise a live public quote while on time ('ok'), else 'missed'. Reads first, then one write."""
    conn.executescript(MOVES_SCHEMA)
    due = conn.execute("SELECT bot, bet_id, venue, market_id, label, side, entry_price, offset_s, due_at FROM"
                       " paper_bet_moves WHERE captured_at IS NULL AND due_at <= ? ORDER BY due_at LIMIT ?",
                       (now.isoformat(), limit)).fetchall()
    if conn.in_transaction:
        conn.commit()
    out = {"settled": 0, "ok": 0, "missed": 0, "pending": 0}
    writes = []
    for bot, bet_id, venue, mid, label, side, entry, off, due_at in due:
        d = _dt(due_at)
        late = (now - d).total_seconds()
        if late < 0:  # legacy non-UTC due_at strings compare wrong lexically; never capture early
            out["pending"] += 1
            continue
        res = known(venue, mid) if known else None
        price = status = source = None
        if res and res[1] <= d:
            price, status, source = (1.0 if (res[0] == "yes") == (side == "YES") else 0.0), "settled", "resolution"
        elif _late_ok(off, late):
            q = quotes.quote(venue, mid, label) if quotes is not None else None
            if q is not None and q.result in ("yes", "no") and res is None:
                price, status, source = q.side_price(side), "settled", q.source
            elif q is not None and q.side_price(side) is not None:
                price, status, source = q.side_price(side), "ok", q.source
            else:
                out["pending"] += 1
                continue
        else:
            status = "missed"
        out[status] += 1
        writes.append((now.isoformat(), price, None if price is None else round(price - entry, 4), status, source,
                       bot, bet_id, off))
    if writes:
        conn.executemany("UPDATE paper_bet_moves SET captured_at=?, price=?, move=?, status=?, source=? WHERE bot=?"
                         " AND bet_id=? AND offset_s=?", writes)
        conn.commit()
    return out


def noema_bets(conn: sqlite3.Connection, since: datetime) -> list[Bet]:
    bets = []
    for tid, at, ticker, side, price, title in _rows(
            conn, "SELECT t.id, t.at, t.ticker, t.side, t.price, COALESCE(m.title,'') FROM fs_paper_trades t"
                  " LEFT JOIN fs_markets m ON m.ticker=t.ticker WHERE t.at >= ?", (since.isoformat(),)):
        bets.append(Bet("noema:fast_settle", f"fs:{tid}", "kalshi", ticker, title, side.upper(), float(price),
                        _dt(at)))
    for sid, at, strategy, legs_json, title in _rows(
            conn, "SELECT id, at, strategy, legs_json, COALESCE(title,'') FROM strategy_signals"
                  " WHERE status IN ('open','closed') AND at >= ?", (since.isoformat(),)):
        try:
            leg = json.loads(legs_json)[0]
        except (ValueError, IndexError, KeyError, TypeError):
            continue
        if leg.get("side") not in ("yes", "no"):
            continue
        bets.append(Bet(f"noema:strategy:{strategy}", f"strat:{sid}", str(leg["venue"]), str(leg["market_id"]),
                        title, str(leg["side"]).upper(), float(leg["price"]), _dt(at)))
    return bets


def noema_resolution(conn: sqlite3.Connection) -> Resolver:
    """Known resolution (result, resolved-at) from NOEMA's own tables: fs_markets then outcomes."""
    def f(venue: str, mid: str | None) -> tuple[str, datetime] | None:
        if not mid:
            return None
        if venue.startswith("kalshi"):
            r = _rows(conn, "SELECT result, close_time FROM fs_markets WHERE ticker=? AND result IN ('yes','no')",
                      (mid,))
            if r:
                return r[0][0], _dt(r[0][1])
        r = _rows(conn, "SELECT outcome_yes, resolved_at FROM outcomes WHERE market_id=? AND outcome_yes IS NOT NULL"
                        " AND resolved_at IS NOT NULL LIMIT 1", (mid,))
        if r:
            return ("yes" if float(r[0][0]) >= 0.5 else "no"), _dt(r[0][1])
        return None
    return f


def moves_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    out = []
    for off, n, avg, ups, miss in _rows(
            conn, "SELECT offset_s, COUNT(*), AVG(move), SUM(move>0), SUM(status='missed') FROM paper_bet_moves"
                  " WHERE captured_at IS NOT NULL GROUP BY offset_s ORDER BY offset_s"):
        out.append({"offset_s": off, "n": n, "avg_move": None if avg is None else round(avg, 4),
                    "favourable": ups, "missed": miss})
    return out
