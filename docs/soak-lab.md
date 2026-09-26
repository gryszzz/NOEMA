# NOEMA Demo Soak Lab

The soak lab exists to answer one question before strategy expansion:

> Is NOEMA collecting trustworthy, replayable market data continuously enough to evaluate forecasting ideas without lookahead?

It does not place real-money orders.

## What it records

The SQLite database stores:

- normalized market snapshots;
- validation outcome for every snapshot;
- invalid snapshots instead of silently discarding them;
- collector heartbeats;
- resolved market outcomes;
- forecast ledger data from the rest of NOEMA.

This gives us a replayable research dataset and a record of data-quality failures.

## Local commands

Take one bounded public-market snapshot batch:

```bash
noema soak-once --limit 100
```

Run continuously:

```bash
noema soak-loop --interval 60
```

Inspect quality:

```bash
noema soak-report
```

Sync settled outcomes:

```bash
noema sync-outcomes --limit 2000
```

Evaluate any forecasts already in the ledger:

```bash
noema evaluate
```

## Persistent worker

The repository includes a container worker that runs:

1. market snapshot collection every 60 seconds by default;
2. settled-outcome synchronization every 15 minutes;
3. a structured quality/evaluation report every 5 minutes.

Environment variables:

```text
NOEMA_DB_PATH=/data/noema.db
NOEMA_SNAPSHOT_INTERVAL_SECONDS=60
NOEMA_OUTCOME_SYNC_INTERVAL_SECONDS=900
NOEMA_REPORT_INTERVAL_SECONDS=300
NOEMA_MAX_MARKETS_PER_SCAN=
```

Mount `/data` to persistent storage. Do not run the worker on ephemeral disk if the goal is a long-lived research dataset.

## Data-integrity policy

Invalid observations remain in the database with their validation failures. They are excluded from replay by default.

This distinction matters: deleting bad rows would make later reliability analysis impossible.

## Demo credentials

Public market discovery can run without authenticated order execution. Authenticated demo credentials become useful for WebSocket/order-book/account reconciliation testing.

Keep production execution disabled during soak testing.

## Promotion criteria

The lab is healthy enough to support strategy research only after it demonstrates:

- high valid-snapshot fraction;
- stable collector heartbeat success;
- repeatable outcome synchronization;
- no unexplained time gaps;
- sufficient resolved-market coverage;
- deterministic replay of historical snapshots.

Only then should strategy-specific forecasting models be evaluated against the stored market baseline.
