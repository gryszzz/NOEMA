# NOEMA Strategist Layer

NOEMA does not assume that intelligence or profitability can be guaranteed.

The strategist layer exists to make the system harder to fool and more capable of learning which forecasting methods deserve trust.

## Core principles

### 1. Market as prior, not enemy

The current market price is treated as a strong prior. Specialist models must provide enough reliable evidence to move NOEMA away from that prior.

Small-sample models receive little weight even if their recent forecasts look impressive.

### 2. Proper scoring before P&L stories

Model trust is updated using probability quality, especially log loss and Brier score, against a market-price baseline.

A model that earns money through luck but produces poor probabilities should not gain durable influence.

### 3. Disagreement is information

When independent specialists disagree, NOEMA widens its uncertainty interval and increases its margin of safety.

The system should become less confident when its internal minds conflict.

### 4. Symmetric YES / NO analysis

NOEMA evaluates both sides of a binary market from the same calibrated fair probability.

The research layer no longer assumes the interesting trade is always YES.

### 5. Costs are explicit

Edge is separated into:

- raw model-vs-price disagreement;
- fee assumption;
- slippage assumption;
- spread;
- liquidity penalty;
- stale-data penalty;
- model-disagreement penalty;
- forecast-interval uncertainty.

NOEMA should never claim an edge merely because model probability differs from displayed market probability.

### 6. Search itself is a risk

Trying many strategies creates false discoveries.

The research guard penalizes apparent edge based on:

- number of strategies tested;
- edge standard error;
- sample size;
- whether evaluation is truly out-of-sample;
- whether the replay is lookahead-free.

### 7. Walk-forward only

Time-series strategy evaluation must preserve chronology.

Training on future data and then evaluating the past is forbidden even if the code technically calls it a backtest.

### 8. Adaptive trust

Every specialist has a persistent reputation.

After a market resolves, the specialist's probability score is compared with the market baseline. Trust evolves gradually and is bounded so one lucky streak cannot dominate the whole ensemble.

### 9. Diversity over echo chambers

Multiple agents built from the same information source are not independent evidence.

The diversity layer can cap total model influence by family so five near-identical news agents do not count as five independent opinions.

### 10. Survival remains above intelligence

Even a high-confidence strategist output is downstream of:

- data validation;
- strategy quarantine;
- capital guardian;
- stale-state checks;
- exposure caps;
- master halt.

The model does not bypass those layers.

## Strategist stack

```text
market prior
    |
    +--> specialist forecasts
            |
            +--> adaptive reliability
            +--> family concentration cap
            +--> Bayesian/log-odds pooling
            +--> disagreement measurement
                        |
                        v
                calibrated fair range
                        |
        +---------------+---------------+
        |                               |
   YES executable price             NO executable price
        |                               |
        +---------------+---------------+
                        |
                    cost model
                        |
                 uncertainty haircut
                        |
                 conservative edge
                        |
                 research guard
                        |
         credible / insufficient evidence
```

## What "alive" means

NOEMA is not alive in a biological sense.

Operationally, the system is adaptive when it can:

1. collect fresh data continuously;
2. remember model performance;
3. reduce trust in deteriorating models;
4. increase trust only after sustained out-of-sample evidence;
5. quarantine weak specialists;
6. preserve uncertainty when evidence conflicts;
7. replay prior decisions exactly;
8. explain why its confidence changed.

## Research-only Kelly diagnostic

A capped fractional Kelly diagnostic exists for research.

It is intentionally not connected directly to execution. The purpose is to study sensitivity of position sizing to estimated edge, not to turn uncertain probability estimates into aggressive stakes.

## Standard of proof

The most important NOEMA question is not:

> Did this strategy make money?

It is:

> Did this strategy produce a persistent, out-of-sample, after-cost advantage over transparent baselines after accounting for uncertainty, search, drawdown, and regime dependence?

Anything weaker remains research.
