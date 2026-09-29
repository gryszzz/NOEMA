"""Allowlisted local experiments. No model-supplied code, network or execution tools."""
from __future__ import annotations

import json
import math
import signal
import sqlite3
import sys
from datetime import UTC, datetime
from decimal import Decimal

from .history_forecaster import MODEL_VERSION
from .paper_execution import parse_aware_time
from .paper_settlements import load_paper_settlements
from .trench_survival_model import audit_database

# A fixed diagnostic grid, never an automatically selected production threshold.
THRESHOLDS = ("0.03", "0.05", "0.08", "0.10")


def cost_threshold_sweep(path: str) -> dict:
    now = datetime.now(UTC)
    settlements = load_paper_settlements(path, now=now)
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT venue, market_id, lower_bound, quote_json, quoted_at FROM paper_quotes "
            "WHERE model_version=? ORDER BY id", (MODEL_VERSION,),
        ).fetchall()
    first: dict[tuple[str, str], Decimal | None] = {}
    for venue, market, lower, payload, quoted in rows:
        try:
            if parse_aware_time(quoted) > now:
                continue
        except (ValueError, TypeError):
            pass  # Like settlement selection, a malformed first record blocks later ones.
        key = (venue, market)
        if key in first:
            continue
        first[key] = None
        try:
            quote = json.loads(payload)
            debit, contracts = (Decimal(str(quote[k])) for k in ("total_debit_usd", "contracts"))
            bound = Decimal(str(lower))
            if not all(v.is_finite() for v in (bound, debit, contracts)):
                continue
            if not 0 <= bound <= 1 or debit <= 0 or contracts <= 0:
                continue
            first[key] = bound - debit / contracts
        except (ValueError, TypeError, KeyError, ArithmeticError):
            continue
    variants = []
    for threshold in THRESHOLDS:
        chosen = [row for row in settlements.settlements
                  if first.get((row.venue, row.market_id)) is not None
                  and first[(row.venue, row.market_id)] >= Decimal(threshold)]
        debit = sum((row.debit_usd for row in chosen), Decimal(0))
        pnl = sum((row.net_pnl_usd for row in chosen), Decimal(0))
        variants.append({
            "threshold": threshold, "markets": len(chosen),
            "events": len({row.event_id for row in chosen}),
            "paper_debit_usd": str(debit), "paper_net_usd": str(pnl),
            "paper_return": str(pnl / debit) if debit else None,
        })
    return {
        "status": "diagnostic_only" if settlements.settlements else "insufficient_evidence",
        "variants": variants, "live_eligible": False,
        "conclusion": "Retrospective filter of original selected paper decisions; all variants "
        "retained. A new preregistered forward cohort is required to validate any threshold. "
        "Hypothetical fills; operating costs and latency losses remain unmeasured.",
    }


def execute(kind: str, path: str) -> dict:
    if kind == "market_data_quality":
        return market_data_quality(path)
    if kind == "cost_threshold_sweep":
        return cost_threshold_sweep(path)
    if kind == "trench_survival_logistic":
        result = audit_database(path).as_dict()
        # Model artifacts remain associated with the run, never installed for trading.
        return result
    if kind == "commercial_opportunity_scan":
        return commercial_opportunity_scan(path)
    raise ValueError("unsupported research handler")


