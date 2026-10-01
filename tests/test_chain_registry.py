import json

import pytest

from noema.chain_registry import load_evm_chains


def test_registry_keeps_existing_network_defaults_and_never_exposes_rpc_urls():
    rows = load_evm_chains({})
    assert [(row.chain_id, row.name) for row in rows] == [
        (1, "ethereum"), (8453, "base"), (42161, "arbitrum"),
        (10, "optimism"), (137, "polygon"), (56, "bnb-chain"),
        (43114, "avalanche"),
    ]
    ethereum = rows[0]
    assert ethereum.canonical_id == "eip155:1"
    assert ethereum.native_asset_id == "eip155:1/native"
    assert "0x_swap_v2_price" in ethereum.public_record()["supported_quote_sources"]
    assert "https://" not in repr(ethereum.public_record())
    assert "https://" not in repr(ethereum)


def test_registry_accepts_new_chain_by_id_without_guessing_native_price_asset():
    env = {
        "NOEMA_EVM_CHAIN_REGISTRY_JSON": json.dumps([{
            "chain_id": 42161, "name": "arbitrum", "native_symbol": "ETH",
        }]),
        "NOEMA_EVM_RPC_URL_42161": "https://rpc.example/secret-path",
    }
    chain = next(row for row in load_evm_chains(env) if row.chain_id == 42161)
    assert chain.canonical_id == "eip155:42161"
    assert chain.rpc_endpoint == "https://rpc.example/secret-path"
    assert chain.native_price_symbol == "ETH"
    assert chain.supported_quote_sources == ("0x_swap_v2_price",)
    public = chain.public_record()
    assert public["provider"] == "configured_json_rpc"
    assert public["execution_authority_state"] == "disabled"
    assert "secret-path" not in repr(public)


def test_registry_discovers_rpc_ids_and_preserves_legacy_rpc_fallbacks():
    rows = load_evm_chains({
        "NOEMA_EVM_RPC_URL_10": "https://optimism.example/rpc",
        "NOEMA_EVM_RPC_URL_BASE": "https://base.example/rpc",
    })
    optimism = next(row for row in rows if row.chain_id == 10)
    base = next(row for row in rows if row.chain_id == 8453)
    assert optimism.name == "optimism"
    assert optimism.native_symbol == "ETH"
    assert optimism.rpc_endpoint == "https://optimism.example/rpc"
    assert base.rpc_endpoint == "https://base.example/rpc"


@pytest.mark.parametrize("metadata", [
    "not-json",
    json.dumps({"chain_id": 10}),
    json.dumps([{"chain_id": True, "name": "fake", "native_symbol": "FAKE"}]),
    json.dumps([
        {"chain_id": 10, "name": "optimism", "native_symbol": "ETH"},
        {"chain_id": 10, "name": "optimism", "native_symbol": "ETH"},
    ]),
    json.dumps([{"chain_id": 10, "name": "bad name", "native_symbol": "ETH"}]),
    json.dumps([{"chain_id": 10, "name": "optimism", "native_symbol": "ETH", "rpc_url": "https://rpc"}]),
])
def test_registry_rejects_malformed_or_unsafe_metadata(metadata):
    with pytest.raises((TypeError, ValueError)):
        load_evm_chains({"NOEMA_EVM_CHAIN_REGISTRY_JSON": metadata})
