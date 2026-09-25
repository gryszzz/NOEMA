# NOEMA

**Calibrated autonomous forecasting for prediction markets.**

NOEMA is designed as a small autonomous forecasting desk: discover markets, understand resolution rules, gather evidence, estimate fair probabilities, subtract uncertainty and trading costs, reject weak edges, paper-execute strong candidates, and score every forecast after resolution.

> **Market price is not truth. Model confidence is not edge.**
>
> NOEMA only promotes an opportunity after uncertainty, spread, fees, slippage, liquidity, and deterministic risk limits are applied.

## Autonomous loop

```text
market discovery
      ↓
resolution rules
      ↓
source-backed evidence
      ↓
independent forecasts
      ↓
calibration
      ↓
skeptic / contradiction pass
      ↓
fair probability range
      ↓
market price + costs
      ↓
robust edge
      ↓
deterministic risk gate
      ↓
PASS or PAPER EXECUTION
      ↓
outcome + calibration update
```

The intended production behavior is selective. Most markets should end in **PASS**.

## Current core

The first branch adds:

- immutable market / forecast / opportunity / action models;
- append-only SQLite forecast ledger;
- deterministic cost and risk gates;
- explicit paper vs live modes;
- generic venue-adapter interface;
- paper broker;
- bounded autonomous scheduler;
- unit tests and CI;
- an agent operating contract that forbids fabricated inputs, lookahead, unsupported venue automation, secret leakage, and silent risk overrides.

See [docs/architecture.md](docs/architecture.md).

## Venue strategy

### Kalshi
Primary first integration. Kalshi documents REST, WebSocket, and FIX APIs for event-contract market data and trade execution, plus a demo environment.

### Polymarket
Second integration. Use only its documented SDKs/APIs and respect current availability and account restrictions.

### Sportsbook comparison data
DraftKings can be useful as a market reference, but NOEMA should **not scrape or automate the consumer DraftKings sportsbook**. Use an authorized odds/data provider for sportsbook consensus instead.

## Modes

```text
PAPER (default)
  market data → forecast → risk → simulated fill → score

LIVE (explicit)
  market data → forecast → risk → venue adapter → order
```

Live execution is intentionally impossible unless:
1. the venue adapter declares live execution support;
2. live mode is explicitly enabled;
3. credentials are configured outside the repository;
4. deterministic limits approve the action.

An LLM never chooses stake size or bypasses the kill switch.

## What makes NOEMA useful

NOEMA is not optimized for number of trades. It is optimized for measurable forecast quality:

- Brier score
- log loss
- calibration error
- realized performance after costs
- closing-price comparison where meaningful
- maximum drawdown
- results by market family, horizon, confidence bucket, and edge bucket

A few profitable outcomes do not prove an edge.

## Budget philosophy

Expensive reasoning should only touch finalists:

```text
thousands of markets
  → deterministic filters
  → cheap statistical features
  → model ensemble
  → selective retrieval
  → skeptic
  → a handful of candidates
```

That lets the agent run continuously without turning every market scan into an expensive AI call.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
ruff check .
```

## Next integrations

1. Kalshi market discovery + order book + demo executor
2. Forecast ledger outcome resolution and scoring
3. Baseline forecasters and calibration tables
4. Specialized first niche
5. Polymarket market-data adapter
6. Authorized sportsbook-odds reference feed
7. Only after sufficient out-of-sample evidence: tightly capped live execution

NOEMA is research software. Prediction markets and sports betting involve real financial risk; paper performance can differ materially from live results.
