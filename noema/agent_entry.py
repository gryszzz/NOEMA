from __future__ import annotations

import asyncio
import os
from pathlib import Path

from .agent_config import AgentConfig
from .agent_runtime import run_agent
from .local_env import load_local_env


def main() -> None:
    os.chdir(Path(__file__).resolve().parents[1])
    load_local_env()
    asyncio.run(run_agent(AgentConfig.from_env()))


if __name__ == "__main__":
    main()
