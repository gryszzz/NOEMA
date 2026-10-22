"""Read-only equity feed for the capital guard.

Every ``NOEMA_EQUITY_FEED_INTERVAL_SECONDS`` (default 300) it sums total USD
equity across every venue and wallet and hands it to :class:`CapitalGuard`, so
the guard's freshness check is satisfied by real readings.

Sources: Kalshi (cash + portfolio value), Polymarket US (USD cash + valued open
positions), NOEMA's Solana wallet (SOL + SPL tokens) and NOEMA's EVM wallet on
Ethereum, Base and Polygon (native + USDC/WETH). Prices come from Coinbase spot
(SOL, ETH, POL); USDC/USDT are 1.00; other SPL mints use Jupiter's price API.

Fail closed: if any source errors, or holds an asset it cannot price, the whole
reading is recorded as stale and the guard is *not* updated, so its snapshot
ages out and live trading stays blocked. A source is skipped only when named in
``NOEMA_EQUITY_EXCLUDED_SOURCES`` (comma list of source ids).

Deposits: when a source's holdings grow with no live trade or swap recorded
since the previous complete reading, the increase is booked as a deposit, so
the floor rises with new money instead of the jump being read as profit.

Strictly read-only: public JSON-RPC reads and authenticated *read* endpoints.
It never loads, reads or references any signer key.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from .sqlite_util import connect as sqlite_connect

SOLANA_ADDRESS = os.environ.get("NOEMA_SOLANA_WALLET_ADDRESS", "")  # private runtime config
EVM_ADDRESS = os.environ.get("NOEMA_EVM_ADDRESS", "")  # private runtime config
EVM_CHAINS: dict[str, dict[str, Any]] = {
    "ethereum": {"native": "ETH", "rpc": "https://ethereum-rpc.publicnode.com",
                 "tokens": {"USDC": ("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6),
                            "WETH": ("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", 18)}},
    "base": {"native": "ETH", "rpc": "https://base-rpc.publicnode.com",
             "tokens": {"USDC": ("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6),
                        "WETH": ("0x4200000000000000000000000000000000000006", 18)}},
    "polygon": {"native": "POL", "rpc": "https://polygon-bor-rpc.publicnode.com",
                "tokens": {"USDC": ("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", 6),
                           "USDC.e": ("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", 6)}},
    "arbitrum": {"native": "ETH", "rpc": "https://arbitrum-one-rpc.publicnode.com", "chain_id": 42161,
                 "tokens": {"USDC": ("0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6),
                            "WETH": ("0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", 18)}},
    "optimism": {"native": "ETH", "rpc": "https://optimism-rpc.publicnode.com", "chain_id": 10,
                 "tokens": {"USDC": ("0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85", 6),
                            "WETH": ("0x4200000000000000000000000000000000000006", 18)}},
    # Robinhood Chain (Arbitrum Orbit L2, ETH gas). Official public RPC + chain id 4663 per docs.robinhood.com/chain.
    "robinhood": {"native": "ETH", "rpc": "https://rpc.mainnet.chain.robinhood.com", "chain_id": 4663,
                  "tokens": {}},
}
EVM_CHAINS["ethereum"]["chain_id"] = 1
EVM_CHAINS["base"]["chain_id"] = 8453
EVM_CHAINS["polygon"]["chain_id"] = 137


def _extra_chains() -> None:
    """NOEMA_EVM_CHAINS_JSON adds/overrides chains: {"name": {"native": "ETH", "rpc": "...", "tokens": {}}}."""
    raw = os.getenv("NOEMA_EVM_CHAINS_JSON", "").strip()
    if not raw:
        return
    for name, spec in json.loads(raw).items():
        spec.setdefault("native", "ETH")
        spec["tokens"] = {k: tuple(v) for k, v in (spec.get("tokens") or {}).items()}
        EVM_CHAINS[name.lower()] = spec


_extra_chains()
STABLE_SYMBOLS = {"USDC", "USDC.e", "USDT"}
SOLANA_STABLE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
}
TOKEN_PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                  "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
SOURCE_IDS = ("kalshi", "polymarket_us", "solana", *(f"evm_{c}" for c in EVM_CHAINS))


WALLET_SOURCES = {"solana", *(f"evm_{c}" for c in EVM_CHAINS)}


class SourceError(RuntimeError):
    """A source could not produce a complete, priced reading."""


@dataclass
class SourceReading:
    source: str
    equity_usd: Decimal
    holdings: dict[str, Decimal] = field(default_factory=dict)  # asset -> quantity (USD for venues)
    prices: dict[str, Decimal] = field(default_factory=dict)


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def excluded_sources() -> set[str]:
    raw = os.getenv("NOEMA_EQUITY_EXCLUDED_SOURCES", "")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def feed_interval_seconds() -> int:
    try:
        value = int(os.getenv("NOEMA_EQUITY_FEED_INTERVAL_SECONDS", "60"))
    except ValueError:
        value = 300
    return min(max(value, 60), 600)  # never slower than the 15-min guard freshness window allows


# -- pricing -----------------------------------------------------------------
class Prices:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self._cache: dict[str, Decimal] = {}

    async def spot(self, symbol: str) -> Decimal:
        symbol = symbol.upper()
        if symbol in {s.upper() for s in STABLE_SYMBOLS}:
            return Decimal(1)
        if symbol == "WETH":
            symbol = "ETH"
        if symbol in self._cache:
            return self._cache[symbol]
        resp = await self.client.get(f"https://api.coinbase.com/v2/prices/{symbol}-USD/spot")
        resp.raise_for_status()
        price = _dec((resp.json().get("data") or {}).get("amount"))
        if price is None or price <= 0:
            raise SourceError(f"no valid {symbol} price")
        self._cache[symbol] = price
        return price

    async def solana_mints(self, mints: list[str]) -> dict[str, Decimal]:
        if not mints:
            return {}
        resp = await self.client.get("https://lite-api.jup.ag/price/v3", params={"ids": ",".join(mints)})
        resp.raise_for_status()
        payload = resp.json()
        out: dict[str, Decimal] = {}
        for mint in mints:
            row = payload.get(mint) if isinstance(payload, dict) else None
            price = _dec((row or {}).get("usdPrice"))
            if price is not None and price >= 0:
                out[mint] = price
        return out


# -- sources -------------------------------------------------------------------
async def _rpc(client: httpx.AsyncClient, url: str, method: str, params: list[Any]) -> Any:
    resp = await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    resp.raise_for_status()
    body = resp.json()
    if not isinstance(body, dict) or body.get("error") is not None or "result" not in body:
        raise SourceError(f"{method} rpc error")
    return body["result"]


async def read_kalshi(client: httpx.AsyncClient, prices: Prices) -> SourceReading:
    from .account import KalshiAccount
    from .account_capital import kalshi_cash_usd

    account = KalshiAccount()
    try:
        balance = await account.balance()
    finally:
        await account.close()
    cash = _dec(kalshi_cash_usd(balance))
    portfolio = _dec(balance.get("portfolio_value_dollars"))
    if portfolio is None:
        raw = _dec(balance.get("portfolio_value"))
        portfolio = None if raw is None else raw / 100
    if cash is None or portfolio is None:
        raise SourceError("kalshi balance missing cash or portfolio value")
    return SourceReading("kalshi", cash + portfolio, {"USD": cash + portfolio})


async def read_polymarket_us(client: httpx.AsyncClient, prices: Prices) -> SourceReading:
    from .venues.polymarket_us_trading import PolymarketUSTradingVenue

    venue = PolymarketUSTradingVenue()
    try:
        balances = await venue.balances()
        positions = await venue.positions()
    finally:
        venue.close()
    cash = _dec(balances.get("current_balance_usd"))
    if cash is None:
        raise SourceError("polymarket us returned no USD balance")
    held = Decimal(0)
    for row in positions:
        qty = _dec(row.get("net_position")) or Decimal(0)
        if qty == 0:
            continue
        value = _dec(row.get("value_usd") or row.get("cash_value_usd") or row.get("market_value_usd"))
        if value is None:
            raise SourceError("polymarket us open position has no reported value")
        held += value
    return SourceReading("polymarket_us", cash + held, {"USD": cash + held})


def _solana_rpcs() -> list[str]:
    urls = [os.getenv("NOEMA_SOLANA_RPC_URL", "").strip(), os.getenv("NOEMA_SOLANA_RPC_FALLBACK_URL", "").strip()]
    key = os.getenv("HELIUS_API_KEY", "").strip()  # never logged; errors report exception type only
    if key:
        urls.insert(0, f"https://mainnet.helius-rpc.com/?api-key={key}")
    return [u for u in urls if u] or ["https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com"]


async def read_solana(client: httpx.AsyncClient, prices: Prices) -> SourceReading:
    last: Exception | None = None
    for url in _solana_rpcs():
        try:
            lamports = (await _rpc(client, url, "getBalance", [SOLANA_ADDRESS]))["value"]
            tokens: dict[str, Decimal] = {}
            for program in TOKEN_PROGRAMS:
                result = await _rpc(client, url, "getTokenAccountsByOwner",
                                    [SOLANA_ADDRESS, {"programId": program}, {"encoding": "jsonParsed"}])
                for acc in result.get("value", []):
                    info = acc["account"]["data"]["parsed"]["info"]
                    amount = info["tokenAmount"]
                    qty = Decimal(str(amount["amount"])) / (Decimal(10) ** int(amount["decimals"]))
                    if qty > 0:
                        tokens[info["mint"]] = tokens.get(info["mint"], Decimal(0)) + qty
            break
        except Exception as exc:  # noqa: BLE001 - try the next RPC, then fail closed
            last = exc
    else:
        raise SourceError(f"solana rpc unavailable: {type(last).__name__}")
    sol = Decimal(int(lamports)) / Decimal(10**9)
    holdings: dict[str, Decimal] = {"SOL": sol}
    price_map: dict[str, Decimal] = {"SOL": await prices.spot("SOL")}
    ignored = {m.strip() for m in os.getenv("NOEMA_EQUITY_IGNORE_SOLANA_MINTS", "").split(",") if m.strip()}
    other = [m for m in tokens if m not in SOLANA_STABLE_MINTS and m not in ignored]
    jup = await prices.solana_mints(other)
    for mint, qty in tokens.items():
        if mint in ignored:
            continue
        if mint in SOLANA_STABLE_MINTS:
            price = Decimal(1)
        elif mint in jup:
            price = jup[mint]
        else:
            raise SourceError(f"solana token {mint[:6]}… cannot be priced")
        holdings[mint] = qty
        price_map[mint] = price
    total = sum((holdings[a] * price_map[a] for a in holdings), Decimal(0))
    return SourceReading("solana", total, holdings, price_map)


def _evm_reader(chain: str) -> Callable[[httpx.AsyncClient, Prices], Awaitable[SourceReading]]:
    async def read(client: httpx.AsyncClient, prices: Prices) -> SourceReading:
        spec = EVM_CHAINS[chain]
        url = os.getenv(f"NOEMA_EVM_RPC_URL_{chain.upper()}", "").strip() or spec["rpc"]
        wei = int(await _rpc(client, url, "eth_getBalance", [EVM_ADDRESS, "latest"]), 16)
        holdings = {spec["native"]: Decimal(wei) / Decimal(10**18)}
        data_prefix = "0x70a08231" + EVM_ADDRESS[2:].lower().rjust(64, "0")
        for symbol, (contract, decimals) in spec["tokens"].items():
            raw = await _rpc(client, url, "eth_call", [{"to": contract, "data": data_prefix}, "latest"])
            qty = Decimal(int(raw, 16) if raw not in ("0x", "") else 0) / (Decimal(10) ** decimals)
            if qty > 0:
                holdings[symbol] = qty
        price_map = {asset: await prices.spot(asset) for asset in holdings}
        total = sum((holdings[a] * price_map[a] for a in holdings), Decimal(0))
        return SourceReading(f"evm_{chain}", total, holdings, price_map)
    return read


SOURCES: dict[str, Callable[[httpx.AsyncClient, Prices], Awaitable[SourceReading]]] = {
    "kalshi": read_kalshi,
    "polymarket_us": read_polymarket_us,
    "solana": read_solana,
    **{f"evm_{chain}": _evm_reader(chain) for chain in EVM_CHAINS},
}


# -- storage, deposits, guard handoff --------------------------------------------
def _connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite_connect(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS equity_feed_readings (
        at TEXT NOT NULL, status TEXT NOT NULL, total_usd TEXT, deposit_usd TEXT NOT NULL,
        sources_json TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS equity_feed_deposits (
        at TEXT NOT NULL, source TEXT NOT NULL, asset TEXT NOT NULL, quantity TEXT NOT NULL,
        amount_usd TEXT NOT NULL)""")
    return conn


