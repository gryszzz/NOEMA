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

## Kalshi Core

The first real venue adapter now targets Kalshi's official Trade API:

- open-market discovery with cursor pagination;
- live binary quotes and settlement rules;
- order-book retrieval;
- exchange-status checks;
- demo and production environment separation;
- RSA / Ed25519 request signing;
- authenticated **demo** order submission;
- production execution hard-disabled unless explicitly armed.

See [docs/kalshi-core.md](docs/kalshi-core.md).

## Next integrations

1. Kalshi WebSocket market/order-book stream
2. Historical resolved-market ingestion + outcome scoring
3. Baseline forecasters and calibration tables
4. Specialized first niche
5. Separate Kalshi perps engine
6. Polymarket market-data adapter
7. Authorized sportsbook-odds reference feed
8. Only after sufficient out-of-sample evidence: tightly capped live execution

NOEMA is research software. Prediction markets and sports betting involve real financial risk; paper performance can differ materially from live results.


## Demo Soak Lab

NOEMA now includes a replayable data-collection lab for long-running demo/paper research.

```bash
noema soak-once --limit 100
noema soak-loop --interval 60
noema soak-report
noema sync-outcomes --limit 2000
noema evaluate
```

The lab persists normalized market snapshots, validation failures, collector heartbeats, and settled outcomes into SQLite. A Docker worker is included for an always-on deployment with a persistent `/data` volume.

See [docs/soak-lab.md](docs/soak-lab.md).


## Strategist Layer

NOEMA now has a research strategist layer designed to learn **where its own beliefs deserve trust**.

It includes:

- market-prior shrinkage;
- reliability-weighted Bayesian/log-odds ensemble pooling;
- adaptive specialist trust updated after resolution;
- explicit model-disagreement penalties;
- symmetric YES / NO edge comparison;
- separate fee, slippage, liquidity and uncertainty haircuts;
- walk-forward evaluation helpers;
- multiple-testing / false-discovery penalties;
- calibration-aware strategy promotion;
- model-family concentration caps;
- research-only capped fractional Kelly diagnostics;
- human-readable strategist reports.

The strategist does **not** assume profit is guaranteed. Apparent edge must survive out-of-sample testing, costs, uncertainty, search penalties, calibration checks and survival controls before it is considered credible.

See [docs/strategist.md](docs/strategist.md).


## Truth + Timing Layer

NOEMA now separates realtime truth, AI interpretation, and timing research.

It includes:

- append-only evidence provenance with payload hashes;
- forecast grounding against stored evidence IDs;
- structured AI reasoning packets with per-claim evidence requirements;
- raw WebSocket frame journaling with receipt timestamps;
- sequence-aware local binary order books;
- feed/processing latency measurement;
- edge-persistence tracking;
- shock/spread/latency-aware entry-quality research;
- source-authority policy;
- deterministic truth + timing gates.

The design intentionally prefers missing an opportunity over acting on stale, corrupt, unsynchronized, or ungrounded state.

See [docs/truth-timing.md](docs/truth-timing.md).
