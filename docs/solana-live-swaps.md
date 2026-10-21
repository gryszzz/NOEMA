# Live Solana memecoin swaps

Trench desks paper-trade unless every gate in this checklist is set. Live entries and exits are Jupiter Swap API swaps (quote, then a `swapTransaction`) signed only inside `noema.wallet_signer_process`. The parent process sends an allowlisted request: side, mint, amount, max slippage (bps), minimum out, and an idempotency key. It does not receive the private key, and the signer ignores any other field.

The private key is `NOEMA_SOLANA_SIGNER_SECRET` (Phantom base58 export or a JSON byte array). The signer process is the only reader. The value must not be written to this repo, the dashboard, prompts, or logs.

## Enablement checklist

Leave these at the defaults until a real wallet, a current SOL price, and a cap are in place. Any missing item fails closed.

1. Keep `NOEMA_MASTER_HALT=0` only when you intend the system to be able to trade. `1` stops swaps.
2. Set `NOEMA_SOLANA_MASTER_HALT=0` and `NOEMA_WALLET_MASTER_HALT=0`. Both default to halted.
3. Set `NOEMA_SOLANA_SIGNER_ENABLED=1`.
4. Set `NOEMA_ALLOW_LIVE_ORDERS=1`.
5. Set `NOEMA_LIVE_VENUES` to include `solana` (or `solana_jupiter`).
6. Set `NOEMA_SOLANA_WALLET_ADDRESS` to the public key of the signer. A mismatch refuses every swap.
7. Inject `NOEMA_SOLANA_SIGNER_SECRET` into the process environment. Do not pass it on the command line in a way that lands in shell history you share, and do not put it in `.env` files that get committed.
8. Set `NOEMA_SOLANA_RPC_URL` to an HTTPS Solana RPC you trust. There is no implicit mainnet default for swaps.
9. Optionally set `NOEMA_JUPITER_SWAP_API_URL`. The default is `https://api.jup.ag/swap/v1` (quote + `swapTransaction`, the same shape as Jupiter v6). `https://quote-api.jup.ag/v6` is a valid override. Set `NOEMA_JUPITER_API_KEY` if that host requires one. The key stays in the signer environment.
10. Set `NOEMA_SOLANA_PRICE_USD` to a current SOL price. The signer uses it for USD caps. A stale low price weakens the cap, so also set a dollar ceiling.
11. Caps, in this order:
    - If `config/autonomy_authority.toml` is present (or `NOEMA_AUTONOMY_AUTHORITY_PATH` points at one), the signer uses that level's `per_trade_fraction` and `daily_notional_fraction` times `NOEMA_SOLANA_BANKROLL_USD`. `NOEMA_AUTONOMY_LEVEL` must be a live level (`micro`, `proven`, `self_funded`, or `expansion`). Paper, shadow, and demo refuse.
    - `NOEMA_SOLANA_MAX_TRADE_USD` and `NOEMA_SOLANA_MAX_DAILY_USD`, when set, can only tighten those fractions.
    - When the authority file is absent, both dollar ceilings are required.
12. `NOEMA_SOLANA_MAX_SLIPPAGE_BPS` defaults to 100. A request above it is refused. `NOEMA_SOLANA_FEE_RESERVE_LAMPORTS` defaults to 20000000 (0.02 SOL) and is kept back from every swap. `NOEMA_SOLANA_PRIORITY_FEE_LAMPORTS` defaults to 100000 and is requested on the Jupiter swap.
13. `NOEMA_SOLANA_MINT_DENYLIST` (comma-separated) and `config/solana_mint_denylist.txt` are refused. Mints with a freeze authority, a mint authority, or a Token-2022 extension other than metadata are refused.
14. `NOEMA_TRENCH_DESKS_ENABLED=1` runs the desks. With the gates above, entries and exits call the signer. Without them, fills stay paper. A master halt while the live gate is on does not fall back to paper fills.

The signer stores idempotency state in `NOEMA_SOLANA_SIGNER_STATE_PATH` (default `data/solana-signer-state.db`). A retry of the same key resends the original signed transaction or returns the confirmed fill. It does not build a second swap while the first outcome is unknown. Confirmed token amounts are written to the trench desk trades and to the economic ledger as `dex_swap_fill` on `wallet:solana`.

Desk exits still use the existing time and price rules: take profit, stop, core stop, and max hold. Those rules submit a real sell only when the live gate is on.
