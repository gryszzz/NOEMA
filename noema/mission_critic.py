"""Deterministic evidence-critic handoff for bounded research results."""
from __future__ import annotations

import json
import math
from typing import Any

CRITIC_ID = "evidence-critic"


def evaluate_result(*, kind: str, result: dict[str, Any], evidence_hash: str) -> dict[str, Any]:
    """Check result integrity and fail closed; this critic cannot infer economic edge."""
    issues: list[str] = []
    try:
        encoded = json.dumps(result, sort_keys=True, allow_nan=False)
        if len(encoded) > 65536:
            issues.append("result exceeds persisted research bound")
    except (TypeError, ValueError):
        encoded = ""
        issues.append("result is not finite JSON")
    if not isinstance(result, dict):
        issues.append("result is not an object")
        result = {}
    if result.get("live_eligible") is not False:
        issues.append("result did not explicitly preserve paper-only eligibility")
    if not isinstance(result.get("status"), str) or not result["status"]:
        issues.append("result status is missing")
    if not isinstance(result.get("conclusion"), str) or not result["conclusion"].strip():
        issues.append("result conclusion is missing")
    if len(evidence_hash) != 64 or any(char not in "0123456789abcdef" for char in evidence_hash):
        issues.append("evidence digest is invalid")
    if kind == "market_data_quality":
        observations = result.get("observations")
        if type(observations) is not int or observations < 0:
            issues.append("observation count is invalid")
        else:
            for field in ("valid_markets", "markets_with_rules", "markets_with_two_sided_quotes"):
                count = result.get(field)
                if type(count) is not int or not 0 <= count <= observations:
                    issues.append(f"{field} is inconsistent with observed rows")
    elif kind == "cost_threshold_sweep":
        variants = result.get("variants")
        if not isinstance(variants, list) or len(variants) != 4:
            issues.append("predeclared threshold grid is incomplete")
        elif any(not isinstance(row, dict) for row in variants):
            issues.append("threshold result contains an invalid row")
    elif kind == "trench_survival_logistic":
        if not isinstance(result.get("metrics", result.get("status")), (dict, str)):
            issues.append("model audit summary is missing")
    elif kind == "commercial_opportunity_scan":
        if result.get("verified_buyer_count") != 0 or result.get("verified_payout_count") != 0:
            issues.append("public metadata cannot establish buyer or payout verification")
        if result.get("revenue_test_status") != "not_tested":
            issues.append("qualification scan must not be reported as a payment test")
        if result.get("mission_cash_receipt_usd") != "0":
            issues.append("unobserved mission receipts must remain zero")
        costs = result.get("attributable_costs_usd")
        if not isinstance(costs, dict) or costs.get("model") is not None or costs.get("compute") is not None:
            issues.append("unmetered costs must remain unknown")
        if result.get("live_eligible") is not False or result.get("delivery_tested") is not False:
            issues.append("commercial qualification cannot authorize live action or claim delivery")
    # Recursive finite-number check protects persisted evidence and review output.
    def finite(value: Any) -> bool:
        if isinstance(value, float):
            return math.isfinite(value)
        if isinstance(value, dict):
            return all(isinstance(key, str) and finite(item) for key, item in value.items())
        if isinstance(value, list):
            return all(finite(item) for item in value)
        return value is None or isinstance(value, (str, int, bool))

    if not finite(result):
        issues.append("result contains a non-finite or unsupported value")
    accepted = not issues
    return {
        "critic": CRITIC_ID,
        "status": "completed",
        "verdict": "PASS" if accepted else "quarantined",
        "result_accepted": accepted,
        "issues": issues,
        "economic_edge_proven": False,
        "live_eligible": False,
        "evidence_hash": evidence_hash,
        "next_priority": result.get("next_priority", "require_forward_validation"),
        "conclusion": ("Paper research result passed integrity checks; no deployment or trade is authorized."
                       if accepted else "Research result failed integrity checks; preserve and investigate."),
    }
