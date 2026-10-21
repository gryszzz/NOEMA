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
  daily cap, a venue allowlist, current mission-specific authority,
  verified venue fee/depth economics, a live deterministic risk pass, and a
  venue adapter that supports live execution and provides current balances,
  positions, and open-order state. `NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD` is an
  optional override. When it is unset, the daily cap is the mission authority
  limit, which earned autonomy sets as a fraction of bankroll. An explicit
  value, including `0`, replaces that fraction.
- Treasury-class wallet sends, approvals, contract calls, and withdrawals also
  require `NOEMA_TREASURY_ACTIONS_ENABLED=1`, the wallet's own policy approval,
  isolated signing, and explicit mission authority.

Environment controls read by the gateway are `NOEMA_EXECUTION_GATEWAY_ENABLED`,
`NOEMA_ALLOW_LIVE_ORDERS`, `NOEMA_MASTER_HALT`,
`NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD` (optional override), `NOEMA_LIVE_VENUES`, and
`NOEMA_TREASURY_ACTIONS_ENABLED`. The trading coordinator also reads
`NOEMA_LIVE_REQUIRES_OWNER_REVIEW` (default required) and
`NOEMA_ORDER_TTL_SECONDS`. The runtime owner must supply them through the
existing secure configuration path. Missing values fail closed. The raw
credential fields are not accepted by the gateway and are not written to its
audit table.

The dashboard projection is available at `GET /api/execution-gateway` and shows
persisted proposals, policy decisions, and provider references without exposing
signing material. A single instrument/side cannot have a second pending order.
Gateway requests are idempotent by proposal/intent ID. If a provider call raises
after dispatch begins, its outcome is recorded as `unknown`; its exposure
remains reserved. Wallet signer/coordinator errors follow the same rule in the
wallet budget ledger. Every gateway state change is also written to
`execution_gateway_transitions`; SQLite triggers reject updates and deletions
through the normal database interface. The workstation exposes recent
transitions at the same read-only endpoint.

`ExecutionGateway.reconcile_pending` asks a venue adapter for an exact,
provider-authenticated observation. It applies only observations matching the
stored venue and proposal identity and only to pending requests. Adapter errors,
malformed data, a mismatched provider reference, and no match leave the current
state and reservation untouched. Kalshi order submission now uses a stable
proposal-derived client order ID, and its adapter can look up that exact ID in
authenticated order pages. A missing Kalshi order is inconclusive because the
current and historical API views have different retention; it does not prove
rejection. Polymarket US live routing uses the same stable client order ID.
The retail create-order API does not accept that field, so the trading adapter
stores it locally and then reads the venue order. A missing saved venue order
id is inconclusive and is not resubmitted. Wallet-chain reconciliation is not
implemented. A known pre-acceptance refusal (`VenueRejected`) is recorded as
`rejected`. A transport failure (`VenueUnavailable` or any other exception)
stays `unknown` and keeps its reservation.
Live preflight now rejects adapters that do not declare authoritative
reconciliation and accept the stable client order identity.

The Kalshi adapter searches current orders at `/portfolio/orders` and falls
back to `/historical/orders`; it combines `/portfolio/fills` and
`/historical/fills` because a long-lived order can have fills on both sides of
the provider cutoff. It sums the provider's
fill count, YES price, and fee fields by unique fill ID; malformed or
incomplete fill data leaves reconciliation unchanged. See Kalshi's
[historical data routing](https://docs.kalshi.com/getting_started/historical_data)
and [authenticated fill schema](https://docs.kalshi.com/api-reference/portfolio/get-fills).

Provider snapshots are cumulative: fill IDs, filled exposure, quantity, and
fees cannot regress or be counted twice. Repeating an identical snapshot is
idempotent, and a terminal state cannot transition back to a pending state. Reconciliation moves
filled exposure into a durable gateway exposure field and releases only the
unfilled remainder. A partially filled order later cancelled, expired, or
rejected keeps its filled exposure counted. An authoritative zero-fill
rejection, cancellation, expiration, or failure releases the reservation;
ambiguous dispatch failures remain `unknown` and fully reserved. Legacy
ambiguous `failed` rows remain conservatively charged.

The gateway itself still stores order exposure, not a venue position ledger.
The trading coordinator appends canonical fill events after reconciliation and
keeps paper results in the paper value state. Wallet transactions are not
reconciled here. Pre-dispatch policy rejection does not reserve exposure.

## Current execution status

The trading coordinator is wired. Its default path records paper fills through
the same fill accounting and economic-ledger shape as a live fill, without
calling a venue. Live orders still require the gateway flags, an earned level
of at least MICRO, `NOEMA_LIVE_REQUIRES_OWNER_REVIEW=0`, and a venue adapter
that declares verified economics and authoritative reconciliation. Kalshi's
existing adapter has not declared verified live fee/depth/fill economics, so
production Kalshi orders remain fail-closed at the gateway. Polymarket US live
trading is a separate adapter behind the same venue interface. Wallet sends are classified
as treasury actions and remain blocked unless the separate owner flag and
existing wallet policy both allow them. This integration used provider-response
fixtures and sent no live order or chain transaction.

Polymarket US
now has a trading adapter, and the gateway will call it only when
`NOEMA_LIVE_VENUES` contains `polymarket_us` and the adapter reports live
execution, verified book and fee facts, and a current balance, position, and
open-order read. Those verification flags stay false until
`prepare_live_execution` succeeds. The default engine also has no prediction
mission-authority resolver configured.
