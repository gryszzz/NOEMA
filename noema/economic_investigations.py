"""Autonomous prioritization of open economic-manifest investigations.

Provider coverage remains the source of blocker truth. This module persists only
NOEMA's decisions and cooldowns; it never changes accounting coverage or policy.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .economic_ledger import EconomicLedger

_ACTIONS = {"INVESTIGATE", "WAIT", "RETRY_LATER", "REQUEST_OWNER_INPUT", "PASS"}
_ADR_VERSION = "autonomous-decision-record-v3"

# These are decision priors, not measured economics. Blockers and evidence always
# come from the current economic manifest; the priors rank what to do about them.
_PLANS: dict[str, dict[str, Any]] = {
    "polymarket_us": {
        "materiality": 0.92, "resolution_probability": 0.72, "information_value": 0.96,
        "cost_usd": 0.0, "engineering_effort": 0.12, "urgency": 0.90,
        "dependency_value": 0.94, "cooldown_hours": 168,
        "action": "REQUEST_OWNER_INPUT",
        "owner_input": "Official September Polymarket US account statement or activity export covering fees, the two balance changes ($10 and $15), and settlement cashflows. Share it through an owner-controlled secure channel; do not include credentials or private wallet data in prompts or logs.",
        "reason": "The activity scan is complete, but fees and cashflow reconciliation remain unknown. A source statement could resolve the largest cross-event accounting gap without repeating a read that already returned incomplete fields.",
        "materiality_basis": "Owner-provided context: 60 unresolved events and $25 across two pending balance changes; manifest does not yet reconcile those amounts.",
        "risk_downside": "Repeated account calls may incur small provider/engineering cost without exposing fees or final cash settlement.",
        "uncertainty": 0.74, "evidence_quality": "provider-observed activity; financial completeness unverified",
        "authority_required": ["read_only_polymarket_activity"],
        "change_my_mind_if": ["An official fee/settlement export becomes available", "Provider activity exposes authoritative fee and final cashflow fields"],
    },
    "wallets": {
        "materiality": 0.44, "resolution_probability": 0.62, "information_value": 0.70,
        "cost_usd": 0.03, "engineering_effort": 0.35, "urgency": 0.63,
        "dependency_value": 0.65, "cooldown_hours": 24, "action": "INVESTIGATE",
        "owner_input": None,
        "reason": "The Base fee is disputed and lacks event-time USD valuation; inspect existing transaction and explorer evidence before asking for new inputs.",
        "materiality_basis": "Fee amount remains disputed and unvalued; no USD materiality is assumed.",
        "risk_downside": "Explorer or price lookup may remain unavailable; do not convert the disputed fee without event-time price evidence.",
        "uncertainty": 0.68, "evidence_quality": "signer estimate and explorer observation conflict",
        "authority_required": ["read_only_base_explorer"],
        "change_my_mind_if": ["A canonical receipt exposes fee components", "Event-time USD price evidence becomes available"],
    },
    "stripe": {
        "materiality": 0.52, "resolution_probability": 0.48, "information_value": 0.64,
        "cost_usd": 0.04, "engineering_effort": 0.42, "urgency": 0.72,
        "dependency_value": 0.80, "cooldown_hours": 24, "action": "INVESTIGATE",
        "owner_input": None,
        "reason": "September revenue, refunds, fees, and payouts need complete read capability; inspect whether another already-authorized Stripe read path is available.",
        "materiality_basis": "Potentially affects revenue and net-period completeness; amounts remain unknown.",
        "risk_downside": "Another read may repeat the existing capability gap.",
        "uncertainty": 0.58, "evidence_quality": "partial provider snapshot; financial history incomplete",
        "authority_required": ["read_only_stripe"],
        "change_my_mind_if": ["An authorized balance-transaction/refund/payout endpoint is available"],
    },
    "operator_expenses": {
        "materiality": 0.36, "resolution_probability": 0.96, "information_value": 0.58,
        "cost_usd": 0.0, "engineering_effort": 0.05, "urgency": 0.62,
        "dependency_value": 0.58, "cooldown_hours": 168, "action": "REQUEST_OWNER_INPUT",
        "owner_input": "Either September NOEMA expense receipts/amounts with source references, or an explicit attestation that there were no operator-paid NOEMA expenses for September.",
        "reason": "This is likely resolvable with one owner response; repeated provider calls cannot establish whether off-ledger expenses occurred.",
        "materiality_basis": "Unknown until receipts or a no-expenses attestation are supplied.",
        "risk_downside": "Owner response may be delayed; silence cannot be interpreted as zero expense.",
        "uncertainty": 0.80, "evidence_quality": "no operator-source evidence yet",
        "authority_required": ["owner_input"],
        "change_my_mind_if": ["Owner supplies receipts or an explicit no-expenses attestation"],
    },
    "render": {
        "materiality": 0.42, "resolution_probability": 0.70, "information_value": 0.55,
        "cost_usd": 0.0, "engineering_effort": 0.10, "urgency": 0.54,
        "dependency_value": 0.56, "cooldown_hours": 168, "action": "REQUEST_OWNER_INPUT",
        "owner_input": "September Render invoice/statement plus the line-item or service-specific basis that attributes any shared charge to NOEMA.",
        "reason": "A hosting estimate exists, but only an invoice and defensible service allocation can turn it into attributable expense evidence.",
        "materiality_basis": "Current $7.25 estimate is not an invoice and is not treated as a cost.",
        "risk_downside": "The invoice may contain shared services that cannot be allocated to NOEMA.",
        "uncertainty": 0.70, "evidence_quality": "estimate only; no invoice",
        "authority_required": ["owner_input"],
        "change_my_mind_if": ["Provider invoice plus evidence-scoped NOEMA allocation is supplied"],
    },
    "openai": {
        "materiality": 0.31, "resolution_probability": 0.54, "information_value": 0.61,
        "cost_usd": 0.0, "engineering_effort": 0.16, "urgency": 0.43,
        "dependency_value": 0.62, "cooldown_hours": 168, "action": "REQUEST_OWNER_INPUT",
        "owner_input": "An OpenAI organization Costs export for September, or owner-authorized Admin API credentials through the existing secret manager (never in prompts or source files).",
        "reason": "Token-price estimates and reservations do not reconcile provider billing; a repeat inference call cannot resolve the invoice gap.",
        "materiality_basis": "Current usage cost remains an estimate; authoritative billed amount is unknown.",
        "risk_downside": "Admin Costs access or export may remain unavailable; the sub-cent estimate does not justify repeated work.",
        "uncertainty": 0.62, "evidence_quality": "usage observed; billing estimate only",
        "authority_required": ["owner_input_or_admin_costs_access"],
        "change_my_mind_if": ["An authoritative September Costs export becomes available"],
    },
    "kalshi": {
        "materiality": 0.40, "resolution_probability": 0.24, "information_value": 0.38,
        "cost_usd": 0.0, "engineering_effort": 0.48, "urgency": 0.38,
        "dependency_value": 0.55, "cooldown_hours": 72, "action": "WAIT",
        "owner_input": None,
        "reason": "The current read-only adapter has no refund/void/correction feed. Wait for a supported authorized evidence path instead of retrying the same incomplete scan.",
        "materiality_basis": "Refund, void, and correction amounts are unknown.",
        "risk_downside": "Same authorized feed may continue to omit corrections.",
        "uncertainty": 0.65, "evidence_quality": "authenticated fills and settlements; correction coverage absent",
        "authority_required": ["read_only_kalshi"],
        "change_my_mind_if": ["Kalshi exposes an authoritative correction/refund history"],
    },
    "cloudflare": {
        "materiality": 0.20, "resolution_probability": 0.10, "information_value": 0.12,
        "cost_usd": 0.0, "engineering_effort": 0.04, "urgency": 0.18,
        "dependency_value": 0.31, "cooldown_hours": 168, "action": "RETRY_LATER",
        "owner_input": None,
        "reason": "The billing endpoint returned HTTP 403. Without a permission/credential change, another request has near-zero expected information value.",
        "materiality_basis": "Provider cost is unknown; 403 is not evidence of zero usage.",
        "risk_downside": "Retrying the same denied billing route adds cost without changing access.",
        "uncertainty": 0.86, "evidence_quality": "attempt activity known; billing outcome unavailable",
        "authority_required": ["billing_permission_or_new_evidence"],
        "change_my_mind_if": ["Billing permission changes", "A provider usage export becomes available"],
    },
}


def _aware(value: str | None, fallback: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return fallback
    return parsed.astimezone(UTC) if parsed.tzinfo else fallback


def _fingerprint(payload: Any) -> str:
    packed = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(packed.encode()).hexdigest()


def _candidate(provider: dict[str, Any], now: datetime) -> dict[str, Any]:
    name = str(provider["provider"])
    plan = _PLANS.get(name)
    if plan is None:
        return {}
    blockers = list(provider.get("unresolved_blockers") or [])
    if not blockers and provider.get("state") in {"COMPLETE", "NOT_APPLICABLE"}:
        return {}
    urgency = float(plan["urgency"])
    evidence = provider.get("latest_evidence") or {}
    if name == "cloudflare" and evidence.get("billing_api_http_status") == 403:
        urgency = min(urgency, 0.12)
    # Values are prioritization estimates, never accounting amounts or facts.
    score = (
        0.28 * float(plan["materiality"])
        + 0.22 * float(plan["resolution_probability"])
        + 0.18 * float(plan["information_value"])
        + 0.13 * urgency
        + 0.13 * float(plan["dependency_value"])
        - 0.03 * min(float(plan["cost_usd"]) / 0.10, 1.0)
        - 0.03 * float(plan["engineering_effort"])
    )
    observed = _aware(provider.get("observed_at"), now)
    retry_at = now + timedelta(hours=int(plan["cooldown_hours"]))
    candidate_fingerprint = _fingerprint({
        "decision_engine_version": _ADR_VERSION,
        "provider": name, "state": provider.get("state"),
        "blockers": blockers, "evidence": evidence,
    })
    return {
        "candidate_id": name,
        "provider": name,
        "current_state": provider.get("state", "UNKNOWN"),
        "blocker": "; ".join(blockers),
        "evidence": evidence,
        "action": plan["action"],
        "expected_information_value": float(plan["information_value"]),
        "expected_benefit": {
            "kind": "information_value",
            "relative_score": float(plan["information_value"]),
            "economic_result_usd": None,
            "basis": "relative prioritization estimate; not a realized or forecast cash amount",
        },
        "estimated_potential_materiality": float(plan["materiality"]),
        "resolution_probability": float(plan["resolution_probability"]),
        "expected_api_model_compute_cost_usd": float(plan["cost_usd"]),
        "engineering_effort": float(plan["engineering_effort"]),
        "expected_resource_cost": {
            "api_model_compute_usd": float(plan["cost_usd"]),
            "engineering_effort_relative": float(plan["engineering_effort"]),
            "basis": "bounded planning estimate; actual use must be recorded after work",
        },
        "risk_downside": plan["risk_downside"],
        "uncertainty": float(plan["uncertainty"]),
        "evidence_quality": plan["evidence_quality"],
        "authority_required": list(plan["authority_required"]),
        "urgency": urgency,
        "dependency_value": float(plan["dependency_value"]),
        "priority_score": round(score, 4),
        "materiality_basis": plan["materiality_basis"],
        "rationale": plan["reason"],
        "owner_input_required": plan["owner_input"],
        "autonomous_action_permitted": plan["action"] in {"INVESTIGATE", "WAIT", "RETRY_LATER", "PASS"},
        "change_my_mind_if": list(plan["change_my_mind_if"]),
        "next_eligible_retry": retry_at.isoformat(),
        "cooldown_hours": int(plan["cooldown_hours"]),
        "manifest_observed_at": provider.get("observed_at"),
        "evidence_age_seconds": max(0, int((now - observed).total_seconds())),
        "candidate_fingerprint": candidate_fingerprint,
    }


class EconomicInvestigationStore:
    """Append-only decision history over the canonical provider coverage manifest."""

    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS economic_investigation_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month_utc TEXT NOT NULL,
                decided_at TEXT NOT NULL,
                input_fingerprint TEXT NOT NULL,
                action TEXT NOT NULL,
                selected_candidate TEXT,
                why TEXT NOT NULL,
                alternatives_json TEXT NOT NULL,
                blocker TEXT,
                expected_information_value REAL,
                expected_cost_usd REAL,
                next_eligible_retry TEXT,
                owner_input_required TEXT,
                last_result TEXT NOT NULL,
                UNIQUE(month_utc,input_fingerprint)
            )
        """)
        decision_columns = {
            "decision_version": "TEXT NOT NULL DEFAULT 'legacy-v1'",
            "objective": "TEXT NOT NULL DEFAULT ''",
            "manifest_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
            "selected_reason": "TEXT NOT NULL DEFAULT ''",
            "rejected_alternatives_json": "TEXT NOT NULL DEFAULT '[]'",
            "change_my_mind_json": "TEXT NOT NULL DEFAULT '[]'",
            "authority_required_json": "TEXT NOT NULL DEFAULT '[]'",
            "work_ref": "TEXT", "mission_id": "TEXT", "trial_id": "TEXT",
            "research_run_id": "TEXT", "cognition_session_id": "TEXT",
            "economic_event_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            "execution_proposal_id": "TEXT", "expected_risk_json": "TEXT NOT NULL DEFAULT '{}'",
            "expected_uncertainty": "REAL", "evidence_quality": "TEXT NOT NULL DEFAULT 'unknown'",
            "priority_change_reason": "TEXT NOT NULL DEFAULT ''",
        }
        columns = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(economic_investigation_decisions)")}
        for name, declaration in decision_columns.items():
            if name not in columns:
                self.conn.execute(
                    f"ALTER TABLE economic_investigation_decisions ADD COLUMN {name} {declaration}"
                )
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS economic_investigation_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER NOT NULL REFERENCES economic_investigation_decisions(id),
                recorded_at TEXT NOT NULL,
                result_state TEXT NOT NULL,
                detail TEXT NOT NULL,
                evidence_json TEXT NOT NULL
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS economic_investigation_handoffs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER NOT NULL REFERENCES economic_investigation_decisions(id),
                observation_ref TEXT NOT NULL UNIQUE,
                observed_at TEXT NOT NULL,
                observation_type TEXT NOT NULL,
                detail TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                actual_resources_json TEXT NOT NULL
            )
        """)
        result_columns = {
            "lifecycle_state": "TEXT NOT NULL DEFAULT 'OBSERVED'",
            "actual_resources_json": "TEXT NOT NULL DEFAULT '{}'",
            "actual_outcome_json": "TEXT NOT NULL DEFAULT '{}'",
            "measured_benefit_json": "TEXT NOT NULL DEFAULT '{}'",
            "counterfactual_json": "TEXT NOT NULL DEFAULT '{}'",
            "calibration_json": "TEXT NOT NULL DEFAULT '{}'",
            "work_ref": "TEXT", "mission_id": "TEXT", "trial_id": "TEXT",
            "research_run_id": "TEXT", "cognition_session_id": "TEXT",
            "economic_event_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            "execution_proposal_id": "TEXT",
        }
        columns = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(economic_investigation_results)")}
        for name, declaration in result_columns.items():
            if name not in columns:
                self.conn.execute(
                    f"ALTER TABLE economic_investigation_results ADD COLUMN {name} {declaration}"
                )
        # Decision-time rows and later result/maturation records are append-only.
        self.conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS economic_investigation_decisions_no_update
            BEFORE UPDATE ON economic_investigation_decisions BEGIN
                SELECT RAISE(ABORT, 'autonomous decision records are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS economic_investigation_decisions_no_delete
            BEFORE DELETE ON economic_investigation_decisions BEGIN
                SELECT RAISE(ABORT, 'autonomous decision records are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS economic_investigation_results_no_update
            BEFORE UPDATE ON economic_investigation_results BEGIN
                SELECT RAISE(ABORT, 'decision maturation records are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS economic_investigation_results_no_delete
            BEFORE DELETE ON economic_investigation_results BEGIN
                SELECT RAISE(ABORT, 'decision maturation records are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS economic_investigation_handoffs_no_update
            BEFORE UPDATE ON economic_investigation_handoffs BEGIN
                SELECT RAISE(ABORT, 'decision handoff observations are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS economic_investigation_handoffs_no_delete
            BEFORE DELETE ON economic_investigation_handoffs BEGIN
                SELECT RAISE(ABORT, 'decision handoff observations are immutable');
            END;
        """)
        self.conn.commit()

    def advance(self, *, month_utc: str, now: datetime | None = None) -> dict[str, Any]:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        db_path = str(self.conn.execute("PRAGMA database_list").fetchone()[2])
        manifest = EconomicLedger.read_projection(db_path, month_utc=month_utc)
        # Owner files only append evidence. The normal agent decision cycle owns
        # maturation, information-gain measurement, and the subsequent rerank.
        self._mature_pending_owner_evidence(month_utc, manifest, now)
        latest_by_provider: dict[str, dict[str, Any]] = {}
        for row in manifest.get("provider_coverage", []):
            if row.get("month_utc") == month_utc:
                latest_by_provider[str(row.get("provider"))] = row
        rows = [latest_by_provider[key] for key in sorted(latest_by_provider)]
        event_refs = self._economic_event_refs(month_utc)
        fingerprint_input = [{
            "provider": row.get("provider"), "state": row.get("state"),
            "blockers": row.get("unresolved_blockers"), "evidence": row.get("latest_evidence"),
            "observed_at": row.get("observed_at"),
        } for row in rows]
        manifest_fingerprint = _fingerprint(fingerprint_input)
        candidates = [item for row in rows if (item := _candidate(row, now))]
        candidates.sort(key=lambda item: (-float(item["priority_score"]), item["candidate_id"]))
        selected = candidates[0] if candidates else None
        for candidate in candidates:
            candidate["economic_event_ids"] = event_refs.get(candidate["candidate_id"], [])
            candidate["selection_status"] = "SELECTED" if candidate is selected else "REJECTED"
            candidate["selection_reason"] = (
                str(candidate["rationale"]) if candidate is selected else
                (f"Lower priority score than selected {selected['candidate_id']}." if selected
                 else "No candidate met current eligibility."))
        action = str(selected["action"]) if selected else "PASS"
        if action not in _ACTIONS:
            action = "PASS"
        # A change to an unrelated provider's snapshot must not repeat the same
        # request or provider call. Keep the current decision while its selected
        # blocker and evidence are unchanged and its backoff is still active.
        previous = self.conn.execute(
            "SELECT * FROM economic_investigation_decisions WHERE month_utc=? ORDER BY id DESC LIMIT 1",
            (month_utc,),
        ).fetchone()
        if previous is not None and selected is not None:
            old_candidates = json.loads(previous["alternatives_json"])
            old_selected = next((item for item in old_candidates
                                 if item.get("candidate_id") == previous["selected_candidate"]), {})
            retry_at = _aware(previous["next_eligible_retry"], now)
            same_source_evidence = (
                old_selected.get("blocker") == selected.get("blocker")
                and old_selected.get("evidence") == selected.get("evidence")
            )
            if (previous["decision_version"] == _ADR_VERSION
                    and previous["selected_candidate"] == selected["candidate_id"]
                    and (old_selected.get("candidate_fingerprint") == selected["candidate_fingerprint"]
                         or same_source_evidence)
                    and now < retry_at):
                return self._payload(
                    previous, old_candidates, created=False,
                    period_closure=str(manifest.get("period_closure") or "OPEN"),
                    last_result=self._latest_result_state(int(previous["id"])),
                    as_of=now,
                )

        # The uniqueness key is a cooldown window, so an unchanged blocker can
        # become eligible again after its backoff expires without mutating history.
        cooldown_seconds = max(1, int(selected["cooldown_hours"] * 3600)) if selected else 1
        retry_window = (int(now.timestamp()) // cooldown_seconds) if selected else None
        fingerprint = _fingerprint({
            "decision_record_version": _ADR_VERSION,
            "manifest": manifest_fingerprint,
            "retry_window": retry_window,
            "selected_candidate": None if selected is None else selected["candidate_id"],
            "selected_evidence": None if selected is None else selected["candidate_fingerprint"],
        })
        # A persisted identical decision is the cooldown: don't repeat work or prompt.
        period_closure = str(manifest.get("period_closure") or "OPEN")
        existing = self.conn.execute(
            "SELECT * FROM economic_investigation_decisions WHERE month_utc=? AND input_fingerprint=?",
            (month_utc, fingerprint),
        ).fetchone()
        if existing is not None:
            return self._payload(
                existing, candidates, created=False, period_closure=period_closure,
                last_result=self._latest_result_state(int(existing["id"])),
                as_of=now,
            )
        self._close_previous_decision(month_utc, fingerprint, latest_by_provider, now)
        why = (str(selected["rationale"]) if selected else
               "No unresolved provider blocker in the current economic manifest justifies work.")
        result = "AWAITING_OWNER_INPUT" if action == "REQUEST_OWNER_INPUT" else "NOT_STARTED"
        objective = (f"Investigate {selected['candidate_id']} to reduce the {month_utc} accounting blocker."
                     if selected else f"Review economic evidence blockers for {month_utc}.")
        if previous is None:
            priority_change_reason = "Initial persisted autonomous ranking for the open period."
        elif previous["selected_candidate"] != (None if selected is None else selected["candidate_id"]):
            priority_change_reason = (
                f"Manifest reevaluation changed the selected priority from "
                f"{previous['selected_candidate'] or 'none'} to "
                f"{None if selected is None else selected['candidate_id']}; see both immutable snapshots."
            )
        elif previous["decision_version"] != _ADR_VERSION:
            priority_change_reason = (
                f"Decision record upgraded from {previous['decision_version']} to {_ADR_VERSION}; "
                "the current manifest was re-evaluated without dispatching provider or financial actions."
            )
        else:
            priority_change_reason = (
                "A new decision became eligible after cooldown or changed evidence; "
                "the selected blocker remains the highest-ranked candidate."
            )
        snapshot = {
            "decision_version": _ADR_VERSION, "month_utc": month_utc,
            "observed_at": now.isoformat(), "period_closure": period_closure,
            "coverage_status": manifest.get("coverage_status"),
            "blocking_providers": manifest.get("blocking_providers", []),
            "provider_coverage": rows,
            "canonical_economic_event_ids_by_provider": event_refs,
            "net_verified_contribution_usd": manifest.get("net_verified_contribution_usd"),
            "operating_reserve_usd": manifest.get("operating_reserve_usd"),
            "self_funding_ratio": manifest.get("self_funding_ratio"),
        }
        rejected = [item for item in candidates if item is not selected]
        selected_risk = {} if selected is None else {"downside": selected["risk_downside"]}
        selected_uncertainty = None if selected is None else selected["uncertainty"]
        selected_quality = "manifest snapshot" if selected is None else selected["evidence_quality"]
        authority = [] if selected is None else selected["authority_required"]
        work_ref = (f"owner-input:{month_utc}:{selected['candidate_id']}:{fingerprint[:16]}"
                    if action == "REQUEST_OWNER_INPUT" and selected else
                    f"economic-investigation:{month_utc}:{selected['candidate_id']}:{fingerprint[:16]}"
                    if selected else None)
        cursor = self.conn.execute(
            "INSERT INTO economic_investigation_decisions(month_utc,decided_at,input_fingerprint,action,"
            "selected_candidate,why,alternatives_json,blocker,expected_information_value,expected_cost_usd,"
            "next_eligible_retry,owner_input_required,last_result,decision_version,objective,"
            "manifest_snapshot_json,selected_reason,rejected_alternatives_json,change_my_mind_json,"
            "authority_required_json,work_ref,economic_event_ids_json,expected_risk_json,"
            "expected_uncertainty,evidence_quality,priority_change_reason) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (month_utc, now.isoformat(), fingerprint, action,
             None if selected is None else selected["candidate_id"], why,
             json.dumps(candidates, sort_keys=True), None if selected is None else selected["blocker"],
             None if selected is None else selected["expected_information_value"],
             None if selected is None else selected["expected_api_model_compute_cost_usd"],
             None if selected is None else selected["next_eligible_retry"],
             None if selected is None else selected["owner_input_required"], result,
             _ADR_VERSION, objective, json.dumps(snapshot, sort_keys=True, default=str), why,
             json.dumps(rejected, sort_keys=True),
             json.dumps([] if selected is None else selected["change_my_mind_if"]),
             json.dumps(authority), work_ref,
             json.dumps(event_refs.get(str(selected["candidate_id"]), []) if selected else []),
             json.dumps(selected_risk, sort_keys=True), selected_uncertainty, selected_quality,
             priority_change_reason),
        )
        decision_id = int(cursor.lastrowid)
        decision_evidence = json.dumps({
            "selected_candidate": None if selected is None else selected["candidate_id"],
            "candidate_fingerprint": None if selected is None else selected["candidate_fingerprint"],
            "manifest_fingerprint": manifest_fingerprint,
            "decision_snapshot_hash": _fingerprint(snapshot),
        }, sort_keys=True)
        final_lifecycle = ("OWNER_INPUT_REQUIRED" if action == "REQUEST_OWNER_INPUT"
                           else "SELECTED" if action == "INVESTIGATE" else action)
        stages = [
            ("PROPOSED", "Candidate actions proposed from the current September manifest."),
            ("EVALUATED", "Candidates compared using recorded materiality, resolution probability, information value, cost, urgency, dependency, evidence age, and authority requirements."),
            (final_lifecycle,
             "Owner input is required before this blocker can be resolved." if action == "REQUEST_OWNER_INPUT"
             else f"Decision selected {action}; no investigation result has been claimed yet."),
        ]
        for lifecycle, detail in stages:
            state = result if lifecycle == final_lifecycle else lifecycle
            self.conn.execute(
                "INSERT INTO economic_investigation_results(decision_id,recorded_at,result_state,detail,evidence_json,"
                "lifecycle_state,work_ref,mission_id,trial_id,research_run_id,cognition_session_id,"
                "economic_event_ids_json,execution_proposal_id,actual_resources_json,actual_outcome_json,"
                "measured_benefit_json,counterfactual_json,calibration_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (decision_id, now.isoformat(), state, detail, decision_evidence, lifecycle,
                 work_ref, None, None, None, None,
                 json.dumps(event_refs.get(str(selected["candidate_id"]), []) if selected else []),
                 None, "{}", "{}", "{}", "{}", "{}"),
            )
        self.conn.commit()
        if "polymarket_owner_evidence_imports" in {
            str(item[0]) for item in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }:
            self.conn.execute(
                "UPDATE polymarket_owner_evidence_imports SET next_decision_id=? "
                "WHERE month_utc=? AND result_recorded=1 AND next_decision_id IS NULL",
                (decision_id, month_utc),
            )
            self.conn.commit()
        row = self.conn.execute("SELECT * FROM economic_investigation_decisions WHERE id=?", (decision_id,)).fetchone()
        return self._payload(
            row, candidates, created=True, period_closure=period_closure,
            last_result=self._latest_result_state(decision_id),
            as_of=now,
        )

    def _mature_pending_owner_evidence(
        self, month_utc: str, manifest: dict[str, Any], now: datetime,
    ) -> None:
        tables = {str(row[0]) for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "polymarket_owner_evidence_imports" not in tables:
            return
        pending = self.conn.execute(
            "SELECT * FROM polymarket_owner_evidence_imports WHERE month_utc=? "
            "AND result_recorded=0 ORDER BY id", (month_utc,),
        ).fetchall()
        current_pm = next((item for item in manifest.get("provider_coverage", [])
                           if item.get("provider") == "polymarket_us"), {})
        for imported in pending:
            source_hash = str(imported["source_sha256"])
            decision_id = imported["decision_id"]
            if decision_id is None:
                row = self.conn.execute(
                    "SELECT id FROM economic_investigation_decisions WHERE month_utc=? "
                    "AND action='REQUEST_OWNER_INPUT' AND selected_candidate='polymarket_us' "
                    "ORDER BY id DESC LIMIT 1", (month_utc,),
                ).fetchone()
                if row is None:
                    continue
                decision_id = int(row[0])
            previous = self.conn.execute(
                "SELECT manifest_snapshot_json FROM economic_investigation_decisions WHERE id=?",
                (decision_id,),
            ).fetchone()
            if previous is None:
                continue
            prior_result = self.conn.execute(
                "SELECT id FROM economic_investigation_results WHERE decision_id=? AND evidence_json LIKE ? LIMIT 1",
                (decision_id, f'%"source_sha256": "{source_hash}"%'),
            ).fetchone()
            if prior_result is not None:
                self.conn.execute(
                    "UPDATE polymarket_owner_evidence_imports SET result_recorded=1,decision_id=? "
                    "WHERE source_sha256=?", (decision_id, source_hash),
                )
                self.conn.commit()
                continue

            summary = json.loads(imported["summary_json"] or "{}")
            snapshot = json.loads(previous[0] or "{}")
            prior_pm = next((item for item in snapshot.get("provider_coverage", [])
                             if item.get("provider") == "polymarket_us"), {})
            before_blockers = list(prior_pm.get("unresolved_blockers") or [])
            after_blockers = list(current_pm.get("unresolved_blockers") or [])
            before_classes = set(prior_pm.get("actual_evidence_classes") or [])
            after_classes = set(current_pm.get("actual_evidence_classes") or [])
            classes_added = sorted(after_classes - before_classes)
            blocker_reduction = max(0, len(before_blockers) - len(after_blockers))
            required = set(prior_pm.get("expected_evidence_classes") or [])
            unresolved_before = len(required - before_classes) + len(before_blockers)
            unresolved_after = len(required - after_classes) + len(after_blockers)
            units_resolved = max(0, unresolved_before - unresolved_after)
            score = round(units_resolved / unresolved_before, 4) if unresolved_before else 0.0
            if current_pm.get("state") in {"COMPLETE", "NOT_APPLICABLE"} and not after_blockers:
                result_state = "RESOLVED"
            elif classes_added or blocker_reduction or summary.get("final_balance_rows"):
                result_state = "PARTIALLY_RESOLVED"
            elif summary.get("unmatched_rows") or summary.get("ambiguous_rows") or summary.get("unknown_rows") or summary.get("pending_balance_rows"):
                result_state = "BLOCKED"
            else:
                result_state = "NO_NEW_EVIDENCE"
            source_evidence = {
                "source_sha256": source_hash,
                "source_reference": imported["source_reference"],
                "source_filename": imported["source_filename"],
                "row_count": int(imported["row_count"]),
                "matched_existing_events": int(summary.get("matched_existing_events", 0)),
                "unmatched_rows": int(summary.get("unmatched_rows", 0)),
                "ambiguous_rows": int(summary.get("ambiguous_rows", 0)),
                "unknown_rows": int(summary.get("unknown_rows", 0)),
                "fee_rows": int(summary.get("fee_rows", 0)),
                "settlement_rows": int(summary.get("settlement_rows", 0)),
                "final_balance_rows": int(summary.get("final_balance_rows", 0)),
                "pending_balance_rows": int(summary.get("pending_balance_rows", 0)),
                "canonical_event_ids": summary.get("event_ids", []),
                "evidence_classes_added": classes_added,
                "blockers_before": before_blockers,
                "blockers_after": after_blockers,
                "unknown_is_not_zero": True,
                "owner_input_only": True,
            }
            resources = {
                "provider_api_calls": 0, "model_tokens": 0,
                "external_api_cost_usd": 0.0, "model_cost_usd": 0.0,
                "local_compute_cost_usd": None,
                "cost_status": "no external calls; local compute not monetized",
            }
            information_gain = {
                "score": score,
                "method": "resolved evidence units / previously unresolved required classes plus explicit blockers",
                "resolved_units": units_resolved,
                "unresolved_units_before": unresolved_before,
                "unresolved_units_after": unresolved_after,
                "evidence_classes_added": classes_added,
                "blockers_removed": blocker_reduction,
            }
            self.record_result(
                int(decision_id), result_state=result_state,
                detail=(f"Owner evidence observed by the normal agent cycle: {result_state}; "
                        f"unresolved units {unresolved_before} → {unresolved_after}."),
                evidence=source_evidence, recorded_at=now, lifecycle_state="MATURED",
                actual_resources=resources,
                actual_outcome={"state": result_state, "manifest_state": current_pm.get("state"),
                                "blockers_remaining": after_blockers,
                                "pending_balance_changes": summary.get("pending_balance_rows", 0)},
                measured_benefit={"information_gain": information_gain,
                                  "actual_external_api_cost_usd": 0.0,
                                  "local_compute_cost_usd": None},
                calibration={"outcome_quality_score": score, "method": information_gain["method"]},
                links={"economic_event_ids": summary.get("event_ids", []),
                       "work_ref": f"owner-export:{source_hash}"},
            )
            self.conn.execute(
                "UPDATE polymarket_owner_evidence_imports SET result_recorded=1,decision_id=? "
                "WHERE source_sha256=?", (decision_id, source_hash),
            )
            self.conn.commit()

    def _economic_event_refs(self, month_utc: str) -> dict[str, list[int]]:
        tables = {str(row[0]) for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "economic_events" not in tables:
            return {}
        refs: dict[str, list[int]] = {}
        for provider, event_id in self.conn.execute(
            "SELECT provider,id FROM economic_events WHERE substr(occurred_at,1,7)=? "
            "AND provider IS NOT NULL ORDER BY id", (month_utc,),
        ):
            key = "wallets" if str(provider).startswith("wallet:") else str(provider)
            refs.setdefault(key, []).append(int(event_id))
        return refs

    def _close_previous_decision(
        self, month_utc: str, new_fingerprint: str,
        latest_by_provider: dict[str, dict[str, Any]], now: datetime,
    ) -> None:
        previous = self.conn.execute(
            "SELECT * FROM economic_investigation_decisions WHERE month_utc=? ORDER BY id DESC LIMIT 1",
            (month_utc,),
        ).fetchone()
        if previous is None or previous["input_fingerprint"] == new_fingerprint:
            return
        previous_result = self._latest_result_state(int(previous["id"])) or previous["last_result"]
        if previous_result not in {"NOT_STARTED", "AWAITING_OWNER_INPUT", "OWNER_INPUT_REQUIRED",
                                   "SELECTED", "WAIT", "RETRY_LATER", "PASS"}:
            return
        provider = str(previous["selected_candidate"] or "")
        old_candidates = json.loads(previous["alternatives_json"])
        old_candidate = next((item for item in old_candidates if item.get("candidate_id") == provider), {})
        current = latest_by_provider.get(provider)
        current_blockers = list((current or {}).get("unresolved_blockers") or [])
        current_state = (current or {}).get("state", "UNKNOWN")
        current_evidence = (current or {}).get("latest_evidence") or {}
        same_blocker = old_candidate.get("blocker") == "; ".join(current_blockers)
        same_evidence = old_candidate.get("evidence") == current_evidence
        if same_blocker and same_evidence:
            # Re-evaluation, schema upgrades, or changes elsewhere in the period
            # are not an outcome for this decision. Keep its existing lifecycle.
            return
        if current and current_state in {"COMPLETE", "NOT_APPLICABLE"} and not current_blockers:
            state = "RESOLVED"
            detail = "The selected provider blocker is now complete in the canonical period manifest."
        else:
            state = "BLOCKED"
            detail = "New evidence changed the selected provider record, but its manifest blocker remains unresolved."
        evidence = {
            "source": "economic_provider_coverage manifest",
            "provider": provider,
            "state": current_state,
            "blockers": current_blockers,
            "manifest_observed_at": (current or {}).get("observed_at"),
        }
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT INTO economic_investigation_results(decision_id,recorded_at,result_state,detail,evidence_json,"
                "lifecycle_state,work_ref,mission_id,trial_id,research_run_id,cognition_session_id,"
                "economic_event_ids_json,execution_proposal_id,actual_resources_json,actual_outcome_json,"
                "measured_benefit_json,counterfactual_json,calibration_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (previous["id"], now.isoformat(), state, detail,
                 json.dumps(evidence, sort_keys=True, default=str), "MATURED", previous["work_ref"],
                 previous["mission_id"], previous["trial_id"], previous["research_run_id"],
                 previous["cognition_session_id"], previous["economic_event_ids_json"],
                 previous["execution_proposal_id"], json.dumps({"provider_api_calls": 0,
                     "model_tokens": 0, "compute_cost_usd": None,
                     "cost_status": "unknown"}, sort_keys=True),
                 json.dumps({"state": state, "provider_state": current_state,
                     "blockers": current_blockers}, sort_keys=True),
                 json.dumps({"amount_usd": None, "status": "unknown"}),
                 json.dumps({"available": False,
                     "reason": "No decision-time counterfactual outcome was observed."}),
                 json.dumps({"status": "not_assessable",
                     "reason": "No matured economic result supports calibration yet."})),
            )
            self.conn.execute(
                "INSERT INTO economic_investigation_results(decision_id,recorded_at,result_state,detail,evidence_json,"
                "lifecycle_state,work_ref,mission_id,trial_id,research_run_id,cognition_session_id,"
                "economic_event_ids_json,execution_proposal_id,actual_resources_json,actual_outcome_json,"
                "measured_benefit_json,counterfactual_json,calibration_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (previous["id"], now.isoformat(), "REVIEWED",
                 "Decision reviewed against the matured manifest observation; benefit and calibration remain unknown.",
                 json.dumps({"source": "economic_provider_coverage manifest", "maturation_state": state}),
                 "REVIEWED", previous["work_ref"], previous["mission_id"], previous["trial_id"],
                 previous["research_run_id"], previous["cognition_session_id"],
                 previous["economic_event_ids_json"], previous["execution_proposal_id"],
                 json.dumps({"provider_api_calls": 0, "model_tokens": 0,
                     "compute_cost_usd": None, "cost_status": "unknown"}),
                 json.dumps({"state": state, "provider_state": current_state}),
                 json.dumps({"amount_usd": None, "status": "unknown"}),
                 json.dumps({"available": False, "reason": "No matched counterfactual."}),
                 json.dumps({"status": "not_assessable"})),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    @staticmethod
    def _payload(
        row: sqlite3.Row, candidates: list[dict[str, Any]], *, created: bool,
        period_closure: str = "OPEN", last_result: str | None = None,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        current = (as_of or datetime.now(UTC)).astimezone(UTC)
        row_keys = set(row.keys())
        current_candidates = [dict(item) for item in candidates]
        for item in current_candidates:
            observed = item.get("manifest_observed_at")
            item["evidence_age_seconds"] = max(
                0, int((current - _aware(observed, current)).total_seconds()),
            )
            if "selection_status" not in item:
                item["selection_status"] = (
                    "SELECTED" if item.get("candidate_id") == row["selected_candidate"] else "REJECTED"
                )
                item["selection_reason"] = (
                    dict(row).get("selected_reason", "")
                    or "Legacy decision did not persist a selection reason; this label is reconstructed for display."
                )
        selected = next((item for item in current_candidates
                         if item.get("candidate_id") == row["selected_candidate"]), None)
        current_result = last_result or row["last_result"]
        active_lifecycle = {
            "AWAITING_OWNER_INPUT": "OWNER_INPUT_REQUIRED",
            "NOT_STARTED": "SELECTED",
            "RESOLVED": "MATURED", "NO_NEW_EVIDENCE": "MATURED",
            "FAILED": "BLOCKED",
        }.get(current_result, current_result)
        return {
            "decision_id": int(row["id"]), "month_utc": row["month_utc"],
            "decided_at": row["decided_at"], "action": row["action"],
            "selected_candidate": row["selected_candidate"], "why": row["why"],
            "alternatives_considered": current_candidates,
            "expected_information_value": row["expected_information_value"],
            "expected_cost_usd": row["expected_cost_usd"], "blocker": row["blocker"],
            "next_eligible_retry": row["next_eligible_retry"],
            "owner_input_required": row["owner_input_required"],
            "last_decision_result": last_result or row["last_result"],
            "selected_assessment": selected,
            "decision_version": row["decision_version"] if "decision_version" in row_keys else "legacy-v1",
            "objective": row["objective"] if "objective" in row_keys else "",
            "decision_state_at_time": json.loads(row["manifest_snapshot_json"] or "{}")
            if "manifest_snapshot_json" in row_keys else {},
            "selected_reason": row["selected_reason"] if "selected_reason" in row_keys else row["why"],
            "rejected_alternatives": json.loads(row["rejected_alternatives_json"] or "[]")
            if "rejected_alternatives_json" in row_keys else [],
            "change_my_mind_if": json.loads(row["change_my_mind_json"] or "[]")
            if "change_my_mind_json" in row_keys else [],
            "authority_required": json.loads(row["authority_required_json"] or "[]")
            if "authority_required_json" in row_keys else [],
            "work_ref": row["work_ref"] if "work_ref" in row_keys else None,
            "mission_id": row["mission_id"] if "mission_id" in row_keys else None,
            "trial_id": row["trial_id"] if "trial_id" in row_keys else None,
            "research_run_id": row["research_run_id"] if "research_run_id" in row_keys else None,
            "cognition_session_id": row["cognition_session_id"] if "cognition_session_id" in row_keys else None,
            "economic_event_ids": json.loads(row["economic_event_ids_json"] or "[]")
            if "economic_event_ids_json" in row_keys else [],
            "execution_proposal_id": row["execution_proposal_id"]
            if "execution_proposal_id" in row_keys else None,
            "expected_risk": json.loads(row["expected_risk_json"] or "{}")
            if "expected_risk_json" in row_keys else {},
            "uncertainty": row["expected_uncertainty"]
            if "expected_uncertainty" in row_keys else None,
            "evidence_quality": row["evidence_quality"]
            if "evidence_quality" in row_keys else "unknown",
            "priority_change_reason": row["priority_change_reason"]
            if "priority_change_reason" in row_keys else "Legacy record did not persist a priority-change reason.",
            "active_lifecycle_state": active_lifecycle,
            "authority_state": "decision_only; deterministic_execution_gateway_required",
            "input_source": "latest economic_provider_coverage manifest",
            "financial_authority_changed": False, "created_now": created,
            "period_closure": period_closure,
        }

    def record_result(
        self, decision_id: int, *, result_state: str, detail: str,
        evidence: dict[str, Any] | None = None, recorded_at: datetime | None = None,
        lifecycle_state: str | None = None,
        actual_resources: dict[str, Any] | None = None,
        actual_outcome: dict[str, Any] | None = None,
        measured_benefit: dict[str, Any] | None = None,
        counterfactual: dict[str, Any] | None = None,
        calibration: dict[str, Any] | None = None,
        links: dict[str, Any] | None = None,
    ) -> None:
        allowed = {"NOT_STARTED", "RESOLVED", "PARTIALLY_RESOLVED", "BLOCKED", "NO_NEW_EVIDENCE", "FAILED",
                   "AWAITING_OWNER_INPUT", "PROPOSED", "EVALUATED", "SELECTED", "PASS",
                   "WAIT", "RETRY_LATER", "OWNER_INPUT_REQUIRED", "STARTED", "MATURING",
                   "MATURED", "REVIEWED"}
        if result_state not in allowed or not detail.strip():
            raise ValueError("investigation result needs an allowed state and detail")
        lifecycle_state = lifecycle_state or {
            "RESOLVED": "MATURED", "PARTIALLY_RESOLVED": "MATURED", "NO_NEW_EVIDENCE": "MATURED",
            "AWAITING_OWNER_INPUT": "OWNER_INPUT_REQUIRED", "FAILED": "BLOCKED",
        }.get(result_state, result_state)
        lifecycle_states = {"PROPOSED", "EVALUATED", "SELECTED", "PASS", "WAIT",
                            "RETRY_LATER", "OWNER_INPUT_REQUIRED", "BLOCKED", "STARTED",
                            "MATURING", "MATURED", "REVIEWED"}
        if lifecycle_state not in lifecycle_states:
            raise ValueError("unsupported autonomous decision lifecycle state")
        at = recorded_at or datetime.now(UTC)
        if at.tzinfo is None:
            raise ValueError("investigation result timestamp must be timezone-aware")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT id FROM economic_investigation_decisions WHERE id=?", (decision_id,)).fetchone()
            if row is None:
                raise ValueError("investigation decision not found")
            links = links or {}
            self.conn.execute(
                "INSERT INTO economic_investigation_results(decision_id,recorded_at,result_state,detail,evidence_json,"
                "lifecycle_state,actual_resources_json,actual_outcome_json,measured_benefit_json,"
                "counterfactual_json,calibration_json,work_ref,mission_id,trial_id,research_run_id,"
                "cognition_session_id,economic_event_ids_json,execution_proposal_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (decision_id, at.astimezone(UTC).isoformat(), result_state, detail,
                 json.dumps(evidence or {}, sort_keys=True, default=str), lifecycle_state,
                 json.dumps(actual_resources or {}, sort_keys=True, default=str),
                 json.dumps(actual_outcome or {}, sort_keys=True, default=str),
                 json.dumps(measured_benefit or {}, sort_keys=True, default=str),
                 json.dumps(counterfactual or {}, sort_keys=True, default=str),
                 json.dumps(calibration or {}, sort_keys=True, default=str),
                 links.get("work_ref"), links.get("mission_id"), links.get("trial_id"),
                 links.get("research_run_id"), links.get("cognition_session_id"),
                 json.dumps(links.get("economic_event_ids", [])), links.get("execution_proposal_id")),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def record_handoff_observation(
        self, decision_id: int, *, observation_ref: str, observation_type: str,
        detail: str, evidence: dict[str, Any], actual_resources: dict[str, Any],
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Append an idempotent local handoff/restart observation, not a new decision result."""
        if not observation_ref.strip() or not observation_type.strip() or not detail.strip():
            raise ValueError("handoff observation reference, type, and detail are required")
        at = observed_at or datetime.now(UTC)
        if at.tzinfo is None:
            raise ValueError("handoff observation timestamp must be timezone-aware")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            decision = self.conn.execute(
                "SELECT action,selected_candidate FROM economic_investigation_decisions WHERE id=?",
                (decision_id,),
            ).fetchone()
            if decision is None:
                raise ValueError("investigation decision not found")
            if (decision["action"] != "REQUEST_OWNER_INPUT"
                    or decision["selected_candidate"] != "polymarket_us"):
                raise ValueError("handoff observation must attach to the active Polymarket owner-input decision")
            existing = self.conn.execute(
                "SELECT id FROM economic_investigation_handoffs WHERE observation_ref=?",
                (observation_ref,),
            ).fetchone()
            if existing is not None:
                self.conn.commit()
                return {"handoff_id": int(existing[0]), "created": False}
            cursor = self.conn.execute(
                "INSERT INTO economic_investigation_handoffs(decision_id,observation_ref,observed_at,"
                "observation_type,detail,evidence_json,actual_resources_json) VALUES(?,?,?,?,?,?,?)",
                (decision_id, observation_ref, at.astimezone(UTC).isoformat(), observation_type,
                 detail, json.dumps(evidence, sort_keys=True, default=str),
                 json.dumps(actual_resources, sort_keys=True, default=str)),
            )
            self.conn.commit()
            return {"handoff_id": int(cursor.lastrowid), "created": True}
        except Exception:
            self.conn.rollback()
            raise

    def latest(self, *, month_utc: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM economic_investigation_decisions WHERE month_utc=? ORDER BY id DESC LIMIT 1",
            (month_utc,),
        ).fetchone()
        if row is None:
            return None
        db_path = str(self.conn.execute("PRAGMA database_list").fetchone()[2])
        projection = EconomicLedger.read_projection(db_path, month_utc=month_utc)
        result = self._payload(
            row, json.loads(row["alternatives_json"]), created=False,
            period_closure=str(projection.get("period_closure") or "OPEN"),
            last_result=self._latest_result_state(int(row["id"])),
        )
        result_rows = self.conn.execute(
            "SELECT * FROM economic_investigation_results WHERE decision_id=? ORDER BY id DESC LIMIT 30",
            (row["id"],),
        ).fetchall()
        result["result_history"] = [self._result_payload(item) for item in result_rows]
        result["active_lifecycle_state"] = (
            result["result_history"][0]["lifecycle_state"] if result["result_history"] else "PROPOSED"
        )
        result["decision_history"] = self._decision_history(self.conn, month_utc, limit=20)
        result["handoff_observations"] = self._handoff_observations(
            self.conn, int(row["id"]), limit=20,
        )
        return result

    @staticmethod
    def _handoff_observations(
        conn: sqlite3.Connection, decision_id: int, *, limit: int,
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM economic_investigation_handoffs WHERE decision_id=? "
            "ORDER BY id DESC LIMIT ?", (decision_id, max(1, min(int(limit), 100))),
        ).fetchall()
        return [{
            "handoff_id": int(item["id"]), "observation_ref": item["observation_ref"],
            "observed_at": item["observed_at"], "observation_type": item["observation_type"],
            "detail": item["detail"], "evidence": json.loads(item["evidence_json"]),
            "actual_resources": json.loads(item["actual_resources_json"]),
        } for item in rows]

    @staticmethod
    def _decision_history(
        conn: sqlite3.Connection, month_utc: str, *, limit: int,
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM economic_investigation_decisions WHERE month_utc=? ORDER BY id DESC LIMIT ?",
            (month_utc, max(1, min(int(limit), 100))),
        ).fetchall()
        history: list[dict[str, Any]] = []
        for row in rows:
            payload = EconomicInvestigationStore._payload(
                row, json.loads(row["alternatives_json"]), created=False,
                last_result=None,
            )
            results = conn.execute(
                "SELECT * FROM economic_investigation_results WHERE decision_id=? ORDER BY id DESC LIMIT 30",
                (row["id"],),
            ).fetchall()
            payload["result_history"] = [EconomicInvestigationStore._result_payload(item)
                                         for item in results]
            if results:
                payload["last_decision_result"] = str(results[0]["result_state"])
            payload["active_lifecycle_state"] = (
                payload["result_history"][0]["lifecycle_state"]
                if payload["result_history"] else "PROPOSED"
            )
            history.append(payload)
        return history

    @staticmethod
    def _result_payload(row: sqlite3.Row) -> dict[str, Any]:
        def json_value(key: str, default: Any) -> Any:
            try:
                raw = row[key]
            except (IndexError, KeyError):
                return default
            try:
                return json.loads(raw) if raw else default
            except (TypeError, ValueError):
                return default
        keys = set(row.keys())
        result_state = row["result_state"]
        lifecycle = row["lifecycle_state"] if "lifecycle_state" in keys else "OBSERVED"
        if lifecycle == "OBSERVED":
            lifecycle = {
                "AWAITING_OWNER_INPUT": "OWNER_INPUT_REQUIRED",
                "RESOLVED": "MATURED", "NO_NEW_EVIDENCE": "MATURED",
                "FAILED": "BLOCKED",
            }.get(result_state, result_state)
        return {
            "result_id": row["id"] if "id" in keys else None,
            "recorded_at": row["recorded_at"], "state": result_state,
            "lifecycle_state": lifecycle,
            "detail": row["detail"], "evidence": json_value("evidence_json", {}),
            "actual_resources": json_value("actual_resources_json", {}),
            "actual_outcome": json_value("actual_outcome_json", {}),
            "measured_benefit": json_value("measured_benefit_json", {}),
            "counterfactual": json_value("counterfactual_json", {}),
            "calibration": json_value("calibration_json", {}),
            "work_ref": row["work_ref"] if "work_ref" in keys else None,
            "mission_id": row["mission_id"] if "mission_id" in keys else None,
            "trial_id": row["trial_id"] if "trial_id" in keys else None,
            "research_run_id": row["research_run_id"] if "research_run_id" in keys else None,
            "cognition_session_id": row["cognition_session_id"] if "cognition_session_id" in keys else None,
            "economic_event_ids": json_value("economic_event_ids_json", []),
            "execution_proposal_id": row["execution_proposal_id"] if "execution_proposal_id" in keys else None,
        }

    def _latest_result_state(self, decision_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT result_state FROM economic_investigation_results "
            "WHERE decision_id=? ORDER BY id DESC LIMIT 1", (decision_id,),
        ).fetchone()
        return None if row is None else str(row[0])


def advance_current_period_investigation(
    path: str = "data/noema.db", *, now: datetime | None = None,
) -> dict[str, Any]:
    month = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m")
    store = EconomicInvestigationStore(path)
    try:
        return store.advance(month_utc=month, now=now)
    finally:
        store.conn.close()


def latest_current_period_investigation(
    path: str = "data/noema.db", *, now: datetime | None = None,
) -> dict[str, Any] | None:
    month = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m")
    if not Path(path).is_file():
        return None
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='economic_investigation_decisions'"
        ).fetchone()
        if not exists:
            return None
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM economic_investigation_decisions WHERE month_utc=? ORDER BY id DESC LIMIT 1",
            (month,),
        ).fetchone()
        if row is None:
            return None
        history = conn.execute(
            "SELECT * FROM economic_investigation_results "
            "WHERE decision_id=? ORDER BY id DESC LIMIT 20", (row["id"],),
        ).fetchall()
        projection = EconomicLedger.read_projection(path, month_utc=month)
        payload = EconomicInvestigationStore._payload(
            row, json.loads(row["alternatives_json"]), created=False,
            period_closure=str(projection.get("period_closure") or "OPEN"),
            last_result=(None if not history else str(history[0]["result_state"])),
        )
        payload["result_history"] = [EconomicInvestigationStore._result_payload(item)
                                     for item in history]
        payload["active_lifecycle_state"] = (
            payload["result_history"][0]["lifecycle_state"] if payload["result_history"]
            else "PROPOSED"
        )
        payload["decision_history"] = EconomicInvestigationStore._decision_history(
            conn, month, limit=20,
        )
        return payload
    finally:
        conn.close()
