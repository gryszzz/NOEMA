import base64
from decimal import Decimal

from noema.config import KalshiConfig
from noema.venues.kalshi import KalshiCredentialError, KalshiSigner, KalshiVenue


def test_demo_is_default_environment() -> None:
    config = KalshiConfig()
    assert config.environment == "demo"
    assert "demo.kalshi.co" in config.base_url
    assert config.allow_live_orders is False


def test_environment_key_id_strips_copy_paste_whitespace(monkeypatch):
    monkeypatch.setenv("KALSHI_API_KEY_ID", "  fixture-id\n")
    config = KalshiConfig.from_env()
    assert config.key_id == "fixture-id"


def test_signer_rejects_key_id_control_characters_without_echoing_them():
    try:
        KalshiSigner("fixture-id\nsecret-suffix", private_key_pem=b"unused")
    except KalshiCredentialError as error:
        assert error.code == "api_key_id_invalid_header_value"
        assert "fixture-id" not in str(error)
        assert "secret-suffix" not in str(error)
    else:
        raise AssertionError("invalid HTTP header characters must fail closed")


def test_environment_uses_keychain_id_and_standard_owner_only_pem(monkeypatch, tmp_path) -> None:
    from pathlib import Path

    import noema.config as config_module

    pem = tmp_path / "kalshi.pem"
    pem.write_text("not parsed here")
    monkeypatch.setenv("NOEMA_KALSHI_ENV", "production")
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.setattr(config_module, "kalshi_key_id_present", lambda: True)
    monkeypatch.setattr(config_module, "load_kalshi_key_id_in_api_boundary", lambda: "fixture-id")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".config/noema/credentials").mkdir(parents=True)
    target = tmp_path / ".config/noema/credentials/kalshi.pem"
    target.write_text("fixture")
    assert config_module.KalshiConfig.from_env().key_id == "fixture-id"
    assert config_module.KalshiConfig.from_env().private_key_path == str(target)


def test_unreadable_keychain_keeps_public_config_available(monkeypatch):
    import noema.config as config_module

    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.setattr(config_module, "kalshi_key_id_present", lambda: True)

    def locked_keychain():
        raise RuntimeError("credential unavailable")

    monkeypatch.setattr(config_module, "load_kalshi_key_id_in_api_boundary", locked_keychain)
    config = KalshiConfig.from_env()
    assert config.key_id is None
    assert config.environment == "demo"


def test_hosted_kalshi_pem_secret_is_loaded_in_memory_and_redacted(monkeypatch, tmp_path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    encoded = base64.b64encode(pem).decode("ascii")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "hosted-fixture-id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PEM_B64", encoded)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    config = KalshiConfig.from_env()
    assert config.private_key_path is None
    assert config.private_key_pem is None
    assert config.private_key_pem_b64 == encoded
    assert config.private_key_source == "environment"
    assert "hosted-fixture-id" not in repr(config)
    assert encoded not in repr(config)
    assert pem.decode("ascii") not in repr(config)
    headers = KalshiSigner(
        config.key_id, config.private_key_path,
        private_key_pem_b64=config.private_key_pem_b64,
    ).headers("GET", "/trade-api/v2/portfolio/balance")
    assert headers["KALSHI-ACCESS-KEY"] == "hosted-fixture-id"
    assert headers["KALSHI-ACCESS-SIGNATURE"]


def test_malformed_hosted_kalshi_pem_has_secret_safe_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KALSHI_API_KEY_ID", "fixture-id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PEM_B64", base64.b64encode(
        b"-----BEGIN PRIVATE KEY-----secret-value-----END PRIVATE KEY-----",
    ).decode("ascii"))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    try:
        malformed_b64 = base64.b64encode(
            b"-----BEGIN PRIVATE KEY-----secret-value-----END PRIVATE KEY-----",
        ).decode("ascii")
        KalshiSigner("fixture-id", private_key_pem_b64=malformed_b64)
    except KalshiCredentialError as error:
        assert error.code == "private_key_malformed"
        assert "secret-value" not in str(error)
        assert "PRIVATE KEY" not in str(error)
    else:
        raise AssertionError("malformed PEM must fail closed")


def test_kalshi_unsupported_private_key_type_is_rejected_before_request():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    try:
        KalshiSigner("fixture-key-id", private_key_pem=pem)
    except KalshiCredentialError as error:
        assert error.code == "private_key_incompatible"
        assert "fixture-key-id" not in str(error)
        assert "signature" not in str(error).lower()
    else:
        raise AssertionError("unsupported signing key must fail closed")


