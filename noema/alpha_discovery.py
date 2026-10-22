"""Alpha discovery for mid/small caps and new tokens. PAPER ONLY.

Candidates come from the culture crawler's token signals (DexScreener boosts and
profiles, Jupiter trending, pump.fun). Each one is priced from DexScreener pairs and
must pass every hard safety screen (liquidity, volume, age, LP locked/burned,
mint/freeze revoked, top-10 concentration, honeypot/sell tax, dev holdings) before
it may be ranked. Ranking blends momentum, buzz acceleration and liquidity growth.

Entries are sized like the capital guard (tier fraction of equity, 75% rising
floor, 10% daily stop) with memecoins at half size and a smaller total exposure
cap than mid caps. Every position carries a take-profit ladder, a trailing stop and
a time stop. Entries are recorded in the accuracy ledger (agent ``alpha``) and
screened-out tokens as ``veto`` so the engine scores both the picks and the screens.

There is no live path in this module. Passing candidates are written to a
*proposed* allowlist that always requires owner review; live trading still has to
go through the autonomy ladder, the capital guard and the signer's own pins.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .capital_guard import TIER_MAX_FRACTION, daily_stop_fraction, floor_fraction
from .news_crawler import sanitize

LIVE_SUPPORTED = False  # deliberate: alpha is paper-only until promoted through the ladder
AGENT_ID = "alpha"
GOPLUS_CHAINS = {"ethereum": "1", "base": "8453", "bsc": "56", "polygon": "137", "arbitrum": "42161"}
DEX_BATCH = 30


@dataclass(frozen=True)
class ScreenLimits:
    min_liquidity_usd: float = 50_000
    min_volume_24h_usd: float = 100_000
    min_age_hours: float = 6.0
    min_lp_locked_pct: float = 90.0
    max_top10_pct: float = 30.0
    max_sell_tax: float = 0.05
    max_dev_pct: float = 5.0
    max_drawdown_24h_pct: float = -50.0

    @classmethod
    def from_env(cls) -> ScreenLimits:
        def f(name: str, default: float) -> float:
            try:
                return float(os.getenv(name, str(default)))
            except ValueError:
                return default
        d = cls()
        return cls(f("NOEMA_ALPHA_MIN_LIQUIDITY_USD", d.min_liquidity_usd),
                   f("NOEMA_ALPHA_MIN_VOLUME_24H_USD", d.min_volume_24h_usd),
                   f("NOEMA_ALPHA_MIN_AGE_HOURS", d.min_age_hours),
                   f("NOEMA_ALPHA_MIN_LP_LOCKED_PCT", d.min_lp_locked_pct),
                   f("NOEMA_ALPHA_MAX_TOP10_PCT", d.max_top10_pct),
                   f("NOEMA_ALPHA_MAX_SELL_TAX", d.max_sell_tax),
                   f("NOEMA_ALPHA_MAX_DEV_PCT", d.max_dev_pct))


@dataclass(frozen=True)
class ExitRules:
    take_profit: tuple[tuple[float, float], ...] = ((0.30, 1 / 3), (0.75, 1 / 2), (1.50, 1.0))
    hard_stop: float = -0.15
    trailing_stop: float = 0.20  # from peak, armed after the first take-profit
    time_stop_hours: float = 24.0
    stale_after_hours: float = 6.0  # exit if not up at least stale_min_return by then
    stale_min_return: float = 0.05

    def as_dict(self) -> dict[str, Any]:
        return {"take_profit_ladder": [{"at_return": a, "sell_fraction_of_remaining": round(b, 3)}
                                       for a, b in self.take_profit],
                "hard_stop": self.hard_stop, "trailing_stop_from_peak": self.trailing_stop,
                "time_stop_hours": self.time_stop_hours,
                "stale_exit": {"after_hours": self.stale_after_hours, "min_return": self.stale_min_return}}


EXITS = ExitRules()
MEME_MCAP_USD = 50_000_000
MAJOR_MCAP_USD = 2_000_000_000
MEME_SIZE_MULT = 0.5
MEME_EXPOSURE_CAP = 0.10
MID_EXPOSURE_CAP = 0.40


@dataclass
class Candidate:
    chain: str
    address: str
    symbol: str | None
    price: float | None = None
    liquidity: float | None = None
    volume_24h: float | None = None
    mcap: float | None = None
    age_hours: float | None = None
    h1: float | None = None
    h6: float | None = None
    h24: float | None = None
    buys_h1: int = 0
    sells_h1: int = 0
    buzz_velocity: float = 0.0
    liquidity_growth: float = 0.0
    screens: dict[str, dict[str, Any]] = field(default_factory=dict)
    score: float = 0.0
    volume_h1: float | None = None
    buys_h24: int = 0
    sells_h24: int = 0
    catalyst: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def has_catalyst(self) -> bool:
        """Safety screens make a coin *tradeable*; only a real catalyst makes it a buy."""
        if os.getenv("NOEMA_ALPHA_CATALYST_GATE", "1") == "0":
            return True
        return bool(self.catalyst) and all(s["pass"] for s in self.catalyst.values())

    @property
    def passed(self) -> bool:
        return bool(self.screens) and all(s["pass"] for s in self.screens.values())

    @property
    def cap_class(self) -> str:
        m = self.mcap or 0
        return "major" if m >= MAJOR_MCAP_USD else "mid" if m >= MEME_MCAP_USD else "meme"


def _f(v: Any) -> float | None:
    try:
        out = float(v)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def apply_pair(c: Candidate, pairs: list[dict[str, Any]], now: datetime) -> None:
    best = max((p for p in pairs if isinstance(p, dict)),
               key=lambda p: _f((p.get("liquidity") or {}).get("usd")) or 0, default=None)
    if not best:
        return
    c.symbol = c.symbol or sanitize((best.get("baseToken") or {}).get("symbol"), 16) or None
    c.price = _f(best.get("priceUsd"))
    c.liquidity = _f((best.get("liquidity") or {}).get("usd"))
    c.volume_24h = _f((best.get("volume") or {}).get("h24"))
    c.mcap = _f(best.get("marketCap")) or _f(best.get("fdv"))
    pc = best.get("priceChange") or {}
    c.h1, c.h6, c.h24 = _f(pc.get("h1")), _f(pc.get("h6")), _f(pc.get("h24"))
    tx = (best.get("txns") or {}).get("h1") or {}
    c.buys_h1, c.sells_h1 = int(_f(tx.get("buys")) or 0), int(_f(tx.get("sells")) or 0)
    tx24 = (best.get("txns") or {}).get("h24") or {}
    c.buys_h24, c.sells_h24 = int(_f(tx24.get("buys")) or 0), int(_f(tx24.get("sells")) or 0)
    c.volume_h1 = _f((best.get("volume") or {}).get("h1"))
    created = _f(best.get("pairCreatedAt"))
    if created:
        c.age_hours = max(0.0, (now.timestamp() - created / 1000) / 3600)


def _screen(ok: bool, value: Any, rule: str) -> dict[str, Any]:
    return {"pass": bool(ok), "value": value, "rule": rule}


def market_screens(c: Candidate, lim: ScreenLimits) -> dict[str, dict[str, Any]]:
    return {
        "liquidity": _screen((c.liquidity or 0) >= lim.min_liquidity_usd, c.liquidity,
                             f">= ${lim.min_liquidity_usd:,.0f}"),
        "volume_24h": _screen((c.volume_24h or 0) >= lim.min_volume_24h_usd, c.volume_24h,
                              f">= ${lim.min_volume_24h_usd:,.0f}"),
        "age": _screen(c.age_hours is not None and c.age_hours >= lim.min_age_hours,
                       None if c.age_hours is None else round(c.age_hours, 1), f">= {lim.min_age_hours:g}h"),
        "not_collapsing": _screen(c.h24 is not None and c.h24 > lim.max_drawdown_24h_pct, c.h24,
                                  f"24h change > {lim.max_drawdown_24h_pct:g}%"),
    }


def solana_screens(report: dict[str, Any] | None, lim: ScreenLimits) -> dict[str, dict[str, Any]]:
    """RugCheck report -> on-chain safety screens. Missing data fails closed."""
    if not isinstance(report, dict) or not report.get("mint"):
        return {"rugcheck": _screen(False, None, "RugCheck report required")}
    known = report.get("knownAccounts") or {}
    pool_owners = {k for k, v in known.items() if isinstance(v, dict) and str(v.get("type", "")).upper() in
                   {"AMM", "LOCKER", "BURN"}}
    markets = [m for m in report.get("markets") or [] if isinstance(m, dict)]
    for m in markets:
        for key in ("liquidityA", "liquidityB", "pubkey"):
            if m.get(key):
                pool_owners.add(str(m[key]))
    holders = [h for h in report.get("topHolders") or [] if isinstance(h, dict)]
    top10 = round(sum(sorted([_f(h.get("pct")) or 0 for h in holders if h.get("owner") not in pool_owners
                              and h.get("address") not in pool_owners], reverse=True)[:10]), 2) if holders else None
    lp_locked = max((_f((m.get("lp") or {}).get("lpLockedPct")) or 0 for m in markets), default=None)
    supply = _f((report.get("token") or {}).get("supply"))
    creator_bal = _f(report.get("creatorBalance")) or 0
    dev_pct = round(100 * creator_bal / supply, 3) if supply else (0.0 if creator_bal == 0 else None)
    fee = _f((report.get("transferFee") or {}).get("pct")) or 0.0
    dangers = [sanitize(r.get("name"), 60) for r in report.get("risks") or []
               if isinstance(r, dict) and r.get("level") == "danger"]
    return {
        "mint_authority_revoked": _screen(report.get("mintAuthority") is None, report.get("mintAuthority") is None,
                                          "mint authority must be null"),
        "freeze_authority_revoked": _screen(report.get("freezeAuthority") is None,
                                            report.get("freezeAuthority") is None, "freeze authority must be null"),
        "lp_locked_or_burned": _screen(lp_locked is not None and lp_locked >= lim.min_lp_locked_pct, lp_locked,
                                       f">= {lim.min_lp_locked_pct:g}% locked/burned"),
        "top10_concentration": _screen(top10 is not None and top10 <= lim.max_top10_pct, top10,
                                       f"<= {lim.max_top10_pct:g}% excl. pools"),
        "sell_tax_honeypot": _screen(not report.get("rugged") and fee <= lim.max_sell_tax * 100 and not dangers,
                                     {"transfer_fee_pct": fee, "rugged": bool(report.get("rugged")),
                                      "danger_risks": dangers}, "no transfer tax, not rugged, no danger risks"),
        "dev_wallet": _screen(dev_pct is not None and dev_pct <= lim.max_dev_pct, dev_pct,
                              f"creator holds <= {lim.max_dev_pct:g}%"),
    }


def evm_screens(sec: dict[str, Any] | None, lim: ScreenLimits) -> dict[str, dict[str, Any]]:
    """GoPlus token_security -> safety screens. Missing data fails closed."""
    if not isinstance(sec, dict) or not sec:
        return {"goplus": _screen(False, None, "GoPlus report required")}

    def flag(k: str) -> bool:
        return str(sec.get(k, "")) == "1"
    sell, buy = _f(sec.get("sell_tax")), _f(sec.get("buy_tax"))
    lp = [h for h in sec.get("lp_holders") or [] if isinstance(h, dict)]
    lp_locked = round(100 * sum((_f(h.get("percent")) or 0) for h in lp
                                if str(h.get("is_locked")) == "1"
                                or str(h.get("address", "")).lower().startswith("0x000000000000000000000000000000000000dead")
                                or str(h.get("address", "")) == "0x0000000000000000000000000000000000000000"), 2) if lp else None
    holders = [h for h in sec.get("holders") or [] if isinstance(h, dict)
               and str(h.get("is_locked")) != "1" and "pool" not in str(h.get("tag", "")).lower()
               and "lp" not in str(h.get("tag", "")).lower()]
    top10 = round(100 * sum(sorted([_f(h.get("percent")) or 0 for h in holders], reverse=True)[:10]), 2) \
        if sec.get("holders") else None
    dev = _f(sec.get("creator_percent"))
    return {
        "mint_authority_revoked": _screen(not flag("is_mintable") and not flag("hidden_owner")
                                          and not flag("can_take_back_ownership"),
                                          {"mintable": flag("is_mintable"), "hidden_owner": flag("hidden_owner")},
                                          "not mintable, no hidden/reclaimable owner"),
        "freeze_authority_revoked": _screen(not flag("transfer_pausable") and not flag("is_blacklisted"),
                                            {"pausable": flag("transfer_pausable"), "blacklist": flag("is_blacklisted")},
                                            "no pause or blacklist"),
        "lp_locked_or_burned": _screen(lp_locked is not None and lp_locked >= lim.min_lp_locked_pct, lp_locked,
                                       f">= {lim.min_lp_locked_pct:g}% locked/burned"),
        "top10_concentration": _screen(top10 is not None and top10 <= lim.max_top10_pct, top10,
                                       f"<= {lim.max_top10_pct:g}% excl. pools"),
        "sell_tax_honeypot": _screen(not flag("is_honeypot") and not flag("cannot_sell_all")
                                     and sell is not None and sell <= lim.max_sell_tax
                                     and (buy or 0) <= lim.max_sell_tax,
                                     {"honeypot": flag("is_honeypot"), "sell_tax": sell, "buy_tax": buy},
                                     f"not honeypot, taxes <= {lim.max_sell_tax:.0%}"),
        "dev_wallet": _screen(dev is not None and dev * 100 <= lim.max_dev_pct,
                              None if dev is None else round(dev * 100, 3), f"creator holds <= {lim.max_dev_pct:g}%"),
    }


def rank_score(c: Candidate) -> float:
    mom = 0.5 * math.tanh((c.h1 or 0) / 20) + 0.5 * math.tanh((c.h6 or 0) / 40)
    flow = (c.buys_h1 - c.sells_h1) / max(1, c.buys_h1 + c.sells_h1)
    buzz = math.tanh(c.buzz_velocity)
    liq = math.tanh(c.liquidity_growth * 2)
    return round(0.35 * mom + 0.15 * flow + 0.30 * buzz + 0.20 * liq, 4)


# -- paper book --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS alpha_book (id INTEGER PRIMARY KEY CHECK (id=1), starting_usd REAL, cash_usd REAL,
    peak_equity REAL, floor REAL, day TEXT, day_start_equity REAL, halted INTEGER DEFAULT 0, halt_reason TEXT,
    updated_at TEXT);
CREATE TABLE IF NOT EXISTS alpha_positions (id INTEGER PRIMARY KEY AUTOINCREMENT, chain TEXT, address TEXT,
    symbol TEXT, cap_class TEXT, opened_at TEXT, entry_price REAL, qty REAL, cost_usd REAL, peak_price REAL,
    tp_hits INTEGER DEFAULT 0, realized_usd REAL DEFAULT 0, status TEXT DEFAULT 'open', closed_at TEXT,
    close_reason TEXT, last_price REAL, score REAL, exits_json TEXT, forecast_id INTEGER);
CREATE TABLE IF NOT EXISTS alpha_candidates (chain TEXT, address TEXT, symbol TEXT, evaluated_at TEXT,
    passed INTEGER, score REAL, cap_class TEXT, price REAL, liquidity REAL, volume_24h REAL, mcap REAL,
    age_hours REAL, h1 REAL, h6 REAL, h24 REAL, buzz_velocity REAL, liquidity_growth REAL, screens_json TEXT,
    PRIMARY KEY (chain, address));
CREATE TABLE IF NOT EXISTS alpha_liquidity (chain TEXT, address TEXT, at TEXT, liquidity REAL);
CREATE TABLE IF NOT EXISTS alpha_allowlist (chain TEXT, address TEXT, symbol TEXT, added_at TEXT,
    owner_reviewed INTEGER NOT NULL DEFAULT 0, screens_json TEXT, PRIMARY KEY (chain, address));
CREATE TABLE IF NOT EXISTS alpha_events (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, event TEXT, payload_json TEXT);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def paper_bankroll() -> float:
    try:
        return max(10.0, float(os.getenv("NOEMA_ALPHA_PAPER_BANKROLL_USD", "1000")))
    except ValueError:
        return 1000.0


def _event(conn: sqlite3.Connection, now: datetime, event: str, **payload: Any) -> None:
    conn.execute("INSERT INTO alpha_events (at, event, payload_json) VALUES (?,?,?)",
                 (now.isoformat(), event, json.dumps(payload, default=str)))


def book_state(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    row = conn.execute("SELECT starting_usd, cash_usd, peak_equity, floor, day, day_start_equity, halted, "
                       "halt_reason FROM alpha_book WHERE id=1").fetchone()
    if row is None:
        start = paper_bankroll()
        conn.execute("INSERT INTO alpha_book VALUES (1,?,?,?,?,?,?,0,NULL,?)",
                     (start, start, start, start * float(floor_fraction()), now.date().isoformat(), start,
                      now.isoformat()))
        row = (start, start, start, start * float(floor_fraction()), now.date().isoformat(), start, 0, None)
    keys = ("starting_usd", "cash_usd", "peak_equity", "floor", "day", "day_start_equity", "halted", "halt_reason")
    return dict(zip(keys, row))


def mark_book(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    """Equity = cash + open positions at last price; apply the rising floor and daily stop."""
    st = book_state(conn, now)
    open_val = conn.execute("SELECT COALESCE(SUM(qty*COALESCE(last_price, entry_price)),0) FROM alpha_positions "
                            "WHERE status='open'").fetchone()[0]
    equity = st["cash_usd"] + float(open_val)
    peak = max(st["peak_equity"], equity)
    floor = max(st["floor"], peak * float(floor_fraction()))
    day, day_start = st["day"], st["day_start_equity"]
    if day != now.date().isoformat():
        day, day_start = now.date().isoformat(), equity
    halted, reason = st["halted"], st["halt_reason"]
    if not halted and equity <= floor:
        halted, reason = 1, "equity at or below rising floor"
    elif not halted and equity < day_start * (1 - float(daily_stop_fraction())):
        halted, reason = 1, "daily loss stop"
    conn.execute("UPDATE alpha_book SET peak_equity=?, floor=?, day=?, day_start_equity=?, halted=?, halt_reason=?, "
                 "updated_at=? WHERE id=1", (peak, floor, day, day_start, halted, reason, now.isoformat()))
    return {**st, "equity": round(equity, 4), "peak_equity": peak, "floor": floor, "day": day,
            "day_start_equity": day_start, "halted": halted, "halt_reason": reason}


def size_entry(book: dict[str, Any], c: Candidate, exposure: dict[str, float], *, tier: str = "micro") -> float:
    """Capital-guard style sizing: tier fraction x conviction, meme half size, class exposure caps."""
    if book["halted"]:
        return 0.0
    eq = book["equity"]
    frac = float(TIER_MAX_FRACTION.get(tier, 0))
    conviction = 0.5 + 0.5 * max(0.0, min(1.0, c.score / 0.6))
    size = eq * frac * conviction * (MEME_SIZE_MULT if c.cap_class == "meme" else 1.0)
    cap = MEME_EXPOSURE_CAP if c.cap_class == "meme" else MID_EXPOSURE_CAP
    room = eq * cap - exposure.get(c.cap_class, 0.0)
    daily_room = book["day_start_equity"] * float(daily_stop_fraction()) - max(0.0, book["day_start_equity"] - eq)
    size = min(size, room, eq - book["floor"], daily_room, book["cash_usd"])
    return round(size, 2) if size >= 5 else 0.0


def _fill(price: float, notional: float, liquidity: float, side: str) -> tuple[float, float]:
    from .trench_desks import CostModel

    fill_price, fee, _ = CostModel.from_env().fill(side=side, quote_price=price, notional_usd=notional,
                                                   liquidity_usd=liquidity)
    return fill_price, fee


def manage(conn: sqlite3.Connection, marks: dict[tuple[str, str], tuple[float, float]], now: datetime,
           rules: ExitRules = EXITS) -> list[dict[str, Any]]:
    """Apply take-profit ladder, trailing, hard and time stops to open paper positions."""
    actions = []
    for (pid, chain, addr, sym, opened, entry, qty, cost, peak, tp_hits, realized) in conn.execute(
            "SELECT id, chain, address, symbol, opened_at, entry_price, qty, cost_usd, peak_price, tp_hits, "
            "realized_usd FROM alpha_positions WHERE status='open'").fetchall():
        mk = marks.get((chain, addr))
        if not mk:
            continue
        price, liq = mk
        peak = max(peak or entry, price)
        ret = price / entry - 1
        age_h = (now - datetime.fromisoformat(opened)).total_seconds() / 3600
        sell_frac, reason = 0.0, None
        if tp_hits < len(rules.take_profit) and ret >= rules.take_profit[tp_hits][0]:
            sell_frac, reason = rules.take_profit[tp_hits][1], f"take profit {tp_hits + 1}"
            tp_hits += 1
        elif tp_hits == 0 and ret <= rules.hard_stop:
            sell_frac, reason = 1.0, "hard stop"
        elif tp_hits > 0 and price <= peak * (1 - rules.trailing_stop):
            sell_frac, reason = 1.0, "trailing stop"
        elif age_h >= rules.time_stop_hours:
            sell_frac, reason = 1.0, "time stop"
        elif age_h >= rules.stale_after_hours and ret < rules.stale_min_return and tp_hits == 0:
            sell_frac, reason = 1.0, "stale exit"
        if reason:
            sell_qty = qty * sell_frac
            fill_price, _ = _fill(price, sell_qty * price, liq, "sell")
            gross = sell_qty * fill_price
            fee = _fill(price, max(gross, 1e-9), liq, "sell")[1]
            proceeds = max(0.0, gross - fee)
            cost_part = cost * sell_frac
            realized += proceeds - cost_part
            qty -= sell_qty
            cost -= cost_part
            conn.execute("UPDATE alpha_book SET cash_usd=cash_usd+? WHERE id=1", (proceeds,))
            closed = qty <= 1e-12 or sell_frac >= 1.0
            conn.execute("UPDATE alpha_positions SET qty=?, cost_usd=?, peak_price=?, tp_hits=?, realized_usd=?, "
                         "last_price=?, status=?, closed_at=?, close_reason=? WHERE id=?",
                         (0.0 if closed else qty, 0.0 if closed else cost, peak, tp_hits, realized, price,
                          "closed" if closed else "open", now.isoformat() if closed else None,
                          reason if closed else None, pid))
            act = {"symbol": sym, "address": addr, "reason": reason, "return": round(ret, 4),
                   "proceeds_usd": round(proceeds, 2), "closed": closed}
            _event(conn, now, "paper_exit", **act)
            actions.append(act)
        else:
            conn.execute("UPDATE alpha_positions SET peak_price=?, last_price=? WHERE id=?", (peak, price, pid))
    return actions


def manage_adaptive(conn: sqlite3.Connection, marks: dict[tuple[str, str], tuple[float, float]], now: datetime,
                    params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Paper exits with adaptive_exits.decide (partial TP -> break-even, vol-trailed runner, conditional time
    extension, liquidity-pull exit). State is kept in exits_json['adaptive_state']. Logs post-exit price paths."""
    from . import adaptive_exits as ax

    p = ax.AdaptiveExit.from_genome(params)
    actions = []
    ax.fill_followups(conn, marks, now.isoformat())
    for (pid, chain, addr, sym, opened, entry, qty, cost, peak, tp_hits, realized, ej) in conn.execute(
            "SELECT id, chain, address, symbol, opened_at, entry_price, qty, cost_usd, peak_price, tp_hits, "
            "realized_usd, exits_json FROM alpha_positions WHERE status='open'").fetchall():
        mk = marks.get((chain, addr))
        if not mk or not entry:
            continue
        price, liq = mk
        meta = json.loads(ej or "{}")
        raw = meta.get("adaptive_state") or {}
        st = ax.PosState(entry=entry, remaining=raw.get("remaining", 1.0), tp_hit=raw.get("tp_hit", bool(tp_hits)),
                         peak=raw.get("peak", peak or entry), liq_high=raw.get("liq_high", liq or 0),
                         rets=raw.get("rets", []), last_price=raw.get("last_price", entry),
                         extended_h=raw.get("extended_h", 0.0))
        age_h = (now - datetime.fromisoformat(opened)).total_seconds() / 3600
        before = st.remaining
        frac, reason = ax.decide(st, p, price=price, age_h=age_h, liquidity=liq)
        meta["adaptive_state"] = {"remaining": st.remaining, "tp_hit": st.tp_hit, "peak": st.peak,
                                  "liq_high": st.liq_high, "rets": st.rets[-20:], "last_price": st.last_price,
                                  "extended_h": st.extended_h}
        meta["exit_policy"] = p.as_dict()
        if reason and frac > 0 and before > 0:
            sell_frac = min(1.0, frac / before)
            sell_qty = qty * sell_frac
            fill_price, _ = _fill(price, sell_qty * price, liq, "sell")
            gross = sell_qty * fill_price
            fee = _fill(price, max(gross, 1e-9), liq, "sell")[1]
            proceeds = max(0.0, gross - fee)
            cost_part = cost * sell_frac
            realized += proceeds - cost_part
            qty -= sell_qty
            cost -= cost_part
            conn.execute("UPDATE alpha_book SET cash_usd=cash_usd+? WHERE id=1", (proceeds,))
            closed = st.remaining <= 1e-9
            conn.execute("UPDATE alpha_positions SET qty=?, cost_usd=?, peak_price=?, tp_hits=?, realized_usd=?, "
                         "last_price=?, status=?, closed_at=?, close_reason=?, exits_json=? WHERE id=?",
                         (0.0 if closed else qty, 0.0 if closed else cost, st.peak, int(st.tp_hit), realized, price,
                          "closed" if closed else "open", now.isoformat() if closed else None,
                          reason if closed else None, json.dumps(meta), pid))
            ax.record_exit(conn, pid, chain, addr, now.isoformat(), price, reason)
            act = {"symbol": sym, "address": addr, "reason": reason, "return": round(price / entry - 1, 4),
                   "proceeds_usd": round(proceeds, 2), "closed": closed}
            _event(conn, now, "paper_exit", **act)
            actions.append(act)
        else:
            conn.execute("UPDATE alpha_positions SET peak_price=?, last_price=?, exits_json=? WHERE id=?",
                         (st.peak, price, json.dumps(meta), pid))
    return actions


