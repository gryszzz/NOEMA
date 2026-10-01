"""Dedicated safe account telemetry logger for hosted runtime diagnostics."""

from __future__ import annotations

import logging


def account_logger() -> logging.Logger:
    """Return an INFO logger that writes account diagnostics without root config.

    Uvicorn configures its own loggers, not necessarily the root logger. Account
    health must remain observable even when the console process has no root
    handler. This logger emits only the safe status fields selected by callers.
    """
    logger = logging.getLogger("noema.account")
    logger.setLevel(logging.INFO)
    if not any(getattr(handler, "_noema_account_handler", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        handler._noema_account_handler = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    logger.propagate = False
    return logger
