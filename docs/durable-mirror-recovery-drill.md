# Hosted durable mirror recovery drill

This checklist is the required hosted proof before NOEMA may be described as fully disaster
recoverable. Applying repository migrations, deploying the Edge Function, exporting data, and
booting a replacement worker are separate steps. Do not run the drill against the active worker
database.

## Before the drill

- Review the source-controlled migrations and Edge Function version deployed to the NOEMA project.
- Confirm the durable mirror token is available only to the worker and the Supabase service-role
  key remains server-side in the Edge Function environment.
- Confirm live execution, signing, order submission, transfers, and withdrawals are OFF.
- Confirm all 20 critical stream checkpoints are present. Mutable streams must have completed the
  baseline seed and drained their local change journal through its recorded high-water cursor.
- Ensure there is an isolated recovery host/path and adequate disk for the export plus SQLite DB.

## Export and restore

1. Run the authenticated mirror export command and require the exported_complete status.
2. Save the export outside the worker host; record its SHA-256 and through-cursor watermark.
3. Restore into a new, nonexistent SQLite path. Never overwrite or merge into the active database.
4. Require the restore report to show all critical streams, expected counts, and integrity_check ok.
5. Independently run SQLite integrity_check and foreign_key_check on the restored database.
6. Verify mission, forecast, economic-event, provider-coverage, account-history, bill,
   specialist, trial, and research-run identities and counts against the export.
7. Verify mutable records reflect their latest version after the stream baseline floor. Confirm
   tombstoned identities are absent from current tables and their event remains in mirror history.
8. Open each critical application store with the restored path and run read-only checks.

## Replacement worker boot

Boot exactly one isolated replacement worker from the restored database with market-data reads
allowed and all live financial actions disabled. Verify startup, health, database writes, and one
read-only worker cycle. Check the deterministic execution policy still reports authority OFF.
Do not enable wallet signing, order submission, transfers, withdrawals, or any production switch.

Record the operator, source commit, migration versions, export hash, watermark, restore counts,
integrity results, store-open results, boot duration, missing streams, and every unresolved issue.
An incomplete or unavailable stream, a failed constraint check, an unexplained count difference,
or an unsuccessful worker boot makes the drill fail. Do not infer deletions from records absent
from the export.

## Current proof boundary

The repository test covers local trigger capture, mutable updates and tombstones, authenticated
export pagination semantics using a mock mirror, bundle validation, fresh database restore, and
store initialization. It is not a hosted recovery drill. Until the migration and function are
applied to the hosted project and this checklist succeeds on a fresh host, describe the anchor as
source-controlled and recovery-capable in tests, not as proven full disaster recovery.
