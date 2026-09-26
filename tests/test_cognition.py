import pytest

from noema.cognition import maybe_run_cognition
from noema.foundry_config import FoundryConfig
from tests.test_foundry_client import row


@pytest.mark.asyncio
async def test_cognition_is_safe_when_unconfigured(tmp_path) -> None:
    result = await maybe_run_cognition(
        [],
        db_path=str(tmp_path / "noema.db"),
        config=FoundryConfig(enabled=True),
    )
    assert result.status == "unconfigured"


@pytest.mark.asyncio
async def test_cognition_rejects_missing_verified_evidence_without_model_call(tmp_path) -> None:
    result = await maybe_run_cognition(
        [row()], db_path=str(tmp_path / "noema.db"),
        config=FoundryConfig(
            endpoint="https://resource.openai.azure.com", api_key="unused",
            deployment="reviewer",
        ),
    )
    assert result.status == "idle"
    assert "evidence" in (result.detail or "")
