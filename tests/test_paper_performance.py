import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from noema.paper_performance import settled_paper_performance


def test_settled_paper_performance_tracks_after_cost_return_and_drawdown(tmp_path) -> None:
    db = tmp_path / "noema.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE paper_quotes (
            id INTEGER PRIMARY KEY,
            venue TEXT,
            market_id TEXT,
            selected INTEGER,
            quoted_at TEXT,
            quote_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE outcomes (
            venue TEXT,
            market_id TEXT,
            outcome_yes INTEGER,
            resolved_at TEXT,
            first_seen_at TEXT
        )
        """
    )

    start = datetime(2026, 9, 1, tzinfo=UTC)
    quotes = [
        (1, "A", 0.50, 1, 1),
        (2, "B", 0.40, 1, 0),
        (3, "C", 0.25, 1, 1),
    ]
    for index, ticker, debit, contracts, outcome in quotes:
        quoted = start + timedelta(hours=index)
        resolved = quoted + timedelta(hours=1)
        conn.execute(
            """
            INSERT INTO paper_quotes
            (id, venue, market_id, selected, quoted_at, quote_json)
            VALUES (?, 'kalshi:demo', ?, 1, ?, ?)
            """,
            (
                index,
                ticker,
                quoted.isoformat(),
                json.dumps(
                    {
                        "total_debit_usd": str(debit),
                        "contracts": str(contracts),
                    }
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO outcomes
            (venue, market_id, outcome_yes, resolved_at, first_seen_at)
            VALUES ('kalshi:demo', ?, ?, ?, ?)
            """,
            (
                ticker,
                outcome,
                resolved.isoformat(),
                resolved.isoformat(),
            ),
        )
    conn.commit()
    conn.close()

    result = settled_paper_performance(str(db), starting_equity_usd=10)

    expected_net = 0.50 - 0.40 + 0.75
    assert result.observations == 3
    assert result.net_pnl_usd == pytest.approx(expected_net)
    assert result.deployed_usd == pytest.approx(1.15)
    assert result.after_cost_return == pytest.approx(expected_net / 1.15)
    assert 0 <= result.max_drawdown_fraction <= 1
