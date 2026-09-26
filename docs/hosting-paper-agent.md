# Hosting the paper research agent

`render.yaml` provisions one paid Render background worker with a 1 GB persistent
disk. It starts `noema-agent` every deploy, storing heartbeats, snapshots,
forecasts, and outcomes in the same SQLite database. It runs a cycle every five
minutes and synchronizes up to 500 outcomes hourly. No trading credentials,
wallet keys, model API keys, or public dashboard are configured.
The `.python-version` file pins Render's runtime to Python 3.12.

## Before creating the service

1. In Render, select the intended workspace and review its worker and disk
   charges. Creating the Blueprint provisions paid infrastructure.
2. Connect the GitHub repository and point the Blueprint at `render.yaml` on
   the branch that contains this file. Review its preview before applying.
3. Keep **one instance**. The SQLite file is on a disk attached only to that
   worker; do not start a second writer or a separate dashboard service against
   a local copy of this file.

## After deploying

Check the worker logs for `agent_started`, `agent_outcome_sync`, and recurring
`agent_cycle` entries. A successful deploy does not demonstrate healthy cycles:
check that new cycles keep arriving after an hour. Investigate repeated
`agent_market_collection_error` or `agent_outcome_sync_error` events. Compare
forecast and outcome counts over time with `noema compare` on a backed-up copy
of the database; avoid copying a live SQLite file without its WAL state.

This worker has no inbound URL. A dashboard needs authenticated access to the
same data, which requires a separate storage/serving design. The Render disk
survives ordinary restarts and deploys but should not be the only backup. Set
up periodic consistent database backups before relying on months of evidence.

Paper forecasts do not authorize live orders. Require resolved, independent
comparisons with the baseline, then account for spread, fees, slippage, and
drawdown before considering even a small controlled live trial.
