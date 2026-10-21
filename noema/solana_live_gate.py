"""Owner gates for live Solana desk swaps.

The desk cycle stays on paper unless every flag below is set. The signer process
reads the same flags itself and does not trust the agent to assert them.
"""

from __future__ import annotations

import os

LIVE_VENUE_NAMES = frozenset({"solana", "solana_jupiter"})


class DeskSwapRejected(RuntimeError):
    """A live desk swap did not execute. The message is safe to persist."""


def live_venue_names() -> set[str]:
    return {
        item.strip()
        for item in os.getenv("NOEMA_LIVE_VENUES", "").split(",")
        if item.strip()
    }


def solana_venue_selected() -> bool:
    return bool(live_venue_names() & LIVE_VENUE_NAMES)


def solana_desk_live_enabled() -> bool:
    """True when desk entries and exits must go to the signer, not paper fills."""
    return (
        os.getenv("NOEMA_SOLANA_SIGNER_ENABLED", "0") == "1"
        and os.getenv("NOEMA_ALLOW_LIVE_ORDERS", "0") == "1"
        and solana_venue_selected()
    )


def master_halt_active() -> bool:
    """Fail closed. Solana and wallet halts default to halted unless set to 0."""
    return (
        os.getenv("NOEMA_MASTER_HALT", "0") == "1"
        or os.getenv("NOEMA_SOLANA_MASTER_HALT", "1") != "0"
        or os.getenv("NOEMA_WALLET_MASTER_HALT", "1") != "0"
    )
