import asyncio
import json
from datetime import UTC, datetime

from noema.mission_store import MissionStore
from noema.stripe_economy import (
    StripeEconomyStore,
    _balance_rows,
    _payments,
    _status_counts,
    stripe_economy_overview,
    sync_stripe_economy,
)


def test_stripe_projection_keeps_only_safe_payment_fields_and_known_mission(tmp_path):
    db = tmp_path / "noema.db"
    missions = MissionStore(str(db))
    mission_id = missions.discover(
        trial_id="trial-1", evidence_hash="evidence-1", objective="Test receipt linkage",
        specialist="NOEMA",
    )
    missions.close()
    payments = _payments([{
        "id": "pi_1234567890", "status": "succeeded", "amount_received": 1234,
        "currency": "usd", "created": 1_800_000_000, "livemode": True,
        "latest_charge": "ch_1234567890", "client_secret": "must-not-persist",
        "customer": "cus_private", "metadata": {
            "noema_mission_id": mission_id, "email": "private@example.test",
            "noema_lane": "saas", "noema_product_id": "prod_1234567890",
        },
    }], {mission_id})
    assert len(payments) == 1
    assert payments[0]["mission_id"] == mission_id
    assert payments[0]["attribution"] == {
        "noema_mission_id": mission_id, "noema_lane": "saas",
        "noema_product_id": "prod_1234567890",
    }
    assert "client_secret" not in payments[0]
    assert "customer" not in payments[0]
    assert "email" not in json.dumps(payments[0])


def test_payment_projection_rejects_unknown_mission_and_invalid_money():
    payments = _payments([{
        "id": "pi_1234567890", "status": "succeeded", "amount_received": -2,
        "currency": "USD", "livemode": True, "metadata": {"mission_id": "unknown"},
    }], set())
    assert payments[0]["amount_received_minor"] is None
    assert payments[0]["currency"] is None
    assert payments[0]["mission_id"] is None
    assert payments[0]["attribution"] == {}
    assert _balance_rows([{"currency": "usd", "amount": 10}, {"currency": "USD", "amount": 4}]) == [
        {"currency": "usd", "amount_minor": 10}
    ]


def test_invoice_and_subscription_projection_stores_only_bounded_status_counts():
    counts, returned = _status_counts({"data": [
        {"status": "active", "customer": "cus_private"},
        {"status": "past_due", "customer": "cus_private"},
        {"status": "private status"},
    ]}, "subscriptions")
    assert counts == {"active": 1, "past_due": 1}
    assert returned == 3
    assert "cus_private" not in json.dumps(counts)


def test_stripe_events_are_deduplicated_and_read_only_projection_is_safe(tmp_path):
    path = str(tmp_path / "noema.db")
    store = StripeEconomyStore(path)
    payment = {
        "id": "pi_1234567890", "status": "succeeded", "amount_received_minor": 2500,
        "currency": "usd", "created_at": "2026-01-01T00:00:00+00:00",
        "charge_id": "ch_1234567890", "livemode": True, "mission_id": None,
        "attribution": {},
    }
    args = {
        "status": "connected", "livemode": True,
        "available": [{"currency": "usd", "amount_minor": 0}],
        "pending": [{"currency": "usd", "amount_minor": 0}],
        "payments": [payment], "scan_limit": 100,
        "capabilities": {"available": ["retrieve_balance", "list_payment_intents"],
                          "not_exposed": ["fees", "refunds", "payouts"]},
    }
    assert len(store.record(**args)["new_successes"]) == 1
    assert store.record(**args)["new_successes"] == []
    store.close()
    result = stripe_economy_overview(path)
    assert result["status"] == "connected"
    assert result["successful_usd_minor"] == 2500
    assert result["payments"][0]["payment_intent_id"] == "pi_1234567890"
    assert "fees, refunds, and payouts are not reconciled" in result["accounting_note"]


def test_stripe_api_projection_does_not_create_a_missing_database(tmp_path):
    path = tmp_path / "missing.db"
    assert stripe_economy_overview(str(path))["status"] == "not_observed"
    assert not path.exists()


def test_persisted_success_backfill_is_idempotent_and_keeps_cash_unclassified(tmp_path):
    path = str(tmp_path / "noema.db")
    store = StripeEconomyStore(path)
    store.conn.execute(
        "INSERT INTO stripe_payment_observations(payment_intent_id,charge_id,created_at,status,"
        "amount_received_minor,currency,livemode,mission_id,attribution_json,first_seen_at,last_seen_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("pi_1234567890", "ch_1234567890", "2024-01-19T01:09:54+00:00", "succeeded", 200,
         "usd", 1, None, "{}", "2026-09-29T00:00:00+00:00", "2026-09-29T00:00:00+00:00"),
    )
    store.conn.commit()
    assert store.import_persisted_successes() == 1
    assert store.import_persisted_successes() == 0
    store.close()
    from noema.economic_ledger import EconomicLedger
    ledger = EconomicLedger.read_projection(path, month_utc="2024-01")
    assert ledger["event_count"] == 1
    assert ledger["unclassified_cash_subtotal_usd"] == "2"
    assert ledger["verified_realized_revenue_usd"] is None


