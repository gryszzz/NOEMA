from __future__ import annotations

import asyncio

from .agent_config import AgentConfig
from .agent_runtime import run_agent


def main() -> None:
    asyncio.run(run_agent(AgentConfig.from_env()))


if __name__ == "__main__":
    main()
