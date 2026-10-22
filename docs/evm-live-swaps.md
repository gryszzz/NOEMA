# Live EVM swaps

EVM swaps stay off unless every gate in this checklist is set. The signer process quotes the public KyberSwap aggregator (no paid API key), pins the router `0x6131B5fae19EA4f9D964eAc0408E4408b66337b5`, simulates the transaction, then signs and submits it. 0x and 1inch need an API key, so they are not the default.

The parent sends chain, side (`buy` spends native gas for an ERC-20, `sell` does the reverse), token, amount, max slippage in basis points, minimum out, and an idempotency key. It does not receive the private key or the signed transaction.

`NOEMA_EVM_SIGNER_SECRET` is a 32-byte hex private key (optional `0x`) or a JSON byte array. Only the signer process reads it. The derived address must be `<YOUR_EVM_ADDRESS>`. Any other key is refused. The value must not be written to this repo, the dashboard, prompts, or logs.

Chains in the registry today are Ethereum (1), Base (8453), Polygon (137), and Robinhood Chain (4663). Adding another chain is a new entry in `SWAP_CHAINS` with a pinned router. There is no default RPC, so a chain does nothing until its own endpoint is set.

## Enablement checklist

Leave these at the defaults until the key, a current native price, an allowlist, and a cap are in place. Any missing item fails closed.

1. Keep `NOEMA_MASTER_HALT=0` only when you intend the system to be able to trade. `1` stops swaps.
2. Set `NOEMA_EVM_MASTER_HALT=0` and `NOEMA_WALLET_MASTER_HALT=0`. Both default to halted.
3. Set `NOEMA_EVM_SIGNER_ENABLED=1`.
4. Set `NOEMA_ALLOW_LIVE_ORDERS=1`.
5. Set `NOEMA_EVM_OWNER_REVIEWED=1`. This is the owner-review flag. It defaults to off, and the agent cannot set it over IPC.
6. Set `NOEMA_LIVE_VENUES` to include `evm` (every configured chain) or the specific names `ethereum`, `base`, `polygon`, and `robinhood`.
7. Set `NOEMA_EVM_ADDRESS=<YOUR_EVM_ADDRESS>`.
8. Inject `NOEMA_EVM_SIGNER_SECRET` into the signer process environment only.
9. Set the RPC for each chain you will use. Swaps do not fall back to a public default:
   - `NOEMA_EVM_RPC_URL_ETHEREUM` or `NOEMA_EVM_RPC_URL_1`
   - `NOEMA_EVM_RPC_URL_BASE` or `NOEMA_EVM_RPC_URL_8453`
   - `NOEMA_EVM_RPC_URL_POLYGON` or `NOEMA_EVM_RPC_URL_137`
   - `NOEMA_EVM_RPC_URL_ROBINHOOD` or `NOEMA_EVM_RPC_URL_4663`
10. Set `NOEMA_EVM_NATIVE_PRICES_USD` to a JSON object of USD prices keyed by chain name, for example `{"ethereum":"3000","base":"3000","polygon":"0.40","robinhood":"3000"}`. A missing chain price refuses that chain.
11. Caps, in this order:
    - If `config/autonomy_authority.toml` is present (or `NOEMA_AUTONOMY_AUTHORITY_PATH` points at one), the signer uses that level's `per_trade_fraction` and `daily_notional_fraction` times `NOEMA_EVM_BANKROLL_USD`. `NOEMA_AUTONOMY_LEVEL` must be `micro`, `proven`, `self_funded`, or `expansion`.
    - `NOEMA_EVM_MAX_TRADE_USD` and `NOEMA_EVM_MAX_DAILY_USD`, when set, can only tighten those fractions.
    - When the authority file is absent, both dollar ceilings are required.
12. Set `NOEMA_EVM_TOKEN_ALLOWLIST` to comma-separated `chain:0xaddress` entries. An empty allowlist refuses every token. `NOEMA_EVM_TOKEN_DENYLIST` and `config/evm_token_denylist.txt` are refused even if also allowlisted. Wrapped native gas tokens and the router cannot be the target token.
13. `NOEMA_EVM_MAX_SLIPPAGE_BPS` defaults to 100 and cannot be raised above 2000. `NOEMA_EVM_FEE_RESERVE_WEI` defaults to 5000000000000000 (0.005 native) and is kept back from every swap. `NOEMA_EVM_MAX_FEE_WEI` defaults to 20000000000000000. `NOEMA_EVM_MAX_GAS_PRICE_WEI` defaults to 500000000000.
14. Optionally set `NOEMA_KYBERSWAP_API_URL` (default `https://aggregator-api.kyberswap.com`) and `NOEMA_KYBERSWAP_CLIENT_ID` (default `noema`). `NOEMA_KYBERSWAP_API_KEY` is optional and is sent only to KyberSwap from the signer process.

Sells need an existing ERC-20 allowance for the pinned router. The signer does not submit an approval. A retry of the same idempotency key resends the original signed transaction or returns the confirmed fill. Confirmed amounts come from the receipt: ERC-20 `Transfer` logs for the token leg, and the native balance change plus gas for a sell.
