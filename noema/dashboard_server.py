from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from .local_env import load_local_env


def main() -> None:
    os.chdir(Path(__file__).resolve().parents[1])
    load_local_env()
    uvicorn.run(
        "noema.dashboard_app:app",
        host=os.getenv("NOEMA_DASHBOARD_HOST", "127.0.0.1"),
        port=int(os.getenv("NOEMA_DASHBOARD_PORT", "8787")),
        reload=False,
    )


if __name__ == "__main__":
    main()
