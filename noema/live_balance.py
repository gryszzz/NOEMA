"""One observed balance projection and immutable history, separate from profit accounting."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .account_capital import _decimal


def _time(value: Any) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(value))
        return at.astimezone(UTC) if at.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def project_balance(venues: dict, wallets: dict, now: datetime, stripe: dict | None = None) -> dict:
    contributions = []
    for name in ("Kalshi", "Polymarket US"):
        venue = next((row for row in venues.get("venues", []) if row.get("venue") == name), {})
        account = venue.get("account", {})
        amount = _decimal(account.get("cash_balance_usd"))
        readable = account.get("status") == "authenticated_read_only"
        contributions.append({"id": f"venue:{name}", "label": name,
                              "amount_usd": str(amount) if readable and amount is not None else None,
                              "observed_at": account.get("observed_at", venues.get("as_of")),
                              "observed": readable, "funded": amount is not None and amount > 0})
    known_native_symbols = {
        "solana": "SOL", "ethereum": "ETH", "base": "ETH",
        "polygon": "POL", "bitcoin": "BTC",
    }
    for row in wallets.get("networks", []):
        if not isinstance(row, dict):
            continue
        chain = str(row.get("chain", "unknown"))
        chain_identity = str(row.get("canonical_network_id") or chain)
        amount = _decimal(row.get("native_value_usd"))
        readable = row.get("readable") is True and row.get("data_freshness", "fresh") != "stale"
        tokens = row.get("tokens") if isinstance(row.get("tokens"), list) else []
        unpriced_tokens = sum(
            1 for token in tokens if isinstance(token, dict)
            and (_decimal(token.get("raw_amount")) or Decimal(0)) > 0
            and _decimal(token.get("value_usd", token.get("usd_value"))) is None
        )
        token_assets = []
        for token in tokens:
            if not isinstance(token, dict):
                continue
            raw_amount = token.get("raw_amount")
            token_assets.append({
                "asset": token.get("symbol") or token.get("asset") or token.get("mint"),
                "raw_amount": str(raw_amount) if raw_amount is not None else None,
                "amount": next((str(token[key]) for key in ("ui_amount", "amount", "balance", "quantity")
                                if _decimal(token.get(key)) is not None), None),
                "decimals": token.get("decimals"),
                "amount_usd": next((str(_decimal(token.get(key))) for key in ("value_usd", "amount_usd", "usd_value")
                                    if _decimal(token.get(key)) is not None), None),
                "observed_at": token.get("observed_at", row.get("observed_at", wallets.get("observed_at"))),
            })
        native_amount = next((str(row[key]) for key in ("sol", "native_balance", "btc")
                              if _decimal(row.get(key)) is not None), None)
        contributions.append({"id": f"wallet:{chain_identity}:{row.get('address') or 'unknown'}",
                              "label": row.get("chain") or chain,
                              "amount_usd": str(amount) if readable and amount is not None else None,
                              "observed_at": row.get("observed_at", wallets.get("observed_at")),
                              "observed": row.get("connected") is True or readable,
                              "funded": row.get("funded") is True,
                              "data_freshness": row.get("data_freshness"),
                              "unpriced_assets": unpriced_tokens,
                              "assets": [{"asset": row.get("native_symbol")
                                                   or known_native_symbols.get(chain, chain),
                                          "amount": native_amount, "amount_usd": str(amount) if amount is not None else None,
                                          "observed_at": row.get("observed_at", wallets.get("observed_at"))}, *token_assets],
                              "price_observed_at": (row.get("native_valuation") or {}).get("observed_at")})
    stripe = stripe or {}
    available = stripe.get("available")
    usd = [row.get("amount_minor") for row in available
           if isinstance(row, dict) and row.get("currency") == "usd"] if isinstance(available, list) else []
    stripe_readable = stripe.get("status") == "connected" and stripe.get("livemode") is True
    stripe_value = Decimal(usd[0]) / 100 if stripe_readable and len(usd) == 1 and type(usd[0]) is int else None
    contributions.append({"id": "revenue:stripe", "label": "Stripe available",
                          "amount_usd": str(stripe_value) if stripe_value is not None else None,
                          "observed_at": stripe.get("observed_at"), "observed": stripe_readable,
                          "funded": stripe_value is not None and stripe_value > 0})
    for row in contributions:
        at = _time(row["observed_at"])
        price_at = _time(row.get("price_observed_at"))
        current = at and 0 <= (now - at).total_seconds() <= 120
        if row.get("data_freshness") == "stale":
            current = False
        if price_at and not 0 <= (now - price_at).total_seconds() <= 120:
            current = False
        row["status"] = ("DISCONNECTED" if not row["observed"] else
                         "STALE" if not current else
                         "UNPRICED" if row["amount_usd"] is None else "CACHED")
        row["included"] = row["status"] == "CACHED"
    known = [row for row in contributions if row["included"]]
    times = [_time(row["observed_at"]) for row in known]
    at = max(times).isoformat() if times and all(times) else None
    amount = sum((Decimal(row["amount_usd"]) for row in known), Decimal(0)) if known else None
    scope = hashlib.sha256("|".join(sorted(row["id"] for row in known)).encode()).hexdigest()
    return {"amount_usd": str(amount) if amount is not None else None,
            "observed_at": at, "valued_sources": len(known), "expected_sources": len(contributions),
            "status": "UNKNOWN" if not known else "CACHED",
            "observed_sources": sum(row["observed"] for row in contributions),
            "unpriced_sources": sum(row["status"] == "UNPRICED" for row in contributions),
            "unpriced_assets": sum(int(row.get("unpriced_assets", 0)) for row in contributions),
            "stale_sources": sum(row["status"] == "STALE" for row in contributions),
            "coverage": "known cash and native assets only", "scope": scope,
            "contributions": contributions,
            "exclusions": ["open positions", "unpriced token assets", "liabilities not reconciled"],
            "is_profit": False}


def balance_history(db_path: str, venues: dict, wallets: dict, *, window: str = "24H",
                    now: datetime | None = None, stripe: dict | None = None) -> dict:
    now = now or datetime.now(UTC)
    current = project_balance(venues, wallets, now, stripe)
    result = {"current": current, "window": window, "points": [], "sample_count": 0,
              "change_24h_usd": None, "history_status": "unavailable"}
    if not Path(db_path).is_file() and current["status"] != "CACHED":
        return result
    try:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path, timeout=1) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS live_balance_observations ("
                         "fingerprint TEXT PRIMARY KEY, observed_at TEXT NOT NULL, "
                         "amount_usd TEXT NOT NULL, scope TEXT NOT NULL, sources_json TEXT NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS live_balance_time ON live_balance_observations(observed_at)")
            if current["status"] == "CACHED" and current["observed_at"]:
                evidence = json.dumps(current["contributions"], sort_keys=True)
                fingerprint = hashlib.sha256(evidence.encode()).hexdigest()
                conn.execute("INSERT OR IGNORE INTO live_balance_observations VALUES (?,?,?,?,?)",
                             (fingerprint, now.isoformat(), current["amount_usd"], current["scope"], evidence))
            # Cursor reads are bounded at the chart boundary; no synthetic backfill.
            seconds = {"1H": 3600, "24H": 86400, "7D": 604800, "30D": 2592000, "ALL": None}[window]
            start = datetime.fromtimestamp(now.timestamp() - seconds, UTC).isoformat() if seconds else ""
            count = conn.execute("SELECT count(*) FROM live_balance_observations WHERE observed_at>=?", (start,)).fetchone()[0]
            indices = {round(index * (count - 1) / 719) for index in range(720)} if count > 720 else None
            cursor = conn.execute("SELECT observed_at,amount_usd,scope,sources_json FROM live_balance_observations "
                                  "WHERE observed_at>=? ORDER BY observed_at", (start,))
            rows = [row for index, row in enumerate(cursor) if indices is None or index in indices]
            result.update(points=[{"at": row[0], "amount_usd": row[1], "scope": row[2],
                                   "contributions": json.loads(row[3])} for row in rows],
                          sample_count=count, history_status="recorded" if count else "collecting")
            cutoff = datetime.fromtimestamp(now.timestamp() - 86400, UTC)
            baseline = conn.execute("SELECT observed_at,amount_usd,scope FROM live_balance_observations "
                                    "WHERE observed_at<=? ORDER BY observed_at DESC LIMIT 1", (cutoff.isoformat(),)).fetchone()
            if (baseline and current["status"] == "CACHED" and baseline[2] == current["scope"]
                    and 0 <= (cutoff - _time(baseline[0])).total_seconds() <= 300):
                result["change_24h_usd"] = str(Decimal(current["amount_usd"]) - Decimal(baseline[1]))
    except (sqlite3.Error, OSError):
        result["history_status"] = "unavailable"
    return result
