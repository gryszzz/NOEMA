"""Auditable, paper-only cross-venue contract comparison.

This module evaluates only evidence present in a frozen observation. Missing
rules, fees, source timestamps, or normalized book depth stay unknown and fail
closed. It has no venue write capability.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .ledger import ForecastLedger
from .research_trials import ResearchTrialStore

EXPERIMENT_VERSION = "cross-venue-paper-v1"
FRESHNESS_THRESHOLD_SECONDS = 5.0
REQUIRED_RULE_CLAUSES = (
    "resolution_condition", "outcome_definition", "settlement_authority",
    "event_timing", "postponement", "cancellation", "void_refund", "edge_cases",
    "rule_revisions", "settlement_rounding", "exceptional_conditions",
)
_CLAUSE_TERMS = {
    "resolution_condition": r"settle|resolv|winner|outcome",
    "outcome_definition": r"yes if|no if|will be|wins|winner|qualif|champion",
    "settlement_authority": r"source|official|provider|league|exchange|authority|data from",
    "event_timing": r"scheduled|by |before|after|through|between|date|time|season",
    "postponement": r"postpon|reschedul|delay|suspend",
    "cancellation": r"cancel|abandon|walkover|forfeit|not begin|not start",
    "void_refund": r"void|refund|return|fair market|no contest",
    "edge_cases": r"tie|draw|overtime|shorten|doubleheader|correction|amend",
    "rule_revisions": r"revision|version|amend|updated rule|change to these rules",
    "settlement_rounding": r"round|nearest|precision|decimal|fraction|tie.break",
    "exceptional_conditions": r"exception|dispute|review|extraordinary|sole discretion|unusual",
}
_AMBIGUITY_TERMS = re.compile(
    r"may settle|fair market|sole discretion|including but not limited|as determined by",
    re.IGNORECASE,
)


def extract_clause_evidence(
    venue: str,
    market_id: str,
    text: str | None,
    source_url: str | None,
    *,
    source_field: str | None = None,
) -> dict[str, Any]:
    """Extract source sentences for review; never infer missing legal clauses."""
    source_text = text.strip() if isinstance(text, str) else ""
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", source_text) if part.strip()]
    text_hash = hashlib.sha256(source_text.encode()).hexdigest() if source_text else None
    clauses: dict[str, Any] = {}
    for name, terms in _CLAUSE_TERMS.items():
        excerpts = [sentence for sentence in sentences if re.search(terms, sentence, re.IGNORECASE)]
        excerpt = " ".join(excerpts) if excerpts else None
        ambiguous = bool(excerpt and _AMBIGUITY_TERMS.search(excerpt))
        clauses[name] = {
            "text": excerpt,
            "status": "unresolved" if not excerpt or ambiguous else "extracted_unverified",
            "citation": {
                "source": f"{venue} official market detail",
                "source_url": source_url,
                "market_id": market_id,
                "field": source_field or ("rules_primary" if venue == "kalshi" else "description"),
                "text_sha256": text_hash,
                "excerpt": excerpt,
            },
        }
    clauses["_source_text"] = source_text or None
    return clauses


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _walk_book(levels: Any, *, target: Decimal, complement: bool = False) -> tuple[Decimal, Decimal, list[dict[str, str]]]:
    """Walk normalized price/contract levels without extending observed depth."""
    remaining, cost = target, Decimal(0)
    fills: list[dict[str, str]] = []
    if not isinstance(levels, list):
        return Decimal(0), Decimal(0), fills
    for level in levels:
        if not isinstance(level, dict):
            continue
        try:
            price = Decimal(str(level["price"]))
            quantity = Decimal(str(level["contracts"]))
            if complement:
                price = Decimal(1) - price
            if not (price.is_finite() and quantity.is_finite() and 0 < price < 1 and quantity > 0):
                continue
        except (KeyError, InvalidOperation, ValueError):
            continue
        taken = min(remaining, quantity)
        cost += taken * price
        remaining -= taken
        fills.append({"price": str(price), "contracts": str(taken)})
        if remaining <= 0:
            break
    return target - remaining, cost, fills


def _depth_matches_quote(book: Any, *, bid: Any, ask: Any) -> bool:
    if not isinstance(book, dict) or not isinstance(book.get("yes_asks"), list) or not isinstance(book.get("yes_bids"), list):
        return False
    try:
        asks = [Decimal(str(item["price"])) for item in book["yes_asks"] if isinstance(item, dict)]
        bids = [Decimal(str(item["price"])) for item in book["yes_bids"] if isinstance(item, dict)]
        return bool(
            asks and bids
            and asks == sorted(asks)
            and bids == sorted(bids, reverse=True)
            and asks[0] == Decimal(str(ask))
            and bids[0] == Decimal(str(bid))
        )
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return False


def _fee_for_levels(fees: dict[str, Any], fills: list[dict[str, str]]) -> Decimal | None:
    terms = fees.get("terms")
    if not fees.get("verified") or not isinstance(terms, dict):
        return None
    kind = terms.get("fee_type") or terms.get("type")
    if kind == "flat_per_contract":
        try:
            rate = Decimal(str(terms["amount_usd"]))
            if not rate.is_finite() or rate < 0:
                return None
            return rate * sum((Decimal(fill["contracts"]) for fill in fills), Decimal(0))
        except (KeyError, InvalidOperation, ValueError):
            return None
    if kind in {"quadratic", "quadratic_with_maker_fees"} and fees.get("venue") == "kalshi":
        try:
            from .paper_execution import FeeTerms, taker_fee

            fee_terms = FeeTerms(kind, Decimal(str(terms["taker_multiplier"])))
            return sum((taker_fee(Decimal(fill["price"]), Decimal(fill["contracts"]), fee_terms)
                        for fill in fills), Decimal(0))
        except (KeyError, InvalidOperation, ValueError, ArithmeticError):
            return None
    return None


def evaluate_candidate(
    candidate: dict[str, Any], *, now: datetime | None = None,
    freshness_threshold_seconds: float = FRESHNESS_THRESHOLD_SECONDS,
) -> dict[str, Any]:
    """Create a complete evidence/gate record; never infer a fill from BBO alone."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None or freshness_threshold_seconds <= 0:
        raise ValueError("aware comparison time and positive freshness limit required")
    venues = {
        "kalshi": candidate.get("kalshi") if isinstance(candidate.get("kalshi"), dict) else {},
        "polymarket_us": candidate.get("polymarket_us") if isinstance(candidate.get("polymarket_us"), dict) else {},
    }
    comparison = (candidate.get("canonical_identity") or {}).get("comparison", {})
    comparison = comparison if isinstance(comparison, dict) else {}
    contracts = (candidate.get("canonical_identity") or {}).get("contracts", [])
    contracts = contracts if isinstance(contracts, list) else []
    clauses = candidate.get("contract_clauses")
    clauses = clauses if isinstance(clauses, dict) else {}
    matched: list[str] = []
    mismatched: list[str] = []
    unresolved: list[str] = []
    clause_citations: dict[str, Any] = {}
    clause_evidence: dict[str, Any] = {}
    for name in REQUIRED_RULE_CLAUSES:
        pair = clauses.get(name)
        citations = pair.get("citations") if isinstance(pair, dict) else None
        left = pair.get("kalshi") if isinstance(pair, dict) else None
        right = pair.get("polymarket_us") if isinstance(pair, dict) else None
        left_text = left.get("text") if isinstance(left, dict) else left
        right_text = right.get("text") if isinstance(right, dict) else right
        has_both_citations = (
            isinstance(citations, dict)
            and bool(citations.get("kalshi"))
            and bool(citations.get("polymarket_us"))
        )
        material_ambiguity = bool(
            isinstance(left, dict) and left.get("status") == "unresolved"
            or isinstance(right, dict) and right.get("status") == "unresolved"
        )
        if (not isinstance(pair, dict) or not left_text or not right_text
                or not has_both_citations or material_ambiguity):
            unresolved.append(name)
        elif pair.get("comparison_status") == "mismatch":
            mismatched.append(name)
            clause_citations[name] = citations
        elif str(left_text).strip().casefold() == str(right_text).strip().casefold():
            matched.append(name)
            clause_citations[name] = citations
        else:
            # Different natural-language excerpts are not automatically proof
            # of economic mismatch; without a deterministic semantic code,
            # retain both as unresolved rather than permitting model judgment.
            unresolved.append(name)
        if isinstance(pair, dict):
            clause_evidence[name] = {
                "kalshi": left,
                "polymarket_us": right,
                "citations": citations,
                "comparison": "exact_text_match" if name in matched else "unresolved",
            }

    quote_records: dict[str, dict[str, Any]] = {}
    missing_quotes = False
    observed_timestamps: list[datetime] = []
    source_timestamps: list[datetime] = []
    for key, venue in venues.items():
        bid, ask = venue.get("yes_bid"), venue.get("yes_ask")
        valid = (isinstance(bid, (int, float)) and isinstance(ask, (int, float))
                 and 0 <= bid <= ask <= 1)
        missing_quotes = missing_quotes or not valid
        observed_at = venue.get("quote_observed_at") or venue.get("observed_at")
        parsed = _parse_time(observed_at)
        source_timestamp = venue.get("source_timestamp")
        source_parsed = _parse_time(source_timestamp)
        if parsed:
            observed_timestamps.append(parsed)
        if source_parsed:
            source_timestamps.append(source_parsed)
        fees = venue.get("fees") if isinstance(venue.get("fees"), dict) else {}
        quote_records[key] = {
            "native_market_id": venue.get("market_id"),
            "native_ticker": venue.get("ticker") or venue.get("market_id"),
            "bid": bid if valid else None,
            "ask": ask if valid else None,
            "source_timestamp": source_timestamp,
            "source_timestamp_semantics": venue.get("source_timestamp_semantics"),
            "observed_at": observed_at,
            "source_timestamp_available": source_parsed is not None,
            "fee_schedule_version": fees.get("schedule_version"),
            "fee_source": fees.get("source"),
            "maker_taker_assumption": fees.get("maker_taker_assumption"),
            "fee_rounding": fees.get("rounding"),
            "fee_amount_or_terms": fees.get("terms"),
            "unknown_fee_fields": (
                fees.get("unknown_fields") if isinstance(fees.get("unknown_fields"), list) else [
                    "schedule_version", "maker_taker_assumption", "rounding", "fee_terms",
                ]
            ),
            "fee_status": (
                "verified"
                if fees.get("verified") is True and fees.get("terms")
                and fees.get("schedule_version") and fees.get("maker_taker_assumption")
                and fees.get("rounding")
                else "unknown"
            ),
            "book_depth": venue.get("normalized_depth"),
            "capacity": venue.get("capacity") if venue.get("capacity") is not None else "unknown",
            "book_source": venue.get("book_source"),
        }

    comparison_at = now.astimezone(UTC)
    ages = {
        key: None if _parse_time(record["observed_at"]) is None else
        (comparison_at - _parse_time(record["observed_at"])).total_seconds()
        for key, record in quote_records.items()
    }
    observed_skew = ((max(observed_timestamps) - min(observed_timestamps)).total_seconds()
                     if len(observed_timestamps) == 2 else None)
    source_skew = ((max(source_timestamps) - min(source_timestamps)).total_seconds()
                   if len(source_timestamps) == 2 else None)
    source_ages = {
        key: None if _parse_time(record["source_timestamp"]) is None else
        (comparison_at - _parse_time(record["source_timestamp"])).total_seconds()
        for key, record in quote_records.items()
    }
    source_times_verified = all(
        record["source_timestamp_available"]
        and record.get("source_timestamp_semantics") == "documented_quote_event_time"
        for record in quote_records.values()
    )
    freshness_ok = (
        len(observed_timestamps) == 2 and len(source_timestamps) == 2
        and all(age is not None and 0 <= age <= freshness_threshold_seconds for age in ages.values())
        and all(age is not None and 0 <= age <= freshness_threshold_seconds for age in source_ages.values())
        and observed_skew is not None and observed_skew <= freshness_threshold_seconds
        and source_skew is not None and source_skew <= freshness_threshold_seconds
        and source_times_verified
    )
    source_texts = clauses.get("_source_text") if isinstance(clauses.get("_source_text"), dict) else {}
    raw_kalshi_rules = source_texts.get("kalshi")
    raw_polymarket_rules = source_texts.get("polymarket_us")
    full_texts_identical = (
        isinstance(raw_kalshi_rules, str) and bool(raw_kalshi_rules.strip())
        and isinstance(raw_polymarket_rules, str) and bool(raw_polymarket_rules.strip())
        and " ".join(raw_kalshi_rules.split()).casefold()
        == " ".join(raw_polymarket_rules.split()).casefold()
    )
    rules_verified = (
        not unresolved and not mismatched and len(matched) == len(REQUIRED_RULE_CLAUSES)
        and full_texts_identical
    )
    identity_confirmed = comparison.get("semantic_match") == "confirmed"
    depth_books_valid = all(
        isinstance(record["book_depth"], dict)
        and isinstance(record["book_depth"].get("yes_asks"), list)
        and isinstance(record["book_depth"].get("yes_bids"), list)
        for record in quote_records.values()
    )
    depth_matches_quotes = depth_books_valid and all(
        _depth_matches_quote(venues[key].get("normalized_depth"), bid=venues[key].get("yes_bid"),
                             ask=venues[key].get("yes_ask"))
        for key in venues
    )
    depth_known = depth_books_valid and depth_matches_quotes
    fees_known = all(record["fee_status"] == "verified"
                     and isinstance(record["fee_amount_or_terms"], dict)
                     and record["unknown_fee_fields"] == []
                     for record in quote_records.values())

    reasons: list[str] = []
    if mismatched:
        reasons.append("venue contract clauses conflict: " + ", ".join(mismatched))
    if not identity_confirmed:
        reasons.append("canonical event/outcome identity is not confirmed")
    if not rules_verified:
        reasons.append("settlement equivalence is unverified; unresolved clauses: " + ", ".join(unresolved)
                        + ("; complete venue rule texts are unavailable or differ" if not full_texts_identical else ""))
    if missing_quotes:
        reasons.append("one or both valid two-sided YES quotes are unavailable")
    if not freshness_ok:
        reasons.append("source quote timestamps/freshness cannot be verified within the configured limit")
    if not fees_known:
        reasons.append("venue fee schedule, assumptions, rounding, or fee terms are unknown")
    depth_size_verified = False
    if depth_known and not missing_quotes:
        left, right = venues["kalshi"], venues["polymarket_us"]
        yes_key, no_key = ("kalshi", "polymarket_us") if left["yes_ask"] <= right["yes_ask"] else ("polymarket_us", "kalshi")
        yes_qty, _, _ = _walk_book((venues[yes_key].get("normalized_depth") or {}).get("yes_asks"), target=Decimal(1))
        no_qty, _, _ = _walk_book((venues[no_key].get("normalized_depth") or {}).get("yes_bids"), target=Decimal(1))
        depth_size_verified = yes_qty == Decimal(1) and no_qty == Decimal(1)
    depth_known = depth_known and depth_size_verified
    if not depth_known:
        reasons.append("verified normalized depth is insufficient or unavailable for the predeclared one-contract paired fill; total capacity remains unknown")

    if mismatched or (not identity_confirmed and comparison.get("semantic_match") == "mismatch"):
        verdict = "REJECTED_RULE_MISMATCH"
    elif not identity_confirmed or not rules_verified:
        verdict = "REJECTED_RULES_UNVERIFIED"
    elif missing_quotes:
        verdict = "REJECTED_MISSING_QUOTES"
    elif not freshness_ok:
        verdict = "REJECTED_STALE_DATA"
    elif not fees_known:
        verdict = "REJECTED_FEES_UNKNOWN"
    elif not depth_known:
        verdict = "REJECTED_MISSING_DEPTH"
    else:
        verdict = "PAPER_TEST_ELIGIBLE"

    paper_simulation: dict[str, Any] = {
        "orders_created": False, "status": "not_run", "paired_fill": None,
        "assumptions": ["prospective observation only", "buy YES on lower ask and complementary NO on the opposing YES bid",
                        "no better later quote substitution", "bid/ask, per-level depth and venue-specific fees required",
                        "partial fills and residual unpaired exposure must be represented",
                        "source-time freshness is required; latency/quote withdrawal risk is not assigned a zero cost",
                        "one paired contract is the predeclared paper size; capacity beyond observed normalized depth is prohibited"],
        "reason": "Required equivalence, timestamp, fee, and normalized-depth gates are not all verified.",
    }
    if verdict == "PAPER_TEST_ELIGIBLE":
        # Predeclared test size: exactly one paired contract. Walk the actual
        # observed ask/bid levels and reject if either leg cannot fill fully.
        target = Decimal(1)
        left, right = venues["kalshi"], venues["polymarket_us"]
        if float(left["yes_ask"]) <= float(right["yes_ask"]):
            yes_key, no_key = "kalshi", "polymarket_us"
        else:
            yes_key, no_key = "polymarket_us", "kalshi"
        yes_depth = venues[yes_key].get("normalized_depth") or {}
        no_depth = venues[no_key].get("normalized_depth") or {}
        yes_qty, yes_cost, yes_fills = _walk_book(yes_depth.get("yes_asks"), target=target)
        no_qty, no_cost, no_fills = _walk_book(no_depth.get("yes_bids"), target=target, complement=True)
        yes_fee = _fee_for_levels(venues[yes_key].get("fees", {}), yes_fills)
        no_fee = _fee_for_levels(venues[no_key].get("fees", {}), no_fills)
        if yes_qty != target or no_qty != target or yes_fee is None or no_fee is None:
            verdict = "PAPER_FILL_FAILED"
            paper_simulation.update(
                status="paper_fill_failed",
                paired_fill={"target_contracts": str(target), "yes_leg": {"venue": yes_key, "contracts": str(yes_qty), "cost_usd": str(yes_cost), "levels": yes_fills},
                             "no_leg": {"venue": no_key, "contracts": str(no_qty), "cost_usd": str(no_cost), "levels": no_fills},
                             "unpaired_contracts": str(abs(yes_qty - no_qty)),
                             "unfilled_contracts_by_leg": {"yes": str(target - yes_qty), "no": str(target - no_qty)},
                             "fees_usd": None},
                reason="Observed depth or supported fee calculation could not complete both one-contract legs.",
            )
        else:
            total_cost = yes_cost + no_cost + yes_fee + no_fee
            net = target - total_cost
            result_state = ("PAPER_RESULT_POSITIVE" if net > 0 else
                            "REJECTED_COSTS_ERASE_EDGE" if net < 0 else "PAPER_RESULT_INCONCLUSIVE")
            if net < 0:
                reasons.append("observed paired entry cost plus supported fees exceeds the one-contract settlement payout")
            verdict = result_state
            paper_simulation.update(
                status="paper_fill_simulated",
                paired_fill={"target_contracts": str(target), "yes_leg": {"venue": yes_key, "contracts": str(yes_qty), "cost_usd": str(yes_cost), "fees_usd": str(yes_fee), "levels": yes_fills},
                             "no_leg": {"venue": no_key, "contracts": str(no_qty), "cost_usd": str(no_cost), "fees_usd": str(no_fee), "levels": no_fills},
                             "total_debit_usd": str(total_cost), "fees_usd": str(yes_fee + no_fee),
                             "gross_payout_usd": str(target), "net_result_usd": str(net)},
                reason="Prospective one-contract paper calculation from the frozen observed books and supported fee terms.",
            )

    event_id = comparison.get("canonical_event_id")
    proposition_id = comparison.get("canonical_proposition_id")
    if not event_id and contracts:
        event_id = contracts[0].get("canonical_event_id")
    if not proposition_id and contracts:
        proposition_id = contracts[0].get("canonical_proposition_id")
    return {
        "experiment": {"name": "cross_venue_contract_discrepancy", "version": EXPERIMENT_VERSION},
        "observed_at": candidate.get("observed_at") or comparison_at.isoformat(),
        "comparison_at": comparison_at.isoformat(),
        "canonical_event_id": event_id,
        "canonical_proposition_id": proposition_id,
        "native_contracts": [
            {"venue": name, "market_id": data.get("market_id"),
             "ticker": data.get("ticker") or data.get("market_id"),
             "source_timestamp": data.get("source_timestamp"),
             "observed_at": data.get("quote_observed_at") or data.get("observed_at")}
            for name, data in venues.items()
        ],
        "contract_equivalence": {
            "status": "mismatch" if mismatched else "verified" if rules_verified else "unverified",
            "identity_status": comparison.get("semantic_match", "unverified"),
            "matched_clauses": matched, "mismatched_clauses": mismatched,
            "unresolved_clauses": unresolved,
            "clause_citations": clause_citations,
            "clause_evidence": clause_evidence,
            "source_text": {"kalshi": raw_kalshi_rules, "polymarket_us": raw_polymarket_rules},
            "full_text_comparison": "exact_match" if full_texts_identical else "unresolved_or_different",
            "evidence_citations": candidate.get("evidence_citations", []),
            "reason": "All required clauses must be evidenced by both venue contracts; semantic identity alone is insufficient.",
        },
        "quotes": quote_records,
        "freshness": {
            "venue_observation_timestamps": {key: value.get("observed_at") for key, value in quote_records.items()},
            "source_timestamps": {key: value.get("source_timestamp") for key, value in quote_records.items()},
            "timestamp_semantics": {key: value.get("source_timestamp_semantics") for key, value in quote_records.items()},
            "receipt_time_is_not_source_time": True,
            "comparison_at": comparison_at.isoformat(), "observed_age_seconds": ages,
            "cross_venue_skew_seconds": source_skew,
            "retrieval_skew_seconds": observed_skew,
            "source_age_seconds": source_ages,
            "allowed_threshold_seconds": freshness_threshold_seconds,
            "status": "verified" if freshness_ok else "unverified_or_stale",
        },
        "depth": {"status": "one_contract_verified" if depth_known else "unknown",
                  "one_contract_eligible": depth_known,
                  "capacity": "unknown"},
        "paper_simulation": paper_simulation,
        "chronological_validation": {
            "status": "not_run_insufficient_forward_outcomes", "diagnostics_invoked": False,
            "walk_forward": None, "overfit_diagnostics": None,
            "reason": "This observation has no matured paired paper outcome cohort; using backtest diagnostics now would fabricate evidence.",
            "attempted_variants": 1,
            "failed_variants_retained": 1 if verdict.startswith(("REJECTED", "PAPER_FILL_FAILED")) else 0,
        },
        "economics": {
            "paper_ledger_status": "simulated_not_settled" if paper_simulation["status"] == "paper_fill_simulated" else "no_fill_recorded",
            "gross_result_usd": (paper_simulation.get("paired_fill") or {}).get("gross_payout_usd"),
            "fees_usd": (paper_simulation.get("paired_fill") or {}).get("fees_usd"),
            "net_result_usd": (paper_simulation.get("paired_fill") or {}).get("net_result_usd"),
            "capacity": "unknown" if not depth_known else {key: record["capacity"] for key, record in quote_records.items()},
        },
        "verdict": verdict, "rejection_reasons": reasons,
        "live_execution_enabled": False, "orders_created": False,
    }


