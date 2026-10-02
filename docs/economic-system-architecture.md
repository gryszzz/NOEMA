# Economic system architecture and capability gaps

This is an implementation inventory, not an authorization grant. The codebase
contains substantial prediction-market, Solana-launch research, accounting,
specialist, and wallet-policy machinery. It does not yet constitute a
production-proven autonomous trading system.

## Current system map

| Layer | Existing implementation | Important boundary |
| --- | --- | --- |
| Prediction-market collection | Kalshi and Polymarket US market/account adapters, activity persistence, console sampler, private account stream, SSE console | Render Kalshi private reads succeeded repeatedly; Polymarket public market collection is separate evidence from authenticated account/stream health |
| Forecast and evidence | immutable forecast ledger, market baselines, Brier/log-loss/calibration, truth and market-data qualification | strategy outcomes and venue account economics are separate evidence streams |
| Specialist research | specialist registry/evolution, research queue, Trench-1, EVM public wallet observation, cross-venue experiments | specialist activation is research scheduling, not financial authority |
| Economic accounting | venue fills/orders/positions/settlements, account history, balance history, economic ledger and paper execution | deposits/transfers are not profit; missing fees, marks or cost basis stay unknown |
| Treasury observation | Phantom browser connection and public address observers | Phantom is a human-side observation interface; no owner key or signing capability enters NOEMA |
| Agent wallet | wallet descriptors, intent/policy, chain checks, simulated native/ERC-20 transfers | agent-wallet factory is deliberately disabled; local Keychain identities are human treasury credentials, not agent custody |
| Multichain routes | normalized read-only Jupiter Swap V2 `/order` and 0x `/price` quote adapters; append-only quote observations | route sampling is not scheduled or displayed; no chain simulation, fill model, receipt, token-delta or swap P&L lifecycle is wired |
| Runtime replication | consistent SQLite worker snapshots and separate console state | worker currently reports snapshot unavailable; new safe diagnostics expose backup/metadata/transport/HTTP failure stage without payloads |
| Execution safety | deterministic policy, owner authority, caps, expiry, allowlists, global halt | do not remove or infer authority from strategy evidence |

## Near-term implementation sequence

1. Diagnose console disk capacity. The latest hosted failure is explicit
   `ENOSPC` while writing the incoming database image on the 10 GB persistent
   disk. Measure free space and the replica/sidecar sizes before considering
   storage expansion or any cleanup; preserve all persisted history.
2. Restore worker-to-console snapshot replication after the disk diagnosis.
   Confirm a persisted snapshot and that console snapshot time advances; `/healthz`
   alone does not prove replica freshness.
3. Render Kalshi cycles repeatedly report authenticated read-only access
   (`orders=1`, `fills=1`, `positions=0`) after request-header sanitation. This
   proves account-read access, not order placement; keep execution disabled.
4. Continue Polymarket account/stream verification. A post-deploy sample at
   00:58:43 UTC reported authenticated read-only status, connected private stream,
   and one record persisted. Verify sustained cadence and account-history coverage,
   including append/update counts and stream connect/message/persist times, without
   logging account values, response bodies or secrets.
5. After console storage recovers, enable the new bounded pair-discovery sampler.
   It uses DEX Screener's documented latest-token-profile and token-pair read
   endpoints, caps each run at eight tokens and five pairs per token, and leaves
   each result as an unreviewed provider observation. The API reference lists
   limits of 60 requests/minute for latest profiles and 300 requests/minute for
   token-pair lookup; NOEMA's proposed cadence is one profile read plus at most
   eight lookups every 15 minutes. See the [official API reference](https://docs.dexscreener.com/api/reference).
6. Connect normalized quote observations to a bounded research scheduler and
   persistent evidence record. Keep request identity, quote freshness, route,
   fees, liquidity and provider failures explicit. Quote polling needs provider
   cadence and budget limits.
7. Implement token identity/decimal registry and independent source checks.
   Reject unknown token metadata rather than using guessed decimals or ticker
   symbols.
8. Build paper fills from forward quote snapshots with measured quote-to-decision
   and decision-to-observation latency. Account for fees in their native asset;
   do not convert to USD without a timestamped price source.
9. Add testnet/dev adapter simulation and receipt lifecycle for swaps. Record
   submitted, pending, confirmed, failed, partial, replaced and reverted states;
   reconcile chain token deltas and native fees before computing realized P&L.
10. Provision a separate bounded agent wallet through an isolated programmable
   signer only after provider threat-model, chain policy, address verification,
   owner recovery, audit, and remote key custody are established. The cognition
   runtime should receive intent/receipt schemas only.
11. Persist strategy promotion evidence as versioned, immutable criteria/results
   spanning forward expectancy, uncertainty, drawdown, fees, slippage, latency,
   capacity, and operating cost. Promotion must remain distinct from explicit
   wallet/venue authority.
12. Surface human treasury and agent wallet as distinct entities in the console;
   expose balances, exposure, P&L, fees, source freshness and evidence coverage
   only when supported by account/chain records. Keep unpriced or stale values
   visible as such.

## What the new DEX adapter does not claim

Jupiter Swap V2 `/order` and 0x Swap v2 `/price` responses are normalized into a
common quote record. Raw response bodies, unsigned transactions, calldata and
API keys are discarded. Jupiter router, fee and provider-expiry fields are kept
when present. A local quote TTL is only an observation bound; provider block/time
expiry is stored separately. The 0x `/price` response is indicative and is not
treated as an executable quote or simulation. Route comparison reports gross
output and declines to name an economic winner when material costs cannot be
compared in a shared denomination. Simulation remains `not_run` and live
execution is hard-coded false.

Provider references: [Jupiter Swap API V2](https://developers.jup.ag/docs/swap)
and [0x Swap API v2](https://docs.0x.org/docs/introduction/quickstart/swap-tokens-with-0x-swap-api).
