from __future__ import annotations

from dataclasses import dataclass

from .models import MarketSnapshot


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    issues: tuple[str, ...]


def validate_market_snapshot(snapshot: MarketSnapshot) -> ValidationResult:
    issues: list[str] = []

    for name, value in (
        ("yes_bid", snapshot.yes_bid),
        ("yes_ask", snapshot.yes_ask),
        ("no_bid", snapshot.no_bid),
        ("no_ask", snapshot.no_ask),
    ):
        if value is not None and not 0 <= value <= 1:
            issues.append(f"{name} outside [0, 1]")

    if (
        snapshot.yes_bid is not None
        and snapshot.yes_ask is not None
        and snapshot.yes_bid > snapshot.yes_ask
    ):
        issues.append("YES book crossed")

    if (
        snapshot.no_bid is not None
        and snapshot.no_ask is not None
        and snapshot.no_bid > snapshot.no_ask
    ):
        issues.append("NO book crossed")

    if snapshot.liquidity_usd is not None and snapshot.liquidity_usd < 0:
        issues.append("negative liquidity")

    if not snapshot.title.strip():
        issues.append("missing market title")

    if not snapshot.resolution_rules or not snapshot.resolution_rules.strip():
        issues.append("missing resolution rules")

    return ValidationResult(valid=not issues, issues=tuple(issues))
