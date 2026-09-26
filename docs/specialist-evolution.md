# NOEMA Specialist Evolution Loop

NOEMA's research ecosystem now evolves specialist maturity from **new forward evidence**.

The loop is:

```text
new resolved evidence
    -> evidence adapter
    -> specialist review
    -> reliability update
    -> promotion / maintain / downshift / quarantine
    -> challenger experiment proposals
    -> immutable research-trial registry
    -> ecosystem attention reallocation
    -> planner focus
```

This loop controls **research maturity and attention**. It does not authorize live capital.

## Event-driven reviews

The runtime may cycle every few seconds, but evidence does not.

NOEMA therefore compares the current evidence payload with the specialist's latest reviewed
payload. If nothing changed:

```text
evidence unchanged
-> no new review
-> no success/failure streak increment
-> no fake promotion progress
```

This prevents runtime frequency from becoming artificial statistical evidence.

## Evidence contract

A specialist review may contain:

- resolved forward sample count;
- specialist Brier score;
- transparent market/baseline Brier score;
- realized paper return after recorded costs;
- paper-bankroll drawdown;
- calibration error;
- research-credibility verdict;
- Probability of Backtest Overfitting (PBO), when available;
- Probabilistic Sharpe Ratio (PSR), when available.

Missing measurements remain missing. The evolution policy does not fabricate replacements.

## State transitions

```text
SHADOW
  |
  | minimum forward sample
  v
PAPER
  |
  | repeated active-quality reviews
  v
ACTIVE_RESEARCH
```

A new specialist may move from Shadow to Paper once it has enough forward labels to justify
serious paper research. Active Research is harder: it requires predictive improvement,
positive after-cost paper performance, bounded drawdown, acceptable calibration, research
credibility, and any supplied overfit diagnostics.

### Hysteresis

Ordinary deterioration does not cause state thrashing.

Defaults:

- Paper -> Active Research: two consecutive active-quality reviews;
- Active Research -> Paper: two consecutive soft-failure reviews;
- Quarantined -> Paper: three consecutive clean active-quality reviews.

### Immediate quarantine

Hard failures bypass hysteresis.

Examples:

- explicit research-credibility failure;
- hard drawdown breach;
- severe calibration failure;
- very high PBO.

Quarantine sets automatic ecosystem attention to zero.

## Reliability

NOEMA computes a bounded research-reliability score from:

- sample maturity;
- predictive improvement;
- calibration;
- after-cost result;
- drawdown;
- research credibility;
- PBO;
- PSR.

Reliability is an **attention input** only. It cannot increase wallet limits.

## Paper bankroll

Kalshi paper performance now has a realized research-equity path.

Only quotes that:

- were recorded before settlement;
- were selected by the paper system;
- have a later verified outcome;

enter the metric.

The paper bankroll reports:

- deployed paper notional;
- realized paper P&L;
- after-cost return;
- peak-to-trough drawdown;
- per-trade return series.

This is still hypothetical execution. It does not prove real fills, latency, or market capacity.

## Current evidence adapters

### Kalshi history specialist

NOEMA derives:

- distinct resolved paired events;
- candidate vs same-snapshot market baseline Brier;
- candidate calibration;
- settled paper after-cost performance;
- paper drawdown;
- PSR when enough paper returns exist.

The specialist cannot reach Active Research merely because its forecasts look interesting.

### Trench-1

Trench-1 currently counts forward counterfactual labels at the one-hour horizon.

That is intentionally enough only for:

```text
SHADOW -> PAPER maturity
```

Trench-1 does not yet have a trained survival model, calibrated probabilities, or paper
execution evidence, so it cannot satisfy the Active Research gate.

## Challenger experiment factory

When evidence exposes a specific weakness, NOEMA proposes bounded experiments instead of
editing itself freely.

Examples:

- Trench forward labels available -> logistic survival baseline + shallow-tree challenger;
- calibration failure -> prior-fold-only calibration challenger;
- failure to beat baseline -> feature ablation;
- non-positive after-cost result -> predeclared execution-threshold study;
- high PBO -> complexity-reduction experiment;
- quarantine with no obvious challenger -> diagnostic replay.

Every proposal is registered in `ResearchTrialStore`.

Repeated proposals with the same hypothesis, parameters, and feature version resolve to the same
trial id, so the search history cannot be erased by renaming a run.

The factory is capped at three proposals per evidence review to prevent uncontrolled search-space
explosion.

## Self-improvement boundary

NOEMA currently improves its **research program**, not its own production code.

That means it may autonomously:

- change specialist attention;
- propose challenger hypotheses;
- register research trials;
- promote or quarantine specialist research maturity.

It may not autonomously:

- merge code;
- rewrite wallet policy;
- change owner limits;
- enable live execution;
- manufacture market activity;
- treat an unvalidated challenger as production.

The acceptance pattern is:

```text
PROPOSE
  -> TEST
  -> SCORE ON FORWARD/HIDDEN DATA
  -> ACCEPT OR REJECT
```

Only accepted evidence changes the specialist's standing.

## Operator inspection

```bash
noema ecosystem-show
```

The Ops API also exposes:

```text
GET /api/ecosystem
```

The response contains:

- specialist profiles;
- evolution streaks;
- latest evolution review;
- current ecosystem attention plan;
- recent research trials.

## Next milestone

The next major step is to make Trench-1 continuously collect launch trajectories and produce its
first calibrated survival baseline.

Once that exists, the evolution loop can judge Trench-1 on actual predictive evidence rather
than sample count alone.
