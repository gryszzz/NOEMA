"""Read-only Stripe account observations through the owner-managed Docker MCP profile.

The Stripe credential remains inside Docker MCP Toolkit. NOEMA stores only a bounded,
allowlisted projection of account balances and payment-intent records; it never stores
MCP payloads, client secrets, customer data, or arbitrary Stripe metadata.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .mission_store import MissionStore
from .research_session import SessionStore
from .resource_control import try_acquire

_PAYMENT_ID = re.compile(r"^pi_[A-Za-z0-9]{8,128}$")
_CHARGE_ID = re.compile(r"^ch_[A-Za-z0-9]{8,128}$")
_MISSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MISSION_METADATA_KEYS = ("noema_mission_id", "mission_id")
_LANES = {"web3", "prediction", "saas", "apis", "data", "subscriptions",
          "automation", "research", "agent_services", "other"}
_READ_TOOLS = ("retrieve_balance", "list_payment_intents")
_NOT_EXPOSED = ("refunds", "payouts", "balance_transactions", "fees")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _tool_payload(result: Any) -> Any:
    if getattr(result, "isError", False):
        raise RuntimeError("Stripe read-only operation failed")
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, (dict, list)):
        return structured
    for block in getattr(result, "content", ()):
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            continue
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            continue
    raise RuntimeError("Stripe response did not contain structured data")


def _minor(value: Any) -> int | None:
    if type(value) is not int or value < 0 or value > 2**63 - 1:
        return None
    return value


def _balance_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows = []
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        currency = item.get("currency")
        amount = _minor(item.get("amount"))
        if not isinstance(currency, str) or not re.fullmatch(r"[a-z]{3}", currency):
            continue
        if amount is not None:
            rows.append({"currency": currency, "amount_minor": amount})
    return rows


def _metadata_attribution(value: Any) -> tuple[str | None, dict[str, str]]:
    if not isinstance(value, dict):
        return None, {}
    safe = {}
    mission_id = None
    for key in _MISSION_METADATA_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, str) and _MISSION_ID.fullmatch(candidate):
            safe[key] = candidate
            mission_id = candidate
    lane = value.get("noema_lane")
    if isinstance(lane, str) and lane in _LANES:
        safe["noema_lane"] = lane
    product_id = value.get("noema_product_id")
    if isinstance(product_id, str) and re.fullmatch(r"prod_[A-Za-z0-9]{6,120}", product_id):
        safe["noema_product_id"] = product_id
    return mission_id, safe


class StripeEconomyStore:
    """Persistent, deduplicated Stripe observations and their last successful poll."""

    def __init__(self, path: str):
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db, timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS stripe_economy_snapshots (
                id INTEGER PRIMARY KEY,
                observed_at TEXT NOT NULL,
                status TEXT NOT NULL,
                livemode INTEGER,
                available_json TEXT,
                pending_json TEXT,
                payment_intent_count INTEGER,
                succeeded_count INTEGER,
                successful_usd_minor INTEGER,
                subscription_status_counts_json TEXT,
                invoice_status_counts_json TEXT,
                invoice_record_count INTEGER,
                scan_limit INTEGER NOT NULL,
                capabilities_json TEXT NOT NULL,
                error_code TEXT
            );
            CREATE TABLE IF NOT EXISTS stripe_payment_observations (
                payment_intent_id TEXT PRIMARY KEY,
                charge_id TEXT,
                created_at TEXT,
                status TEXT NOT NULL,
                amount_received_minor INTEGER,
                currency TEXT,
                livemode INTEGER,
                mission_id TEXT,
                attribution_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS stripe_payments_created
                ON stripe_payment_observations(created_at DESC);
            CREATE TABLE IF NOT EXISTS stripe_sync_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                next_sync_at TEXT NOT NULL
            );
        """)
        columns = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(stripe_economy_snapshots)"
        )}
        for name, declaration in (
            ("subscription_status_counts_json", "TEXT"),
            ("invoice_status_counts_json", "TEXT"),
            ("invoice_record_count", "INTEGER"),
        ):
            if name not in columns:
                self.conn.execute(f"ALTER TABLE stripe_economy_snapshots ADD COLUMN {name} {declaration}")
        self.conn.commit()

    def due(self) -> bool:
        row = self.conn.execute(
            "SELECT next_sync_at FROM stripe_sync_state WHERE singleton=1"
        ).fetchone()
        if row is None:
            return True
        try:
            next_sync = datetime.fromisoformat(row[0])
        except (ValueError, TypeError):
            return True
        return datetime.now(UTC) >= next_sync

    def record(
        self, *, status: str, livemode: bool | None, available: list[dict[str, Any]] | None,
        pending: list[dict[str, Any]] | None, payments: list[dict[str, Any]], scan_limit: int,
        capabilities: dict[str, Any], subscription_status_counts: dict[str, int] | None = None,
        invoice_status_counts: dict[str, int] | None = None,
        invoice_record_count: int | None = None, error_code: str | None = None,
    ) -> dict[str, Any]:
        observed_at = _now()
        succeeded = [item for item in payments if item["status"] == "succeeded"]
        usd_total = sum(item["amount_received_minor"] or 0 for item in succeeded
                        if item["currency"] == "usd")
        new_successes: list[dict[str, Any]] = []
        with self.conn:
            self.conn.execute(
                "INSERT INTO stripe_economy_snapshots(observed_at,status,livemode,available_json,"
                "pending_json,payment_intent_count,succeeded_count,successful_usd_minor,"
                "subscription_status_counts_json,invoice_status_counts_json,invoice_record_count,"
                "scan_limit,capabilities_json,error_code) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (observed_at, status, None if livemode is None else int(livemode),
                 json.dumps(available, sort_keys=True) if available is not None else None,
                 json.dumps(pending, sort_keys=True) if pending is not None else None,
                 len(payments), len(succeeded), usd_total,
                 json.dumps(subscription_status_counts or {}, sort_keys=True),
                 json.dumps(invoice_status_counts or {}, sort_keys=True), invoice_record_count,
                 scan_limit,
                 json.dumps(capabilities, sort_keys=True), error_code),
            )
            for payment in payments:
                existing = self.conn.execute(
                    "SELECT status FROM stripe_payment_observations WHERE payment_intent_id=?",
                    (payment["id"],),
                ).fetchone()
                if payment["status"] == "succeeded" and (existing is None or existing[0] != "succeeded"):
                    new_successes.append(payment)
                self.conn.execute(
                    "INSERT INTO stripe_payment_observations(payment_intent_id,charge_id,"
                    "created_at,status,amount_received_minor,currency,livemode,mission_id,"
                    "attribution_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(payment_intent_id) DO UPDATE SET charge_id=excluded.charge_id,"
                    "created_at=excluded.created_at,status=excluded.status,"
                    "amount_received_minor=excluded.amount_received_minor,currency=excluded.currency,"
                    "livemode=excluded.livemode,mission_id=COALESCE(stripe_payment_observations.mission_id,"
                    "excluded.mission_id),attribution_json=excluded.attribution_json,"
                    "last_seen_at=excluded.last_seen_at",
                    (payment["id"], payment["charge_id"], payment["created_at"], payment["status"],
                     payment["amount_received_minor"], payment["currency"],
                     None if payment["livemode"] is None else int(payment["livemode"]),
                     payment["mission_id"], json.dumps(payment["attribution"], sort_keys=True),
                     observed_at, observed_at),
                )
            interval = max(60, min(86400, int(os.getenv("NOEMA_STRIPE_SYNC_INTERVAL_SECONDS", "900"))))
            next_sync = datetime.now(UTC).timestamp() + interval
            self.conn.execute(
                "INSERT INTO stripe_sync_state VALUES(1,?) ON CONFLICT(singleton) "
                "DO UPDATE SET next_sync_at=excluded.next_sync_at",
                (datetime.fromtimestamp(next_sync, UTC).isoformat(),),
            )
        return {"payment_count": len(payments), "succeeded_count": len(succeeded),
                "new_successes": new_successes}

    def latest(self, *, limit: int = 12) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM stripe_economy_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return {"status": "not_observed", "capabilities": {"available": [],
                    "not_exposed": list(_NOT_EXPOSED)}, "payments": []}
        payments = self.conn.execute(
            "SELECT payment_intent_id,charge_id,created_at,status,amount_received_minor,currency,"
            "livemode,mission_id,attribution_json,first_seen_at,last_seen_at "
            "FROM stripe_payment_observations ORDER BY COALESCE(created_at,'') DESC LIMIT ?",
            (max(1, min(100, limit)),),
        ).fetchall()
        return {
            "status": row["status"], "observed_at": row["observed_at"],
            "livemode": None if row["livemode"] is None else bool(row["livemode"]),
            "available": json.loads(row["available_json"]) if row["available_json"] else None,
            "pending": json.loads(row["pending_json"]) if row["pending_json"] else None,
            "payment_intent_count": row["payment_intent_count"],
            "succeeded_count": row["succeeded_count"],
            "successful_usd_minor": row["successful_usd_minor"],
            "subscription_status_counts": json.loads(row["subscription_status_counts_json"] or "{}"),
            "invoice_status_counts": json.loads(row["invoice_status_counts_json"] or "{}"),
            "invoice_record_count": row["invoice_record_count"],
            "scan_limit": row["scan_limit"],
            "capabilities": json.loads(row["capabilities_json"]),
            "accounting_note": "Successful PaymentIntents are gross captured amounts; fees, refunds, and payouts are not reconciled.",
            "fees_refunds_payouts": "not_exposed_by_configured_read_only_tools",
            "error_code": row["error_code"],
            "payments": [{
                "payment_intent_id": item["payment_intent_id"], "charge_id": item["charge_id"],
                "created_at": item["created_at"], "status": item["status"],
                "amount_received_minor": item["amount_received_minor"],
                "currency": item["currency"],
                "livemode": None if item["livemode"] is None else bool(item["livemode"]),
                "mission_id": item["mission_id"],
                "attribution": json.loads(item["attribution_json"]),
                "first_seen_at": item["first_seen_at"],
            } for item in payments],
        }

    def close(self) -> None:
        self.conn.close()


