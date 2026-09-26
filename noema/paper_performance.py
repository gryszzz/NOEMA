from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class PaperPerformance:
    observations: int
    deployed_usd: float
    net_pnl_usd: float
    after_cost_return: float | None
    max_drawdown_fraction: float
    per_trade_returns: tuple[float, ...]


def settled_paper_performance(
    path: str = "data/noema.db",
    *,
    starting_equity_usd: float = 100.0,
) -> PaperPerformance:
    """Build a conservative realized paper-equity path.

    P&L is recognized only when the corresponding market is known to have resolved after
    the quote. This remains research accounting: it does not prove real fills or capacity.
    """

    if starting_equity_usd <= 0:
        raise ValueError("starting_equity_usd must be positive")
    if not Path(path).exists():
        return PaperPerformance(0, 0.0, 0.0, None, 0.0, ())

    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            """
            SELECT q.quote_json, o.outcome_yes, q.quoted_at,
                   o.resolved_at, o.first_seen_at
            FROM paper_quotes q
            JOIN outcomes o
              ON o.venue = q.venue AND o.market_id = q.market_id
            WHERE q.selected = 1
            ORDER BY o.resolved_at, q.id
            """
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()

    deployed = 0.0
    net_total = 0.0
    trade_returns: list[float] = []
    realized_pnl: list[float] = []

    for quote_json, outcome, quoted_at, resolved_at, first_seen_at in rows:
        try:
            quote = json.loads(str(quote_json))
            debit = float(quote["total_debit_usd"])
            contracts = float(quote["contracts"])
            quoted = datetime.fromisoformat(str(quoted_at))
            resolved = datetime.fromisoformat(str(resolved_at))
            first_seen = datetime.fromisoformat(str(first_seen_at))
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
        if (
            debit <= 0
            or quoted.tzinfo is None
            or resolved.tzinfo is None
            or first_seen.tzinfo is None
            or resolved <= quoted
            or first_seen <= quoted
        ):
            continue

        net = int(outcome) * contracts - debit
        deployed += debit
        net_total += net
        trade_returns.append(net / debit)
        realized_pnl.append(net)

    equity = starting_equity_usd
    peak = equity
    max_drawdown = 0.0
    for pnl in realized_pnl:
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
        if equity <= 0:
            max_drawdown = 1.0

    return PaperPerformance(
        observations=len(trade_returns),
        deployed_usd=deployed,
        net_pnl_usd=net_total,
        after_cost_return=(net_total / deployed if deployed > 0 else None),
        max_drawdown_fraction=max(0.0, min(1.0, max_drawdown)),
        per_trade_returns=tuple(trade_returns),
    )