def enter(conn: sqlite3.Connection, c: Candidate, book: dict[str, Any], now: datetime) -> float:
    from .accuracy_ledger import record_forecast

    exposure = dict(conn.execute("SELECT cap_class, SUM(qty*COALESCE(last_price, entry_price)) FROM alpha_positions "
                                 "WHERE status='open' GROUP BY cap_class").fetchall())
    notional = size_entry(book, c, exposure)
    if notional <= 0 or not c.price or not c.liquidity:
        return 0.0
    fill_price, fee = _fill(c.price, notional, c.liquidity, "buy")
    qty = (notional - fee) / fill_price
    prob = round(0.5 + 0.15 * math.tanh(c.score * 2), 4)
    fid = record_forecast(conn, agent_id=AGENT_ID, venue=c.chain, market_id=c.address, symbol=c.symbol,
                          predicted_probability=prob, market_price=c.price, recorded_at=now, decision="paper_trade",
                          reasoning={"score": c.score, "cap_class": c.cap_class, "screens": "all passed"},
                          horizon="1h", metadata={"kind": "token_up", "alpha": True})
    conn.execute("INSERT INTO alpha_positions (chain, address, symbol, cap_class, opened_at, entry_price, qty, "
                 "cost_usd, peak_price, last_price, score, exits_json, forecast_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (c.chain, c.address, c.symbol, c.cap_class, now.isoformat(), fill_price, qty, notional, fill_price,
                  c.price, c.score, json.dumps({**EXITS.as_dict(), "gate": "catalyst_v1", "entry_cost_usd": notional,
                              "catalyst": c.catalyst}, default=str), fid))
    conn.execute("UPDATE alpha_book SET cash_usd=cash_usd-? WHERE id=1", (notional,))
    book["cash_usd"] -= notional
    _event(conn, now, "paper_entry", symbol=c.symbol, address=c.address, chain=c.chain, notional_usd=notional,
           score=c.score, cap_class=c.cap_class)
    return notional


