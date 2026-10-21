<div align="center">

<img src="docs/images/tev-banner.svg" alt="TEV // NOEMA" width="100%" />

<br />

# NOEMA

### Observe. Infer. Verify. Act.

**An evidence-first autonomous agent ecosystem for economic intelligence, prediction markets, Web3 research, and bounded machine action.**

[![NOEMA Verify](https://github.com/gryszzz/NOEMA/actions/workflows/verify.yml/badge.svg)](https://github.com/gryszzz/NOEMA/actions/workflows/verify.yml)
![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)
![Mode](https://img.shields.io/badge/mode-reality--first-7957d5)
![Authority](https://img.shields.io/badge/live%20authority-fail--closed-cb4b50)
![Audit](https://img.shields.io/badge/state-persistent%20audit-4f8dd6)
![Autonomy](https://img.shields.io/badge/autonomy-SHADOW-555)
![Venues](https://img.shields.io/badge/venues-Kalshi%20%7C%20Polymarket%20US%20%7C%20Solana%20(paper)-22e3ff)
![Hosting](https://img.shields.io/badge/hosting-self--hosted%20box-2bff88)

[Public experience](https://gryszzz.github.io/NOEMA/) ·
[Quick start](docs/quick-connect.md) ·
[Operator guide](docs/operator-guide.md) ·
[Architecture](docs/architecture.md) ·
[Economic Habitat](docs/economic-habitat.md) ·
[Brand](docs/brand.md) ·
[Meridian](https://github.com/gryszzz/Meridian-Intel)

</div>

## Contents

- [The mission](#the-mission) · [Reality-first status](#reality-first-status)
- [TEV trading room](#tev-trading-room) · [Venues](#venues) · [Trench desks](#trench-desks-paper-only)
- [Decision pipeline: TEV JUDGE + CHIEF](#decision-pipeline-tev-judge--chief) · [Earned-autonomy ladder](#earned-autonomy-ladder)
- [Fail-closed execution flags](#fail-closed-execution-flags) · [Swap signers (in progress)](#swap-signers-in-progress) · [Self-hosting](#self-hosting)
- [System architecture](#system-architecture) · [Start with one cycle](#start-with-one-cycle) · [Repository map](#repository-map)

---

## The mission

NOEMA is built around a simple rule:

> **Increase autonomy only when evidence, economics, and authority boundaries can support it.**

The system observes markets and other public information, records forecasts and research state, evaluates later outcomes chronologically, allocates bounded research attention across specialists, and exposes the resulting system as an auditable operating world.

The long-term direction is broader than a trading bot: NOEMA is an **agent economy and intelligence runtime** that can turn observed information into structured research, bounded decisions, and eventually real-world action — without hiding uncertainty or silently expanding its own authority.

## Reality-first status

NOEMA separates **implemented capability** from **future authority**.

| Layer | Current repository state |
| --- | --- |
| Research runtime | Persistent specialist ecosystem, bounded research attention, missions, experiments, evidence, immutable forecasts, and outcome evaluation |
| Cognition | Optional model-assisted reasoning behind explicit configuration and budget gates |
| Prediction markets | Kalshi and Polymarket integrations for market/account observation where configured; paper research remains the default operating mode |
| Web3 | Trench research path for on-chain discovery and forward evidence collection |
| Meridian | Frozen request/review bridge for world-intelligence context; no always-on shared service is implied |
| Economic OS | Cash, reservations, costs, budgets, measured outcomes, and reconciliation rules remain distinct |
| Wallets | Observed wallet/network state can be surfaced; visibility never grants signing or transfer authority |
| Execution | Deterministic gateway exists and is **fail-closed by default**; a visible gateway is not proof of live trading |
| Ops Console | Runtime-backed Economic Habitat, inspectors, replay, live capital surfaces, work packets, and Autonomous Desk |
| Public site | Static documentation/report explorer; it is not the private worker or authority surface |

**Paper results are not revenue. Wallet observations are not authority. Missing state stays missing.**

See [Economic integrity](docs/economic-integrity.md) and [Production boundaries](docs/production-boundaries.md).

## A living operating system

NOEMA's console is no longer just a table of records. It projects canonical runtime state into a spatial operating world.

### Economic Habitat

The habitat organizes real entities into functional zones:

- **Cognition Array** — model and data providers
- **Agent Colony** — registered specialists and handoff participants
- **Mission Control** — persisted active missions
- **Market Deck** — markets and immutable forecasts
- **Evidence Vault** — evidence, sessions, experiments, lessons, and recorded outputs
- **Treasury & Gateway** — wallets, runtime tools, policy, and execution boundary

Agents move or light rooms only when persisted assignments, handoffs, or mission relationships support the visualization. Historical replay hides later/current-only state.

### Autonomous Desk

Phase 3 adds an auditable, single-responsibility desk model:

```text
CHIEF → SCOUT → MAP / CONTEXT → VET → ODDS → SIZE → EXECUTION → RISK / EXIT
```

These are **presentation roles over canonical entities**, not magically-created agents. If NOEMA cannot defensibly bind a real registered specialist or tool to a seat, the seat stays:

```text
VACANT / UNBOUND
```

Active Work Packets reconstruct mission routes from exact assignments and persisted handoffs. The Shift Tape reuses NOEMA's canonical event timeline.

Read the full contract in [Economic Habitat](docs/economic-habitat.md).

## TEV trading room

TEV is NOEMA's private, read-only war-room dashboard served by `noema-dashboard`. Every panel renders real crawler/desk records (replays are labelled); none of these pages can place orders.

<p align="center"><img src="docs/images/trading-room-v5.png" alt="TEV trading room v5" width="100%" /></p>

| Page | What it shows | API |
| --- | --- | --- |
| `/trading-room` (v5) | TEV CRAWLER flipping through real per-pool GeckoTerminal candles, decision graph, CHIEF dial, pool hex map, telemetry ticker | `/api/trading-room` |
| `/trading-room-v4` | Neon pixel room: 8 agent panels, crawler radar, agent floor, balance ribbon, `terminal.py` feed | `/api/room-v4` |
| `/command-center` (v3) | 4×4 grid: prediction scanner, forecast ledger / Brier / calibration, gateway, ladder progress, model budget, system health | `/api/command-center` |
| `/trench-desks` | Original paper-desk panel | — |

<details><summary>v4 and phone views</summary>

<img src="docs/images/trading-room-v4.png" alt="TEV trading room v4" width="100%" />
<img src="docs/images/trading-room-v5-phone.png" alt="TEV trading room on phone" width="320" />

</details>

## Venues

| Venue | Status in code |
| --- | --- |
| Kalshi | Market + authenticated account observation; live orders only through the fail-closed gateway |
| Polymarket US | Market observation + gated live adapter (`polymarket_us`, off by default) |
| Solana memecoins | Trench crawler (GeckoTerminal, DexScreener, pump.fun) feeding **paper-only** desks |
| Kraken / Robinhood | Not integrated |

## Trench desks (paper only)

Five parallel paper desks — **VET, RISK, FILLS, TAPE, FLOOR** seats — each with its own bankroll slice. A desk holds a ~10% core leg and trades bounces with the rest; it is fired after **2 consecutive losses** and replaced by a mutated successor.

| Entry filter (`trench_risk.py`) | Default |
| --- | --- |
| Top-10 holder concentration | ≤ 60% |
| Pool age | ≥ 1 hour |
| Liquidity | ≥ $12,000 |
| Mid-spike guard | no entry if 5m move > 15% or accelerating |
| Stops (bounce & core) | hard ceiling < 8% |

## Decision pipeline: TEV JUDGE + CHIEF

`CRAWLER → FILTERS → VET → TEV JUDGE → CHIEF → FILLS / RISK / SIZE`

- **TEV JUDGE** — optional typed model judgement that only picks among candidates already passing deterministic rules; budget-capped (`NOEMA_DESK_JUDGEMENT_MAX_USD_PER_DAY`).
- **CHIEF** — approves a pick only if probability ≥ `0.60` and confidence ≥ `0.55` (`NOEMA_CHIEF_MIN_PROBABILITY`, `NOEMA_CHIEF_MIN_CONFIDENCE`).
- **SOCIAL** — research-only seat; currently a placeholder.

## Earned-autonomy ladder

Autonomy is computed from evidence (`noema/autonomy.py`), never granted by config.

| Level | Evidence required | Live sizing (per trade / per day) |
| --- | --- | --- |
| SHADOW | < 30 resolved forecasts | none |
| PAPER | ≥ 30 resolved | none |
| DEMO | ≥ 100 resolved, ≥ 7 live-observation days | none |
| MICRO | ≥ 250 resolved, ≥ 21 days (plus further gates) | 1% / 10% |
| PROVEN | ≥ 1000 resolved, ≥ 60 days, ≥ 30 profitable days | 3% / 30% |

The authority config also defines SELF_FUNDED (5% / 50%) and EXPANSION (8% / 80%) tiers. Nothing trades live below MICRO, and `NOEMA_LIVE_REQUIRES_OWNER_REVIEW=1` by default.

## Fail-closed execution flags

Every flag defaults to off; all must be set for any live order.

| Flag | Default | Meaning |
| --- | --- | --- |
| `NOEMA_EXECUTION_GATEWAY_ENABLED` | `0` | Gateway accepts nothing unless `1` |
| `NOEMA_ALLOW_LIVE_ORDERS` | `0` | Second independent live switch |
| `NOEMA_LIVE_VENUES` | empty | Explicit venue allowlist |
| `NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD` | unset | Daily notional cap |
| `NOEMA_MASTER_HALT` | `0` | `1` halts everything |
| `NOEMA_SOLANA_SIGNER_ENABLED` | `0` | Solana signing off |
| `NOEMA_TREASURY_ACTIONS_ENABLED` | `0` | Treasury moves off |

## Swap signers (in progress)

Solana and EVM (ETH/Base/Polygon) swap signers are being built as a **separate signer process**. Keys are read only from its environment, never from the agent, LLM context, or repo; the design refuses keys that don't match NOEMA's configured wallet address, ships **disabled**, and enforces a daily cap. Not merged yet.

## Self-hosting

NOEMA now runs self-hosted on a single box (`noema-agent` + `noema-dashboard` behind a private authenticated proxy). `render.yaml` and Render docs are **legacy** and not the current deployment.

## System architecture

```mermaid
flowchart LR
    A[Market / Web3 / World Sources] --> B[Perception + Research]
    M[Meridian Context] --> B
    T[Trench Crawler: GeckoTerminal / DexScreener / pump.fun] --> B
    T --> TD[Paper Trench Desks] --> J[TEV JUDGE] --> K[CHIEF gate]

    B --> C[Specialist Ecosystem]
    C --> D[Autonomous Desk]
    D --> E[Evidence + Immutable Forecasts]
    E --> F[Economics + Deterministic Risk]

    F -->|reject| P[PASS / Kill]
    F -->|paper-authorized| Q[Paper Action]
    F -->|all live gates satisfied| G[Execution Gateway]

    Q --> H[Outcomes + Scoring]
    G --> H
    H --> I[Calibration + Specialist Evolution]
    I --> C
```

The graph describes flow, not current live authority. The execution gateway remains separately gated and fail-closed.

## What makes NOEMA different

**Evidence over theater.** The UI is allowed to look alive only when canonical state supports it.

**Bounded specialists.** Research attention is allocated across specialist families instead of assuming one universal model should do everything.

**Chronological evaluation.** Forecasts are recorded before outcomes and scored later; winning streaks are not promotion criteria.

**Economic honesty.** Model/API/compute costs, cash, reservations, paper outcomes, and reconciled economic value remain separate concepts.

**Deterministic authority.** Model output can propose; deterministic policy governs whether action is even eligible.

**Restraint is valid.** NOEMA may leave capital, compute, or attention unused when nothing clears the evidence threshold.

## Roadmap

| Phase | State | Direction |
| --- | --- | --- |
| **01 · Reality-first console** | ✅ Implemented | Evidence-backed operations view, replay, diagnostics, economics |
| **02 · Living habitat** | ✅ Implemented | Inhabitants, workstations, mission occupancy, handoffs, recorded outputs |
| **03 · Autonomous Desk** | ✅ In progress | Single-responsibility seats, active work packets, shift tape |
| **03B · Rule Rack / Kill Board / Shift Report** | ⏭ Next | Surface governing rules, rejected opportunities and exact rejection reasons, end-of-shift audit |
| **04 · Policy-gated execution** | 🔒 Gated | Only after explicit authority, supported adapters, budgets, evidence, and reconciliation |
| **05 · Real-world autonomous impact** | ◌ Vision | Expand from research loops into legitimate external work with the same audit discipline |

The roadmap is directional. It does **not** grant execution authority or claim profitability.

## Start with one cycle

Python 3.12+, from a checkout:

```sh
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

noema agent-once
noema doctor
noema ladder
```

Run the continuous paper worker:

```sh
noema-agent
```

Run the private/local Ops Console:

```sh
noema-dashboard
# http://127.0.0.1:8787
```

Public observation paths may require network access. Optional providers, authenticated account observation, cognition, and hosted services require their own configuration. See [Quick connect](docs/quick-connect.md).

## Inspect the economics

```sh
noema economics-report > economics-report.json
```

The public explorer can import that report locally in the browser. The importer checks consistency; it does not authenticate a runtime or prove receipts.

Full net economic profit remains **unknown** until the relevant costs, transfers, balances, and realized outcomes are reconciled.

## Build and verify

```sh
ruff check .
pytest -q

npm ci
npm run site:test
npm run ops:test
npm run site:build

npx playwright install chromium
npm run site:qa
npm run ops:qa
```

The repository's **NOEMA Verify** workflow runs the Python suite, Node contracts, static-site build, and production browser QA on changes.

## NOEMA + Meridian

NOEMA and [Meridian](https://github.com/gryszzz/Meridian-Intel) have separate responsibilities.

**Meridian** organizes world evidence, entities, relationships, events, and timelines.

**NOEMA** consumes frozen research context, performs bounded economic research, records forecasts/decisions, evaluates outcomes, and enforces its own authority/economic boundaries.

The bridge is intentionally explicit and hash-bound. Meridian context does not become an order merely because NOEMA can read it.

See [Meridian ↔ NOEMA contract](docs/meridian-noema-contract.md).

## Repository map

| Area | Purpose |
| --- | --- |
| `noema/` | Python runtime, research, economics, venue/wallet observation, mission and persistence logic |
| `noema/static/` | Private Ops Console and runtime-backed habitat UI |
| `site/` | Static public experience and local report explorer |
| `docs/` | Architecture, operating contracts, evidence/economic boundaries, setup |
| `tests/` | Python + Node regression and truth-contract tests |
| `scripts/` | Browser QA and build/verification tooling |
| `render.yaml` | **Legacy** Render blueprint; current deployment is self-hosted |

## Read next

- [Master mission](docs/master-mission.md)
- [Economic Habitat + Autonomous Desk](docs/economic-habitat.md)
- [Agent ecosystem](docs/agent-ecosystem.md)
- [Live workstation](docs/live-workstation.md)
- [Economic integrity](docs/economic-integrity.md)
- [Prediction venues](docs/prediction-venues.md)
- [Trench-1](docs/trench-1.md)
- [Agent wallet](docs/agent-wallet.md)
- [Production boundaries](docs/production-boundaries.md)
- [Brand system](docs/brand.md)

---

<div align="center">

<img src="noema/static/brand/noema-v2-mark.svg" alt="NOEMA v2 orbital intelligence core" width="220" />

**Evidence → Belief → Edge → Restraint**

<sub>NOEMA is an experimental research and agent-runtime project. Nothing in this repository is a guarantee of profit, a promise of self-funding, or automatic authorization to place orders or move funds.</sub>

</div>