def persist_evaluation(path: str, candidate: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Append source evidence and register the exact evaluation in existing ledgers."""
    observation = {**candidate, "experiment_evaluation": result}
    ledger = ForecastLedger(path)
    try:
        inserted = ledger.append_canonical_pair_observation(observation)
        row = ledger.conn.execute(
            "SELECT observation_hash FROM canonical_pair_observations WHERE observation_json=?",
            (json.dumps(observation, sort_keys=True, separators=(",", ":"), default=str),),
        ).fetchone()
        if row is None:
            # Default=repr and sorted keys can differ only for non-JSON-native
            # provider data; derive the same canonicalized bytes as the ledger.
            encoded = json.dumps(observation, sort_keys=True, separators=(",", ":"), default=str)
            evidence_hash = hashlib.sha256(encoded.encode()).hexdigest()
        else:
            evidence_hash = str(row[0])
    finally:
        ledger.conn.close()

    params = {"experiment": "cross_venue_contract_discrepancy", "version": EXPERIMENT_VERSION,
              "variant_id": "one_contract_taker_pair_v1",
              "candidate_observation_hash": evidence_hash}
    trials = ResearchTrialStore(path)
    try:
        trial_id = trials.register(
            family="prediction_markets_cross_venue_paper",
            hypothesis="A prospective apparent cross-venue discrepancy survives proven contract equivalence, fresh quotes, fees, depth, and paired paper execution.",
            params=params, feature_set_version=EXPERIMENT_VERSION,
        )
        if result["verdict"].startswith("REJECTED"):
            trials.set_status(trial_id, "rejected")
    finally:
        trials.conn.close()

    if inserted and isinstance(result.get("paper_simulation", {}).get("paired_fill"), dict):
        from decimal import InvalidOperation

        from .economic_ledger import EconomicEvent, EconomicLedger

        fill = result["paper_simulation"]["paired_fill"]
        try:
            net_result = Decimal(str(fill["net_result_usd"]))
            if not net_result.is_finite():
                net_result = None
        except (KeyError, InvalidOperation, TypeError, ValueError):
            net_result = None
        # This is explicitly a hypothetical, unsettled result. amount_usd
        # stays null so it cannot be mistaken for cash revenue/profit.
        economics = EconomicLedger(path)
        try:
            economics.append_event(
                "paper_cross_venue_simulation",
                payload={"trial_id": trial_id, "evidence_hash": evidence_hash,
                         "simulated_net_if_settled_usd": None if net_result is None else str(net_result),
                         "settled": False, "live_execution": False,
                         "assumptions": result["paper_simulation"].get("assumptions", [])},
            )
            if net_result is not None:
                economics.record_event(EconomicEvent(
                    provider="paper_execution", external_reference_id=evidence_hash,
                    event_type="paper_result", occurred_at=datetime.now(UTC),
                    currency="USD", amount=net_result, amount_usd=net_result,
                    reconciliation_state="OBSERVED", value_state="paper",
                    capital_class="none", confidence_state="derived",
                    completeness_state="incomplete", activity_id=trial_id,
                    lane="prediction", evidence={"evidence_hash": evidence_hash,
                                                  "settled": False, "live_execution": False},
                ))
        finally:
            economics.conn.close()

    now = datetime.now(UTC).isoformat()
    # Reuse the repository's existing run-store schema and lease conventions.
    from .autonomous_research import ResearchWorkStore

    run_store = ResearchWorkStore(path)
    conn = run_store.conn
    try:
        conn.execute(
            "INSERT OR IGNORE INTO autonomous_research_runs "
            "(trial_id,specialist,kind,evidence_hash,worker_version,status,created_at,completed_at,"
            "deadline_at,elapsed_seconds,compute_cost_usd,result_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (trial_id, "kalshi-history", "cross_venue_paper_experiment", evidence_hash,
             EXPERIMENT_VERSION, "completed", now, now, now, None, None,
             json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)),
        )
        conn.commit()
        run = conn.execute(
            "SELECT id FROM autonomous_research_runs WHERE trial_id=? AND evidence_hash=? AND worker_version=?",
            (trial_id, evidence_hash, EXPERIMENT_VERSION),
        ).fetchone()
    finally:
        conn.close()
    return {"persistence_status": "recorded" if inserted else "already_recorded",
            "trial_id": trial_id, "run_id": None if run is None else run[0],
            "evidence_hash": evidence_hash}


def recent_evaluations(path: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """Read immutable candidate lifecycle records for operator visibility."""
    if not 1 <= limit <= 100:
        raise ValueError("limit must be 1..100")
    try:
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            rows = conn.execute(
                "SELECT observation_hash,observation_json FROM canonical_pair_observations "
                "ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            run_by_hash: dict[str, tuple[str, int]] = {}
            if "autonomous_research_runs" in tables:
                for trial_id, run_id, digest in conn.execute(
                    "SELECT trial_id,id,evidence_hash FROM autonomous_research_runs "
                    "WHERE kind='cross_venue_paper_experiment' ORDER BY id DESC LIMIT ?", (limit,),
                ):
                    run_by_hash.setdefault(str(digest), (str(trial_id), int(run_id)))
            settlement_by_hash: dict[str, dict[str, Any]] = {}
            latest_walk_forward = None
            if "economic_events" in tables:
                for (payload,) in conn.execute(
                    "SELECT payload_json FROM economic_events "
                    "WHERE event_type='paper_cross_venue_settlement' ORDER BY id DESC LIMIT 1000"
                ):
                    try:
                        item = json.loads(payload)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(item, dict) and item.get("evidence_hash"):
                        settlement_by_hash.setdefault(str(item["evidence_hash"]), item)
                row = conn.execute(
                    "SELECT payload_json FROM economic_events "
                    "WHERE event_type='paper_cross_venue_walk_forward' ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if row:
                    try:
                        latest_walk_forward = json.loads(row[0])
                    except (ValueError, TypeError):
                        latest_walk_forward = None
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    records: list[dict[str, Any]] = []
    for evidence_hash, payload in rows:
        try:
            item = json.loads(payload)
        except (ValueError, TypeError):
            continue
        if isinstance(item, dict) and isinstance(item.get("experiment_evaluation"), dict):
            trial = run_by_hash.get(str(evidence_hash))
            if trial:
                item["trial_id"], item["research_run_id"] = trial
            item["evidence_hash"] = str(evidence_hash)
            settlement = settlement_by_hash.get(str(evidence_hash))
            if settlement:
                item["paper_settlement"] = settlement
            if latest_walk_forward:
                item["chronological_audit"] = latest_walk_forward
            records.append(item)
    return records


def mature_paper_pairs(
    path: str, *, outcome_path: str | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    """Settle only previously simulated pairs from prospective official outcomes.

    Result events stay non-cash in the economic ledger. A later contradictory
    venue outcome is retained as a settlement-rule divergence, never netted as
    a profitable paired result.
    """
    now = now or datetime.now(UTC)
    db = Path(path)
    outcomes_db = Path(outcome_path or path)
    if not db.exists() or not outcomes_db.exists():
        return {"status": "outcomes_unavailable", "matured": 0, "matured_total": 0,
                "paper_fill_count": 0, "live_trade_count": 0, "capacity": "unknown",
                "walk_forward": None}
    state_conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    same_database = outcomes_db.resolve() == db.resolve()
    outcomes_conn = state_conn if same_database else sqlite3.connect(
        outcomes_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2,
    )
    try:
        state_tables = {row[0] for row in state_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        outcome_tables = {row[0] for row in outcomes_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if not {"canonical_pair_observations", "economic_events"} <= state_tables \
                or "outcomes" not in outcome_tables:
            return {"status": "outcomes_unavailable", "matured": 0, "matured_total": 0,
                    "paper_fill_count": 0, "live_trade_count": 0, "capacity": "unknown",
                    "walk_forward": None}
        outcome_columns = {row[1] for row in outcomes_conn.execute("PRAGMA table_info(outcomes)")}
        if not {"venue", "market_id", "outcome_yes", "resolved_at", "first_seen_at"} <= outcome_columns:
            return {"status": "outcomes_unavailable", "matured": 0, "matured_total": 0,
                    "paper_fill_count": 0, "live_trade_count": 0, "capacity": "unknown",
                    "walk_forward": None}
        raw_column = "raw_json" if "raw_json" in outcome_columns else "NULL"
        outcomes = {
            ("polymarket_us" if str(venue).casefold() in {"polymarket-us", "polymarket_us"}
             else "kalshi" if str(venue).casefold() in {"kalshi", "kalshi:production"}
             else str(venue).casefold(), str(market_id)): {
                "outcome_yes": value, "resolved_at": resolved, "first_seen_at": first_seen,
                "raw_json": raw, "source": source,
                "canonical_proposition_id": proposition,
            }
            for venue, market_id, value, resolved, first_seen, raw, source, proposition in outcomes_conn.execute(
                "SELECT venue,market_id,outcome_yes,resolved_at,first_seen_at," + raw_column + ","
                + ("source" if "source" in outcome_columns else "NULL") + ","
                + ("canonical_proposition_id" if "canonical_proposition_id" in outcome_columns else "NULL")
                + " FROM outcomes"
            )
        }
        records = state_conn.execute(
            "SELECT observation_hash,observation_json FROM canonical_pair_observations ORDER BY id"
        ).fetchall()
        existing: set[str] = set()
        existing_statuses: dict[str, str] = {}
        for (payload,) in state_conn.execute(
            "SELECT payload_json FROM economic_events WHERE event_type='paper_cross_venue_settlement'"
        ):
            try:
                item = json.loads(payload)
                if item.get("evidence_hash"):
                    digest = str(item["evidence_hash"])
                    existing.add(digest)
                    existing_statuses[digest] = str(item.get("status", "unknown"))
            except (ValueError, TypeError, AttributeError):
                continue
        settled_rows: list[dict[str, Any]] = []
        pending = 0
        paper_fill_count = 0
        for digest, payload in records:
            if str(digest) in existing:
                continue
            try:
                observation = json.loads(payload)
                evaluation = observation.get("experiment_evaluation", {})
                if evaluation.get("paper_simulation", {}).get("status") != "paper_fill_simulated":
                    continue
                paper_fill_count += 1
                run_row = None
                if "autonomous_research_runs" in state_tables:
                    run_row = state_conn.execute(
                        "SELECT trial_id FROM autonomous_research_runs WHERE evidence_hash=? "
                        "AND kind='cross_venue_paper_experiment' ORDER BY id DESC LIMIT 1",
                        (str(digest),),
                    ).fetchone()
                linked_trial_id = run_row[0] if run_row else None
                comparison_at = _parse_time(evaluation.get("comparison_at"))
                fill = evaluation["paper_simulation"]["paired_fill"]
                if comparison_at is None:
                    continue
                legs = {str(leg["venue"]): leg for leg in (fill["yes_leg"], fill["no_leg"])}
                identity_contracts = (observation.get("canonical_identity") or {}).get("contracts", [])
                active_at_decision = {
                    str(item.get("venue")): item.get("active_at_observation")
                    for item in identity_contracts if isinstance(item, dict)
                }
                if active_at_decision != {"kalshi": True, "polymarket_us": True}:
                    pending += 1
                    continue
                source_markets = {
                    ("polymarket_us" if str(item.get("venue", "")).casefold()
                     in {"polymarket-us", "polymarket_us"} else str(item.get("venue", "")).casefold()):
                    str(item.get("market_id", ""))
                    for item in observation.get("experiment_evaluation", {}).get("native_contracts", [])
                }
                settled: dict[str, dict[str, Any]] = {}
                for venue, market_id in source_markets.items():
                    key = "polymarket_us" if venue in {"polymarket-us", "polymarket_us"} else venue
                    raw = outcomes.get((venue, market_id))
                    if key not in legs or raw is None:
                        settled = {}
                        break
                    resolved_at = _parse_time(raw.get("resolved_at"))
                    first_seen_at = _parse_time(raw.get("first_seen_at"))
                    try:
                        raw_evidence = json.loads(raw.get("raw_json") or "{}")
                    except (ValueError, TypeError):
                        raw_evidence = {}
                    if (not evaluation.get("canonical_proposition_id")
                            or raw.get("canonical_proposition_id")
                            != evaluation.get("canonical_proposition_id")):
                        settled = {}
                        break
                    if key == "kalshi":
                        authoritative = (
                            raw.get("source") == "kalshi_official_market_api"
                            and isinstance(raw_evidence, dict)
                            and raw_evidence.get("finality") == "official_kalshi_settlement"
                        )
                        time_gate = resolved_at is not None and resolved_at > comparison_at
                    else:
                        market_evidence = raw_evidence.get("market", {}) if isinstance(raw_evidence, dict) else {}
                        settlement_evidence = raw_evidence.get("settlement", {}) if isinstance(raw_evidence, dict) else {}
                        authoritative = (
                            raw.get("source") == "polymarket_us_official_settlement_endpoint"
                            and raw_evidence.get("finality") == "official_closed_market_and_settlement_endpoint"
                            and isinstance(market_evidence, dict) and market_evidence.get("closed") is True
                            and isinstance(settlement_evidence, dict)
                            and settlement_evidence.get("slug") == market_id
                            and settlement_evidence.get("settlement") in (0, 1, 0.0, 1.0)
                        )
                        # The official SDK documents no PM settled-at field. Its
                        # first observed, closed+settled response is the event
                        # time lower bound, never an invented venue timestamp.
                        time_gate = (resolved_at is not None and resolved_at > comparison_at) or (
                            resolved_at is None and first_seen_at is not None
                            and first_seen_at > comparison_at
                            and raw_evidence.get("venue_resolved_at_available") is False
                        )
                    if (type(raw.get("outcome_yes")) is not int or raw["outcome_yes"] not in {0, 1}
                            or not authoritative or first_seen_at is None
                            or not time_gate or first_seen_at <= comparison_at
                            or resolved_at is not None and resolved_at > now
                            or first_seen_at > now):
                        settled = {}
                        break
                    settled[key] = {**raw, "resolved_at_parsed": resolved_at,
                                    "first_seen_at_parsed": first_seen_at,
                                    "raw_evidence_parsed": raw_evidence}
                if len(settled) != 2:
                    pending += 1
                    continue
                values = {item["outcome_yes"] for item in settled.values()}
                if len(values) != 1:
                    result = {
                        "status": "settlement_rule_divergence", "evidence_hash": str(digest),
                        "trial_id": linked_trial_id, "settled_venues": {
                            name: {"outcome_yes": row["outcome_yes"], "resolved_at": row["resolved_at"],
                                   "first_seen_at": row["first_seen_at"],
                                   "canonical_proposition_id": row.get("canonical_proposition_id"),
                                   "source": row.get("source"), "source_url": row.get("source_url"),
                                   "source_identifier": f"{name}:{source_markets[name]}",
                                   "raw_evidence": row.get("raw_evidence_parsed"),
                                   "evidence_sha256": (hashlib.sha256(str(row["raw_json"]).encode()).hexdigest()
                                                       if row.get("raw_json") else None)}
                            for name, row in settled.items()},
                        "settled": True, "cash_amount_usd": None,
                    }
                else:
                    yes_outcome = next(iter(values))
                    yes_leg = fill["yes_leg"]
                    no_leg = fill["no_leg"]
                    yes_payout = Decimal(str(yes_leg["contracts"])) * yes_outcome
                    no_payout = Decimal(str(no_leg["contracts"])) * (1 - yes_outcome)
                    debit = Decimal(str(fill["total_debit_usd"]))
                    net = yes_payout + no_payout - debit
                    result = {
                        "status": "settled", "evidence_hash": str(digest),
                        "trial_id": linked_trial_id,
                        "canonical_event_id": evaluation.get("canonical_event_id"),
                        "canonical_proposition_id": evaluation.get("canonical_proposition_id"),
                        "outcome_yes": yes_outcome, "gross_payout_usd": str(yes_payout + no_payout),
                        "paper_debit_usd": str(debit), "paper_fees_usd": fill.get("fees_usd"),
                        "realized_net_if_paper_usd": str(net), "settled": True,
                        "cash_amount_usd": None,
                        "settled_venues": {
                            name: {"outcome_yes": row["outcome_yes"], "resolved_at": row["resolved_at"],
                                   "first_seen_at": row["first_seen_at"],
                                   "canonical_proposition_id": row.get("canonical_proposition_id"),
                                   "source": row.get("source"), "source_url": row.get("source_url"),
                                   "source_identifier": f"{name}:{source_markets[name]}",
                                   "raw_evidence": row.get("raw_evidence_parsed"),
                                   "evidence_sha256": (hashlib.sha256(str(row["raw_json"]).encode()).hexdigest()
                                                       if row.get("raw_json") else None)}
                            for name, row in settled.items()},
                    }
                result["observed_at"] = evaluation.get("comparison_at")
                settled_rows.append(result)
            except (ValueError, TypeError, KeyError, InvalidOperation, ArithmeticError, AttributeError):
                continue
    finally:
        state_conn.close()
        if not same_database:
            outcomes_conn.close()

    if settled_rows:
        from .economic_ledger import EconomicEvent, EconomicLedger

        for result in settled_rows:
            ledger = EconomicLedger(path)
            try:
                ledger.append_event("paper_cross_venue_settlement", payload=result)
                if result.get("realized_net_if_paper_usd") is not None:
                    amount = Decimal(str(result["realized_net_if_paper_usd"]))
                    ledger.record_event(EconomicEvent(
                        provider="paper_execution",
                        external_reference_id=(f"{result.get('canonical_event_id')}:"
                                               f"{result.get('evidence_hash')}"),
                        event_type="paper_settlement", occurred_at=datetime.now(UTC),
                        currency="USD", amount=amount, amount_usd=amount,
                        reconciliation_state="OBSERVED", value_state="paper",
                        capital_class="none", confidence_state="derived",
                        completeness_state="incomplete", activity_id=result.get("trial_id"),
                        lane="prediction", evidence={"evidence_hash": result.get("evidence_hash"),
                                                      "settled_venues": result.get("settled_venues"),
                                                      "live_execution": False},
                    ))
            finally:
                ledger.conn.close()
    walk_forward = _record_pair_walk_forward(path, now=now)
    statuses = [*existing_statuses.values(), *(str(row.get("status")) for row in settled_rows)]
    return {"status": "settled" if settled_rows else "waiting_for_prospective_outcomes",
            "matured": sum(row.get("status") == "settled" for row in settled_rows),
            "matured_total": sum(status == "settled" for status in statuses),
            "rule_divergences": sum(row.get("status") == "settlement_rule_divergence" for row in settled_rows),
            "rule_divergences_total": sum(status == "settlement_rule_divergence" for status in statuses),
            "paper_fill_count": len(existing) + paper_fill_count,
            "live_trade_count": 0,
            "capacity": "unknown",
            "pending": pending, "walk_forward": walk_forward}


def _record_pair_walk_forward(path: str, *, now: datetime) -> dict[str, Any]:
    """Run chronological diagnostics only on matured independent event outcomes."""
    from .walkforward import expanding_walk_forward

    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        rows = []
        for (payload,) in conn.execute(
            "SELECT payload_json FROM economic_events WHERE event_type='paper_cross_venue_settlement'"
        ):
            try:
                item = json.loads(payload)
                if item.get("status") != "settled" or not item.get("canonical_event_id"):
                    continue
                at = _parse_time(item.get("observed_at"))
                net = Decimal(str(item["realized_net_if_paper_usd"]))
                if at and at <= now and net.is_finite():
                    rows.append((at, str(item["canonical_event_id"]), net))
            except (ValueError, TypeError, KeyError, InvalidOperation):
                continue
    finally:
        conn.close()
    # Repeated candidate snapshots for one canonical event are correlated. Keep
    # only the first mature observation per event, then order by its decision time.
    unique: dict[str, tuple[datetime, Decimal]] = {}
    for at, event_id, net in sorted(rows, key=lambda row: row[0]):
        unique.setdefault(event_id, (at, net))
    ordered = sorted(unique.values(), key=lambda row: row[0])
    if len(ordered) < 4:
        return {"status": "insufficient_independent_matured_events", "diagnostics_invoked": False,
                "matured_event_count": len(ordered), "walk_forward": None,
                "overfit_diagnostics": "not_applicable_single_predeclared_variant"}
    folds = expanding_walk_forward(ordered, min_train=3, test_size=1)
    fold_results = [{"train_count": fold.train_end - fold.train_start,
                     "test_count": fold.test_end - fold.test_start,
                     "test_net_sum_usd": str(sum((ordered[index][1]
                                                   for index in range(fold.test_start, fold.test_end)), Decimal(0)))}
                    for fold in folds]
    audit = {"status": "walk_forward_invoked", "diagnostics_invoked": True,
             "matured_event_count": len(ordered), "folds": fold_results,
             "overfit_diagnostics": "not_applicable_single_predeclared_variant",
             "variant_count": 1}
    digest = hashlib.sha256(json.dumps(
        [(at.isoformat(), str(net)) for at, net in ordered], separators=(",", ":")
    ).encode()).hexdigest()
    from .economic_ledger import EconomicLedger

    ledger = EconomicLedger(path)
    try:
        exists = ledger.conn.execute(
            "SELECT 1 FROM economic_events WHERE event_type='paper_cross_venue_walk_forward' "
            "AND json_extract(payload_json,'$.evidence_hash')=? LIMIT 1", (digest,),
        ).fetchone()
        if not exists:
            ledger.append_event(
                "paper_cross_venue_walk_forward",
                payload={**audit, "evidence_hash": digest, "cash_amount_usd": None},
            )
    finally:
        ledger.conn.close()
    return audit
