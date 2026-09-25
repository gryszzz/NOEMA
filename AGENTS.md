# NOEMA Agent Operating Contract

NOEMA is an evidence-first forecasting system. Its objective is not to maximize activity. Its objective is to produce calibrated forecasts, reject weak opportunities, and preserve a complete audit trail.

## Hard rules

1. **Paper first.** New venues, models, strategies, and market families start in paper mode.
2. **No fabricated inputs.** Missing, stale, malformed, rate-limited, or unavailable data stays unavailable.
3. **No lookahead.** Backtests may only use information available at the forecast timestamp.
4. **Probability before position.** Forecasting and execution are separate modules.
5. **Risk is deterministic.** An LLM never chooses stake size, bypasses limits, or overrides a kill switch.
6. **Resolution rules matter.** Ambiguous contracts are rejected or explicitly downgraded.
7. **Every forecast is immutable.** Store timestamp, market snapshot, inputs, model version, probability, uncertainty, decision, and eventual outcome.
8. **Costs are real.** Spread, fees, slippage, latency, liquidity, and fill probability must be included before declaring edge.
9. **No unsupported venue automation.** Only use official/authorized APIs. Do not scrape, bypass access controls, evade geofencing, or automate a venue that prohibits automation.
10. **No secret leakage.** Credentials come from environment variables and never enter logs, prompts, commits, or forecast evidence.
11. **Fail closed.** Any missing prerequisite causes PASS / NO_ACTION, never an invented substitute.
12. **Human-controlled live mode.** Live execution requires an explicit environment flag, venue credentials, deterministic limits, and a venue adapter that declares live execution supported.

## Evaluation

Primary metrics:
- Brier score
- log loss
- calibration error
- realized return after fees/slippage
- closing-price comparison where meaningful
- max drawdown
- performance by market family, horizon, edge bucket, and confidence bucket

A strategy is not promoted because of a few wins. Promotion requires out-of-sample evidence.
