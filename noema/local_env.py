from __future__ import annotations

import os
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCAL_ENV = REPOSITORY_ROOT / ".env.local"


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_local_env(
    path: str | Path | None = None,
    *,
    override: bool = False,
) -> dict[str, str]:
    file = Path(path) if path is not None else DEFAULT_LOCAL_ENV
    if not file.exists():
        return {}

    loaded: dict[str, str] = {}
    for raw_line in file.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = _unquote(value)
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded


def env_local_present(path: str | Path | None = None) -> bool:
    file = Path(path) if path is not None else DEFAULT_LOCAL_ENV
    return file.exists()
