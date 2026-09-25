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

## Promotion

A new specialist remains paper-only until enough resolved observations exist to make its calibration and cost-adjusted performance meaningful.

There is no fixed universal sample size: required evidence depends on market frequency, dependence between observations, regime stability, and model complexity.
