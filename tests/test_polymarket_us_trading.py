from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from noema.config import PolymarketUSTradingConfig
from noema.execution_gateway import (
    ExecutionGateway,
    ExecutionProposal,
    PredictionExecutionAuthority,
)
from noema.models import Forecast, MarketSnapshot, Mode, Opportunity
from noema.risk import RiskEngine, RiskPolicy
from noema.venues.errors import VenueRejected, VenueUnavailable
from noema.venues.polymarket_us import PolymarketUSVenue
from noema.venues.polymarket_us_trading import (
    PolymarketUSTradingVenue,
    run_pm_account,
    run_pm_order,
)
from noema.wallet_credentials import (
    load_polymarket_us_credentials_in_api_boundary,
    polymarket_us_credentials_present,
)

SECRET = "fixture-secret-do-not-log"


class APITimeoutError(Exception):
    pass


class BadRequestError(Exception):
    status_code = 400

    def __init__(self) -> None:
        super().__init__(f"rejected near {SECRET} and X-PM-Signature")


class FakeOrders:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.cancelled: list[tuple] = []
        self.fail_create_with: Exception | None = None

    def list(self):
        return {"orders": []}

    def create(self, body):
        self.created.append(body)
        if self.fail_create_with is not None:
            raise self.fail_create_with
        return {"id": "pm-order-1"}

    def cancel(self, order_id, body):
        self.cancelled.append((order_id, body))
        return {"id": order_id}

    def retrieve(self, order_id):
        return {"order": {
            "id": order_id, "marketSlug": "test-market", "state": "ORDER_STATE_FILLED",
            "cumQuantity": 2, "avgPx": {"value": "0.50", "currency": "USD"},
            "commissionNotionalTotalCollected": {"value": "0", "currency": "USD"},
            "intent": "ORDER_INTENT_BUY_LONG",
        }}

    def preview(self, _body):
        return {"order": {"commissionsBasisPoints": "0", "makerCommissionsBasisPoints": "0"}}


class FakeMarkets:
    def book(self, slug):
        return {"marketData": {
            "marketSlug": slug,
            "bids": [{"px": {"value": "0.40"}, "qty": "5"}],
            "offers": [{"px": {"value": "0.44"}, "qty": "4"}],
        }}


class FakeAccount:
    def balances(self):
        return {"balances": [{
            "currency": "USD", "buyingPower": "25.00", "currentBalance": "30.00",
        }]}


class FakePortfolio:
    def positions(self, _params):
        return {"positions": [{
            "marketSlug": "test-market", "netPositionDecimal": "1",
            "marketMetadata": {"slug": "test-market", "outcome": "Yes"},
        }]}

    def activities(self, _params):
        return {"activities": [{
            "type": "ACTIVITY_TYPE_TRADE",
            "trade": {
                "id": "trade-1", "marketSlug": "test-market", "qtyDecimal": "2",
                "price": {"value": "0.50", "currency": "USD"},
                "createTime": "2026-10-21T00:00:00Z", "state": "TRADE_STATE_CLEARED",
            },
        }]}


class FakeClient:
    def __init__(self) -> None:
        self.orders = FakeOrders()
        self.markets = FakeMarkets()
        self.account = FakeAccount()
        self.portfolio = FakePortfolio()

    def close(self) -> None:
        return None


def _armed(**overrides) -> PolymarketUSTradingConfig:
    values = {
        "key_id_present": True, "secret_present": True, "secret_source": "environment",
        "allow_live_orders": True, "master_halt": False,
    }
    values.update(overrides)
    return PolymarketUSTradingConfig(**values)


def _venue(tmp_path, client=None, **overrides) -> PolymarketUSTradingVenue:
    return PolymarketUSTradingVenue(
        _armed(**overrides), client if client is not None else FakeClient(),
        idempotency_path=str(tmp_path / "client-orders.sqlite3"),
    )


def _arm_env(monkeypatch) -> None:
    monkeypatch.setenv("NOEMA_ALLOW_LIVE_ORDERS", "1")
    monkeypatch.setenv("NOEMA_MASTER_HALT", "0")
    monkeypatch.setenv("NOEMA_EXECUTION_GATEWAY_ENABLED", "1")
    monkeypatch.setenv("NOEMA_LIVE_VENUES", "polymarket_us")
    monkeypatch.setenv("NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD", "5")
    monkeypatch.setenv("POLYMARKET_US_KEY_ID", "fixture-key")
    monkeypatch.setenv("POLYMARKET_US_SECRET_KEY", SECRET)


