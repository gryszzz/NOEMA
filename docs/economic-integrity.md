# Economic integrity: implementation and evidence

September 27, 2026. This release improves measurement and resource controls. It
neither demonstrates profitability nor enables live orders, payments or signing.

## Architectural judgment

Preserve Meridian's evidence workspace and NOEMA's research/evaluation boundary.
The strongest existing pieces are immutable forecast snapshots, paired baseline
comparisons, paper execution with fee/depth checks, and bounded model spending.
The highest-priority weakness was the interpretation of their outputs: repeated
samples, unknown costs and unrealized equity could appear more useful than they
were. Another agent framework would not address that weakness.

The current runtime has public-market collection, exploratory historical-series
forecasts, optional evidence review by a language model, paper quotes and later
settlements. Trench collection and chronological survival-model evaluation are a
separate optional research track. Specialist states allocate research attention;
they do not prove a causal advantage or grant payment authority. Challenger
registration is an experiment proposal, not evidence that the experiment ran.

## Changes delivered

| Failure mode | Implemented control | Regression evidence |
| --- | --- | --- |
| Rising equity mistaken for allocatable profit | Allocation is bounded by realized profit above its allocation watermark, equity above its allocation watermark, and unearmarked equity. Legacy snapshots derive a conservative realized watermark. | Unrealized/funding-only gains allocate zero; later realization can allocate once; tiny allocations conserve value. |
| Failed/in-flight model requests evade token limits | Atomic SQLite reservation of calls, worst-case tokens, daily/monthly estimates and per-market cooldown before the request. Failures retain reservations. Full request/schema framing is included in the estimate. | Competing connections cannot overspend; failed calls still consume token allowance; mixed timezone and unknown legacy usage are covered. |
| Metric revisions advance research maturity | Promotion/recovery requires resolved count above the historical review watermark. Hard failures still quarantine immediately. | Alternating aggregate revisions do not promote; hard failure without a new resolution quarantines. |
| NaN or infinity passes quality comparisons | Economic snapshots and specialist evidence reject nonfinite values. | Adversarial numeric cases. |
| Duplicate or unrelated paper quotes inflate a strategy | Shared settlement cohort uses the first recorded decision per market/model, including PASS; excludes malformed values and future outcomes. | Audit and performance agree; duplicate, other-model, first-PASS, timezone and future-settlement cases. |
| Correlated contracts look like independent return samples | Specialist significance diagnostics use aggregated event returns. | Opposing contracts in one event form one return. |
| Trench model learns from mismatched or repeated evidence | One earliest candidate per token; exact feature/control timing; consistent scheduled horizons and bounded lateness; label observation gates; cache hashes full input and audit version. | Duplicate candidates, corrected features, late/future controls and incomplete labels. |
| Training labels counted as tested evidence | Trench specialist resolved count uses actual walk-forward tests. | Existing chronological model and loader tests. |
| Missing calibration permits capital-level classification | Missing calibration fails the micro-capital gate. | Otherwise strong evidence remains at DEMO. |
| Container and hosted worker run different systems | Docker now starts `noema-agent`, matching Render; legacy lab is explicitly selected. | Entry module/CLI checks and existing runtime suite; no image deployment claimed. |

## Measure what the system can actually know

Run `noema economics-report --db data/noema.db`, or inspect
`GET /api/economic-measurement` and the Ops Console's Operating Bill section.
The read-only report covers the current UTC month through its stated timestamp.
It does not create a missing database.

- Cash: operator-reported receipts minus expenses. $100 received and $120 spent
  is a $20 recorded cash deficit. Initial capital is excluded.
- Estimates: monthly operating estimate and reserved model attempts. These are
  exposure estimates, not additional expenses to subtract again from invoices.
- Paper: first selected, later-observed settlements for the named model, net of
  recorded execution costs. These are hypothetical fills, not cash receipts.
- Full net economic profit: unknown until provider reconciliation, complete cost
  coverage and attribution exist. A positive cash subtotal does not prove that
  the operation is self-funded. Invalid records are counted and surfaced.

Record verified receipts and expenses against the work that caused them using the
existing append-only bill journal:

```bash
noema bill-entry --kind receipt --amount 12.00 --source payment-processor \
  --reference payout-or-charge-id --activity-id <research-trial-or-mission-id>
noema bill-entry --kind expense --amount 1.25 --source provider-invoice \
  --reference invoice-line-id --activity-id <research-trial-or-mission-id>
```

The reference must identify the underlying processor or invoice record. Model
reservations and token-price calculations remain estimates, not cash expenses.
Unpriced local compute, delivery, data, or hosting keeps full net profit unknown;
do not enter a zero-dollar estimate to make the ledger look complete. The Ops
Home shows job-linked cash alongside those separate estimates.

The report is a measurement surface, not another source of authority. Internal
Economic OS snapshots are planning records supplied by a caller, not an audited
bank balance. A live Stripe account alone does not establish project revenue;
payouts, deposits and transfers need classifications that prevent double counting.
No account identifiers or private financial records belong in this repository.

## What remains unproven

The research maturity count watermark is a conservative freshness gate, not proof
of disjoint samples or a causal edge. It can hold promotion after sample exclusions
until the historical count is exceeded. Review/profile persistence still assumes
one worker; multi-worker coordination needs a transaction/lease design.

The paper drawdown is a settlement-only path with a stated starting-equity
assumption. It excludes marks on open positions and concurrent capital commitments;
it is not a funded portfolio simulation. Sharpe diagnostics are exploratory and do
not correct for all regime dependence or multiple experiments. Model test results
are chronological replays, not proof that those predictions were issued live.

The allocation watermarks advance by the amount actually allocated. Deposits,
withdrawals and realized outcomes still need a reconciled cash-flow ledger before
these internal plans can justify financial authority. No live authority was added.

## Next experiments, in order

1. Reconcile provider records into an append-only, currency-aware journal with
   source IDs, reversals, funding/transfer/revenue classifications and cost activity
   IDs. Require complete period coverage before computing net economic profit.
2. Freeze competing research policies and resource budgets prospectively. Compare
   the same opportunities against the market baseline and abstention, including
   missing outcomes, failed work and all attributable costs.
3. Replace the file bridge with authenticated durable jobs, cancellation, recovery
   and measured cost. Keep research findings separate from independent evidence.
4. Promote a private deployment only after backup/restore, restart, bounded soak
   and native GUI checks. Deploying code is not evidence of an economic advantage.
