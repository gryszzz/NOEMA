from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .economic_ledger import EconomicLedger
from .foundry_config import FoundryConfig
from .local_env import env_local_present


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    detail: str


def _configured(value: str | None) -> bool:
    return bool(value and value.strip())


def doctor_report(db_path: str = "data/noema.db") -> dict[str, Any]:
    checks: list[DoctorCheck] = []

    foundry = FoundryConfig.from_env()
    try:
        foundry.validate()
        foundry_valid = True
    except ValueError as exc:
        foundry_valid = False
        checks.append(DoctorCheck("foundry", "error", str(exc)))

    if foundry_valid:
        if foundry.ready:
            checks.append(
                DoctorCheck(
                    "foundry",
                    "ready",
                    f"deployment={foundry.deployment}; API key configured",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    "foundry",
                    "missing",
                    "endpoint, deployment and API key are required",
                )
            )

    kalshi_id = os.getenv("KALSHI_API_KEY_ID")
    kalshi_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if _configured(kalshi_id) and _configured(kalshi_path):
        exists = Path(str(kalshi_path)).expanduser().is_file()
        checks.append(
            DoctorCheck(
                "kalshi",
                "ready" if exists else "error",
                "API key ID configured; PEM exists"
                if exists
                else "API key ID configured but PEM path does not exist",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                "kalshi",
                "missing",
                "API key ID and private PEM path are required",
            )
        )

    evm_rpc = os.getenv("NOEMA_EVM_RPC_URL")
    evm_address = os.getenv("NOEMA_EVM_ADDRESS")
    if _configured(evm_rpc) and _configured(evm_address):
        checks.append(
            DoctorCheck(
                "evm",
                "ready",
                "RPC and public address configured",
            )
        )
    elif _configured(evm_rpc) or _configured(evm_address):
        checks.append(
            DoctorCheck(
                "evm",
                "error",
                "RPC and address must be configured together",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                "evm",
                "missing",
                "RPC and public address not configured",
            )
        )

    economic = EconomicLedger(db_path).latest_snapshot()
    checks.append(
        DoctorCheck(
            "economic_os",
            "ready" if economic is not None else "missing",
            "initialized"
            if economic is not None
            else "run noema economy-init --capital <amount>",
        )
    )

    ready_count = sum(check.status == "ready" for check in checks)
    return {
        "env_local_present": env_local_present(),
        "ready": ready_count == len(checks),
        "ready_count": ready_count,
        "total_checks": len(checks),
        "checks": [asdict(check) for check in checks],
    }