async def _prepare(venue: PolymarketUSTradingVenue) -> None:
    await venue.prepare_live_execution("test-market")


def test_read_only_adapter_stays_disabled():
    assert PolymarketUSVenue(client=object()).supports_live_execution is False


def test_live_execution_requires_keys_allow_flag_and_inactive_halt(monkeypatch):
    for name in (
        "NOEMA_ALLOW_LIVE_ORDERS", "NOEMA_MASTER_HALT",
        "POLYMARKET_US_KEY_ID", "POLYMARKET_US_SECRET_KEY", "POLYMARKET_US_SECRET_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    assert PolymarketUSTradingVenue().supports_live_execution is False

    monkeypatch.setenv("NOEMA_ALLOW_LIVE_ORDERS", "1")
    assert PolymarketUSTradingConfig.from_env().allow_live_orders is True
    assert PolymarketUSTradingVenue().supports_live_execution is False

    monkeypatch.setenv("POLYMARKET_US_KEY_ID", "fixture-key")
    monkeypatch.setenv("POLYMARKET_US_SECRET_KEY", SECRET)
    assert PolymarketUSTradingVenue().supports_live_execution is True
    assert SECRET not in repr(PolymarketUSTradingConfig.from_env())

    monkeypatch.setenv("NOEMA_MASTER_HALT", "1")
    halted = PolymarketUSTradingVenue()
    assert halted.supports_live_execution is False
    assert halted.config.master_halt is True


def test_secret_file_overrides_environment_and_missing_file_does_not_fall_back(
    monkeypatch, tmp_path,
):
    secret_path = tmp_path / "polymarket-us.secret"
    secret_path.write_text("file-secret-value\n", encoding="utf-8")
    monkeypatch.delenv("POLYMARKET_US_SECRET_FILE", raising=False)
    monkeypatch.setenv("POLYMARKET_US_KEY_ID", "fixture-key")
    monkeypatch.setenv("POLYMARKET_US_SECRET_KEY", "env-secret-value")
    monkeypatch.setenv("POLYMARKET_US_SECRET_FILE", str(secret_path))

    assert polymarket_us_credentials_present() == (True, True)
    assert load_polymarket_us_credentials_in_api_boundary() == ("fixture-key", "file-secret-value")
    assert "file-secret-value" not in repr(PolymarketUSTradingConfig.from_env())

    monkeypatch.setenv("POLYMARKET_US_SECRET_FILE", str(tmp_path / "missing.secret"))
    assert polymarket_us_credentials_present() == (True, False)
    with pytest.raises(RuntimeError) as caught:
        load_polymarket_us_credentials_in_api_boundary()
    assert "env-secret-value" not in str(caught.value)
    assert "file-secret-value" not in str(caught.value)


def test_sdk_client_receives_secret_file_without_logging(monkeypatch, tmp_path, caplog):
    secret_path = tmp_path / "polymarket-us.secret"
    secret_path.write_text("file-secret-value\n", encoding="utf-8")
    monkeypatch.setenv("POLYMARKET_US_KEY_ID", "fixture-key")
    monkeypatch.setenv("POLYMARKET_US_SECRET_FILE", str(secret_path))
    monkeypatch.delenv("POLYMARKET_US_SECRET_KEY", raising=False)
    captured: dict[str, str] = {}

    class FakeSDK:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def close(self) -> None:
            return None

    monkeypatch.setitem(__import__("sys").modules, "polymarket_us", SimpleNamespace(PolymarketUS=FakeSDK))
    caplog.set_level(logging.DEBUG)
    venue = PolymarketUSTradingVenue(PolymarketUSTradingConfig.from_env())
    assert venue.client is not None
    assert captured["key_id"] == "fixture-key"
    assert captured["secret_key"] == "file-secret-value"
    assert captured["max_retries"] == 0
    assert "file-secret-value" not in caplog.text
    assert "file-secret-value" not in repr(venue)


@pytest.mark.asyncio
async def test_place_limit_maps_yes_buy_and_keeps_client_id_local(tmp_path, monkeypatch):
    _arm_env(monkeypatch)
    client = FakeClient()
    venue = _venue(tmp_path, client)
    await _prepare(venue)
    order_id = await venue.execute(
        _action(), client_order_id="11111111-1111-4111-8111-111111111111",
    )
    assert order_id == "pm-order-1"
    body = client.orders.created[0]
    assert body["marketSlug"] == "test-market"
    assert body["intent"] == "ORDER_INTENT_BUY_LONG"
    assert body["type"] == "ORDER_TYPE_LIMIT"
    assert body["price"] == {"value": "0.5100", "currency": "USD"}
    assert body["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
    assert body["manualOrderIndicator"] == "MANUAL_ORDER_INDICATOR_AUTOMATIC"
    assert "client_order_id" not in body
    assert "clordId" not in body
    assert Decimal(str(body["quantity"])) == Decimal("9.8039")
    replay = await venue.execute(
        _action(), client_order_id="11111111-1111-4111-8111-111111111111",
    )
    assert replay == "pm-order-1"
    assert len(client.orders.created) == 1


@pytest.mark.asyncio
async def test_unknown_dispatch_is_not_resubmitted(tmp_path, monkeypatch):
    _arm_env(monkeypatch)
    client = FakeClient()
    client.orders.fail_create_with = APITimeoutError("timed out")
    venue = _venue(tmp_path, client)
    await _prepare(venue)
    with pytest.raises(VenueUnavailable) as first:
        await venue.place_limit(
            "test-market", "buy_yes", Decimal("0.50"), Decimal(2), "gtc", "retry-id",
        )
    assert first.value.code == "venue_unavailable"
    with pytest.raises(VenueUnavailable) as second:
        await venue.place_limit(
            "test-market", "buy_yes", Decimal("0.50"), Decimal(2), "gtc", "retry-id",
        )
    assert second.value.code == "client_order_outcome_unknown"
    assert len(client.orders.created) == 1


@pytest.mark.asyncio
async def test_rejection_cancel_and_errors_omit_secrets(tmp_path, monkeypatch, caplog):
    _arm_env(monkeypatch)
    caplog.set_level(logging.DEBUG)
    client = FakeClient()
    client.orders.fail_create_with = BadRequestError()
    venue = _venue(tmp_path, client)
    await _prepare(venue)
    with pytest.raises(VenueRejected) as rejected:
        await venue.place_limit(
            "test-market", "buy_yes", Decimal("0.50"), Decimal(2), "gtc", "reject-id",
        )
    assert rejected.value.code == "venue_rejected_400"
    assert SECRET not in str(rejected.value)
    assert SECRET not in caplog.text
    cancelled = await venue.cancel("pm-order-1", market_slug="test-market")
    assert cancelled == {"order_id": "pm-order-1", "market_slug": "test-market"}
    assert client.orders.cancelled == [("pm-order-1", {"marketSlug": "test-market"})]
    monkeypatch.setenv("NOEMA_MASTER_HALT", "1")
    fresh = FakeClient()
    halted = _venue(tmp_path, fresh)
    await _prepare(halted)
    with pytest.raises(VenueRejected) as halted_order:
        await halted.place_limit(
            "test-market", "buy_yes", Decimal("0.50"), Decimal(2), "gtc", "halt-id",
        )
    assert halted_order.value.code == "master_halt"
    assert fresh.orders.created == []


@pytest.mark.asyncio
async def test_account_reads_and_reconcile_use_venue_order_facts(tmp_path, monkeypatch):
    _arm_env(monkeypatch)
    client = FakeClient()
    venue = _venue(tmp_path, client)
    report = await run_pm_account(venue)
    assert report["balances"]["buying_power_usd"] == "25.00"
    assert report["positions"][0]["market_slug"] == "test-market"
    assert report["open_orders"] == []
    fills = await venue.fills(market_slug="test-market")
    assert fills[0]["fill_id"] == "trade-1"
    await _prepare(venue)
    client_order_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "noema:proposal-9"))
    await venue.place_limit(
        "test-market", "buy_yes", Decimal("0.50"), Decimal(2), "gtc", client_order_id,
    )
    observation = await venue.reconcile_execution({
        "proposal_id": "proposal-9", "venue": "polymarket_us", "instrument": "test-market",
        "notional_usd": "5", "request_json": '{"client_order_id": "' + client_order_id + '"}',
    })
    assert observation is not None
    assert observation.provider_reference == "pm-order-1"
    assert observation.status == "filled"
    assert observation.filled_quantity == Decimal(2)
    assert observation.remaining_reserved_usd == Decimal(0)
    assert SECRET not in str(observation)


