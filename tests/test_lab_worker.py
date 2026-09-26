import pytest

from noema.lab_worker import LabConfig, run_lab


@pytest.mark.asyncio
async def test_lab_rejects_too_fast_snapshot_interval() -> None:
    with pytest.raises(ValueError, match="snapshot_interval_seconds"):
        await run_lab(
            LabConfig(
                snapshot_interval_seconds=1,
                outcome_sync_interval_seconds=60,
                report_interval_seconds=60,
            )
        )
