from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "noema.dashboard_app:app",
        host=os.getenv("NOEMA_DASHBOARD_HOST", "127.0.0.1"),
        port=int(os.getenv("NOEMA_DASHBOARD_PORT", "8787")),
        reload=False,
    )


if __name__ == "__main__":
    main()
