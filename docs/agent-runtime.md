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
6. chooses the current operating goal and records a heartbeat.

## Identity

NOEMA has a persistent identity record with:

- agent id;
- name/version;
- mission;
- operating principles.

The current mission is:

> Observe uncertain markets, form calibrated beliefs, protect capital, learn from outcomes, and expand only when evidence earns it.

## Goal selection

The current planner is deliberately simple and deterministic.

Priority:

```text
broken public market perception
  -> high-attention independent research market
  -> calibration/data collection
  -> world-state collection
```

This is the first layer of the agent's operational self-direction. Future planners can become richer without changing the fail-closed wallet or risk layers.

Unconfigured account and wallet observation appear in health/status and the ladder,
but do not prevent public market research. Market baselines cannot trigger cognition
or trade attention; an independently validated forecast source is still needed
before the radar can surface actual research opportunities.

## Persistent heartbeats

The runtime writes heartbeats independently of full market cycles.

NOEMA OPS determines whether the agent is alive from heartbeat age rather than trusting a stale `running=true` flag.

If the process dies unexpectedly, the UI eventually reports it as offline/stale.

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