def _payments(payload: Any, mission_ids: set[str]) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        rows = payload.get("data", payload.get("payment_intents", []))
    else:
        rows = payload
    if not isinstance(rows, list):
        return []
    result = []
    now = datetime.now(UTC).isoformat()
    for item in rows[:100]:
        if not isinstance(item, dict):
            continue
        payment_id = item.get("id")
        status = item.get("status")
        if not isinstance(payment_id, str) or not _PAYMENT_ID.fullmatch(payment_id):
            continue
        if status not in {"succeeded", "processing", "requires_payment_method", "canceled"}:
            continue
        currency = item.get("currency")
        created = item.get("created")
        if type(created) is int and 0 <= created <= 2**63 - 1:
            created_at = datetime.fromtimestamp(created, UTC).isoformat()
        else:
            created_at = None
        mission_id, attribution = _metadata_attribution(item.get("metadata"))
        if mission_id not in mission_ids:
            mission_id = None
            attribution.pop("mission_id", None)
            attribution.pop("noema_mission_id", None)
        if isinstance(currency, str) and not re.fullmatch(r"[a-z]{3}", currency):
            currency = None
        latest_charge = item.get("latest_charge")
        charge_id = latest_charge if isinstance(latest_charge, str) and _CHARGE_ID.fullmatch(latest_charge) else None
        livemode = item.get("livemode") if type(item.get("livemode")) is bool else None
        result.append({
            "id": payment_id, "status": status,
            "amount_received_minor": _minor(item.get("amount_received")),
            "currency": currency, "created_at": created_at, "charge_id": charge_id,
            "livemode": livemode, "mission_id": mission_id, "attribution": attribution,
            "observed_at": now,
        })
    return result


