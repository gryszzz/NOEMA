# NOEMA Architecture

NOEMA is an autonomous forecasting desk, not a single prediction model.

```text
AUTHORIZED MARKET DATA
        │
        ▼
    DISCOVERY
        │
        ▼
 RESOLUTION PARSER
        │
        ▼
 EVIDENCE COLLECTORS ──────┐
        │                  │
        ▼                  │
 DETERMINISTIC FEATURES    │
        │                  │
        ├──► BASE-RATE MODEL
        ├──► DOMAIN MODEL
        ├──► MARKET MODEL
        └──► CONTEXT MODEL
                 │
                 ▼
              ENSEMBLE
                 │
                 ▼
           CALIBRATION
                 │
                 ▼
              SKEPTIC
                 │
                 ▼
      FAIR PROBABILITY RANGE
                 │
        ┌────────┴────────┐
        ▼                 ▼
   MARKET PRICE       COST MODEL
        └────────┬────────┘
                 ▼
            ROBUST EDGE
                 │
                 ▼
      DETERMINISTIC RISK GATE
                 │
          ┌──────┴──────┐
          ▼             ▼
        PASS          PAPER
                        │
                        ▼
                OUTCOME + SCORING
                        │
                        ▼
              CALIBRATION UPDATE
```

## Autonomous loop

A worker may run continuously:

1. discover eligible markets;
2. discard ambiguous, illiquid, stale, or unsupported contracts;
3. gather source-backed evidence;
4. generate independent forecasts;
5. calibrate the ensemble using prior out-of-sample results;
6. subtract spread, fees, slippage, forecast uncertainty, and execution uncertainty;
7. send only surviving opportunities to the deterministic risk engine;
8. paper-execute;
9. resolve outcomes;
10. update evaluation tables.

Live execution is downstream of the exact same pipeline and is not enabled by default.

## Venue policy

### Kalshi
Official API supports real-time market data and trade execution. Initial development should use its demo environment before any live credentials.

### Polymarket
Use official documented market-data/trading APIs only. Availability, account eligibility, and legal access must be respected; NOEMA must never bypass restrictions.

### DraftKings
Do not scrape or automate the DraftKings consumer sportsbook. Use an authorized third-party odds/data provider if sportsbook consensus is needed.

## Cost control

Use a cascade:

```text
all markets
  -> deterministic eligibility filters
  -> cheap features
  -> statistical models
  -> selective retrieval
  -> expensive reasoning only for finalists
```

Most markets should cost effectively zero model tokens.

## Promotion gates

A strategy remains paper-only until it has:
- enough resolved out-of-sample forecasts to estimate calibration;
- acceptable Brier/log loss versus baselines;
- positive simulated performance after realistic costs;
- bounded drawdown;
- stable behavior across time slices;
- no evidence of lookahead or leakage.

Winning streaks are not a promotion criterion.
