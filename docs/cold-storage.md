# Cold storage (compressed payloads + verified zstd Parquet)

Paper/research evidence only. Ledgers that desks, verdicts, tests or training read in full
(`forecast_ledger`, `trench_observations`, `outcomes`, trades/orders/treasury) are **not** moved.

| Data | Hot (SQLite) | Cold (Parquet, zstd) | Read both with |
|---|---|---|---|
| `pool_trades.db` `pool_trades` | `received_at` within `NOEMA_POOL_HOT_H` (24h) | `data/cold/pool_trades/pool_trades/date=YYYY-MM-DD/part-*.parquet` | `noema.pool_stream.read_trades(db, mints=, pools=, since_ts=, until_ts=)` |
| console-state `canonical_pair_observations` | 48h (`NOEMA_PAIR_HOT_HOURS`); paper-fill rows always hot | `data/cold/noema.console-state/canonical_pair_observations/...` | `noema.cold_store.union_rows(...)` / `read_archive(...)` |
| `noema.db` `outcome_observations` | 48h | `data/cold/noema/outcome_observations/...` | `noema.cold_store.union_rows(...)` |

* `canonical_pair_observations.observation_json` is stored as an `NZ1` zlib blob (~12x smaller);
  always read it through `noema.cold_store.decode_payload` / `loads_payload`. `observation_hash`
  is unchanged (sha256 of the plain JSON).
* Compaction (`pool_stream` housekeeping every 10 min; watchdog retention every 30 min) writes a
  Parquet part, fsyncs, reads it back and compares row count + a digest of every value, and only
  then deletes those rows. Failures leave rows in SQLite. Part names are deterministic, so a re-run
  after a crash rewrites rather than duplicates; readers dedupe on the primary key (hot wins).
* Each table directory has `_manifest.json` (files, rows, bytes).
* One-shot: `python -m noema.pool_stream --compact`,
  `python -m noema.retention --recompress --noema-db data/noema.db`.
* Outside readers of `pool_trades.db` (e.g. GOMD `mark_audit`) see only the hot 24h in SQLite;
  older rows are in the Parquet glob above (`pd.read_parquet(dir)` / duckdb `read_parquet('.../*/*.parquet')`).
