# NOEMA Agent Runtime

NOEMA is no longer only a collection of modules. The agent runtime binds perception, memory, account state, wallet observation, the Opportunity Radar, and the Economic OS into one persistent process.

It also syncs settled outcomes at a separate bounded cadence so a running agent
can gather strictly prior observations for an exploratory independent forecast.

## Start the agent

```bash
noema-agent
```

Run one diagnostic cycle:

```bash
noema agent-once
```

The one-shot command records a cycle but does not claim the agent is persistently running.

## Required connections

### Kalshi

Public Kalshi market collection and baseline recording can run without credentials.
Account telemetry is a separate optional connection:

Configure the existing Kalshi variables:

```text
NOEMA_KALSHI_ENV=demo
KALSHI_API_KEY_ID=...
KALSHI_PRIVATE_KEY_PATH=/secure/path/key.pem
```

The runtime uses authenticated read-only telemetry for account health and bounded public market collection for perception.

The runtime uses the production Kalshi API for read-only market/account observation with order authority explicitly disabled. Hosted environments use Render-managed `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PEM_B64` secrets. Local development retains Keychain Key ID lookup and the protected PEM path `~/.config/noema/credentials/kalshi.pem` through `KALSHI_PRIVATE_KEY_PATH`.

### Polymarket US

Public Polymarket US markets and quotes are collected through the official `polymarket-us` Python SDK into the same market snapshot and immutable forecast ledger. Authenticated account status is read-only. Its Key ID and Secret Key are separate Keychain entries; see [prediction venues](prediction-venues.md). No order route is enabled by this market-data integration.

### Dedicated EVM wallet

Wallet observation is optional for market research.

Configure the public address and an RPC endpoint:

```text
NOEMA_EVM_RPC_URL=https://...
NOEMA_EVM_ADDRESS=0x...
```

The runtime currently performs read-only JSON-RPC calls:

- `eth_chainId`
- `eth_blockNumber`
- `eth_getTransactionCount`
- `eth_getBalance`

No EVM private key is needed for observation.

## Runtime environment

```text
NOEMA_AGENT_CYCLE_SECONDS=30
NOEMA_AGENT_HEARTBEAT_SECONDS=15
NOEMA_AGENT_RADAR_LIMIT=50
NOEMA_AGENT_MAX_MARKETS_PER_CYCLE=100
NOEMA_AGENT_MAX_EVENT_CHECKS_PER_CYCLE=12
NOEMA_AGENT_OUTCOME_SYNC_SECONDS=900
NOEMA_AGENT_MAX_OUTCOMES_PER_SYNC=2000
```

A cycle:

1. collects a bounded public market snapshot batch and records PASS-only market baselines;
2. checks previously seen settled one- or two-market series for exploratory, PASS-only forecasts;
3. checks authenticated Kalshi account telemetry;
4. observes the dedicated EVM wallet;
5. reads recent Opportunity Radar state and the Economic OS;
6. reviews the persistent specialist ecosystem and chooses a research focus;
7. chooses the current operating goal and records a heartbeat.

## Identity

NOEMA has a persistent identity record with:

- agent id;
- name/version;
- mission;
- operating principles.

The current mission is:

> Autonomously discover, test, and pursue legitimate economic edge across prediction markets, Web3, and the programmable internet within approved resources and deterministic limits; measure net outcomes honestly, learn, and compound only advantages supported by evidence.

The [master mission](master-mission.md) is the durable north star. The local
`AgentIdentity` also supplies reusable cognitive instructions to the shared
provider research-triage request. Each request adds its narrower task authority;
mission instructions do not grant that call tools, spending, promotion, or
execution access. Changing economic state remains in the datastore and request
context. This local definition is not evidence of a remotely saved OpenAI agent.

## Goal selection

The current planner is deliberately simple and deterministic.

Priority:

```text
broken public market perception
  -> high-attention independent research market
  -> dominant ecosystem specialist
  -> calibration/data collection
  -> world-state collection
```

The ecosystem focus is research attention, not trading authority. The specialist registry keeps
Kalshi paper research and Trench-1 shadow research separate and applies bounded exploration plus
family concentration limits. See [Agent ecosystem](agent-ecosystem.md).

