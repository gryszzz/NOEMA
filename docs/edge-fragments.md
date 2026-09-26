# NOEMA Edge Fragments

This layer contains small, testable research diagnostics that may explain why an apparent edge succeeds or fails.

None of them is assumed profitable.

## Arrival toxicity

Volume-based toxicity and trade-arrival toxicity are different concepts.

NOEMA therefore keeps a separate arrival diagnostic using:

- directional trade persistence;
- longest same-side run;
- buy/sell count imbalance;
- inter-arrival-time burstiness.

This is deliberately separate from the VPIN-like volume-flow signal.

Recent prediction-market research suggests PIN-style arrival measures and VPIN-style volume measures can disagree materially, so the system should not collapse them into one number.

## Tail calibration

A model can look well calibrated overall and still be badly wrong in its most confident tails.

NOEMA separately evaluates:

- high-probability forecasts;
- low-probability forecasts;
- mean predicted probability;
- observed frequency;
- signed tail error.

This matters because rare/high-confidence mistakes can dominate log loss and capital risk.

## Calibration drift

Model trust should not be permanent.

NOEMA includes a lightweight cumulative drift detector over recent Brier scores relative to a historical reference.

A previously good specialist can therefore be flagged when recent probability quality deteriorates.

## Shock resilience

A short-lived price dislocation and a genuine information repricing are not the same event.

NOEMA can measure how much a market price recovers after a shock and how many observations it takes to recover a chosen fraction of the move.

This can later help distinguish transient liquidity shocks from persistent repricing.

## Edge half-life

An edge that exists for one observation is very different from an edge that remains for minutes.

NOEMA estimates:

- initial edge;
- approximate half-life in observation steps;
- fraction of observations for which the edge remains positive.

This is intended for replay analysis and timing research.

## Research Attention Radar

The browser console can display recent forecast-ledger rows with:

- model probability;
- executable YES ask;
- robust edge;
- spread;
- liquidity;
- freshness;
- forecast uncertainty width;
- current decision and pass reason;
- a research-attention score.

The score is triage only. It does not place orders and does not represent guaranteed expectancy.

Automatic attention scoring is suppressed for obvious election/political contract titles.

## Standard

A fragment earns more importance only when it improves out-of-sample prediction or execution attribution after costs.

The desired lifecycle is:

```text
hypothesis
  -> measurement
  -> replay
  -> walk-forward validation
  -> incremental value test
  -> keep / throttle / delete
```

NOEMA should delete clever features that do not add evidence-backed value.
