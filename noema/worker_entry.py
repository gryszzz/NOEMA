from __future__ import annotations

import asyncio
import os

from .lab_worker import LabConfig, run_lab


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw is not None else default


def _int_env(name: str) -> int | None:
    raw = os.getenv(name)
    return int(raw) if raw else None


def main() -> None:
    config = LabConfig(
        db_path=os.getenv("NOEMA_DB_PATH", "data/noema.db"),
        snapshot_interval_seconds=_float_env("NOEMA_SNAPSHOT_INTERVAL_SECONDS", 60.0),
        outcome_sync_interval_seconds=_float_env(
            "NOEMA_OUTCOME_SYNC_INTERVAL_SECONDS",
            900.0,
        ),
        report_interval_seconds=_float_env("NOEMA_REPORT_INTERVAL_SECONDS", 300.0),
        max_markets_per_scan=_int_env("NOEMA_MAX_MARKETS_PER_SCAN"),
    )
    asyncio.run(run_lab(config))


if __name__ == "__main__":
    main()