This is the first layer of the agent's operational self-direction. Future planners can become richer without changing the fail-closed wallet or risk layers.

Unconfigured account and wallet observation appear in health/status and the ladder,
but do not prevent public market research. Market baselines cannot trigger cognition
or trade attention; an independently validated forecast source is still needed
before the radar can surface actual research opportunities.

## Persistent heartbeats

The runtime writes heartbeats independently of full market cycles.

NOEMA OPS determines whether the agent is alive from heartbeat age rather than trusting a stale `running=true` flag.

If the process dies unexpectedly, the UI eventually reports it as offline/stale.

## Low-resource control node

Keep `noema-agent` and `noema-dashboard` as the resident processes. Heavy tasks use
one process-shared slot, so only one Docker worker, experiment, browser session, or
local model call can occupy the heavyweight lane at a time. macOS memory pressure
is sampled before admission; the default minimum is 20% available memory and can be
adjusted with `NOEMA_MIN_AVAILABLE_MEMORY_PERCENT`. Locks are released by the OS if
NOEMA crashes, and work that cannot be admitted is recorded as queued or skipped.

OpenClaw Gateway uses the existing Compose project only when a bounded worker task
needs it. NOEMA starts Docker Desktop only if it is stopped, starts the configured
Gateway, verifies the existing fail-closed sandbox policy, runs the task, cleans up
its session, and stops the Gateway. NOEMA stops Docker Desktop afterward only when
it started Desktop and no containers remain running. Set
`NOEMA_DOCKER_DESKTOP_AUTOSTART=0` to require an already-running engine. The worker
still has no Docker socket, NOEMA environment file, host workspace, network access,
or financial authority.

The current Chronos and FinBERT integrations are health-only displays; NOEMA does
not start them for a health check. No browser agent is currently dispatched by the
research loop. The shared resource gate is ready for those capabilities if a future
goal adds an actual inference or browser task. The workstation reports available
memory, active heavyweight work, and queued/resource-limited state.

## Degraded operation

A broken connection does not automatically kill the whole runtime.

Examples:

- Kalshi degraded, EVM healthy -> health is degraded and goal becomes market-perception recovery.
- EVM not configured -> runtime remains alive in partial state.
- Economic OS not initialized -> agent can keep observing while reporting the missing economic memory.

The system records degraded state rather than fabricating successful connectivity.

## Current execution boundary

The runtime currently observes the dedicated EVM wallet and Kalshi account.

It does **not** turn the dedicated EVM wallet into a production signer.

The existing NOEMA wallet policy and disabled signer remain authoritative.

This order is intentional:

```text
persistent perception
-> coherent memory
-> reliable heartbeat
-> stable account mirroring
-> demo/paper validation
-> policy-enforced signer adapter later
```

## Deployment shape

A practical first deployment is:

```text
VPS / server
├── noema-agent
├── noema-dashboard
└── persistent /data/noema.db
```

The dashboard may be exposed only behind private networking/authentication.

The database must live on persistent storage.

## Operator view

NOEMA OPS exposes:

- online/offline heartbeat status;
- current health;
- active goal;
- Kalshi connection status;
- EVM wallet connection status;
- last cycle note;
- existing Economic OS / Radar / telemetry panels.

The dashboard is observation/control surface. The runtime is the agent process itself.

## Cognition providers and specialists

The cognition panel reads the live Docker Model Runner catalog and reports model
IDs, selected-model availability, loopback specialist health, and hosted credential
presence as booleans only. Structured chat models can support research triage;
embedding models belong to retrieval/memory. An installed reranker or vision model
is not treated as a working service unless its runtime route is verified. Chronos
forecasts numeric series and FinBERT scores supplied text. Neither output is a
calibrated event probability or trade signal by itself.

Inject `OPENAI_API_KEY` into the NOEMA process environment. `NOEMA_OPENAI_PROJECT_ID`
is optional. Hosted calls also require an explicit enable flag, prices matching the
selected model, rate/token limits, and a recorded monthly model budget and owner
limit. Missing credentials, prices, or budget keep hosted cognition idle. Secret
values are never included in workstation responses.