def test_market_mapping_uses_fixed_point_dollars() -> None:
    raw = {
        "ticker": "TEST-YES",
        "title": "Will the test pass?",
        "yes_bid_dollars": "0.4300",
        "yes_ask_dollars": "0.4700",
        "yes_bid_size_fp": "100.00",
        "yes_ask_size_fp": "50.00",
        "no_bid_dollars": "0.5300",
        "no_ask_dollars": "0.5700",
        "close_time": "2026-10-01T12:00:00Z",
        "rules_primary": "Resolves YES if the test passes.",
    }

    market = KalshiVenue._market_snapshot(raw)

    assert market is not None
    assert market.market_id == "TEST-YES"
    assert market.venue == "kalshi:demo"
    assert KalshiVenue._market_snapshot(raw, environment="production").venue == "kalshi:production"
    assert market.yes_bid == 0.43
    assert market.yes_ask == 0.47
    assert market.no_bid == 0.53
    assert market.resolution_rules == "Resolves YES if the test passes."
    assert market.liquidity_usd == 66.5


def test_kalshi_reconciliation_matches_exact_stable_client_order_id(tmp_path):
    import asyncio
    import uuid

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Client:
        async def get(self, endpoint, **_kwargs):
            if endpoint.endswith("/fills"):
                if endpoint == "/historical/fills":
                    return Response({"fills": [], "cursor": None})
                return Response({"fills": [{
                    "order_id": "provider-order-7", "fill_id": "fill-7",
                    "ticker": "KXTEST-EVENT-A", "outcome_side": "yes",
                    "book_side": "bid", "action": "buy",
                    "count_fp": "2.00", "yes_price_dollars": "0.50", "fee_cost": "0.02",
                }], "cursor": None})
            return Response({"orders": [{
                "client_order_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "noema:proposal-7")),
                "order_id": "provider-order-7", "ticker": "KXTEST-EVENT-A", "status": "executed",
            }], "cursor": None})

    venue = KalshiVenue(KalshiConfig(environment="demo"), client=Client())
    venue.signer = type("Signer", (), {"headers": lambda *_args: {}})()
    request = {"proposal_id": "proposal-7", "venue": "kalshi:demo",
               "instrument": "KXTEST-EVENT-A", "notional_usd": "1.10"}

    observation = asyncio.run(venue.reconcile_execution(request))

    assert observation.status == "filled"
    assert observation.provider_reference == "provider-order-7"
    assert observation.source == "kalshi_authenticated_orders_and_fills_api"
    assert observation.filled_exposure_usd == Decimal("1.0200")
    assert observation.external_fill_ids == ("fill-7",)


def test_kalshi_reconciliation_absence_is_inconclusive():
    import asyncio

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Client:
        async def get(self, *_args, **_kwargs):
            return Response({"orders": [], "cursor": None})

    venue = KalshiVenue(KalshiConfig(environment="demo"), client=Client())
    venue.signer = type("Signer", (), {"headers": lambda *_args: {}})()
    result = asyncio.run(venue.reconcile_execution({
        "proposal_id": "missing", "venue": "kalshi:demo",
    }))
    assert result is None


def test_kalshi_reconciliation_checks_historical_orders_after_current_orders():
    import asyncio
    import uuid

    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Client:
        async def get(self, endpoint, **_kwargs):
            calls.append(endpoint)
            if endpoint.endswith("/fills"):
                return Response({"fills": [], "cursor": None})
            if "historical" in endpoint:
                return Response({"orders": [{
                    "client_order_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "noema:old")),
                    "order_id": "historical-order", "ticker": "KXTEST-EVENT-A", "status": "canceled",
                }], "cursor": None})
            return Response({"orders": [], "cursor": None})

    venue = KalshiVenue(KalshiConfig(environment="demo"), client=Client())
    venue.signer = type("Signer", (), {"headers": lambda *_args: {}})()
    observation = asyncio.run(venue.reconcile_execution({
        "proposal_id": "old", "venue": "kalshi:demo", "instrument": "KXTEST-EVENT-A",
    }))
    assert calls == ["/portfolio/orders", "/historical/orders",
                     "/portfolio/fills", "/historical/fills"]
    assert observation.status == "cancelled"
