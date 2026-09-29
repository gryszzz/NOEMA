from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "noema.dashboard_app:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        reload=False,
    )


if __name__ == "__main__":
    main()