The `noema-agent` and `noema-dashboard` launchers anchor their working directory
to the repository, then load its ignored `.env.local`. This keeps runtime secrets
and the relative database path consistent when either entry point starts from
another directory. Keep credentials in that owner-only file or inject them through
the process environment; do not duplicate shell exports or expose them in the UI.

With `NOEMA_COGNITION_PROVIDER=auto`, NOEMA selects configured Cloudflare Workers
AI before paid OpenAI for market cognition. The autonomous research session uses
the verified local model first, then Cloudflare, then OpenAI. OpenAI and
Cloudflare inference both pass through the existing model-budget reservation;
missing prices or budget keep cognition idle or deterministic. Cloudflare
credentials are read from `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`.
Its workstation health check uses the authenticated read-only model-search API
and does not invoke a model. Cloudflare offers a daily free neuron allocation,
but usage above it can be billed on paid accounts, so NOEMA uses configured model
prices as a conservative estimate rather than assuming zero cost. Keep
`NOEMA_CLOUDFLARE_PRICING_MODEL` aligned with the selected model and verify rates
against Cloudflare's current [Workers AI pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/).

## Persisted autonomous missions

Registered research trials remain the experiment source; mission records now bind a
trial to its frozen evidence digest, owning specialist, cognitive session, bounded
run, capability/resource grants, result, lesson, and durable events. Idempotent mission
creation prevents the same trial/evidence pair from producing duplicate missions.
Claims require the specialist chosen by NOEMA and an explicit allowlisted grant.
The experiment subprocess remains the allowlisted, networkless worker with no inherited
credentials, a row bound, and a hard timeout.
The agent's selected operational goal is passed into the research selector as fixed
routing context, and the selector may choose only a registered experiment that advances
that goal or return idle. A perception-recovery goal is fulfilled by the bounded live
collection step and does not dispatch an unrelated research experiment.

Successful runs request a persisted `evidence-critic` handoff. That reviewer is
currently deterministic code: it checks report integrity, finite values, data-count
consistency where applicable, and explicit paper-only status. `PASS` means the report
may be retained as research evidence; it does not prove economic edge or grant live
eligibility. Optional OpenClaw review is a second handoff and remains unavailable unless
its sandbox boundary passes the existing policy. The mission records the lesson, while
the existing research feedback loop adjusts future research attention from observed
quality/failures. A later wake reads that history and the evidence digest deduplication
prevents repeating an unchanged experiment.

Home projects missions, immutable mission events, handoffs, evidence links, costs,
and lesson IDs from bounded read-only SQLite projections. Resource and provider state
remain host/runtime observations; the dashboard does not create missions or initialize
the economic ledger. Unknown compute cost and unreconciled cash remain explicitly
unknown. These mission records coordinate research only and do not grant treasury,
wallet signing, venue trading, or other financial authority.

Stripe account observations use the existing owner-managed Docker MCP profile; no
Stripe credential is copied into `.env.local`, the agent process, or source. The
runtime discovers the profile's tools and calls only the read-only
`retrieve_balance`, `list_payment_intents`, `list_subscriptions`, and `list_invoices`
operations when exposed, at a bounded interval controlled by
`NOEMA_STRIPE_SYNC_INTERVAL_SECONDS` (default 900 seconds). It persists a
secret-free, allowlisted projection of available/pending balances and a bounded
PaymentIntent batch capped at 100, deduplicated by PaymentIntent ID. Known mission IDs
in the explicit `mission_id` or `noema_mission_id` metadata fields receive an
append-only mission observation; unrecognized IDs remain unlinked. An allowlisted
`noema_lane` or valid Stripe `noema_product_id` may annotate a payment without
claiming a mission link. Subscription
and invoice records are reduced to bounded status counts; no customer fields are
retained. Newly observed successful payments also create a Stripe runtime session for NOEMA's history.
The dashboard reads only this persisted projection and refreshes when SQLite
changes. Gross captured USD from that bounded scan is not net revenue. The
currently configured tools do not expose balance transactions, fees, refunds, or
payouts, so those remain explicitly unknown and cannot support net profit or cash
reconciliation. This feed grants no Stripe write capability.
