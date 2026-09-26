from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime

from .engine import CostModel
from .grounding import GroundingPolicy, validate_forecast_grounding
from .ledger import ForecastLedger
from .models import Action, Decision, Forecast, MarketSnapshot, Opportunity
from .outcomes import OutcomeStore
from .provenance import EvidenceStore

MODEL_VERSION = "series-frequency-v1"
_SERIES = re.compile(r"^[A-Z][A-Z0-9]{2,30}$")


def record_history_candidate(
    market: MarketSnapshot,
    *,
    outcomes: OutcomeStore,
    evidence: EvidenceStore,
    ledger: ForecastLedger,
    min_events: int = 30,
    current_event_size: int = 1,
    verified_market_ids: frozenset[str] | None = None,
) -> bool:
    """Paper-only outcome frequency for homogeneous one- or two-market events."""
    if market.venue not in {"kalshi:demo", "kalshi:production"} or market.yes_ask is None:
        return False
    if market.captured_at.tzinfo is None:
        return False
    series, sep, _ = market.market_id.partition("-")
    if not sep or not _SERIES.fullmatch(series):
        return False
    if (
        verified_market_ids is None
        or market.market_id not in verified_market_ids
        or len(verified_market_ids) != current_event_size
        or any(
            not ticker.startswith(market.market_id.rsplit("-", 1)[0] + "-")
            for ticker in verified_market_ids
        )
    ):
        return False
    if ledger.has_model_forecast(market.venue, market.market_id, MODEL_VERSION):
        return False

    rows = outcomes.conn.execute(
        """
        SELECT market_id, outcome_yes, resolved_at, first_seen_at, raw_json
        FROM outcomes WHERE venue = ? AND market_id LIKE ?
        """,
        (market.venue, series + "-%"),
    ).fetchall()
    cutoff = market.captured_at.astimezone(UTC)
    by_event: dict[str, list[tuple[str, int, str]]] = {}
    latest_seen: datetime | None = None
    for ticker, outcome, settled, seen, raw_json in rows:
        if not settled or not seen:
            continue
        try:
            settled_at = datetime.fromisoformat(settled)
            seen_at = datetime.fromisoformat(seen)
        except ValueError:
            continue
        if settled_at.tzinfo is None or seen_at.tzinfo is None:
            continue
        if not (settled_at < cutoff and seen_at < cutoff):
            continue
        raw = json.loads(raw_json)
        event = raw.get("event_ticker")
        if not isinstance(event, str) or not event.startswith(series + "-"):
            continue
        if raw.get("mve_collection_ticker"):
            continue
        by_event.setdefault(event, []).append((ticker, int(outcome), event))
        latest_seen = max(latest_seen or seen_at, seen_at)

    if any(len(group) > 2 for group in by_event.values()):
        return False
    if current_event_size == 1:
        # A series containing any two-market event is not a single-market family.
        if any(len(group) != 1 for group in by_event.values()):
            return False
        samples = [group[0] for group in by_event.values()]
    elif current_event_size == 2:
        # Incomplete sync pages can leave a one-market event; only use complete
        # prior pairs where the two YES outcomes are complementary.
        pairs = [
            group for group in by_event.values()
            if len(group) == 2 and sum(row[1] for row in group) == 1
        ]
        if len(pairs) < min_events or len(pairs) < 0.9 * len(by_event):
            return False
        samples = [row for group in pairs for row in group]
    else:
        return False
    if len(samples) < min_events * current_event_size:
        return False
    if latest_seen is None:
        return False
    yes = sum(result for _, result, _ in samples)
    n = len(samples)
    p = (yes + 1) / (n + 2)  # Beta(1, 1) posterior mean.
    half_width = max(0.10, 1.96 * math.sqrt(p * (1 - p) / (n + 3)))

    payload = {
        "series": series, "model": MODEL_VERSION, "yes": yes,
        "events": n // current_event_size, "markets_per_event": current_event_size,
        "sample": sorted(samples), "as_of": cutoff.isoformat(),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]
    evidence_id = f"history:{market.market_id}:{digest}"
    evidence.append(
        evidence_id=evidence_id, source="Kalshi settled market outcomes",
        source_type="historical_outcomes", observed_at=latest_seen, payload=payload,
    )
    forecast = Forecast(
        market_id=market.market_id, venue=market.venue,
        probability_yes=p, lower_bound=max(0.0, p - half_width),
        upper_bound=min(1.0, p + half_width), model_version=MODEL_VERSION,
        evidence_ids=(evidence_id,),
    )
    grounding = validate_forecast_grounding(
        forecast, evidence,
        policy=GroundingPolicy(
            max_evidence_age_seconds=math.inf,
            allowed_source_types=frozenset({"historical_outcomes"}),
        ),
        now=cutoff,
    )
    if not grounding.grounded:
        return False

    costs = CostModel().estimate(market)
    uncertainty = (forecast.upper_bound - forecast.lower_bound) / 2
    opportunity = Opportunity(
        forecast=forecast, snapshot=market,
        market_probability=market.yes_ask, raw_edge=p - market.yes_ask,
        estimated_cost=costs, uncertainty_penalty=uncertainty,
        robust_edge=p - market.yes_ask - costs - uncertainty,
    )
    ledger.append(
        market, forecast, opportunity,
        Action(
            decision=Decision.PASS, market_id=market.market_id,
            venue=market.venue, max_price=None, stake_usd=0.0,
            reason="History-frequency candidate is paper-only pending paired evaluation",
        ),
    )
    return True
