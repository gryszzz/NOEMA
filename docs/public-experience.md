# Public experience and deployment

`site/` is a standalone HTML/CSS/ES-module product experience. It contains no
backend credentials, telemetry collection, model calls, transaction controls,
external fonts, or production JavaScript dependencies. Playwright is a development
dependency. The CSP denies network connections. The FastAPI Ops Console remains
in `noema/static/` and must be served by `noema-dashboard`, not opened as a file.

## Routes and build

```sh
npm ci
npm run site:test
SITE_BASE_PATH=/NOEMA/ npm run site:build
npx playwright install chromium
# Activate the Python environment, or set PYTHON=.venv/bin/python.
npm run site:qa
```

The deterministic build copies explicit site assets to `dist-site`, rewrites only
local asset references, checks local anchors/assets, and creates `.nojekyll`.
`SITE_BASE_PATH` accepts `/` or slash-terminated repository paths. Navigation uses
hash anchors (`/NOEMA/#economics`), so reloads need no SPA server rewrite.

## Report import boundary

The importer accepts at most 128 KiB of `noema economics-report` JSON. It checks
required fields, bounded decimal strings, exact cash arithmetic using BigInt,
nonnegative counts, chronological metadata, status consistency, and the current
contract's `net_economic_profit_usd: null` / `self_funding_demonstrated: false`.
It preserves the prior snapshot on rejection and clears data on reload or Clear.
Imported text is rendered with `textContent`; files are never uploaded or saved.

Validation does not authenticate the file, classify funding, reconcile provider
cash, or establish profitability. The UI preserves “operator-reported,
unreconciled” and hypothetical paper labels. No report is bundled as real telemetry.

## GitHub Pages

In repository **Settings → Pages → Build and deployment**, choose **GitHub Actions**.
Merge the dependency PR before merging this stacked PR into main. Then push main
or dispatch **Deploy YSZ NOEMA**. Expected default URL:
`https://gryszzz.github.io/NOEMA/`.

The workflow reads the actual Pages URL to support custom domains/base paths.
HTTP 404 produces an explicit disabled-deployment summary; other lookup errors
fail. Ruff, Python tests, importer tests, build, and production browser QA must pass
before a Pages artifact can be uploaded. Deployment alone receives write/token
permissions; the build receives read access. A green build with Pages disabled is
not a deployment. Both repositories returned Pages API 404 during this work.

## Browser checks

`site:qa` starts a temporary loopback server against the built artifact at the
repository path. Chromium checks 390, 768, 1024, and 1440 px; all lifecycle,
specialist, architecture, and disclosure controls; hash reload; keyboard skip;
reduced motion; a real Python-generated empty report; rejected financial claims;
inert hostile text; clear/reload privacy; overflow; assets; errors; and outbound
requests. Set `QA_SCREENSHOTS` to save viewport screenshots. These are automated
Chromium checks, not a claim of complete assistive-technology or Safari coverage.

## Operational console

`noema-dashboard` serves work at `/`, with no product explanation, marketing
navigation or hardcoded capability claims. `/detailed` retains the established
diagnostics. The separate `site/` Pages build remains the place for explaining
NOEMA's purpose and architecture.

The main console reads `/api/operations` and `/api/economic-measurement`. Its new
operational projection opens SQLite in read-only/query-only mode, takes one read
transaction, selects explicit columns and limits each section to 50 recent rows.
It never instantiates migration-capable stores. Missing tables, incompatible
schemas, corrupt databases and absent records are distinguished. Stored runtime
heartbeats must be nonfuture and no older than 90 seconds to show running.

Queue, specialist, experiment, decision/PASS, review, reservation and recorded
outcome views support loaded-record filtering, inspection and backtracking.
Related-record navigation requires matching venue/market, specialist or explicit
parent trial IDs. Unknown-venue market IDs are not joined. These are operational
records, not proof of out-of-sample performance or complete experiment accounting.

Snapshots refresh every 30 seconds while the tab is visible. Failed refreshes
retain prior records with an explicit stale warning. Operational and economic
snapshots have separate timestamps; they are not one atomic cross-report view.
No full-profit or self-funding claim is inferred from paper returns or cash entries.

Run `PYTHON=.venv/bin/python npm run ops:qa` to exercise the actual FastAPI/SQLite
path with a temporary synthetic database and Chromium at four viewport sizes.