@pytest.mark.asyncio
async def test_private_stream_reconnects_with_backoff(tmp_path, monkeypatch):
    _arm_env(monkeypatch)
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(
        "noema.venues.polymarket_us_trading.asyncio.sleep", fake_sleep,
    )

    class Socket:
        def __init__(self, factory) -> None:
            self.factory = factory
            self.handlers: dict = {}
            self.is_connected = False

        def on(self, name, callback):
            self.handlers[name] = callback

        async def connect(self):
            if self.factory.attempts == 1:
                raise ConnectionError("socket down")
            self.is_connected = True
            self.handlers["order_snapshot"]({"orderSubscriptionSnapshot": {"orders": [{"id": "o1"}]}})
            self.handlers["order_update"]({"orderSubscriptionUpdate": {"execution": {
                "tradeId": "t1", "type": "EXECUTION_TYPE_FILL",
            }}})
            self.handlers["account_balance_update"]({"accountBalancesUpdate": {"balanceChange": {}}})

        async def subscribe_account_balance(self, _name):
            return None

        async def subscribe_positions(self, _name):
            return None

        async def subscribe_orders(self, _name):
            return None

        async def subscribe(self, *_args):
            return None

        async def close(self):
            self.is_connected = False

    class Factory:
        def __init__(self) -> None:
            self.attempts = 0

        def private(self):
            self.attempts += 1
            return Socket(self)

    factory = Factory()
    venue = _venue(tmp_path, SimpleNamespace(ws=factory, close=lambda: None))
    stream = venue.stream_private()
    first, second, third = [await anext(stream) for _ in range(3)]
    await stream.aclose()
    assert [first.type, second.type, third.type] == ["order", "fill", "balance"]
    assert factory.attempts == 2
    assert sleeps == [1.0]