def test_stripe_september_coverage_is_partial_when_only_payment_intents_are_available(tmp_path):
    store = StripeEconomyStore(str(tmp_path / "noema.db"))
    store.record(
        status="connected", livemode=True, available=[], pending=[],
        payments=[{
            "id": "pi_1234567890", "status": "succeeded", "amount_received_minor": 200,
            "currency": "usd", "created_at": "2024-01-19T01:09:54+00:00",
            "charge_id": "ch_1234567890", "livemode": True, "mission_id": None, "attribution": {},
        }], scan_limit=100,
        capabilities={"available": ["list_payment_intents"], "not_exposed": ["fees", "refunds", "payouts"]},
    )
    status = store.attest_current_period(now=datetime(2026, 9, 29, 6, tzinfo=UTC))
    assert status["state"] == "PARTIAL"
    assert status["period_successful_payment_intents"] == 0
    store.close()
    from noema.economic_ledger import EconomicLedger
    projection = EconomicLedger.read_projection(str(tmp_path / "noema.db"), month_utc="2026-09")
    stripe = next(row for row in projection["provider_coverage"] if row["provider"] == "stripe")
    assert stripe["applicable_activity_detected"] is False
    assert stripe["canonical_event_count"] == 0
    assert stripe["reconciled_amount_usd"] is None
    assert stripe["unresolved_blockers"]


def test_mission_observation_adds_history_without_changing_status(tmp_path):
    store = MissionStore(str(tmp_path / "noema.db"))
    mission_id = store.discover(
        trial_id="trial-2", evidence_hash="evidence-2", objective="Observe Stripe",
        specialist="NOEMA",
    )
    before = store.conn.execute("SELECT status FROM missions WHERE mission_id=?", (mission_id,)).fetchone()[0]
    store.record_observation(
        mission_id, actor="stripe", event_type="economic_receipt_observed",
        detail="gross only", payload={"amount_received_minor": 2500, "currency": "usd"},
    )
    after = store.conn.execute("SELECT status FROM missions WHERE mission_id=?", (mission_id,)).fetchone()[0]
    event = store.conn.execute(
        "SELECT event_type,status FROM mission_events WHERE mission_id=? ORDER BY id DESC LIMIT 1",
        (mission_id,),
    ).fetchone()
    assert before == after == "discovered"
    assert tuple(event) == ("economic_receipt_observed", "observed")
    store.close()


def test_stripe_sync_queues_when_shared_docker_worker_slot_is_busy(tmp_path, monkeypatch):
    monkeypatch.setenv("NOEMA_MCP_ENABLED", "1")
    monkeypatch.setattr("noema.stripe_economy.try_acquire", lambda _kind: (None, "QUEUED: test slot busy"))
    monkeypatch.setattr(
        "noema.stripe_economy.stdio_client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("gateway must not start")),
    )
    result = asyncio.run(sync_stripe_economy(str(tmp_path / "noema.db"), force=True))
    assert result["status"] == "queued"
    assert result["reason"] == "QUEUED: test slot busy"


def test_hosted_rest_sync_uses_get_only_and_persists_financial_projection(tmp_path, monkeypatch):
    import sqlite3

    import httpx

    from noema import stripe_economy

    calls = []

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *, params=None, headers=None):
            calls.append(("GET", str(url), dict(params or {})))
            assert headers["Authorization"] == "Bearer test-secret"
            if str(url) == "/v1/balance":
                payload = {"livemode": True, "available": [{"currency": "usd", "amount": 500}],
                           "pending": []}
            elif str(url) == "/v1/balance_transactions":
                payload = {"data": [{"id": "txn_1234567890", "type": "payment", "created": 1,
                                      "currency": "usd", "amount": 600, "fee": 100, "net": 500,
                                      "livemode": True}]}
            elif str(url) == "/v1/subscriptions":
                payload = {"data": [{"id": "sub_1234567890", "status": "active",
                                      "customer": "cus_privatecustomer"}]}
            else:
                payload = {"data": []}
            return httpx.Response(
                200, json=payload,
                request=httpx.Request("GET", f"https://api.stripe.com{url}"),
            )

    monkeypatch.setattr(stripe_economy.httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "test-secret")
    path = str(tmp_path / "noema.db")
    result = asyncio.run(stripe_economy.sync_stripe_economy(path, force=True))
    assert result["status"] == "connected", (result, calls)
    assert result["persisted_record_count"] == 2
    assert calls and all(method == "GET" for method, _, _ in calls)
    assert all(params == {"limit": 100} for _, url, params in calls if url != "/v1/balance")
    overview = stripe_economy.stripe_economy_overview(path)
    assert overview["records_by_type"] == {"balance_transactions": 1, "subscriptions": 1}
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT net_minor,fee_minor FROM stripe_read_observations"
        ).fetchone() == (500, 100)
        assert "cus_privatecustomer" not in str(conn.execute(
            "SELECT * FROM stripe_read_observations"
        ).fetchall())
