# NOEMA execution gateway

`ExecutionGateway` is the shared fail-closed entry point for structured
prediction-market proposals and wallet intents. Cognitive output is not passed
to provider SDKs as free-form text. A prediction order is represented by an
`ExecutionProposal` containing the mission, venue, instrument, side, bounded
notional/loss/price, expected edge, evidence references, and expiry. The
gateway rechecks the deterministic risk decision and market freshness, reads
current mission authority, reserves cumulative daily/mission exposure
atomically, then calls only the venue adapter.

The capability tiers are distinct:

- Research is read-only and cannot create a proposal.
- Proposal records intent and evidence but cannot call a provider.
- Execution requires the gateway and existing live-order switches, a positive
  owner daily cap, a venue allowlist, current mission-specific authority,
  verified venue fee/depth economics, a live deterministic risk pass, and a
  venue adapter that supports live execution and provides current balances,
  positions, and open-order state.
- Treasury-class wallet sends, approvals, contract calls, and withdrawals also
  require `NOEMA_TREASURY_ACTIONS_ENABLED=1`, the wallet's own policy approval,
  isolated signing, and explicit mission authority.

Environment controls read by the gateway are `NOEMA_EXECUTION_GATEWAY_ENABLED`,
`NOEMA_ALLOW_LIVE_ORDERS`, `NOEMA_MASTER_HALT`,
`NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD`, `NOEMA_LIVE_VENUES`, and
`NOEMA_TREASURY_ACTIONS_ENABLED`. The runtime owner must supply them through the
existing secure configuration path. Missing values fail closed. The raw
credential fields are not accepted by the gateway and are not written to its
audit table.

The dashboard projection is available at `GET /api/execution-gateway` and shows
persisted proposals, policy decisions, and provider references without exposing
signing material. A single instrument/side cannot have a second pending order.
Gateway requests are idempotent by proposal/intent ID. If a provider call raises
after dispatch begins, its outcome is recorded as `unknown`; its exposure
remains reserved. Wallet signer/coordinator errors follow the same rule in the
wallet budget ledger. This checkpoint has no provider reconciliation API or
worker, so `unknown`, `submitted`, and wallet `submitted`/`confirmed` states
remain reserved indefinitely. A pre-dispatch policy rejection releases its
reservation. No live authority should be enabled until provider-authoritative
reconciliation can resolve uncertain and open requests, safely release or
convert reservations, and append the resulting economic events. Current request
rows store their latest state; an append-only transition history is also a
follow-up requirement.

## Current execution status

No live prediction venue is currently eligible through the gateway. Kalshi's
existing adapter has not declared verified live fee/depth/fill economics, and
Polymarket US has no live order adapter. The default engine also has no
prediction mission-authority resolver configured. Wallet sends are classified
as treasury actions and remain blocked unless the separate owner flag and
existing wallet policy both allow them. No live order or chain transaction was
sent while integrating the gateway.
