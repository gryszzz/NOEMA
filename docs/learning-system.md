# Source-linked learning

NOEMA keeps protocol mechanics separate from empirical economic beliefs. The
versioned source registry is seeded in `noema/knowledge.py`; fetched content is
normalized into immutable snapshots and bounded chunks. A mission retrieves only
high-relevance chunks for its assigned specialist. Every result carries the source
URL, source ID, content version, fetch time, freshness, and chunk ID. Retrieved text
is untrusted reference data, never an instruction or permission grant.

The initial registry covers Solana/RPC/fees, Jupiter, Raydium, Jito, Ethereum,
OpenZeppelin, Kalshi, Stripe, Uniswap v3, CFMM research, and Flash Boys 2.0. The
reading curricula in `noema/knowledge.py` are task-routing metadata; they do not
create additional runtime agents. Actual NOEMA roles remain the registered
specialists in the existing ecosystem.

Use the existing database and CLI to initialize, inspect, and refresh the registry:

```sh
noema knowledge-init --db data/noema.db
noema knowledge-show --db data/noema.db
noema knowledge-refresh --db data/noema.db --limit 3
noema knowledge-refresh --db data/noema.db --source solana-rpc
```

Refreshes use the exact seeded HTTPS URLs, do not follow redirects, send no
credentials, cap each response at 1 MB, reject binary PDF responses, and preserve
prior versions. A failed or missing refresh remains visible; it does not imply
freshness. Source freshness is based on the most recent successful fetch.

Structured belief revisions are append-only and include claim/domain, support and
contradiction references, confidence, applicable regime, timestamps, source
provenance, sample size, validation state, and optional expiry. `documented_fact`,
`hypothesis`, and `empirically_supported` are distinct states. Protocol documentation
alone cannot produce an empirically supported belief or an execution decision.
Forecasts, observations, matured labels, paper outcomes, and financial policy remain
in their existing stores and systems; this layer does not rewrite those records.

The read-only Home API at `/api/knowledge` reports source freshness, retrieval
citations, and the latest belief revisions. Knowledge never grants tools, wallets,
payments, or signing authority; financial actions remain behind the deterministic
policy and execution path.
