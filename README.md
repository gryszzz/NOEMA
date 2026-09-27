# NOEMA

**An evidence-first economic intelligence laboratory.**

NOEMA observes public markets, records immutable forecasts, evaluates later
outcomes, and allocates bounded research attention. Its broader mission is an
ecosystem whose specialists earn resources through measurable evidence across
legitimate economic environments.

**Today:** autonomous paper research, optional model cognition, chronological
evaluation, and a local Ops Console. Live execution and demonstrated self-funding
are not established by this repository's paper worker.

[Public experience](https://gryszzz.github.io/NOEMA/) ·
[Start locally](docs/quick-connect.md) · [Documentation](docs/index.md) ·
[Economic integrity](docs/economic-integrity.md) ·
[Meridian](https://github.com/gryszzz/Meridian-Intel)

The public URL becomes available after the Pages workflow is merged and Pages is
enabled. It is a static guide and local report explorer, not the private worker.

## Start with one cycle

Python 3.12+, from a checkout:

```sh
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
noema agent-once
noema doctor
noema ladder
```

The public Kalshi observation cycle needs network access, but no login, wallet,
or paid model. Missing data stays missing. Run `noema-agent` for continuous paper
research and `noema-dashboard` for the local console at `http://127.0.0.1:8787`.

## Inspect the cost of knowing

```sh
noema economics-report > economics-report.json
```

Import that file into the public explorer. It stays in browser memory. Cash,
operating estimates, reservations, and paper results remain separate. Full net
economic profit stays **unknown** until complete reconciliation exists. The
importer validates consistency; it cannot authenticate a runtime or audit receipts.

## Two systems, separate responsibilities

Meridian organizes world evidence. NOEMA reviews frozen research packets and
returns a hash-bound deterministic review. It does not turn a world event into
an order or a forecast. [Working bridge and recovery](docs/meridian-noema-contract.md).

## Build and verify

```sh
ruff check .
pytest -q
npm ci
npm run site:test
npm run site:build
npx playwright install chromium
npm run site:qa
```

The public site has no production JavaScript dependencies or private API calls.
[Pages setup, routes, and browser QA](docs/public-experience.md).
For the full operational command reference, see the [operator guide](docs/operator-guide.md).
