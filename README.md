<div align="center">

# NOEMA //

### Observe. Infer. Verify. Act.

**A calibrated autonomous market-intelligence system for prediction markets, realtime microstructure research, and bounded multichain agents.**

[![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![CI](https://img.shields.io/github/actions/workflow/status/gryszzz/NOEMA/verify.yml?branch=main&label=verify)](https://github.com/gryszzz/NOEMA/actions)
![Research First](https://img.shields.io/badge/mode-research--first-8A7DFF)
![Fail Closed](https://img.shields.io/badge/risk-fail--closed-2E8B57)
![Ops Console](https://img.shields.io/badge/console-NOEMA%20OPS-111827)

**Evidence → Belief → Edge → Restraint**

</div>

---

## What is NOEMA?

NOEMA is an autonomous forecasting and market-research stack built around one idea:

> **A model is not allowed to call itself smart. It has to prove it.**

The system continuously gathers market data, preserves evidence, forms probabilistic beliefs, compares those beliefs with executable prices, measures uncertainty and execution quality, rejects weak opportunities, and scores itself after resolution.

It is designed to become more selective as it becomes more capable.

Most markets should end in **PASS**.

## Quick connect

The easiest first-time setup is:

```bash
noema agent-once
noema ladder
```

For an always-on paper research worker, see [Render hosting](docs/hosting-paper-agent.md).

The first research cycle uses public Kalshi market data and needs no account or wallet
credentials. It records market-price baseline forecasts for later scoring. Baselines
always PASS and are suppressed from Opportunity Radar attention; they do not imply edge.

To add optional authenticated account observation, a dedicated public EVM address,
or Microsoft Foundry research, run `noema setup` and then `noema doctor`.

Then run the persistent agent and dashboard:

```bash
noema-agent
noema-dashboard
```

Run `noema ladder` at any time to see which steps have actual observed evidence,
which need a connection, and which remain locked. A successful one-shot cycle is
not a running background process.

See [the real life ladder](docs/real-life-ladder.md) for what each rung proves.

`noema setup` writes secrets/config to a git-ignored, owner-only `.env.local` file. The wizard covers Kalshi, the dedicated EVM wallet connection, and Microsoft Foundry cognition. See [docs/quick-connect.md](docs/quick-connect.md).

The independent research candidate uses previously observed YES/NO outcomes from
homogeneous one-market events or two-market events with exactly one YES winner
in the same series. It only records a forecast when at least 30 complete settled
events were seen before the new market snapshot.
The series frequency is exploratory: it stays PASS-only and cannot trigger
automatic model attention or execution.

```bash
noema sync-outcomes --limit 2000  # one-time bootstrap for an agent-once run
noema agent-once
noema compare                     # paired outcomes versus the same-market baseline
```

`noema-agent` syncs up to 2000 settled outcomes every 15 minutes by default.
Paired comparison counts a market once, excludes forecasts made after settlement,
and remains unqualified for live execution. See [evaluation](docs/evaluation.md).

## System map

```mermaid
flowchart TD
    A[Authorized Market + World Data] --> B[Validation + Provenance]
    B --> C[Realtime Journal + Sequence-Safe Books]
    C --> D[Specialist Models]
    D --> E[Adaptive Trust]
    E --> F[Bayesian Ensemble]
    F --> G[Fair Probability Range]

    G --> H[YES / NO Edge]
    H --> I[Fees + Spread + Slippage]
    I --> J[Uncertainty + Toxicity + Timing]
    J --> K[Research Credibility]
    K --> L[Strategy Health]
    L --> M[Capital Guardian]
    M --> N{Decision}

    N -->|weak / stale / uncertain| P[PASS]
    N -->|research-worthy| Q[PAPER / DEMO]

    Q --> R[Outcome + Fill + Fee Telemetry]
    R --> S[Calibration + Trust Update]
    S --> D
```

## Current stack

| Layer | What NOEMA does |
| --- | --- |
| **Truth** | Evidence IDs, payload hashes, source authority, stale/future-data rejection |
| **Realtime** | WebSocket capture, receipt timestamps, sequence-aware local books, latency metrics |
| **Forecasting** | Market prior, specialist probabilities, Bayesian/log-odds ensemble |
| **Self-critique** | Model disagreement, skeptic logic, multiple-testing/search penalties |
| **Calibration** | Brier score, log loss, ECE, tail calibration, drift detection |
| **Microstructure** | Spread, liquidity, queue crowding, toxicity, shock resilience, lead/lag |
| **Timing** | Quote freshness, latency, edge persistence, price-shock rejection |
| **Execution truth** | Fills, fees, maker/taker mix, partial fills, reconciliation |
| **Survival** | Drawdown stops, exposure caps, reserve floor, loss quarantine, master halt |
| **Ops** | Browser dashboard, Opportunity Radar, account telemetry, model-trust view |
| **Wallet foundation** | Multichain intents, policy gate, daily budget, provider-neutral signer |
| **Economic OS** | High-water accounting, capital buckets, earned autonomy, profit waterfall, R&D/infrastructure budgets |

## Agent runtime

NOEMA can now run as one persistent process instead of a collection of independent modules.

```bash
noema-agent
```

A single diagnostic cycle is also available:

```bash
noema agent-once
```

The runtime owns:

- identity and mission;
- persistent heartbeat/state;
- bounded Kalshi market perception;
- authenticated Kalshi account health;
- dedicated EVM wallet observation;
- Opportunity Radar awareness;
- Economic OS awareness;
- goal selection based on degraded/healthy subsystems.

Configure the dedicated EVM wallet with its **public address** and an RPC endpoint:

```text
NOEMA_EVM_RPC_URL=...
NOEMA_EVM_ADDRESS=0x...
```

The runtime does not require the EVM private key for observation, and the default wallet signer remains fail-closed.

NOEMA OPS computes liveness from heartbeat age, so a crashed process cannot remain falsely displayed as alive forever.

See [docs/agent-runtime.md](docs/agent-runtime.md).

## Opportunity Radar

The Ops Console contains an expandable **Research Attention Radar**.

Each market can expose:

```text
model probability
market probability
executable ask
raw edge
estimated cost
uncertainty penalty
robust edge
spread
liquidity
freshness
forecast width
evidence IDs
decision
exact PASS / attention reason
```

The radar ranks **research attention**, not guaranteed trades.

Automatic attention scoring is suppressed for obvious election/political contract titles.

## Edge-fragment laboratory

NOEMA includes small diagnostics that can be measured independently and deleted if they do not add value.

```mermaid
mindmap
  root((EDGE FRAGMENTS))
    Flow
      VPIN-like volume toxicity
      Arrival toxicity
      Directional persistence
    Book
      Queue crowding
      Spread
      Depth imbalance
    Time
      Edge half-life
      Time-to-resolution pressure
      Latency
    Behavior
      Tail calibration
      Calibration drift
      Shock resilience
    Structure
      Probability graph consistency
      Threshold monotonicity
      Lead / lag
```

These are hypotheses, not alpha declarations.

The lifecycle is:

```text
hypothesis
  -> collect
  -> replay
  -> walk-forward test
  -> incremental-value test
  -> KEEP / THROTTLE / DELETE
```

## Strategist

NOEMA does not average a pile of agents and call it intelligence.

Specialists earn influence.

```mermaid
flowchart LR
    M[Market Prior] --> E[Ensemble]
    A[Model A] --> T1[Trust A] --> E
    B[Model B] --> T2[Trust B] --> E
    C[Model C] --> T3[Trust C] --> E
    E --> U[Disagreement + Uncertainty]
    U --> V[Fair Value Range]
```

A model can lose influence when:

- recent calibration deteriorates;
- Brier/log-loss stops beating transparent baselines;
- out-of-sample performance weakens;
- drawdown breaches strategy policy;
- a formerly useful signal decays.

See [docs/strategist.md](docs/strategist.md).

## Truth + timing

NOEMA treats hallucination as a systems problem, not a prompting problem.

A factual AI claim must resolve to stored evidence.

```text
CLAIM
  -> evidence ID
  -> source
  -> observed timestamp
  -> retrieval timestamp
  -> payload hash
  -> integrity / freshness gate
```

Realtime market books also fail closed on sequence gaps.

See [docs/truth-timing.md](docs/truth-timing.md).

## NOEMA // OPS

Run the browser console:

```bash
noema-dashboard
```

Open:

```text
http://127.0.0.1:8787
```

The console can display:

- exchange-reported realized P&L;
- fees;
- orders, fills and positions;
- reconciliation status;
- model trust;
- data health;
- resolved-market coverage;
- evidence/realtime counts;
- Opportunity Radar;
- research diagnostics.

The UI deliberately separates **observed**, **derived**, **model**, and **research** values.

## Agent wallet architecture

The long-term goal is autonomous action without handing unlimited authority to one process.

```mermaid
flowchart LR
    H[Human Treasury] -->|bounded funding| A[NOEMA Agent Wallet]
    N[NOEMA Strategist] --> I[Wallet Intent]
    I --> P[Deterministic Wallet Policy]
    A --> P
    P -->|approved only| S[Policy-Enforced Signer]
    S --> SOL[Solana]
    S --> EVM[EVM]
    K[Master Halt] -. veto .-> P
    R[Reserve / Tx / Daily Limits] -. veto .-> P
    L[Venue + Contract Allowlists] -. veto .-> P
```

### Treasury vs agent

**Treasury / human wallet**

- long-term funds;
- deposits/withdrawals;
- recovery;
- user-visible wallet such as Phantom;
- never exposes its seed to NOEMA.

**NOEMA agent wallet**

- intentionally small operating balance;
- autonomous only inside pre-authorized limits;
- separate signer/provider;
- replaceable;
- hard policy boundaries.

Current code includes:

- wallet roles/providers;
- Solana/EVM/Bitcoin-capable chain descriptors;
- structured wallet intents;
- deterministic policy evaluation;
- daily notional accounting;
- evidence requirement;
- reserve/slippage/size caps;
- provider-neutral signer interface;
- fail-closed disabled signer by default.

See [docs/agent-wallet.md](docs/agent-wallet.md).

## Multichain direction

The future multichain research engine is not intended to be a blind meme-coin sniper.

It should ask:

```text
Is the move real?
Is the liquidity real?
What venue moved first?
What is the executable route?
What is the price impact?
What are gas / priority costs?
Is order flow toxic?
Is the edge already decaying?
Does the route violate wallet policy?
```

Potential adapters:

- Solana DEX routing;
- EVM DEX routing;
- cross-venue reference prices;
- on-chain flow;
- bridge-cost/latency research;
- volatility/regime models.

Every chain/venue remains an isolated adapter behind the wallet policy.

## Economic OS

NOEMA has an internal economic operating system for tracking how capability should grow **only after realized evidence supports it**.

```mermaid
flowchart LR
    P[Realized After-Cost Result] --> H[High-Water Review]
    H --> R[Reserve]
    H --> S[Strategy Capital]
    H --> D[Research / Data]
    H --> I[Infrastructure]
    H --> T[Treasury Sweep]

    E[Evidence Quality] --> A[Autonomy Review]
    A --> L[SHADOW -> PAPER -> DEMO -> MICRO -> PROVEN -> SELF-FUNDED -> EXPANSION]
    L --> W[Wallet Ceiling]
    K[Loss / Drawdown] -->|immediate contraction| L
```

The economic layer separates:

- survival reserve;
- strategy capital;
- research/data budget;
- infrastructure budget;
- pending treasury sweep.

Only new equity above the previous high-water mark is eligible for a new profit waterfall. Promotions happen **one autonomy level at a time**; evidence deterioration can demote the system immediately.

Initialize internal accounting:

```bash
noema economy-init --capital 500
```

Inspect it:

```bash
noema economy-show
```

These commands maintain NOEMA's internal ledger; they do not transfer money.

The Ops Console exposes the same economic state, including autonomy, equity, high-water mark and budget buckets.

See [docs/economic-os.md](docs/economic-os.md).

## Demo soak lab

Build a replayable dataset:

```bash
noema soak-once --limit 100
noema soak-loop --interval 60
noema soak-report
noema sync-outcomes --limit 2000
noema evaluate
```

The long-running worker records normalized snapshots, invalid observations, heartbeats and resolved outcomes.

See [docs/soak-lab.md](docs/soak-lab.md).

## Execution telemetry

Authenticated read-only telemetry:

```bash
noema account
noema telemetry
```

NOEMA can reconcile:

- exchange-reported order fill counts;
- raw fill records;
- partial fills;
- maker/taker mix;
- actual fee cost;
- position-level fees;
- realized P&L.

See [docs/execution-telemetry.md](docs/execution-telemetry.md).

## Kalshi

The current venue stack includes:

- market discovery;
- binary quote normalization;
- resolution rules;
- authenticated orderbook reads;
- WebSocket ingestion;
- market-history/outcome sync;
- account telemetry;
- queue position observation;
- demo/production separation.

See [docs/kalshi-core.md](docs/kalshi-core.md).

## Modes

```text
RESEARCH
  ingest -> replay -> forecast -> evaluate

PAPER / DEMO
  live data -> forecast -> policy -> simulated/demo behavior -> score

PRODUCTION
  intentionally gated behind explicit credentials, wallet/venue policy,
  survival controls, current API verification and human-controlled limits
```

## Repository principles

1. **No fabricated inputs.**
2. **No lookahead.**
3. **Probability before position.**
4. **Evidence before narrative.**
5. **Risk is deterministic.**
6. **Unknown state fails closed.**
7. **Forecasts are immutable.**
8. **Execution costs are measured.**
9. **Models earn trust.**
10. **Survival outranks activity.**

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

ruff check .
pytest -q
```

## Project status

```text
forecasting core          ██████████
Kalshi data stack         ██████████
replay / evaluation       ██████████
adaptive strategist       ██████████
truth / provenance        ██████████
execution telemetry       ██████████
Ops Console / radar       ██████████
agent-wallet policy       ████████░░
economic OS               █████████░
multichain adapters       ██░░░░░░░░
mainnet autonomy          ░░░░░░░░░░
```

The unfinished pieces are intentionally unfinished until they can be validated against real provider/venue behavior.

---

<div align="center">

### NOEMA //

**Observe reality. Form beliefs. Measure edge. Protect capital.**

Research software for uncertain markets. No strategy, model, or automation can guarantee profit.

</div>
