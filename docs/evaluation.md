# NOEMA Evaluation System

NOEMA may only claim an edge after resolved, out-of-sample forecasts support it.

## Required metrics

For binary event forecasts:

- **Brier score**: squared probability error.
- **Log loss**: strongly penalizes confident incorrect forecasts.
- **Calibration error**: compares predicted probabilities with observed frequencies.
- **After-cost return**: paper/live return after spread, fees, slippage, and fill assumptions.
- **Drawdown**: peak-to-trough capital decline.
- **Segment stability**: results split by series, market family, horizon, liquidity, confidence, and edge bucket.

## Baselines

Every model must be compared against at least:

1. the midpoint of a valid, simultaneous YES bid and ask (forecast scoring only);
2. unconditional/base-rate forecast;
3. prior production model.

A model that cannot outperform transparent baselines out-of-sample is not promoted.

## No lookahead

The forecast ledger is append-only. Evaluation uses the forecast snapshot recorded before resolution. Re-running a model after the event and treating that as a historical forecast is forbidden.

NOEMA stores when each settlement was first seen. The series-frequency candidate
may only learn from settlements that were both resolved **and first seen** before
its snapshot. A backfilled old settlement cannot retroactively influence a
forecast. Corrections reset that outcome's first-seen time for future forecasts.

New observations are tagged `kalshi:demo` or `kalshi:production` so identical
tickers from separate API environments cannot be mixed in training or scoring.
Older untagged `kalshi` rows stay in the ledger for audit, but cannot train this
new candidate. Sync outcomes again in the chosen environment to build its
separate history.

The candidate also checks the full current event's market membership against
Kalshi's public event endpoint before recording a one- or two-market forecast.
If the event cannot be verified or has extra markets, NOEMA records no candidate.

`noema evaluate` counts the earliest pre-settlement forecast per market/model.
`noema compare` pairs the earliest series-frequency forecast per market with
the YES bid/ask midpoint reconstructed from its exact archived snapshot. It
requires a valid two-sided quote. The original ask remains in the ledger as
`market-baseline-v1` for execution review; newer snapshots also record the
midpoint as `market-midpoint-v1`. Neither quote is an independent forecast.
The comparison averages scores within
an event before averaging events, so the two sides of a match do not count as
two independent observations. It reports Brier and log loss for the same set of
resolved markets; positive Brier improvement means
only that the candidate scored better on that sample. It never unlocks live mode.

## Promotion

A new specialist remains paper-only until enough resolved observations exist to make its calibration and cost-adjusted performance meaningful.

There is no fixed universal sample size: required evidence depends on market frequency, dependence between observations, regime stability, and model complexity.

## Learned calibration research

`noema model-audit` trains `noema-calibration-v2` on the earliest market-price
snapshots that also have a verified, paper-only history candidate and a later
observed settlement. It learns a regularized blend of the market's log odds and
the historical candidate's log odds, separately for each venue and series,
shrunk toward the original quote. This is a learned **forecast blend** with an
independent historical input, not an LLM. Foundry research does not set its probability.

The audit requires at least 100 training events and 30 later test events in a
series. At each test event it trains only on outcomes resolved and first seen
before that event's first market snapshot. It averages scores within events,
and reports both Brier score and log loss versus the same-snapshot midpoint. Fewer
eligible events produce an explicit insufficient-data status. A better score
calls for research review; it never unlocks trading or claims profitability.
No model weights are deployed by this audit command.

## Before any profitability claim

The forecast score is separate from an executable trading return. A YES buy
pays the ask; costs depend on order size, actual venue and market fees, fills,
slippage and subsequent settlement. The prototype engine currently uses a
conservative paper fee placeholder and does not use verified depth or fee
terms; it refuses Kalshi live mode. Its apparent edge must not be reported as
profit.

The paper quote collector obtains the current series and event fee fields and
the public current market's best bid, ask, and displayed sizes. An authenticated
Kalshi key can also supply full orderbook depth. It treats NO bids as YES asks
when using the full book, checks that the spread is within the paper risk policy,
and declines any fill beyond the observed size. Public data supplies only the
best price level, so a larger quote must fail if it exceeds that level. It
uses the July 7, 2026 general taker rate (7% times the series/event multiplier,
contract count, price, and one minus price) with conservative fee rounding at
each price level. Unsupported fee types or missing fee fields fail closed. The
schedule version and inputs are saved with an immutable first quote. The
published [Kalshi fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf)
must be rechecked if it changes.

The agent can save a quote for a freshly recorded independent candidate if its
authenticated orderbook connection works. The lower forecast bound must exceed
the quoted price and estimated fee by at least four cents per contract: three
cents for the risk edge threshold and one cent reserved for latency/slippage.
`noema paper-quote TICKER --contracts 1` can inspect that same path within the
30-second forecast validity window; it cannot replace a stale forecast with a
new guess. Quotes above the paper risk policy's $10 stake limit are saved as
passes. `noema paper-audit --db data/noema.db` reports hypothetical payout
minus quoted entry costs and fees only for selected quotes and later settlements.
It reports `no_settled_paper_quotes` when evidence is absent, and never marks
the strategy eligible for live execution.

Displayed book depth can vanish before an order arrives; account limits,
competition, liquidity changes, and repeated correlated positions are not
simulated. Demo-market quotes cannot establish production profitability. A
forward paper sample with actual order latency and fill observations, calibrated
forecast scores, position limits, and verified current fee rules is still needed
before any claim that NOEMA can fund itself.

Outcome sync divides a bounded page budget between current and archived
settlements. Each partition saves its own cursor between runs, and new selected
paper quotes get a small, direct lookup by ticker so they need not wait for a
broad scan. A failed request keeps its cursor for retry. `noema paper-audit`
stays empty until the quoted market settles and that later result is observed.
