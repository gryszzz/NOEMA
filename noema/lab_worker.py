from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from .outcomes import OutcomeStore
from .soak import SoakStore
from .soak_report import build_soak_quality_report
from .soak_runner import collect_market_snapshot_batch
from .sync import sync_kalshi_outcomes
from .venues.kalshi import KalshiVenue
from .venues.kalshi_history import KalshiHistory


@dataclass(frozen=True)
class LabConfig:
    db_path: str = "data/noema.db"
    snapshot_interval_seconds: float = 60.0
    outcome_sync_interval_seconds: float = 900.0
    report_interval_seconds: float = 300.0
    max_markets_per_scan: int | None = None


def _log(event: str, **fields: object) -> None:
    payload = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        **fields,
    }
    print(json.dumps(payload, sort_keys=True, default=str), flush=True)


async def run_lab(config: LabConfig | None = None) -> None:
    config = config or LabConfig()
    if config.snapshot_interval_seconds < 5:
        raise ValueError("snapshot_interval_seconds must be >= 5")
    if config.outcome_sync_interval_seconds < 30:
        raise ValueError("outcome_sync_interval_seconds must be >= 30")
    if config.report_interval_seconds < 30:
        raise ValueError("report_interval_seconds must be >= 30")

    soak_store = SoakStore(config.db_path)
    outcome_store = OutcomeStore(config.db_path)
    venue = KalshiVenue()
    history = KalshiHistory()

    loop = asyncio.get_running_loop()
    next_outcome_sync = loop.time()
    next_report = loop.time()

    _log(
        "lab_started",
        db_path=config.db_path,
        snapshot_interval_seconds=config.snapshot_interval_seconds,
        outcome_sync_interval_seconds=config.outcome_sync_interval_seconds,
        max_markets_per_scan=config.max_markets_per_scan,
    )

    try:
        while True:
            started = loop.time()
            batch = await collect_market_snapshot_batch(
                venue,
                soak_store,
                max_markets=config.max_markets_per_scan,
            )
            _log("snapshot_batch", **asdict(batch))

            now = loop.time()
            if now >= next_outcome_sync:
                sync = await sync_kalshi_outcomes(
                    outcome_store,
                    history,
                    max_markets=2000,
                )
                _log("outcome_sync", **asdict(sync))
                next_outcome_sync = now + config.outcome_sync_interval_seconds

            if now >= next_report:
                report = build_soak_quality_report(config.db_path)
                evaluation = outcome_store.evaluate_ledger()
                _log(
                    "lab_report",
                    **asdict(report),
                    evaluation=asdict(evaluation),
                )
                next_report = now + config.report_interval_seconds

            elapsed = loop.time() - started
            await asyncio.sleep(max(0.0, config.snapshot_interval_seconds - elapsed))
    finally:
        await venue.close()
        await history.close()
