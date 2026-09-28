# Production boundaries and next infrastructure decision

## Current deployment units

- Public Pages: static product guide and read-only local report explorer.
- Python worker: bounded paper research; can run persistently with the existing
  Render configuration and a persistent disk. Actual deployed health must be
  checked independently of code/configuration.
- Ops Console: private/local FastAPI view of structured records, not model
  chain-of-thought or a financial authorization service.
- SQLite: forecasts, outcomes, reservations, and audit records. Keep the current
  single-worker transactional model while its workload is supported.
- Meridian integration: frozen request/review envelopes, idempotent audit, portable
  file recovery. No always-on authenticated service exists yet.

The public experience deliberately shares the economics-report contract, rather
than importing the private console and failing repeatedly against absent APIs.
This creates a usable inspection surface without changing execution authority.

## Durable integration path

A hosted job service is justified when users need investigations to survive device
closure and resume across devices. Its first implementation should own immutable
request blobs and a transactional job table (request hash, owner, state, attempts,
lease expiry, cancellation request, result hash, version, timestamps). Identical
request IDs with different hashes must fail; retrying the same request must not
reserve cost or produce financial actions twice. Workers need bounded retries and
lease recovery. A cancellation affects future work, not historical evidence.

Authenticate and authorize per job; keep provider credentials server-side; retain
the current file envelopes as import/export and disaster recovery. Prove restart,
duplicate delivery, lease expiry, cancellation, unauthorized access, and backup
restore in integration tests before introducing network access to the worker.

Do not adopt a new database solely for the vision. Measure concurrency, record
volume, and query latency first. A multi-worker service may justify PostgreSQL;
Meridian's viewport/relationship workload may justify spatial indexing separately.
Neither migration nor a hosted job service is implemented by this public UI pass.

## What still blocks a final operational launch

Provider reconciliation and full cost attribution; proven forward research
quality; persistent deployment health and restore checks; authenticated shared
investigations; and venue-specific authorized execution controls. Paper results,
wallet observations, and a deployed website cannot satisfy those gates.