# -- cycle ---------------------------------------------------------------------------------

def _candidates_from_signals(conn: sqlite3.Connection, now: datetime, limit: int) -> list[Candidate]:
    cutoff = (now - timedelta(hours=2)).isoformat()
    out = []
    for entity, sym, chain, vel in conn.execute(
            "SELECT entity, symbol, chain, buzz_velocity FROM culture_signals WHERE kind='token' AND updated_at>=? "
            "ORDER BY score DESC LIMIT ?", (cutoff, limit)).fetchall():
        ch, _, addr = entity.partition(":")
        if ch == "solana" or ch in GOPLUS_CHAINS:
            out.append(Candidate(chain=ch, address=addr, symbol=sym, buzz_velocity=float(vel or 0)))
    for chain, addr, sym in conn.execute("SELECT chain, address, symbol FROM alpha_positions WHERE status='open'"):
        if not any(c.address == addr for c in out):
            out.append(Candidate(chain=chain, address=addr, symbol=sym))
    return out


def run_cycle(conn: sqlite3.Connection, client: Any, *, db_path: str | None = None, now: datetime | None = None,
              limit: int = 40, max_new: int = 3) -> dict[str, Any]:
    ensure_schema(conn)
    now = now or datetime.now(UTC)
    lim = ScreenLimits.from_env()
    cands = _candidates_from_signals(conn, now, limit)
    by_chain: dict[str, list[Candidate]] = {}
    for c in cands:
        by_chain.setdefault(c.chain, []).append(c)
    for chain, group in by_chain.items():
        for i in range(0, len(group), DEX_BATCH):
            batch = group[i:i + DEX_BATCH]
            data = client.get_json(f"https://api.dexscreener.com/tokens/v1/{chain}/"
                                   + ",".join(c.address for c in batch))
            pairs = data if isinstance(data, list) else []
            for c in batch:
                apply_pair(c, [p for p in pairs if isinstance(p, dict)
                               and (p.get("baseToken") or {}).get("address", "").lower() == c.address.lower()], now)
    for c in cands:
        if c.symbol:
            conn.execute("UPDATE culture_signals SET symbol=? WHERE kind='token' AND entity=? AND symbol IS NULL",
                         (c.symbol, f"{c.chain}:{c.address}"))
    marks = {(c.chain, c.address): (c.price, c.liquidity or 1.0) for c in cands if c.price}
    if os.getenv("NOEMA_ALPHA_ADAPTIVE_EXITS", "1") != "0":
        exits = manage_adaptive(conn, marks, now)
    else:
        exits = manage(conn, marks, now)
    for c in cands:
        if c.liquidity is None:
            continue
        conn.execute("INSERT INTO alpha_liquidity VALUES (?,?,?,?)", (c.chain, c.address, now.isoformat(), c.liquidity))
        old = conn.execute("SELECT liquidity FROM alpha_liquidity WHERE chain=? AND address=? AND at<=? "
                           "ORDER BY at DESC LIMIT 1", (c.chain, c.address, (now - timedelta(hours=1)).isoformat())
                           ).fetchone()
        c.liquidity_growth = (c.liquidity / old[0] - 1) if old and old[0] else 0.0
        c.screens = market_screens(c, lim)
        report = None
        if all(s["pass"] for s in c.screens.values()):  # only spend safety-API calls on liquid tokens
            if c.chain == "solana":
                report = client.get_json(f"https://api.rugcheck.xyz/v1/tokens/{c.address}/report")
                c.screens.update(solana_screens(report, lim))
            else:
                data = client.get_json(f"https://api.gopluslabs.io/api/v1/token_security/{GOPLUS_CHAINS[c.chain]}"
                                       f"?contract_addresses={c.address}")
                res = ((data or {}).get("result") or {}) if isinstance(data, dict) else {}
                c.screens.update(evm_screens(res.get(c.address.lower()) or next(iter(res.values()), None), lim))
        c.score = rank_score(c)
        safe = c.passed
        if safe:
            from .edge_strategies import catalyst_screen

            c.catalyst = catalyst_screen(c, conn, now, report if isinstance(report, dict) else None)
        conn.execute(
            "INSERT OR REPLACE INTO alpha_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c.chain, c.address, c.symbol, now.isoformat(), int(c.passed), c.score, c.cap_class, c.price, c.liquidity,
             c.volume_24h, c.mcap, c.age_hours, c.h1, c.h6, c.h24, c.buzz_velocity, c.liquidity_growth,
             json.dumps({**c.screens, **({"catalyst_gate": {
                 "pass": c.has_catalyst, "value": {k: v["pass"] for k, v in c.catalyst.items()},
                 "rule": "entry needs: no wash/bundle, healthy flow, volume accel, >=2 catalysts"}}
                 if c.catalyst else {})}, default=str)))
        if safe:
            conn.execute("INSERT OR IGNORE INTO alpha_allowlist (chain, address, symbol, added_at, screens_json) "
                         "VALUES (?,?,?,?,?)", (c.chain, c.address, c.symbol, now.isoformat(),
                                                json.dumps(c.screens, default=str)))
    # Screened-out tokens that failed must drop off the proposed allowlist immediately.
    for c in cands:
        if c.screens and not c.passed:
            conn.execute("DELETE FROM alpha_allowlist WHERE chain=? AND address=? AND owner_reviewed=0",
                         (c.chain, c.address))
    book = mark_book(conn, now)
    entries = []
    held = {r[0] for r in conn.execute("SELECT address FROM alpha_positions WHERE status='open'")}
    # Safety screens only make a token tradeable; an entry also needs a real catalyst.
    for c in sorted((c for c in cands if c.passed and c.has_catalyst and c.address not in held and c.score > 0.1),
                    key=lambda c: c.score, reverse=True)[:max_new]:
        n = enter(conn, c, book, now)
        if n:
            entries.append({"symbol": c.symbol, "address": c.address, "notional_usd": n, "score": c.score})
    _record_vetoes(conn, cands, now)
    try:
        from .accuracy_ledger import resolve_token_marks

        resolve_token_marks(conn, {c.address: c.price for c in cands if c.price}, now=now, db_path=db_path)
    except (sqlite3.Error, ValueError):
        pass
    conn.execute("DELETE FROM alpha_liquidity WHERE at < ?", ((now - timedelta(days=2)).isoformat(),))
    conn.commit()
    return {"candidates": len(cands), "priced": sum(1 for c in cands if c.price),
            "passed": sum(1 for c in cands if c.passed), "entries": entries, "exits": exits,
            "equity": book["equity"], "halted": bool(book["halted"])}


