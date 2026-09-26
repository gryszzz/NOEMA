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

1. current market probability;
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
the market baseline recorded for its exact snapshot. It averages scores within
an event before averaging events, so the two sides of a match do not count as
two independent observations. It reports Brier and log loss for the same set of
resolved markets; positive Brier improvement means
only that the candidate scored better on that sample. It never unlocks live mode.

## Promotion

A new specialist remains paper-only until enough resolved observations exist to make its calibration and cost-adjusted performance meaningful.

There is no fixed universal sample size: required evidence depends on market frequency, dependence between observations, regime stability, and model complexity.
