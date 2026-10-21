# Changelog

## Unreleased

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