def _last_complete(conn: sqlite3.Connection) -> tuple[datetime, dict[str, Any]] | None:
    row = conn.execute("SELECT at, sources_json FROM equity_feed_readings WHERE status='complete' "
                       "ORDER BY at DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return datetime.fromisoformat(row[0]), json.loads(row[1])


def _count_since(path: str, sql: str, since: str) -> int:
    if not Path(path).exists():
        return 0
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as conn:
            return int(conn.execute(sql, (since,)).fetchone()[0])
    except sqlite3.OperationalError:
        return 0  # table absent: no trades ever recorded


def trades_since(db_path: str, since: datetime) -> int:
    """Live trades/swaps recorded since ``since`` (any route). Paper desks never touch real balances."""
    stamp = since.isoformat()
    total = _count_since(db_path, "SELECT COUNT(*) FROM execution_gateway_requests "
                         "WHERE created_at >= ? AND status NOT IN ('rejected','blocked','refused')", stamp)
    for env, default, table in (
        ("NOEMA_SOLANA_SIGNER_STATE_PATH", "data/solana-signer-state.db", "solana_swap_requests"),
        ("NOEMA_EVM_SIGNER_STATE_PATH", "data/evm-signer-state.db", "evm_swap_requests"),
    ):
        path = os.getenv(env, "").strip() or default
        total += _count_since(path, f"SELECT COUNT(*) FROM {table} WHERE updated_at >= ?", stamp)
    return total


def _deposit_threshold(previous_usd: Decimal) -> Decimal:
    return max(Decimal("1.00"), previous_usd * Decimal("0.005"))


def detect_deposits(previous: dict[str, Any], current: dict[str, SourceReading],
                    *, trades: int) -> list[tuple[str, str, Decimal, Decimal]]:
    """Return (source, asset, quantity, usd) for balance increases not explained by trades.

    Wallets compare *quantities*, so a price move is never mistaken for a deposit.
    Venues compare USD equity; a jump is a deposit only with no trade since the last reading.
    """
    out: list[tuple[str, str, Decimal, Decimal]] = []
    for source, reading in current.items():
        prev = previous.get(source)
        if prev is None and source in WALLET_SOURCES:
            # Newly tracked wallet (e.g. a chain just added): its existing balance is capital, not profit.
            for asset, qty in reading.holdings.items():
                usd = qty * reading.prices.get(asset, Decimal(1))
                if qty > 0 and usd > 0:
                    out.append((source, asset, qty, usd))
            continue
        if trades:
            continue
        if not isinstance(prev, dict) or prev.get("status") != "ok":
            continue  # no comparable baseline for this source
        prev_holdings = {k: Decimal(v) for k, v in (prev.get("holdings") or {}).items()}
        for asset, qty in reading.holdings.items():
            delta = qty - prev_holdings.get(asset, Decimal(0))
            if delta == 0:
                continue
            price = reading.prices.get(asset, Decimal(1))
            usd = delta * price
            if delta < 0:  # wallet outflow with no trade = withdrawal/transfer out (booked as negative flow, not loss)
                if source in WALLET_SOURCES and -usd >= _deposit_threshold(Decimal(str(prev.get("equity_usd") or 0))):
                    out.append((source, asset, delta, usd))
                continue
            if usd >= _deposit_threshold(Decimal(str(prev.get("equity_usd") or 0))):
                out.append((source, asset, delta, usd))
    return out


Reader = Callable[[httpx.AsyncClient, Prices], Awaitable[SourceReading]]


async def take_reading(db_path: str, *, sources: dict[str, Reader] | None = None,
                       client: httpx.AsyncClient | None = None,
                       now: datetime | None = None) -> dict[str, Any]:
    """Read every source once, record the result, and feed the guard only if complete."""
    from .capital_guard import CapitalGuard

    sources = sources if sources is not None else SOURCES
    now = now or datetime.now(UTC)
    skip = excluded_sources()
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0), headers={"User-Agent": "NOEMA/0.1"})
    prices = Prices(client)
    readings: dict[str, SourceReading] = {}
    report: dict[str, Any] = {}
    try:
        for source, reader in sources.items():
            if source in skip:
                report[source] = {"status": "excluded"}
                continue
            t0 = time.perf_counter()
            try:
                reading = await reader(client, prices)
            except Exception as exc:  # noqa: BLE001 - any failure makes the whole reading stale
                report[source] = {"status": "error", "error": f"{type(exc).__name__}: {str(exc)[:120]}",
                                  "read_at": datetime.now(UTC).isoformat(),
                                  "latency_ms": round((time.perf_counter() - t0) * 1000)}
                continue
            readings[source] = reading
            report[source] = {"status": "ok", "equity_usd": str(reading.equity_usd),
                              "read_at": datetime.now(UTC).isoformat(),
                              "latency_ms": round((time.perf_counter() - t0) * 1000),
                              "holdings": {k: str(v) for k, v in reading.holdings.items()},
                              "prices": {k: str(v) for k, v in reading.prices.items()}}
    finally:
        if owns_client:
            await client.aclose()
    complete = all(row["status"] != "error" for row in report.values()) and bool(readings)
    total = sum((r.equity_usd for r in readings.values()), Decimal(0))
    deposit = Decimal(0)
    guard_state = None
    with _connect(db_path) as conn:
        if complete:
            last = _last_complete(conn)
            if last is not None:
                found = detect_deposits(last[1], readings, trades=trades_since(db_path, last[0]))
                for source, asset, qty, usd in found:
                    conn.execute("INSERT INTO equity_feed_deposits VALUES (?,?,?,?,?)",
                                 (now.isoformat(), source, asset, str(qty), str(usd)))
                deposit = sum((usd for *_, usd in found), Decimal(0))
        conn.execute("INSERT INTO equity_feed_readings VALUES (?,?,?,?,?)",
                     (now.isoformat(), "complete" if complete else "stale",
                      str(total) if complete else None, str(deposit), json.dumps(report)))
    if complete:
        guard_state = CapitalGuard(db_path).record_equity(total, deposit_usd=deposit, now=now)
    return {"at": now.isoformat(), "status": "complete" if complete else "stale",
            "total_usd": str(total) if complete else None, "deposit_usd": str(deposit),
            "sources": report,
            "guard": None if guard_state is None else {
                "equity": str(guard_state.equity), "peak": str(guard_state.peak_equity),
                "floor": str(guard_state.floor), "tripped": guard_state.tripped}}


