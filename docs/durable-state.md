# Durable state and disaster recovery

NOEMA's local SQLite database remains the worker's high-throughput operational store. It is no longer
the only place critical state may exist.

The durable anchor mirrors a bounded set of control, mission, economic and audit streams into the
dedicated Supabase project through the private `noema-durable-mirror` Edge Function.

## Failure model

The design assumes any compute host or attached volume can fail.

- Worker compute is replaceable.
- Local SQLite is the fast operational cache/source during a healthy run.
- Critical append-only/control state is mirrored out-of-host after every completed cycle.
- During a storage emergency, the worker attempts a read-only critical-state drain before waiting.
- The mirror is idempotent: each local row is addressed by stream + SQLite rowid and content hash.
- Remote stream checkpoints make normal operation incremental.
- Replaying an already-mirrored row does not duplicate it.
- A mirror outage never grants authority and never stops the autonomous research cycle.

## Mirrored streams

The default critical set includes runtime status/heartbeats/timings, missions and handoffs,
specialist/research state, immutable forecasts, canonical economic events and coverage, bill
records, and authenticated prediction-account history.

High-volume raw Trench observations and other replaceable collection caches are intentionally not
part of the first durable set. They can be recollected; mission/economic/audit state cannot.

## Authentication boundary

The worker receives only:

- `NOEMA_DURABLE_MIRROR_URL`
- `NOEMA_DURABLE_MIRROR_TOKEN`

The token is stored only as a SHA-256 digest in the durable database. The Edge Function uses
Supabase's server-side secret credentials internally. The NOEMA worker never receives a Supabase
service-role/secret key.

Tables live in the private `noema` schema with Row Level Security enabled and no anonymous or
authenticated-user policies.

## Recovery behavior

The mirror supports:

- health checks,
- remote checkpoint reads,
- bounded idempotent ingest,
- bounded pull/replay,
- snapshot metadata manifests.

Current automatic recovery preserves and mirrors the critical state. Full operational rehydration
into a new SQLite worker remains an explicit recovery step until table-by-table restore validation
is complete. This is intentional: NOEMA must not silently rebuild financial/economic state without
verification.

## Operator commands

```sh
noema durable-mirror-health
noema durable-mirror-sync --db data/noema.db --rounds 100
```

The backfill command advances in bounded batches until no mirrored stream reports backlog or the
requested round limit is reached.

## Truth boundary

A successful durable mirror proves that critical records exist outside the worker volume. It does
not prove:

- the local SQLite database is healthy,
- every raw research observation has been copied,
- the worker can yet be automatically rehydrated from zero,
- provider-period economic coverage is complete,
- live execution is authorized.

Those remain separate evidence gates.
