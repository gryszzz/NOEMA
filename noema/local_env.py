from __future__ import annotations

import os
from pathlib import Path

DEFAULT_LOCAL_ENV = ".env.local"


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_local_env(
    path: str = DEFAULT_LOCAL_ENV,
    *,
    override: bool = False,
) -> dict[str, str]:
    file = Path(path)
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


def env_local_present(path: str = DEFAULT_LOCAL_ENV) -> bool:
    return Path(path).exists()
