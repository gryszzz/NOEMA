"""Evidence-backed proposition, settlement, and quote comparability.

This module describes evidence; it grants no forecast, order, or execution
authority. Unknown fields stay unknown, and every new assessment may revoke a
prior one when venue rules or observations change.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .canonical_market_identity import (
    CanonicalContractIdentity,
    identify_structured_contract,
    register_resolver,
)

PROPOSITION_FIELDS: dict[str, tuple[str, ...]] = {
    "winner": ("winner_id",),
    "occurrence_by_deadline": ("occurs", "deadline_utc"),
    "numeric_threshold": ("operator", "threshold", "unit", "period"),
    "numeric_bucket": ("lower", "upper", "unit", "period", "boundary_rule"),
    "asset_price_at_time": ("asset_id", "benchmark", "timestamp_utc", "operator", "threshold"),
    "economic_release_bucket": ("series_id", "release_period", "vintage", "lower", "upper", "unit", "boundary_rule"),
}
PROPOSITION_FAMILIES = tuple(sorted(PROPOSITION_FIELDS))

_KALSHI_TESLA_Q3 = re.compile(r"KXTSLA-26OCTDELIV-(?P<value>\d+)")
_POLY_TESLA_Q3 = re.compile(r"kpic-tsla-dlvrs-2026-q3-above-(?P<value>\d+)k")
_KALSHI_TESLA_RULE = re.compile(
    r"above\s+(?P<value>\d+)\s+total deliveries in q3 2026", re.IGNORECASE,
)
_POLY_TESLA_RULE = re.compile(
    r"deliveries greater than\s+(?P<value>\d+)\s+for the 3rd fiscal quarter of 2026",
    re.IGNORECASE,
)
_KALSHI_RAIN_ID = re.compile(r"KXRAIN-26SEP29-NYC")
_KALSHI_RAIN_RULE = re.compile(
    r"total precipitation at (?P<station>CLI[A-Z]+) in (?P<city>.+?) in "
    r"Sep 29, 2026 is strictly greater than 0 inches", re.IGNORECASE,
)
_KALSHI_BTC_BUCKET_ID = re.compile(r"KXBTC-26SEP2901-B\d+")
_KALSHI_BTC_BUCKET_RULE = re.compile(
    r"between (?P<lower>\d+(?:\.\d+)?)-(?P<upper>\d+(?:\.\d+)?) at 1 AM EDT "
    r"on Sep 29, 2026", re.IGNORECASE,
)
_KALSHI_ETH_TIME_ID = re.compile(r"KXETH-26SEP2901-T(?P<value>\d+(?:\.\d+)?)")
_KALSHI_ETH_TIME_RULE = re.compile(
    r"CF Benchmarks' Ethereum Real-Time Index \(ERTI\).*?before 1 AM EDT is below "
    r"(?P<value>\d+(?:\.\d+)?) at 1 AM EDT on Sep 29, 2026", re.IGNORECASE,
)


def identify_explicit_proposition(
    *, venue: str, contract_id: str, topic_id: str, event_id: str,
    proposition_type: str, fields: dict[str, Any], source_rule_version: str | None = None,
    resolution_rules: str | None = None,
) -> CanonicalContractIdentity:
    """Canonicalize a family only from explicit source-mapped fields.

    The caller must map source schema identifiers and rule text into these
    fields. This helper does not parse or fuzzy-match titles.
    """
    required = PROPOSITION_FIELDS.get(proposition_type)
    if required is None or any(fields.get(key) is None for key in required):
        return identify_structured_contract(
            venue=venue, contract_id=contract_id, topic_id=topic_id,
            canonical_event_id=None, proposition_type=proposition_type,
            canonical_outcome_id=None, resolution_rules=resolution_rules,
            source_rule_version=source_rule_version,
        )
    canonical = json.dumps(
        {key: fields[key] for key in required}, sort_keys=True,
        separators=(",", ":"), default=str,
    )
    outcome = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    return identify_structured_contract(
        venue=venue, contract_id=contract_id, topic_id=topic_id,
        canonical_event_id=event_id, proposition_type=proposition_type,
        canonical_outcome_id=outcome, resolution_rules=resolution_rules,
        source_rule_version=source_rule_version,
    )


def identify_tesla_q3_deliveries_threshold(
    *, venue: str, contract_id: str, resolution_rules: str | None,
) -> CanonicalContractIdentity:
    """Resolve only the two observed, venue-specific Q3 delivery ID schemas.

    Both the strict instrument identifier and rule-text threshold must agree.
    Similar titles or another Tesla metric are insufficient.
    """
    if venue == "kalshi":
        id_match = _KALSHI_TESLA_Q3.fullmatch(contract_id)
        rule_match = _KALSHI_TESLA_RULE.search(resolution_rules or "")
        value = None if id_match is None else int(id_match.group("value"))
    elif venue == "polymarket-us":
        id_match = _POLY_TESLA_Q3.fullmatch(contract_id)
        rule_match = _POLY_TESLA_RULE.search(resolution_rules or "")
        value = None if id_match is None else int(id_match.group("value")) * 1000
    else:
        id_match = rule_match = None
        value = None
    if id_match is None or rule_match is None or value != int(rule_match.group("value")):
        return identify_structured_contract(
            venue=venue, contract_id=contract_id, topic_id="company:automotive",
            canonical_event_id=None, proposition_type="numeric_threshold",
            canonical_outcome_id=None, resolution_rules=resolution_rules,
            source_rule_version="strict-ticker-and-rule-v1",
        )
    return identify_explicit_proposition(
        venue=venue, contract_id=contract_id, topic_id="company:automotive",
        event_id="company:tesla:deliveries:2026-Q3", proposition_type="numeric_threshold",
        fields={
            "operator": ">", "threshold": str(value),
            "unit": "vehicle deliveries", "period": "2026-Q3",
        }, resolution_rules=resolution_rules,
        source_rule_version="strict-ticker-and-rule-v1",
    )


def _registered_tesla_resolver(**kwargs: Any) -> CanonicalContractIdentity:
    return identify_tesla_q3_deliveries_threshold(**kwargs)


register_resolver("company:automotive", "q3_deliveries_numeric_threshold", _registered_tesla_resolver)


def identify_kalshi_rain_occurrence_by_deadline(
    *, venue: str, contract_id: str, resolution_rules: str | None, close_time: str | None,
) -> CanonicalContractIdentity:
    id_match = _KALSHI_RAIN_ID.fullmatch(contract_id) if venue == "kalshi" else None
    rule_match = _KALSHI_RAIN_RULE.search(resolution_rules or "")
    expected_station = "CLINYC"
    valid = bool(
        id_match and rule_match and rule_match.group("station").upper() == expected_station
        and rule_match.group("city").strip().casefold() == "new york city"
        and close_time and close_time.startswith("2026-09-30T05:00:00")
    )
    if not valid:
        return identify_structured_contract(
            venue=venue, contract_id=contract_id, topic_id="weather:precipitation",
            canonical_event_id=None, proposition_type="occurrence_by_deadline",
            canonical_outcome_id=None, resolution_rules=resolution_rules,
            source_rule_version="kalshi-rain-explicit-rule-v1",
        )
    station = expected_station.lower()
    return identify_explicit_proposition(
        venue=venue, contract_id=contract_id, topic_id="weather:precipitation",
        event_id=f"weather:station:{station}:2026-09-29",
        proposition_type="occurrence_by_deadline",
        fields={"occurs": "daily_total_precipitation_gt_0_inches", "deadline_utc": close_time},
        resolution_rules=resolution_rules, source_rule_version="kalshi-rain-explicit-rule-v1",
    )


def identify_kalshi_btc_numeric_bucket(
    *, venue: str, contract_id: str, resolution_rules: str | None,
    close_time: str | None,
) -> CanonicalContractIdentity:
    id_match = _KALSHI_BTC_BUCKET_ID.fullmatch(contract_id) if venue == "kalshi" else None
    rule_match = _KALSHI_BTC_BUCKET_RULE.search(resolution_rules or "")
    valid = bool(
        id_match and rule_match and "CF Benchmarks' Bitcoin Real-Time Index (BRTI)" in (resolution_rules or "")
        and close_time and close_time.startswith("2026-09-29T05:00:00")
    )
    if not valid:
        return identify_structured_contract(
            venue=venue, contract_id=contract_id, topic_id="asset:bitcoin:price",
            canonical_event_id=None, proposition_type="numeric_bucket",
            canonical_outcome_id=None, resolution_rules=resolution_rules,
            source_rule_version="kalshi-btc-bucket-explicit-rule-v1",
        )
    fields = {
        "lower": rule_match.group("lower"), "upper": rule_match.group("upper"),
        "unit": "USD/BTC CF-BRTI", "period": close_time,
        # The current primary rule says "between" and the source schema does
        # not clarify endpoint inclusivity; never guess bucket boundaries.
        "boundary_rule": None,
    }
    return identify_explicit_proposition(
        venue=venue, contract_id=contract_id, topic_id="asset:bitcoin:price",
        event_id=f"asset:bitcoin:CF-BRTI:{close_time}", proposition_type="numeric_bucket",
        fields=fields, resolution_rules=resolution_rules,
        source_rule_version="kalshi-btc-bucket-explicit-rule-v1",
    )


def identify_kalshi_eth_price_at_timestamp(
    *, venue: str, contract_id: str, resolution_rules: str | None,
    close_time: str | None,
) -> CanonicalContractIdentity:
    id_match = _KALSHI_ETH_TIME_ID.fullmatch(contract_id) if venue == "kalshi" else None
    rule_match = _KALSHI_ETH_TIME_RULE.search(resolution_rules or "")
    valid = bool(
        id_match and rule_match and id_match.group("value") == rule_match.group("value")
        and close_time and close_time.startswith("2026-09-29T05:00:00")
    )
    if not valid:
        return identify_structured_contract(
            venue=venue, contract_id=contract_id, topic_id="asset:ethereum:price",
            canonical_event_id=None, proposition_type="asset_price_at_time",
            canonical_outcome_id=None, resolution_rules=resolution_rules,
            source_rule_version="kalshi-eth-timestamp-explicit-rule-v1",
        )
    return identify_explicit_proposition(
        venue=venue, contract_id=contract_id, topic_id="asset:ethereum:price",
        event_id=f"asset:ethereum:CF-ERTI:{close_time}",
        proposition_type="asset_price_at_time",
        fields={
            "asset_id": "ETH", "benchmark": "CF-ERTI", "timestamp_utc": close_time,
            "operator": "<", "threshold": id_match.group("value"),
        }, resolution_rules=resolution_rules,
        source_rule_version="kalshi-eth-timestamp-explicit-rule-v1",
    )


def _registered_rain_resolver(**kwargs: Any) -> CanonicalContractIdentity:
    return identify_kalshi_rain_occurrence_by_deadline(**kwargs)


def _registered_eth_timestamp_resolver(**kwargs: Any) -> CanonicalContractIdentity:
    return identify_kalshi_eth_price_at_timestamp(**kwargs)


register_resolver("weather:precipitation", "daily_station_precipitation_occurrence", _registered_rain_resolver)
register_resolver("asset:ethereum:price", "eth_price_at_timestamp", _registered_eth_timestamp_resolver)


@dataclass(frozen=True)
class SettlementTerms:
    """Normalized rule facts plus evidence references, not raw credentials."""

    authoritative_resolution_source: str | None = None
    measurement_publication_source: str | None = None
    cutoff_timestamp_utc: str | None = None
    timezone: str | None = None
    preliminary_or_final: str | None = None
    revision_correction_policy: str | None = None
    cancellation_policy: str | None = None
    postponement_policy: str | None = None
    rescheduling_policy: str | None = None
    tie_dead_heat_policy: str | None = None
    rounding_policy: str | None = None
    exceptional_conditions: str | None = None
    expiration_policy: str | None = None
    dispute_handling: str | None = None
    evidence_ref: str | None = None
    evidence_sha256: str | None = None
    rule_version: str | None = None
    observed_at: str | None = None


SETTLEMENT_FIELDS = tuple(k for k in SettlementTerms.__dataclass_fields__ if k not in {
    "evidence_ref", "evidence_sha256", "rule_version", "observed_at",
})


def assess_settlement_equivalence(left: SettlementTerms, right: SettlementTerms) -> dict[str, Any]:
    field_states: dict[str, str] = {}
    differences: list[str] = []
    unknown: list[str] = []
    missing_evidence: list[str] = []
    for field in SETTLEMENT_FIELDS:
        a, b = getattr(left, field), getattr(right, field)
        if a is None or b is None:
            field_states[field] = "unverified"
            unknown.append(field)
        elif str(a).strip().casefold() == str(b).strip().casefold():
            field_states[field] = "matched"
        else:
            field_states[field] = "mismatch"
            differences.append(field)
    for side, terms in (("left", left), ("right", right)):
        if not terms.evidence_ref:
            missing_evidence.append(f"{side}.evidence_ref")
        try:
            valid_hash = bool(terms.evidence_sha256 and len(bytes.fromhex(terms.evidence_sha256)) == 32)
        except ValueError:
            valid_hash = False
        if not valid_hash:
            missing_evidence.append(f"{side}.evidence_sha256")
        if not terms.rule_version:
            missing_evidence.append(f"{side}.rule_version")
        if not terms.observed_at:
            missing_evidence.append(f"{side}.observed_at")
    status = "mismatch" if differences else "unverified" if unknown or missing_evidence else "confirmed"
    evidence = {
        "left": asdict(left), "right": asdict(right),
        "field_states": field_states, "status": status,
        "differences": differences, "unknown_fields": unknown,
        "missing_evidence": missing_evidence,
        "assessed_at": datetime.now(UTC).isoformat(),
    }
    evidence["assessment_sha256"] = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    return evidence


@dataclass(frozen=True)
class EconomicQuote:
    fee_model: str | None = None
    denomination: str | None = None
    bid: str | None = None
    ask: str | None = None
    quote_timestamp: str | None = None
    venue_timestamp: str | None = None
    freshness_seconds: int | None = None
    available_depth: str | None = None
    minimum_size: str | None = None
    maximum_usable_size: str | None = None
    expected_slippage: str | None = None
    capital_lock_seconds: int | None = None
    evidence_ref: str | None = None
    observed_at: str | None = None


ECONOMIC_REQUIRED = tuple(k for k in EconomicQuote.__dataclass_fields__ if k not in {
    "evidence_ref", "observed_at",
})


def assess_economic_comparability(
    left: EconomicQuote, right: EconomicQuote, *, settlement_status: str,
) -> dict[str, Any]:
    unknown = [f"left.{key}" for key in ECONOMIC_REQUIRED if getattr(left, key) is None]
    unknown.extend(f"right.{key}" for key in ECONOMIC_REQUIRED if getattr(right, key) is None)
    invalid: list[str] = []
    for side, quote in (("left", left), ("right", right)):
        for key in ("fee_model", "denomination", "evidence_ref", "observed_at"):
            value = getattr(quote, key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                invalid.append(f"{side}.{key}")
        for key in ("bid", "ask", "available_depth", "minimum_size", "maximum_usable_size", "expected_slippage"):
            value = getattr(quote, key)
            if value is None:
                continue
            try:
                number = Decimal(value)
                if not number.is_finite() or number < 0:
                    raise ValueError
            except (InvalidOperation, TypeError, ValueError):
                invalid.append(f"{side}.{key}")
        if quote.bid is not None and quote.ask is not None:
            try:
                if Decimal(quote.bid) > Decimal(quote.ask):
                    invalid.append(f"{side}.bid_ask_order")
            except (InvalidOperation, TypeError, ValueError):
                pass
        if quote.minimum_size is not None and quote.maximum_usable_size is not None:
            try:
                if Decimal(quote.minimum_size) <= 0 or Decimal(quote.maximum_usable_size) < Decimal(quote.minimum_size):
                    invalid.append(f"{side}.usable_size_bounds")
            except (InvalidOperation, TypeError, ValueError):
                pass
        for key in ("freshness_seconds", "capital_lock_seconds"):
            value = getattr(quote, key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                invalid.append(f"{side}.{key}")
        for key in ("quote_timestamp", "venue_timestamp"):
            value = getattr(quote, key)
            if value is None:
                continue
            try:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    raise ValueError
            except (AttributeError, TypeError, ValueError):
                invalid.append(f"{side}.{key}")
    if not left.evidence_ref or not left.observed_at:
        unknown.append("left.quote_evidence")
    if not right.evidence_ref or not right.observed_at:
        unknown.append("right.quote_evidence")
    if settlement_status != "confirmed":
        unknown.append("settlement_equivalence")
    status = "comparable" if not unknown and not invalid else "unavailable"
    return {
        "status": status, "executable_comparability": "unavailable",
        "unknown_fields": unknown, "invalid_fields": invalid,
        "left": asdict(left), "right": asdict(right),
        "reason": "Executable edge requires confirmed settlement equivalence and complete, fresh venue economics." if unknown else
                  "Economic comparison inputs are complete; deterministic execution policy remains a separate gate.",
    }


def canonical_observation_event(observation: dict[str, Any]) -> dict[str, Any]:
    """Return a DTO for the shared EconomicLedger; do not create a second ledger."""
    identity = observation.get("canonical_identity", {}).get("comparison", {})
    observation_hash = hashlib.sha256(
        json.dumps(observation, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    return {
        "event_type": "canonical_market_observation",
        "amount_usd": None,
        "payload": {
            "observed_at": observation.get("observed_at"),
            "canonical_event_id": identity.get("canonical_event_id"),
            "canonical_proposition_id": identity.get("canonical_proposition_id"),
            "semantic_match": identity.get("semantic_match", "unverified"),
            "settlement_equivalence": identity.get("settlement_equivalence", "unverified"),
            "economic_comparability": identity.get("economic_comparability", "unavailable"),
            "source_observation_hash": observation_hash,
        },
    }


def append_shared_economic_event(
    ledger: Any, event: dict[str, Any], *, amount_usd: Decimal | None = None,
) -> None:
    """Send an economic fact to NOEMA's existing ledger with provenance intact."""
    event_type = event.get("event_type")
    payload = event.get("payload")
    if not isinstance(event_type, str) or not event_type or not isinstance(payload, dict):
        raise ValueError("shared economic event requires a type and provenance payload")
    ledger.append_event(event_type, amount_usd=amount_usd, payload=payload)
