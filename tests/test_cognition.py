import pytest

from noema.cognition import maybe_run_cognition
from noema.foundry_config import FoundryConfig


@pytest.mark.asyncio
async def test_cognition_is_safe_when_unconfigured(tmp_path) -> None:
    result = await maybe_run_cognition(
        [],
        db_path=str(tmp_path / "noema.db"),
        config=FoundryConfig(enabled=True),
    )
    assert result.status == "unconfigured"
