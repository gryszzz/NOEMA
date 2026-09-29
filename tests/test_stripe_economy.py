import asyncio
import json

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