@pytest.mark.asyncio
async def test_gateway_routes_polymarket_us_and_records_rejection_without_secrets(
    tmp_path, monkeypatch,
):
    _arm_env(monkeypatch)
    proposal, opportunity, action, risk = _proposal()
    authority = _authority()
    client = FakeClient()
    venue = _venue(tmp_path, client)
    await _prepare(venue)
    gateway = ExecutionGateway(
        str(tmp_path / "gateway.db"), authority_resolver=lambda _mission: authority,
    )
    result = await gateway.submit_prediction_order(
        proposal=proposal, opportunity=opportunity, action=action,
        risk_engine=risk, venue_adapter=venue, bankroll_usd=500,
    )
    assert result.allowed is True
    assert result.provider_reference == "pm-order-1"
    assert result.status == "submitted"
    assert len(client.orders.created) == 1
    stored_client_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "noema:" + proposal.proposal_id))
    assert client.orders.created[0].get("clientOrderId") is None
    with sqlite3.connect(gateway.db_path) as conn:
        status, reference, payload = conn.execute(
            "SELECT status,provider_reference,request_json FROM execution_gateway_requests"
        ).fetchone()
    assert status == "submitted"
    assert reference == "pm-order-1"
    assert stored_client_id in payload
    assert SECRET not in payload

    blocked, blocked_opportunity, blocked_action, blocked_risk = _proposal(proposal_id="proposal-2")
    monkeypatch.setenv("NOEMA_LIVE_VENUES", "polymarket")
    refused = await gateway.submit_prediction_order(
        proposal=blocked, opportunity=blocked_opportunity, action=blocked_action,
        risk_engine=blocked_risk, venue_adapter=venue, bankroll_usd=500,
    )
    assert refused.allowed is False
    assert "unrecognized Polymarket venue" in " ".join(refused.reasons)
    assert len(client.orders.created) == 1


@pytest.mark.asyncio
async def test_gateway_records_known_rejection_and_releases_exposure(tmp_path, monkeypatch):
    _arm_env(monkeypatch)
    proposal, opportunity, action, risk = _proposal()
    client = FakeClient()
    client.orders.fail_create_with = BadRequestError()
    venue = _venue(tmp_path, client)
    await _prepare(venue)
    gateway = ExecutionGateway(
        str(tmp_path / "gateway.db"), authority_resolver=lambda _mission: _authority(),
    )
    result = await gateway.submit_prediction_order(
        proposal=proposal, opportunity=opportunity, action=action,
        risk_engine=risk, venue_adapter=venue, bankroll_usd=500,
    )
    assert result.status == "rejected"
    assert result.reasons == ("venue_rejected_400",)
    assert gateway._daily_exposure("other") == Decimal(0)
    raw = (tmp_path / "gateway.db").read_bytes()
    assert SECRET.encode() not in raw