def _record_vetoes(conn: sqlite3.Connection, cands: list[Candidate], now: datetime) -> None:
    """Screened-out tokens go in the ledger as veto so the screens themselves get scored."""
    from .accuracy_ledger import ensure_accuracy_schema, record_forecast

    ensure_accuracy_schema(conn)
    for c in cands:
        if not c.screens or c.passed or not c.price:
            continue
        if conn.execute("SELECT 1 FROM accuracy_forecasts WHERE agent_id=? AND market_id=? AND recorded_at>?",
                        (AGENT_ID, c.address, (now - timedelta(hours=6)).isoformat())).fetchone():
            continue
        failed = sorted(k for k, s in c.screens.items() if not s["pass"])
        record_forecast(conn, agent_id=AGENT_ID, venue=c.chain, market_id=c.address, symbol=c.symbol,
                        predicted_probability=0.4, market_price=c.price, recorded_at=now, decision="veto",
                        reasoning={"failed_screens": failed}, horizon="1h",
                        metadata={"kind": "token_up", "alpha": True, "screened_out": failed})


def alpha_board(db_path: str, *, limit: int = 15) -> dict[str, Any]:
    out: dict[str, Any] = {"live_supported": LIVE_SUPPORTED, "mode": "paper", "exits": EXITS.as_dict(),
                           "limits": ScreenLimits.from_env().__dict__, "candidates": [], "positions": [],
                           "book": None, "allowlist": []}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return out
    try:
        cutoff = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        cols = ("chain", "address", "symbol", "evaluated_at", "passed", "score", "cap_class", "price", "liquidity",
                "volume_24h", "mcap", "age_hours", "h1", "h6", "h24", "buzz_velocity", "liquidity_growth")
        for r in conn.execute(f"SELECT {', '.join(cols)}, screens_json FROM alpha_candidates WHERE evaluated_at>=? "
                              "ORDER BY passed DESC, score DESC LIMIT ?", (cutoff, limit)):
            row = dict(zip(cols, r[:-1]))
            screens = json.loads(r[-1] or "{}")
            row["screens"] = {k: bool(v.get("pass")) for k, v in screens.items()}
            row["failed"] = [k for k, v in screens.items() if not v.get("pass")]
            out["candidates"].append(row)
        pcols = ("symbol", "address", "cap_class", "opened_at", "entry_price", "last_price", "cost_usd", "tp_hits",
                 "realized_usd", "status", "close_reason")
        out["positions"] = [dict(zip(pcols, r)) for r in conn.execute(
            f"SELECT {', '.join(pcols)} FROM alpha_positions ORDER BY id DESC LIMIT 20")]
        b = conn.execute("SELECT starting_usd, cash_usd, peak_equity, floor, halted, halt_reason FROM alpha_book").fetchone()
        if b:
            out["book"] = dict(zip(("starting_usd", "cash_usd", "peak_equity", "floor", "halted", "halt_reason"), b))
        out["allowlist"] = [dict(zip(("chain", "address", "symbol", "owner_reviewed"), r)) for r in conn.execute(
            "SELECT chain, address, symbol, owner_reviewed FROM alpha_allowlist ORDER BY added_at DESC LIMIT 30")]
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return out
