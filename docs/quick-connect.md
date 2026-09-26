# NOEMA Quick Connect

This is the shortest path from a fresh checkout to a running NOEMA agent.

## 1. Install

```bash
git pull
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

On Windows PowerShell, activate the virtual environment with the normal Windows activation command instead.

## 2. Run guided setup

```bash
noema setup
```

The wizard asks for the important connection values:

- Kalshi environment;
- Kalshi API key ID;
- Kalshi private PEM file path;
- dedicated EVM wallet RPC URL;
- dedicated EVM public address;
- Microsoft Foundry / Azure OpenAI endpoint;
- Foundry model deployment name;
- Foundry API key;
- whether model cognition is enabled.

Secrets are written to:

```text
.env.local
```

The file is git-ignored and written owner-readable/writable only.

NOEMA does not ask for the dedicated EVM wallet private key during this setup phase.

## 3. Check readiness

```bash
noema doctor
```

Doctor reports readiness without printing secret values.

It checks:

- Foundry configuration;
- Kalshi key ID and PEM path;
- EVM RPC/address pair;
- Economic OS initialization.

## 4. Initialize the Economic OS

Only if it has not already been initialized:

```bash
noema economy-init --capital 500
```

The capital number initializes internal accounting only. It does not transfer money.

## 5. Run one complete cycle

```bash
noema agent-once
```

This is the best first live connectivity test.

Look for:

```text
Kalshi       connected
EVM wallet   connected
cognition    idle/completed
economy      initialized
```

Cognition may remain `idle` when no market clears the deterministic model-call gates.

## 6. Start NOEMA

```bash
noema-agent
```

In another terminal:

```bash
noema-dashboard
```

Open:

```text
http://127.0.0.1:8787
```

## Microsoft Foundry

For API-key setup, use an Azure OpenAI / Foundry endpoint compatible with the v1 Responses API.

Example resource endpoint:

```text
https://YOUR-RESOURCE.openai.azure.com
```

NOEMA appends:

```text
/openai/v1/responses
```

The deployment field is the **deployment name configured in Foundry**, which does not have to be identical to the catalog model ID.

The cognition client uses:

- Responses API;
- structured JSON Schema outputs;
- configurable reasoning effort;
- no server-side response storage;
- bounded output tokens.

## Background cognition logic

NOEMA does not call the model every cycle.

A market must pass deterministic gates including:

- non-suppressed research attention;
- minimum robust edge;
- data freshness;
- uncertainty width;
- per-market cooldown;
- calls-per-hour budget;
- tokens-per-hour budget.

The model receives only selected market/research telemetry. It does not receive API keys, PEM contents, EVM private keys, or arbitrary process environment variables.

Model output is constrained to a structured research packet:

```text
thesis
confidence
attention reason
counterarguments
unknowns
requested research
recommended mode
evidence IDs
```

The only recommended modes are:

```text
ignore
collect_more
investigate
```

Cognition can refine NOEMA's research goal, but it does not bypass deterministic wallet/risk policy and does not choose position size.

## Research memory

Requested research from cognition is added to a persistent research queue.

This prevents useful unanswered questions from disappearing after one model response.

## Useful tuning variables

```text
NOEMA_FOUNDRY_REASONING_EFFORT=medium
NOEMA_FOUNDRY_MAX_OUTPUT_TOKENS=1500

NOEMA_COGNITION_MIN_ATTENTION=0.70
NOEMA_COGNITION_MIN_ROBUST_EDGE=0.01
NOEMA_COGNITION_MAX_FRESHNESS_SECONDS=120
NOEMA_COGNITION_MAX_UNCERTAINTY=0.20
NOEMA_COGNITION_COOLDOWN_SECONDS=300
NOEMA_COGNITION_MAX_CALLS_PER_HOUR=6
NOEMA_COGNITION_MAX_TOKENS_PER_HOUR=20000
```

Start conservative. Increase model spend only after replay/operating evidence shows the cognition layer adds value.
