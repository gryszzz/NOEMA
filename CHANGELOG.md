# Changelog

## Unreleased

### Culture radar and alpha discovery (paper)

- A new read-only culture crawler covers crypto, stocks and prediction markets: DexScreener, Jupiter, pump.fun, ApeWisdom (Reddit), StockTwits, Kalshi movers, and Polymarket volume, new markets and leaderboard. It respects robots.txt and rate limits, and treats every input as untrusted. Each entity gets a culture signal (buzz velocity, sentiment, novelty), and the `culture` agent's forecasts go into the accuracy ledger.
- Alpha discovery finds mid/small-cap and new tokens and runs them through hard safety screens (RugCheck/GoPlus, LP lock, authorities, top-10 holders, taxes, dev wallet). Sizing follows the capital guard, and memecoins get a smaller exposure cap. Exits use a take-profit ladder, a trailing stop and a time stop. It runs paper-only, with a proposed allowlist that needs owner review. See docs/culture-and-alpha.md.
- The v6 room adds a CULTURE RADAR panel and an ALPHA CANDIDATES board.

### Live Solana and EVM swap signers

- Solana trench desks can swap through Jupiter inside the isolated signer process. The key is `NOEMA_SOLANA_SIGNER_SECRET`, read only there. The only signing address is `J57oJ1GAraQuts7htkrRN7mzhkq93sqM3iRKMRuHn5sv`.
- EVM swaps quote the public KyberSwap aggregator for Ethereum, Base, and Polygon. Robinhood Chain (4663) is in the same chain table and stays inert until its RPC, price, venue, and allowlist are set. The key is `NOEMA_EVM_SIGNER_SECRET`. The only signing address is `0x92B9a0C66a37fE9635e55045b3bf40cB035a59C8`.
- Both signers default off. Live requires the signer flag, `NOEMA_ALLOW_LIVE_ORDERS`, a venue allowlist, and an owner-review flag (`NOEMA_SOLANA_OWNER_REVIEWED` or `NOEMA_EVM_OWNER_REVIEWED`). Those review flags are environment-only. A swap request cannot set them. Master halt still wins.
- Each swap is simulated before it is sent. A retry of the same idempotency key resends the original signed transaction or returns the stored fill. Per-trade and daily USD caps come from `config/autonomy_authority.toml` at `NOEMA_AUTONOMY_LEVEL`; env dollar caps can only tighten them.
- Checklists: `docs/solana-live-swaps.md` and `docs/evm-live-swaps.md`.

### M3 — Trading coordinator

- Added `noema/trading_coordinator.py`. Each cycle qualifies a forecast, asks
  the risk engine to decide, and either records a paper fill or submits an
  `ExecutionProposal` through the execution gateway.
- Paper and live fills both go through `summarize_fills` and
  `EconomicLedger.record_event`. Paper is the path taken unless live gates are
  open. `NOEMA_MASTER_HALT` stops new orders in the same cycle.
- Reconciliation drains optional private-stream events, polls `order_status`,
  applies gateway observations, books fill deltas, and cancels stale unfilled
  orders after `NOEMA_ORDER_TTL_SECONDS`.
- The agent loop and the shutdown path call the coordinator. The console status
  now reports the trading coordinator as wired. Wallet live execution stays
  disabled.

### M4 — Earned autonomy authority

- Added `noema/autonomy_authority.py` and `config/autonomy_authority.toml`.
  SHADOW, PAPER, and DEMO cannot trade live. MICRO and above size orders as a
  fraction of bankroll instead of the fixed $10 cap.
- `NOEMA_LIVE_REQUIRES_OWNER_REVIEW` defaults to `1`. Setting it to `0` lets an
  earned MICRO level unlock live together with the existing gateway flags.
- `NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD` is an optional override of the level's
  daily fraction. `NOEMA_MASTER_HALT` wins over every level.
- Drawdown and calibration breaches re-run `earned_autonomy` each cycle and
  demote immediately. Open orders above the new per-trade cap are cancelled.
  Level transitions are appended to the economic ledger.

### Polymarket US live adapter

- Add a Polymarket US trading adapter (`PolymarketUSTradingVenue`) for authenticated balances, positions, open orders, fills, order status, limit entry, and cancel. Live entry stays off unless credentials, `NOEMA_ALLOW_LIVE_ORDERS=1`, and an inactive master halt are all present. The execution gateway routes to it only when `NOEMA_LIVE_VENUES` contains `polymarket_us` and book, fee, and account-state checks pass. `noema pm-account` and `noema pm-order` (dry-run by default) are the owner commands.
