# Polymarket US owner-evidence import

The September Polymarket owner-input request accepts a statement/activity export through local NOEMA CLI commands. First inspect the schema without opening or changing the NOEMA database:

```sh
.venv/bin/noema polymarket-owner-inspect --input "$HOME/Downloads/polymarket-september.csv"
```

Inspection reports only column names, row counts, mapped fields, date coverage, a source hash, and classification counts; it does not print row values. After review, import through the existing canonical ledger:

```sh
.venv/bin/noema polymarket-owner-import \
  --db data/noema.db \
  --input "$HOME/Downloads/polymarket-september.csv" \
  --source-reference "owner-held Polymarket US September export" \
  --month 2026-09
```

CSV, JSON arrays/object arrays (`activities`, `transactions`, `data`, `records`, or `results`), JSONL, and NDJSON are supported. The source is limited to 50 MiB and 100,000 rows. The exact original remains at the supplied path; its hash and reference are persisted for audit. Unknown column names, unmatched IDs, unsupported row types, and ambiguous timestamps/statuses remain unknown. Full raw rows and credentials are not copied into SQLite or rendered in Home.

Rows match existing Polymarket activity by an explicit activity/transaction/fill/order ID. A balance change is matured only when the row has an existing exact activity ID, an explicit deposit/withdrawal/refund/transfer/balance type, a final status, an amount and currency exactly matching the existing canonical observation. A fee or settlement is preserved as an economic event only when its amount and currency are present; it remains operator-reported until independently provider-verified. An absent row never proves zero fees, zero refunds, or complete period coverage.

The importer appends event and coverage evidence but does not mature the decision itself. The normal agent cycle observes the changed manifest, appends an outcome to the original `REQUEST_OWNER_INPUT · polymarket_us` decision, measures information gain and actual resource cost, then reranks the full manifest. If an import adds no new evidence, cooldown/no-new-evidence suppression keeps the existing decision rather than creating a duplicate. It does not rewrite decision-time snapshots, close September, use a venue API, change financial authority, or release unknown cash. Reimporting the same file hash is idempotent. Different exports can add provenance while activity IDs prevent duplicate fee/settlement financial events.

Current cash/balance, unresolved historical activity, and attribution to `owner`, `noema`, `external`, or `unknown` are separate facts. Importing a provider export does not itself prove that NOEMA initiated a trade or that a reported balance change is available to spend.