@pytest.mark.asyncio
async def test_pm_order_dry_run_records_proposal_and_live_requires_gates(tmp_path, monkeypatch):
    for name in (
        "NOEMA_ALLOW_LIVE_ORDERS", "NOEMA_EXECUTION_GATEWAY_ENABLED", "NOEMA_LIVE_VENUES",
        "NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NOEMA_MASTER_HALT", "0")
    exploding = FakeClient()

    class ExplodingVenue(PolymarketUSTradingVenue):
        async def place_limit(self, *args, **kwargs):
            raise AssertionError("dry-run must not place")

        async def prepare_live_execution(self, slug):
            raise AssertionError("dry-run must not preview")

    dry = await run_pm_order(
        slug="test-market", side="buy_yes", price=Decimal("0.50"), quantity=Decimal(2),
        db_path=str(tmp_path / "manual.db"), venue=ExplodingVenue(_armed(), exploding),
    )
    assert dry["status"] == "proposed"
    assert dry["live"] is False
    with sqlite3.connect(tmp_path / "manual.db") as conn:
        row = conn.execute(
            "SELECT status,tier FROM execution_gateway_requests"
        ).fetchone()
    assert row == ("proposed", "proposal")

    refused = await run_pm_order(
        slug="test-market", side="buy_yes", price=Decimal("0.50"), quantity=Decimal(2),
        live=True, db_path=str(tmp_path / "manual.db"),
        venue=ExplodingVenue(_armed(), exploding),
    )
    assert refused["status"] == "rejected"
    assert exploding.orders.created == []

    _arm_env(monkeypatch)
    client = FakeClient()
    venue = _venue(tmp_path, client)
    placed = await run_pm_order(
        slug="test-market", side="buy_yes", price=Decimal("0.50"), quantity=Decimal(2),
        live=True, client_order_id="manual-order-1", db_path=str(tmp_path / "live.db"),
        venue=venue,
    )
    assert placed["status"] == "cancelled"
    assert placed["order_id"] == "pm-order-1"
    assert client.orders.created[0]["manualOrderIndicator"] == "MANUAL_ORDER_INDICATOR_MANUAL"
    assert client.orders.cancelled[0][1] == {"marketSlug": "test-market"}
    raw = (tmp_path / "live.db").read_bytes()
    assert SECRET.encode() not in raw


def _action():
    return _proposal()[2]


def _proposal(proposal_id="proposal-1"):
    now = datetime.now(UTC)
    snapshot = MarketSnapshot(
        venue="polymarket_us", market_id="test-market", title="Test market",
        yes_bid=.50, yes_ask=.51, no_bid=.49, no_ask=.50, liquidity_usd=5000,
        closes_at=None, resolution_rules="Official rules", captured_at=now,
    )
    forecast = Forecast(
        market_id=snapshot.market_id, venue=snapshot.venue,
        probability_yes=.90, lower_bound=.88, upper_bound=.92,
        model_version="test", evidence_ids=("evidence-1",), created_at=now,
    )
    opportunity = Opportunity(
        forecast=forecast, snapshot=snapshot, market_probability=.51,
        raw_edge=.39, estimated_cost=.01, uncertainty_penalty=.02,
        robust_edge=.36,
    )
    risk = RiskEngine(RiskPolicy(
        mode=Mode.LIVE, min_robust_edge=.01, min_liquidity_usd=1,
        max_spread=.1, max_stake_usd=10, max_fraction_of_bankroll=.01,
        max_forecast_width=.1, max_market_data_age_seconds=30,
    ))
    action = risk.decide(opportunity, bankroll_usd=500)
    proposal = ExecutionProposal.from_opportunity(
        proposal_id=proposal_id, mission_id="mission-1", opportunity=opportunity,
        action=action, evidence_refs=forecast.evidence_ids,
        expires_at=now + timedelta(seconds=20),
    )
    return proposal, opportunity, action, risk


def _authority():
    return PredictionExecutionAuthority(
        authority_id="owner-limit-1", mission_id="mission-1",
        allowed_venues=frozenset({"polymarket_us"}),
        allowed_actions=frozenset({"buy_yes"}),
        allowed_instruments=frozenset({"test-market"}),
        per_action_limit_usd=Decimal(5), per_mission_limit_usd=Decimal(5),
        daily_limit_usd=Decimal(5), maximum_exposure_usd=Decimal(5),
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
