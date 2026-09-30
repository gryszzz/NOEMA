# Canonical economic event ledger

`economic_events` is NOEMA's shared, append-only evidence ledger. It extends the
existing database table and leaves the existing mission, cognition, venue, wallet,
and cash stores as their source-of-truth records. A canonical row is a projection
input with a stable provider reference, not a grant of financial authority.

Each event records provider and event type, provider time, currency/asset, amount,
optional USD valuation, mission/strategy/activity/lane attribution, reconciliation,
realization, capital class, confidence, completeness, owned-account references,
and allowlisted evidence. Events sharing `(provider, external_reference_id,
event_type)` deduplicate. An identical repeat is a no-op; changed facts append a
dispute record; reconciliation and corrections append linked records rather than
editing provider evidence. An adjustment amount is a signed delta.

`MATCHED` means records have been linked; it does not mean accounting is complete.
Only `RECONCILED`, complete, provider-confirmed or derived, realized USD events
enter verified subtotals. Owner capital is separate from revenue. Transfers between
NOEMA-owned accounts have the `transfer` class and are excluded from contribution.
Paper results, unrealized marks, and reservations are separate states and never
become realized revenue or profit. Unpriced non-USD values remain unknown.

`economic_provider_coverage` is an append-only period manifest. The autonomous
runtime idempotently declares the core source/evidence classes for each UTC month.
Providers then attest `COMPLETE`, `PARTIAL`, `UNAVAILABLE`, `NOT_APPLICABLE`, or
`FAILED`, with actual evidence classes, observation time, provenance, and blockers.
Legacy `INCOMPLETE` is read as `PARTIAL`. A period is closed only when every
expected source is `COMPLETE` or explicitly `NOT_APPLICABLE`, and all financial
events reconcile. Silence, empty data, or a provider being disabled does not
mean `NOT_APPLICABLE`. `COMPLETE` requires every expected evidence class plus
provenance. Home identifies the exact blockers.

Wallet events may retain atomic/native values without USD conversion. A USD value
for a non-USD wallet asset requires decimals, a price, source, timestamp no later
than the event, and `event_time` valuation basis; the conversion is checked against
the native amount. A current quote cannot silently revalue historical activity.
Operating reserve is a separate append-only snapshot, available only when every
designated account is present and owned, balances and prices are fresh, liabilities
and reservations are complete, and a positive free balance remains. Otherwise it
is `UNKNOWN` with blockers.

Counterfactuals are first persisted as outcome-free `hypothesis` declarations for
the mission's actual action, do-nothing, baseline, and alternative allocation.
Simulation, paper, and realized outcomes append to the declared scenario ID and
cannot precede the hypothesis. They stay outside financial events and authority.

## Current source adapters

- Stripe records successful USD PaymentIntent captures as matched but incomplete
  unclassified cash; fees, refunds, disputes, earned-revenue attribution, and
  payouts are not yet reconciled.
- Wallet confirmation records a chain-specific action and observed network fee.
  Amounts remain in atomic chain units unless an independently reconciled USD
  valuation is supplied. The action is not treated as revenue.
- OpenAI and Cloudflare cognition reserve estimated budgets before calls and record
  token-based usage cost estimates after persisted usage. Provider invoices are not
  reconciled; reservations are not expenses. OpenAI's organization Costs API is
  authoritative for provider costs but requires an organization Admin API key; the
  standard cognition key is not sufficient. Cloudflare's billable usage API is
  restricted/limited availability and requires a token with billing-read scope.
  Until those sources are successfully ingested and reconciled, usage-based amounts
  remain estimates.
- Operator bill entries map to observed reports. Receipts remain unclassified cash;
  expense entries remain operator-reported estimates until provider-backed.
- Cross-venue experiment outcomes map to paper-only events. Kalshi and Polymarket
  US currently supply read-only research; live fills and fees do not yet feed the
  canonical ledger. The canonical venue event vocabulary is prepared for fills,
  fees, settlements, voids/refunds, and realized P&L, but no event is generated
  without provider evidence.

The Home projection is read-only. It reports known subtotals, unresolved amounts,
owner capital, reservations, attribution, cost drivers, and completeness blockers.
It does not modify venue/wallet permissions or deterministic allocation policy.

Mission alternatives are appended to `economic_counterfactuals`, a separate stream
for `actual_action`, `do_nothing`, `baseline_strategy`, and
`alternative_allocation` scenarios. Each scenario records whether its result is a
hypothesis, simulation, paper outcome, or realized observation. These comparison
records are visible in Home but never enter cash/P&L totals and never change
financial authority.