def _status_counts(payload: Any, collection: str) -> tuple[dict[str, int], int]:
    if isinstance(payload, dict):
        rows = payload.get("data", payload.get(collection, []))
    else:
        rows = payload
    if not isinstance(rows, list):
        return {}, 0
    counts: dict[str, int] = {}
    for item in rows[:100]:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if isinstance(status, str) and re.fullmatch(r"[a-z_]{1,40}", status):
            counts[status] = counts.get(status, 0) + 1
    return counts, min(len(rows), 100)


async def sync_stripe_economy(path: str, *, force: bool = False) -> dict[str, Any]:
    """Fetch only the configured profile's explicitly allowlisted read-only tools."""
    if os.getenv("NOEMA_MCP_ENABLED", "0") != "1":
        return {"status": "disabled", "reason": "Docker MCP is disabled"}
    profile = os.getenv("NOEMA_MCP_PROFILE", "noema")
    if not profile or len(profile) > 64 or not all(c.isalnum() or c in "-_" for c in profile):
        return {"status": "unavailable", "reason": "invalid MCP profile configuration"}
    store = StripeEconomyStore(path)
    resource_lease = None
    try:
        if not force and not store.due():
            return {"status": "cached", "reason": "read-only account sync interval has not elapsed"}
        resource_lease, resource_reason = try_acquire("docker_worker")
        if resource_lease is None:
            return {"status": "queued", "reason": resource_reason or "Docker worker slot unavailable"}
        missions = MissionStore(path)
        try:
            mission_ids = {row[0] for row in missions.conn.execute("SELECT mission_id FROM missions")}
        finally:
            missions.close()

        params = StdioServerParameters(
            command="docker", args=["mcp", "gateway", "run", f"--profile={profile}",
                                    "--log-calls=false"],
            env={"PATH": os.getenv("PATH", "")},
        )
        started = time.monotonic()
        async with asyncio.timeout(35):
            with open(os.devnull, "w") as err:  # noqa: ASYNC230 -- /dev/null cannot block.
                async with stdio_client(params, errlog=err) as (read, write):  # noqa: SIM117
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        available_tools = {tool.name for tool in (await client.list_tools()).tools}
                        stripe_tools = {name for name in available_tools if name in {
                            "retrieve_balance", "list_payment_intents", "list_invoices",
                            "list_subscriptions", "list_charges", "list_refunds", "list_payouts",
                            "list_balance_transactions",
                        }}
                        capabilities = {
                            "available": sorted(stripe_tools),
                            "not_exposed": [name for name, tool in (
                                ("refunds", "list_refunds"), ("payouts", "list_payouts"),
                                ("balance_transactions", "list_balance_transactions"),
                                ("fees", "list_balance_transactions"),
                            ) if tool not in stripe_tools],
                        }
                        required = set(_READ_TOOLS)
                        if not required <= stripe_tools:
                            store.record(
                                status="unsupported", livemode=None, available=None, pending=None,
                                payments=[], scan_limit=100, capabilities=capabilities,
                                error_code="required_read_tools_unavailable",
                            )
                            return {"status": "unsupported", "reason": "required Stripe read tools unavailable",
                                    "elapsed_seconds": time.monotonic() - started}
                        balance_result = await client.call_tool("retrieve_balance", {})
                        balance = _tool_payload(balance_result)
                        payment_result = await client.call_tool("list_payment_intents", {"limit": 100})
                        payment_payload = _tool_payload(payment_result)
                        payments = _payments(payment_payload, mission_ids)
                        subscription_counts: dict[str, int] = {}
                        invoice_counts: dict[str, int] = {}
                        invoice_count = None
                        if "list_subscriptions" in stripe_tools:
                            subscription_payload = _tool_payload(await client.call_tool(
                                "list_subscriptions", {"limit": 100},
                            ))
                            subscription_counts, _subscription_count = _status_counts(
                                subscription_payload, "subscriptions",
                            )
                        if "list_invoices" in stripe_tools:
                            invoice_payload = _tool_payload(await client.call_tool(
                                "list_invoices", {"limit": 100},
                            ))
                            invoice_counts, invoice_count = _status_counts(invoice_payload, "invoices")
                        is_live = balance.get("livemode") if isinstance(balance, dict) else None
                        if type(is_live) is not bool:
                            is_live = None
                        recorded = store.record(
                            status="connected", livemode=is_live,
                            available=_balance_rows(balance.get("available")) if isinstance(balance, dict) else None,
                            pending=_balance_rows(balance.get("pending")) if isinstance(balance, dict) else None,
                            payments=payments, scan_limit=100, capabilities=capabilities,
                            subscription_status_counts=subscription_counts,
                            invoice_status_counts=invoice_counts,
                            invoice_record_count=invoice_count,
                        )
        new_successes = recorded["new_successes"]
        if new_successes:
            sessions = SessionStore(path)
            try:
                session_id = sessions.begin_service_session(
                    f"Reconcile {len(new_successes)} newly observed Stripe payment event(s)",
                    provider="stripe", model="Docker MCP read-only",
                )
                gross_usd_minor = sum(
                    (item["amount_received_minor"] or 0) for item in new_successes
                    if item["currency"] == "usd"
                )
                detail = (f"Observed {len(new_successes)} new successful PaymentIntent(s); "
                          f"gross USD captured={gross_usd_minor / 100:.2f}; fees/refunds/payouts unknown")
                sessions.event(session_id, "reconcile", "recorded", detail,
                               tool="list_payment_intents", elapsed=time.monotonic() - started)
                sessions.finish(session_id, "completed", {
                    "status": "stripe_payment_observed",
                    "new_successful_payment_count": len(new_successes),
                    "gross_usd_captured_minor": gross_usd_minor,
                    "fees_refunds_payouts": "not_exposed_by_configured_read_only_tools",
                })
            finally:
                sessions.conn.close()
            missions = MissionStore(path)
            try:
                for payment in new_successes:
                    if payment["mission_id"]:
                        missions.record_observation(
                            payment["mission_id"], actor="stripe",
                            event_type="economic_receipt_observed",
                            detail="Successful Stripe payment observed; gross only, net costs unknown",
                            payload={"provider": "stripe", "payment_intent_id": payment["id"],
                                     "charge_id": payment["charge_id"],
                                     "amount_received_minor": payment["amount_received_minor"],
                                     "currency": payment["currency"], "livemode": payment["livemode"]},
                        )
            finally:
                missions.close()
        return {"status": "connected", "payment_intent_count": len(payments),
                "succeeded_count": sum(item["status"] == "succeeded" for item in payments),
                "new_successful_payment_count": len(new_successes),
                "elapsed_seconds": time.monotonic() - started}
    except (TimeoutError, OSError, RuntimeError, ValueError, TypeError, sqlite3.Error):
        try:
            store.record(status="unavailable", livemode=None, available=None, pending=None,
                         payments=[], scan_limit=100,
                         capabilities={"available": [], "not_exposed": list(_NOT_EXPOSED)},
                         error_code="read_only_sync_failed")
        except sqlite3.Error:
            pass
        return {"status": "unavailable", "reason": "read-only Stripe sync failed"}
    finally:
        if resource_lease is not None:
            resource_lease.release()
        store.close()


