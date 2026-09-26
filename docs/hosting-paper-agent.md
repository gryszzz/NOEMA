# Hosting the paper research agent

`render.yaml` provisions one paid Render background worker with a 1 GB persistent
disk. It starts `noema-agent` every deploy, storing heartbeats, snapshots,
forecasts, and outcomes in the same SQLite database. It runs a cycle every five
minutes and synchronizes up to 500 outcomes hourly. No trading credentials,
wallet keys, model API keys, or public dashboard are configured.
The `.python-version` file pins Render's runtime to Python 3.12.

## Track the bill before increasing spend

Read the actual Render workspace invoice and model provider pricing. Set the
monthly hosting estimate, other recurring services (including any model spend),
and the most you are prepared to cover personally:

```bash
noema bill-config --hosting 7 --other 1 --model-budget 0 --owner-limit 10
noema bill-show
```

These figures are **examples**, not Render quotes. Use the real service price,
disk price, and any taxes or usage charges you expect. When an actual invoice or
settled cash receipt arrives, record it once with a unique invoice/payout ID:

```bash
noema bill-entry --kind expense --amount 7.25 --source render --reference invoice-2026-09
noema bill-entry --kind receipt --amount 3 --source settled-payout --reference payout-2026-09
```

The Ops Console's Operating Bill section shows estimated uncovered exposure and
warns when it exceeds the owner limit. Entries are operator-reported; NOEMA
cannot verify them or pay Render. A warning cannot cap Render's actual charges:
set a provider-side spend limit or stop the service if the bill is too high.
The paper worker has no receipts; paper P&L and account deposits do not count.
Model calls have a separate estimated daily cap; set provider billing limits
before enabling a paid model. Paid model calls require a configured positive
`--model-budget` within `--other`. NOEMA also reserves each call against that
monthly model budget in SQLite. For example, allocating $2 to the model requires
`--other` of at least $2; include your other non-hosting services in that total.

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
