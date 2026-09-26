from __future__ import annotations

import asyncio

from .agent_config import AgentConfig
from .agent_runtime import run_agent
from .local_env import load_local_env


def main() -> None:
    load_local_env()
    asyncio.run(run_agent(AgentConfig.from_env()))


if __name__ == "__main__":
    main()
