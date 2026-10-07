# Durable state and disaster recovery

NOEMA uses local SQLite as its operational store and a private Supabase project as a durable
out-of-host mirror for a bounded set of critical streams. The source-controlled schema, RPC
migrations, and Edge Function live under supabase/.

## Stream semantics

The critical set contains runtime state, heartbeats/timings, missions and handoffs,
specialist/research state, forecasts, economic events and coverage, bills, and authenticated
prediction-account history.

Append-only streams use rowid high-water cursors for transport and canonical primary-key-derived
record keys for identity. Mutable streams (agent_runtime, agent_cycle_timings, missions, mission_handoffs,
ecosystem_specialists, research_trials, autonomous_research_runs, bill_budget,
prediction_account_records, and prediction_account_sync_state) use local SQLite triggers and an
append-only change journal. Every version includes its original source rowid; updates are distinct
versions, and deletes use explicit tombstones. Absence from an export never means deletion.

Mutable stream baseline seeding is bounded across worker cycles. The mirror records an epoch and a
restore floor; export remains incomplete until the baseline and change journal have drained through
the local high-water cursor. Replayed events are idempotent by stream, stable record key, and content
hash.

High-volume raw Trench observations and replaceable collection caches are not in the critical set.
They may be recollected and are not represented as durable recovery data.

## Authentication and authority

The worker receives only NOEMA_DURABLE_MIRROR_URL and NOEMA_DURABLE_MIRROR_TOKEN. The token is
stored remotely as a SHA-256 digest. The Edge Function uses its server-side Supabase secret
internally; the service-role key is never present in the worker, source, export, or logs. Mirror
failure does not grant execution authority. Live orders, signing, transfers, and withdrawals remain
behind their existing independent fail-closed gates.

## Export and restore

Run the authenticated exporter:

    noema durable-mirror-export --output recovery.json

The exporter requests a manifest with a pinned mirror watermark, then downloads each stream in
ordered pages. It refuses to write a bundle unless all required streams have complete checkpoints,
all mutable baselines are complete, high-water cursors are drained, and each fetched record count
matches the manifest. It writes atomically with owner-only file permissions. Empty streams are
explicit entries.

Restore into a new nonexistent SQLite path:

    noema durable-mirror-restore --bundle recovery.json --db data/recovered-noema.db

The version-2 bundle includes format noema-durable-mirror-export, complete true, every critical
stream, from_cursor zero, a through_cursor watermark, checkpoint metadata, ordered mirror IDs,
original logical keys, content hashes, operations, source rowids, and payloads. Restore verifies the
bundle, hashes, current application schema, constraints, and SQLite integrity before atomically
installing the new database. All versions and tombstones are retained in noema_mirror_history;
current mutable table contents are projected from the latest version after that stream's restore
floor.

## Recovery proof boundary

The automated integration test exercises populated SQLite state, mirror ingest, a mutable update,
a tombstone, complete export, fresh-database restore, retained version history, row/hash checks,
SQLite integrity, and opening critical application stores. Export and restore tests use a mock
mirror transport. The test is not evidence that the source migrations or function have been
applied to the hosted project, and it is not a hosted recovery drill.

Do not describe NOEMA as fully disaster recoverable until the source migrations and function are
deployed and the isolated hosted drill in durable-mirror-recovery-drill.md succeeds, including a
fresh worker boot with execution authority OFF.

## Commands

    noema durable-mirror-health
    noema durable-mirror-sync --db data/noema.db --rounds 100
    noema durable-mirror-export --output recovery.json
    noema durable-mirror-restore --bundle recovery.json --db data/recovered-noema.db

## What remains local or cache-only

The mirror is bounded to the streams listed above. Raw provider snapshots, high-volume Trench
observations, and replaceable caches remain local/cache-only. A successful mirror/export says
nothing about their completeness. A successful offline restore does not alone establish provider
coverage, operational health, or execution readiness.
