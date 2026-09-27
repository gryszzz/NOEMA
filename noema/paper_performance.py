from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .history_forecaster import MODEL_VERSION
from .paper_settlements import load_paper_settlements


@dataclass(frozen=True)
class PaperPerformance:
    observations: int
    deployed_usd: float
    net_pnl_usd: float
    after_cost_return: float | None
    max_drawdown_fraction: float
    per_trade_returns: tuple[float, ...]
    per_event_returns: tuple[float, ...] = ()


def settled_paper_performance(
    path: str = "data/noema.db",
    *,
    starting_equity_usd: float = 100.0,
    model_version: str = MODEL_VERSION,
    now: datetime | None = None,
) -> PaperPerformance:
    """Build a conservative realized paper-equity path.

    P&L is recognized only when the corresponding market is known to have resolved after
    the quote. This remains research accounting: it does not prove real fills or capacity.
    """

    if not math.isfinite(starting_equity_usd) or starting_equity_usd <= 0:
        raise ValueError("starting_equity_usd must be positive and finite")
    audit = load_paper_settlements(path, model_version=model_version, now=now)
    deployed = Decimal(0)
    net_total = Decimal(0)
    trade_returns: list[float] = []
    event_values: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
    for settled in audit.settlements:
        deployed += settled.debit_usd
        net_total += settled.net_pnl_usd
        trade_returns.append(float(settled.net_pnl_usd / settled.debit_usd))
        debit, pnl = event_values.get(settled.event_id, (Decimal(0), Decimal(0)))
        event_values[settled.event_id] = (
            debit + settled.debit_usd, pnl + settled.net_pnl_usd,
        )

    equity = Decimal(str(starting_equity_usd))
    peak = equity
    max_drawdown = 0.0
    for settled in audit.settlements:
        equity += settled.net_pnl_usd
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
        if equity <= 0:
            max_drawdown = 1.0

    return PaperPerformance(
        observations=len(trade_returns),
        deployed_usd=float(deployed),
        net_pnl_usd=float(net_total),
        after_cost_return=(float(net_total / deployed) if deployed > 0 else None),
        max_drawdown_fraction=float(max(0.0, min(1.0, max_drawdown))),
        per_trade_returns=tuple(trade_returns),
        per_event_returns=tuple(float(pnl / debit) for debit, pnl in event_values.values()),
    )
