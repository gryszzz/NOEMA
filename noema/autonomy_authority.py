"""Map earned autonomy to prediction-market execution authority.

The table lives in ``config/autonomy_authority.toml``. Live authority starts at
MICRO. SHADOW, PAPER, and DEMO never receive it. ``NOEMA_MASTER_HALT`` blocks
live permission before any other gate. ``NOEMA_LIVE_REQUIRES_OWNER_REVIEW``
defaults to required, so an earned MICRO level does not unlock live until the
owner sets that flag to ``0``. Gateway arming flags remain a separate fail-closed
check. ``NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD``, when set, overrides the level's
daily bankroll fraction.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

from .economic_models import AutonomyLevel
from .execution_gateway import PredictionExecutionAuthority
from .models import Mode
from .risk import RiskPolicy

_LEVEL_ORDER = (
    AutonomyLevel.SHADOW,
    AutonomyLevel.PAPER,
    AutonomyLevel.DEMO,
    AutonomyLevel.MICRO,
    AutonomyLevel.PROVEN,
    AutonomyLevel.SELF_FUNDED,
    AutonomyLevel.EXPANSION,
)
_LIVE_LEVELS = frozenset({
    AutonomyLevel.MICRO,
    AutonomyLevel.PROVEN,
    AutonomyLevel.SELF_FUNDED,
    AutonomyLevel.EXPANSION,
})


def level_at_least(level: AutonomyLevel, minimum: AutonomyLevel) -> bool:
    return _LEVEL_ORDER.index(level) >= _LEVEL_ORDER.index(minimum)


@dataclass(frozen=True)
class LevelAuthority:
    level: AutonomyLevel
    live: bool
    per_trade_fraction: Decimal | None
    daily_notional_fraction: Decimal | None


def authority_config_path() -> Path:
    override = os.getenv("NOEMA_AUTONOMY_AUTHORITY_CONFIG", "").strip()
    if override:
        return Path(override)
    candidates = (
        Path.cwd() / "config" / "autonomy_authority.toml",
        Path(__file__).resolve().parents[1] / "config" / "autonomy_authority.toml",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[-1]


@lru_cache(maxsize=8)
def load_authority_table(path: str) -> dict[AutonomyLevel, LevelAuthority]:
    """Load the authority table. A missing or malformed file grants nothing."""
    source = Path(path)
    if not source.is_file():
        return {}
    try:
        payload = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    table: dict[AutonomyLevel, LevelAuthority] = {}
    for level in AutonomyLevel:
        row = payload.get(level.value)
        parsed = _parse_level(level, row)
        if parsed is not None:
            table[level] = parsed
    return table


def authority_table() -> dict[AutonomyLevel, LevelAuthority]:
    return load_authority_table(str(authority_config_path()))


def master_halt_active() -> bool:
    return os.getenv("NOEMA_MASTER_HALT", "0") == "1"


def owner_review_required() -> bool:
    """Rung 8 stays locked unless the owner explicitly sets the flag to 0."""
    return os.getenv("NOEMA_LIVE_REQUIRES_OWNER_REVIEW", "1") != "0"


def gateway_live_flags_open() -> tuple[bool, tuple[str, ...]]:
    """Execution flags only. Halt and owner review are reported separately."""
    reasons: list[str] = []
    if os.getenv("NOEMA_EXECUTION_GATEWAY_ENABLED", "0") != "1":
        reasons.append("execution gateway is disabled")
    if os.getenv("NOEMA_ALLOW_LIVE_ORDERS", "0") != "1":
        reasons.append("owner live-order setting is disabled")
    venues = {
        item.strip() for item in os.getenv("NOEMA_LIVE_VENUES", "").split(",") if item.strip()
    }
    if not venues:
        reasons.append("venue is not explicitly allowlisted")
    return (not reasons, tuple(reasons))


def live_permission(level: AutonomyLevel | None) -> tuple[bool, tuple[str, ...]]:
    """Whether this level may size a live order right now.

    Halt wins over an earned level, an owner-review override, and open gateway
    flags. No level, a non-live level, a required owner review, or a closed
    gateway flag each deny live permission.
    """
    if master_halt_active():
        return False, ("owner master halt is active",)
    if level is None or level not in _LIVE_LEVELS:
        name = "absent" if level is None else level.value
        return False, (f"{name} does not permit live execution",)
    row = authority_table().get(level)
    if row is None or not row.live or row.per_trade_fraction is None or row.daily_notional_fraction is None:
        return False, ("autonomy authority config does not grant this level",)
    if owner_review_required():
        return False, ("owner review is required before live execution",)
    flags_open, flag_reasons = gateway_live_flags_open()
    if not flags_open:
        return False, flag_reasons
    return True, ()


def console_trading_status() -> dict[str, object]:
    """Secret-free coordinator projection for the local console."""
    halted = master_halt_active()
    review = owner_review_required()
    flags_open, _reasons = gateway_live_flags_open()
    if halted:
        status = "TRADING COORDINATOR WIRED · MASTER HALT · LIVE EXECUTION DISABLED"
    elif flags_open and not review:
        status = "TRADING COORDINATOR WIRED · LIVE GATES OPEN · EARNED AUTHORITY STILL REQUIRED"
    else:
        status = "TRADING COORDINATOR WIRED · PAPER DEFAULT · LIVE EXECUTION DISABLED"
    return {
        "status": status,
        "trading_coordinator_wired": True,
        "trading_live_gates_open": bool(flags_open and not review and not halted),
        "owner_review_required": review,
        "master_halt": halted,
    }


def authority_for(
    level: AutonomyLevel | None,
    bankroll_usd: Decimal | float | str,
    *,
    mission_id: str,
    allowed_venues: frozenset[str],
    allowed_instruments: frozenset[str],
    now: datetime | None = None,
    ttl: timedelta = timedelta(minutes=5),
) -> PredictionExecutionAuthority | None:
    """Build mission authority for one cycle, or None when live is not permitted."""
    permitted, _reasons = live_permission(level)
    if not permitted or level is None:
        return None
    if not mission_id.strip() or not allowed_venues or not allowed_instruments:
        return None
    bankroll = _positive_decimal(bankroll_usd)
    if bankroll is None:
        return None
    row = authority_table().get(level)
    if row is None or row.per_trade_fraction is None or row.daily_notional_fraction is None:
        return None
    per_trade = _money(bankroll * row.per_trade_fraction)
    daily = effective_daily_cap(level, bankroll)
    if per_trade is None or daily is None or per_trade <= 0 or daily <= 0:
        return None
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        return None
    return PredictionExecutionAuthority(
        authority_id=f"autonomy:{level.value}:{mission_id}",
        mission_id=mission_id,
        allowed_venues=allowed_venues,
        allowed_actions=frozenset({"buy_yes"}),
        allowed_instruments=allowed_instruments,
        per_action_limit_usd=per_trade,
        per_mission_limit_usd=daily,
        daily_limit_usd=daily,
        maximum_exposure_usd=daily,
        expires_at=moment + ttl,
        revoked=False,
    )


def effective_daily_cap(level: AutonomyLevel, bankroll_usd: Decimal) -> Decimal | None:
    """Level daily fraction, replaced when the owner sets an explicit cap."""
    row = authority_table().get(level)
    if row is None or row.daily_notional_fraction is None:
        return None
    fraction_cap = _money(bankroll_usd * row.daily_notional_fraction)
    if fraction_cap is None:
        return None
    raw = os.getenv("NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD")
    if raw is None or str(raw).strip() == "":
        return fraction_cap
    override = _decimal(raw)
    if override is None:
        return None
    if override <= 0:
        return Decimal(0)
    return min(override, fraction_cap)


def per_trade_cap(level: AutonomyLevel, bankroll_usd: Decimal) -> Decimal | None:
    row = authority_table().get(level)
    if row is None or row.per_trade_fraction is None:
        return None
    return _money(bankroll_usd * row.per_trade_fraction)


def risk_policy_for_level(
    base: RiskPolicy,
    level: AutonomyLevel | None,
    bankroll_usd: Decimal | float | str,
    *,
    live: bool,
) -> RiskPolicy:
    """Paper keeps the caller's policy. Live sizing is the level fraction."""
    if not live or level is None:
        return replace(base, mode=Mode.PAPER)
    bankroll = _positive_decimal(bankroll_usd)
    row = None if bankroll is None else authority_table().get(level)
    if bankroll is None or row is None or row.per_trade_fraction is None:
        return replace(base, mode=Mode.PAPER, max_stake_usd=0)
    cap = per_trade_cap(level, bankroll)
    if cap is None or cap <= 0:
        return replace(base, mode=Mode.PAPER, max_stake_usd=0)
    fraction = float(row.per_trade_fraction)
    return replace(
        base,
        mode=Mode.LIVE,
        max_stake_usd=float(cap),
        max_fraction_of_bankroll=fraction,
    )


def _parse_level(level: AutonomyLevel, row: object) -> LevelAuthority | None:
    if not isinstance(row, dict) or not isinstance(row.get("live"), bool):
        return None
    if not row["live"]:
        return LevelAuthority(level, False, None, None)
    per_trade = _fraction(row.get("per_trade_fraction"))
    daily = _fraction(row.get("daily_notional_fraction"))
    if per_trade is None or daily is None or daily < per_trade:
        return None
    return LevelAuthority(level, True, per_trade, daily)


def _fraction(value: object) -> Decimal | None:
    number = _decimal(value)
    if number is None or not Decimal(0) < number <= Decimal(1):
        return None
    return number


def _positive_decimal(value: object) -> Decimal | None:
    number = _decimal(value)
    if number is None or number <= 0:
        return None
    return number


def _money(value: Decimal) -> Decimal | None:
    if not value.is_finite() or value < 0:
        return None
    return value.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    return number
