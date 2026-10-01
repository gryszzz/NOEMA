# Economic system architecture and capability gaps

This is an implementation inventory, not an authorization grant. The codebase
contains substantial prediction-market, Solana-launch research, accounting,
specialist, and wallet-policy machinery. It does not yet constitute a
production-proven autonomous trading system.

## Current system map

| Layer | Existing implementation | Important boundary |
| --- | --- | --- |
| Prediction-market collection | Kalshi and Polymarket US market/account adapters, activity persistence, sampler, SSE console | Kalshi hosted auth is not considered healthy until a private account request succeeds; Polymarket market data is not proof of authenticated account access |
| Forecast and evidence | immutable forecast ledger, market baselines, Brier/log-loss/calibration, truth and market-data qualification | strategy outcomes and venue account economics are separate evidence streams |
| Specialist research | specialist registry/evolution, research queue, Trench-1, EVM public wallet observation, cross-venue experiments | specialist activation is research scheduling, not financial authority |
| Economic accounting | venue fills/orders/positions/settlements, account history, balance history, economic ledger and paper execution | deposits/transfers are not profit; missing fees, marks or cost basis stay unknown |
| Treasury observation | Phantom browser connection and public address observers | Phantom is a human-side observation interface; no owner key or signing capability enters NOEMA |
| Agent wallet | wallet descriptors, intent/policy, chain checks, simulated native/ERC-20 transfers, local Keychain child signer | current server deployment has no provisioned isolated agent signer; `wallet_signer_process.py` denies canonical live authority |
| Multichain routes | new normalized read-only Jupiter and 0x quote adapters; append-only normalized quote evidence | quote path is not scheduled or displayed; no swap, route simulation, fill, receipt, token-delta or swap P&L lifecycle is wired |
| Execution safety | deterministic policy, owner authority, caps, expiry, allowlists, global halt | do not remove or infer authority from strategy evidence |

## Near-term implementation sequence

1. Deploy safe Kalshi diagnostics, then use the next authenticated read-only
   account cycle to classify key parsing, signature/API rejection, response
   failure, or network failure. Do not infer the cause from file presence alone.
2. Verify the Polymarket account endpoint and private stream separately. Record
   authenticated balance/position/activity coverage, append/update counts, and
   stream connect/message/persist times without logging account values or
   secrets.
3. Connect normalized quote observations to a bounded research scheduler and
   persistent evidence record. Keep request identity, quote freshness, route,
   fees, liquidity and provider failures explicit. Quote polling needs provider
   cadence and budget limits.
4. Implement token identity/decimal registry and independent source checks.
   Reject unknown token metadata rather than using guessed decimals or ticker
   symbols.
5. Build paper fills from forward quote snapshots with measured quote-to-decision
   and decision-to-observation latency. Account for fees in their native asset;
   do not convert to USD without a timestamped price source.
6. Add testnet/dev adapter simulation and receipt lifecycle for swaps. Record
   submitted, pending, confirmed, failed, partial, replaced and reverted states;
   reconcile chain token deltas and native fees before computing realized P&L.
7. Provision a separate bounded agent wallet through an isolated programmable
   signer only after provider threat-model, chain policy, address verification,
   owner recovery, audit, and remote key custody are established. The cognition
   runtime should receive intent/receipt schemas only.
8. Persist strategy promotion evidence as versioned, immutable criteria/results
   spanning forward expectancy, uncertainty, drawdown, fees, slippage, latency,
   capacity, and operating cost. Promotion must remain distinct from explicit
   wallet/venue authority.
9. Surface human treasury and agent wallet as distinct entities in the console;
   expose balances, exposure, P&L, fees, source freshness and evidence coverage
   only when supported by account/chain records. Keep unpriced or stale values
   visible as such.

## What the new DEX adapter does not claim

Jupiter Swap V2 `/order` and 0x Swap v2 `/price` responses are normalized into a
common quote record. Raw response bodies, unsigned transactions, calldata and
API keys are discarded. A local quote TTL is only a short-lived observation
window, not a provider guarantee or a chain-level block expiry. Route comparison
reports gross output; it declines to name an economic winner when fees cannot
be compared in the same denomination. Simulation status is `not_run` and live
execution is hard-coded false.

Provider references: [Jupiter Swap API V2](https://developers.jup.ag/docs/swap/index.md)
and [0x Swap API v2](https://docs.0x.org/docs/introduction/quickstart/swap-tokens-with-0x-swap-api).