def stripe_economy_overview(path: str) -> dict[str, Any]:
    if not Path(path).exists():
        return {"status": "not_observed", "payments": [], "available": None,
                "pending": None, "capabilities": {"available": [], "not_exposed": list(_NOT_EXPOSED)}}
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        conn.row_factory = sqlite3.Row
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "stripe_economy_snapshots" not in tables or "stripe_payment_observations" not in tables:
            return {"status": "not_observed", "payments": [], "available": None,
                    "pending": None, "capabilities": {"available": [],
                    "not_exposed": list(_NOT_EXPOSED)}}
        row = conn.execute(
            "SELECT * FROM stripe_economy_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return {"status": "not_observed", "payments": [], "available": None,
                    "pending": None, "capabilities": {"available": [],
                    "not_exposed": list(_NOT_EXPOSED)}}
        payments = conn.execute(
            "SELECT payment_intent_id,charge_id,created_at,status,amount_received_minor,currency,"
            "livemode,mission_id,attribution_json,first_seen_at FROM stripe_payment_observations "
            "ORDER BY COALESCE(created_at,'') DESC LIMIT 12"
        ).fetchall()
        history_rows = conn.execute(
            "SELECT observed_at,available_json,pending_json FROM stripe_economy_snapshots "
            "WHERE status='connected' ORDER BY id DESC LIMIT 30"
        ).fetchall()
        history = []
        for item in reversed(history_rows):
            balances = []
            for field in ("available_json", "pending_json"):
                try:
                    balances.extend(json.loads(item[field]) or [])
                except (ValueError, TypeError):
                    continue
            usd_minor = sum(
                row["amount_minor"] for row in balances
                if isinstance(row, dict) and row.get("currency") == "usd"
                and type(row.get("amount_minor")) is int and row["amount_minor"] >= 0
            )
            history.append({"observed_at": item["observed_at"], "usd_balance_minor": usd_minor})
        return {
            "status": row["status"], "observed_at": row["observed_at"],
            "livemode": None if row["livemode"] is None else bool(row["livemode"]),
            "available": json.loads(row["available_json"]) if row["available_json"] else None,
            "pending": json.loads(row["pending_json"]) if row["pending_json"] else None,
            "payment_intent_count": row["payment_intent_count"],
            "succeeded_count": row["succeeded_count"],
            "successful_usd_minor": row["successful_usd_minor"],
            "subscription_status_counts": json.loads(row["subscription_status_counts_json"] or "{}"),
            "invoice_status_counts": json.loads(row["invoice_status_counts_json"] or "{}"),
            "invoice_record_count": row["invoice_record_count"],
            "scan_limit": row["scan_limit"],
            "capabilities": json.loads(row["capabilities_json"]),
            "accounting_note": "Successful PaymentIntents are gross captured amounts; fees, refunds, and payouts are not reconciled.",
            "fees_refunds_payouts": "not_exposed_by_configured_read_only_tools",
            "error_code": row["error_code"],
            "balance_history": history,
            "payments": [{
                "payment_intent_id": item["payment_intent_id"], "charge_id": item["charge_id"],
                "created_at": item["created_at"], "status": item["status"],
                "amount_received_minor": item["amount_received_minor"],
                "currency": item["currency"],
                "livemode": None if item["livemode"] is None else bool(item["livemode"]),
                "mission_id": item["mission_id"],
                "attribution": json.loads(item["attribution_json"]),
                "first_seen_at": item["first_seen_at"],
            } for item in payments],
        }
    finally:
        conn.close()
