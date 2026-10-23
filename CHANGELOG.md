# Changelog

## Unreleased

### Other-chain launch scanner (paper)

- Added `noema/other_chains.py`, a paper-only scanner for new pairs on Base, Robinhood, Ethereum, Polygon, BSC and Arbitrum. It only follows pairs NOEMA could buy (Kyber route via the pinned router, GoPlus honeypot and tax checks, enough liquidity) and counts skips by reason. Entries pay measured discovery lag, gas, slippage, DEX fees, taxes and, for unfunded chains, a real Relay bridge quote. Results go to `data/other_chains.db`, an "Other chains" header chip and `/api/other-chains`. The go/no-go rule is pre-registered in docs/research/other-chains-go-no-go.md.

- Added a separate, newly pre-registered v2 paper arm, `noema/otherchain_v2.py`, for Base and Robinhood. It keeps v1's pass bar and costs, and v1 stays frozen. If DexScreener has no listing, v2 prices and follows the pair through GeckoTerminal. If the GoPlus tax is unknown, it checks the honeypot.is simulation (Base only) and re-checks GoPlus at 30/60/120 s. It enters only once the tax is known and both a buy and a sell route exist. The wait counts as entry lag.
- Other-chain scanner: added a chain allowlist, `NOEMA_OTHERCHAIN_CHAINS` (default `base,ethereum,robinhood`). Dropped chains are skipped entirely, and their open paper rows are closed as `chain_dropped` without a result.
- `feed:geckoterminal` now tracks the newest successful GeckoTerminal response from any reader. Before, it tracked only timing's new-mint pool lookups, which go quiet when no new candidates arrive.

### Trench edge research (paper)

- Added `noema/trench_edges.py`, a walk-forward, no-lookahead test of 5 trench ideas: copy-wallet, exits, graduation, X velocity and avoid filters. It uses curve-math costs, a held-out fold and Bonferroni correction. None passed. Results and the 180 s coverage caveat are in docs/research/trench-edges-2026-10-22.md.
- Trench edge study: added a creator track-record veto test (it failed out of sample) and a capture-outage guard. Tape-wide silences longer than 10 s now cut coverage, because they produced frozen marks. All numbers were rerun.
- Added `noema/trench_specs.py`. It runs the three proven_edges specs (originality/creator pre-filter, manipulation veto, sniper-overhang re-entry) on 7.4 h of tape. The results are preliminary and underpowered, nothing passes, and the 10 min S3 rule cannot be priced. See docs/research/trench-specs-2026-10-22.md.

### Culture radar and alpha discovery (paper)

- A new read-only culture crawler covers crypto, stocks and prediction markets: DexScreener, Jupiter, pump.fun, ApeWisdom (Reddit), StockTwits, Kalshi movers, and Polymarket volume, new markets and leaderboard. It respects robots.txt and rate limits, and treats every input as untrusted. Each entity gets a culture signal (buzz velocity, sentiment, novelty), and the `culture` agent's forecasts go into the accuracy ledger.
- Alpha discovery finds mid/small-cap and new tokens and runs them through hard safety screens (RugCheck/GoPlus, LP lock, authorities, top-10 holders, taxes, dev wallet). Sizing follows the capital guard, and memecoins get a smaller exposure cap. Exits use a take-profit ladder, a trailing stop and a time stop. It runs paper-only, with a proposed allowlist that needs owner review. See docs/culture-and-alpha.md.
- The v6 room adds a CULTURE RADAR panel and an ALPHA CANDIDATES board.

### Live Solana and EVM swap signers

- Solana trench desks can swap through Jupiter inside the isolated signer process. The key is `NOEMA_SOLANA_SIGNER_SECRET`, read only there. The only signing address is `<YOUR_SOLANA_ADDRESS>`.
- EVM swaps quote the public KyberSwap aggregator for Ethereum, Base, and Polygon. Robinhood Chain (4663) is in the same chain table and stays inert until its RPC, price, venue, and allowlist are set. The key is `NOEMA_EVM_SIGNER_SECRET`. The only signing address is `<YOUR_EVM_ADDRESS>`.
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