def latest_reading(db_path: str) -> dict[str, Any] | None:
    if not Path(db_path).exists():
        return None
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as conn:
            row = conn.execute("SELECT at, status, total_usd, deposit_usd, sources_json FROM "
                               "equity_feed_readings ORDER BY at DESC LIMIT 1").fetchone()
            deposits = conn.execute("SELECT COALESCE(SUM(CAST(amount_usd AS REAL)),0), COUNT(*) "
                                    "FROM equity_feed_deposits").fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    sources = {k: {kk: vv for kk, vv in v.items() if kk in ("status", "equity_usd", "error")}
               for k, v in json.loads(row[4]).items()}
    return {"at": row[0], "status": row[1], "total_usd": row[2], "deposit_usd": row[3],
            "sources": sources, "deposits_total_usd": deposits[0], "deposits_count": deposits[1]}


async def run_equity_feed_loop(db_path: str, log: Callable[..., None] | None = None) -> None:
    while True:
        started = asyncio.get_running_loop().time()
        try:
            result = await take_reading(db_path)
            if log:
                log("equity_feed_reading", status=result["status"], total_usd=result["total_usd"],
                    deposit_usd=result["deposit_usd"],
                    sources={k: v["status"] for k, v in result["sources"].items()})
        except Exception as exc:  # noqa: BLE001 - the loop must survive; the guard goes stale on its own
            if log:
                log("equity_feed_reading", status="failed", error_type=type(exc).__name__)
        elapsed = asyncio.get_running_loop().time() - started
        await asyncio.sleep(max(0.0, feed_interval_seconds() - elapsed))


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Read-only equity feed (balances only, never signs).")
    ap.add_argument("--db", default=os.getenv("NOEMA_DB_PATH", "data/noema.db"))
    ap.add_argument("--loop", action="store_true")
    a = ap.parse_args()
    if a.loop:
        def log(event: str, **kw: Any) -> None:
            print(datetime.now(UTC).isoformat(), event, json.dumps(kw, default=str), flush=True)
        asyncio.run(run_equity_feed_loop(a.db, log))
    else:
        print(json.dumps(asyncio.run(take_reading(a.db)), indent=2))


if __name__ == "__main__":
    main()