def commercial_opportunity_scan(path: str) -> dict:
    """Qualify public paid-work signals without treating a listing as a buyer or receipt."""
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT source,source_type,payload_json FROM evidence_records ORDER BY rowid LIMIT 2"
        ).fetchall()
    if len(rows) != 1 or rows[0][0:2] != (
        "github:search_issues", "public_paid_work_discovery",
    ):
        raise ValueError("frozen commercial evidence is missing or ambiguous")
    payload = json.loads(rows[0][2])
    issues = payload.get("issues")
    if not isinstance(issues, list) or len(issues) > 10:
        raise ValueError("commercial evidence issue list is invalid")
    if payload.get("issue_bodies_included") is not False:
        raise ValueError("untrusted issue bodies must not enter commercial qualification")
    payment_terms = ("bounty", "paid", "reward", "fixed price", "$")
    payment_signal_count = sum(
        any(term in str(issue.get("title", "")).lower() for term in payment_terms)
        or any(any(term in str(label).lower() for term in payment_terms)
               for label in issue.get("labels", []) if isinstance(label, str))
        for issue in issues if isinstance(issue, dict)
    )
    return {
        "status": "no_verified_buyer_opportunity",
        "public_search_result_count": max(0, int(payload.get("total_count", len(issues)))),
        "metadata_records_reviewed": len(issues),
        "visible_payment_signal_count": payment_signal_count,
        "verified_buyer_count": 0,
        "verified_payout_count": 0,
        "deliverable_produced": False,
        "delivery_tested": False,
        "revenue_test_status": "not_tested",
        "mission_cash_receipt_usd": "0",
        "attributable_costs_usd": {
            "model": None, "compute": None, "source_api": None,
            "delivery": "0", "support": "0",
        },
        "realized_net_value_usd": None,
        "live_eligible": False,
        "next_priority": "find_verifiable_buyer_and_safe_delivery_channel",
        "conclusion": (
            "This public metadata scan did not verify a buyer, funded payout, bounded safe scope, "
            "or delivery channel. No product was offered or delivered, no payment was requested, "
            "and no revenue is claimed. Search results are leads only, not demand evidence."
        ),
    }


def market_data_quality(path: str) -> dict:
    """Falsify readiness for a reusable data feed using observed data, not a trade signal."""
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT venue,market_id,captured_at,valid,snapshot_json FROM market_snapshots ORDER BY id"
        ).fetchall()
    latest = {}
    now = datetime.now(UTC)
    for venue, market, captured, valid, payload in rows:
        at = parse_aware_time(captured)
        if at <= now:
            previous = latest.get((venue, market))
            if previous is None or at >= previous[0]:
                latest[(venue, market)] = (at, valid, json.loads(payload))
    valid_count = sum(int(v == 1) for _, v, _ in latest.values())
    rules = sum(bool(p.get("resolution_rules")) for _, _, p in latest.values())
    quotes = sum(all(p.get(k) is not None for k in ("yes_bid", "yes_ask", "no_bid", "no_ask"))
                 for _, _, p in latest.values())
    result = {
        "status": "research_only" if latest else "insufficient_evidence",
        "observations": len(latest), "valid_markets": valid_count,
        "markets_with_rules": rules, "markets_with_two_sided_quotes": quotes,
        "oldest_snapshot_at": min((v[0].isoformat() for v in latest.values()), default=None),
        "newest_snapshot_at": max((v[0].isoformat() for v in latest.values()), default=None),
        "live_eligible": False, "net_economic_profit_usd": None,
        "conclusion": "Observed feed coverage only. Customer demand, reuse permissions, "
        "fees, fill probability, latency losses and total operating costs require independent "
        "evidence before a product or strategy can be judged economically viable.",
    }
    result["next_priority"] = (
        "repair_market_data" if valid_count < len(latest)
        else "investigate_resolution_rules" if rules < len(latest)
        else "validate_demand_and_all_in_costs"
    )
    return result


def main() -> None:
    try:
        timeout = float(sys.argv[3])
        if not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError("invalid worker timeout")
        signal.setitimer(signal.ITIMER_REAL, timeout)
        result = execute(sys.argv[1], sys.argv[2])
        serialized = json.dumps(result, sort_keys=True, allow_nan=False)
        if len(serialized) > 65536:
            raise ValueError("oversized result")
        print(serialized)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, ArithmeticError, sqlite3.Error):
        # Provider/data exception strings can contain private input.
        print('{"status":"failed","reason":"local experiment failed"}')
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
