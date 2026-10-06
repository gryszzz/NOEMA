# NOEMA Economic Habitat

The Economic Habitat is the spatial projection layer for NOEMA's operational console.

It does **not** invent agent activity, balances, missions, authority, capabilities, or economic
results. The same canonical nodes and relationships used by the existing topology remain the
source of truth; the habitat only assigns those entities to stable functional zones so the
operator can understand the system as a living environment.

## Functional zones

| Zone | Canonical entities |
| --- | --- |
| Cognition Array | model/data providers and prediction venues |
| Agent Colony | NOEMA specialists and recorded handoff participants |
| Mission Control | persisted missions |
| Market Deck | recorded markets and immutable forecasts |
| Evidence Vault | evidence, sessions, experiments, and lessons |
| Treasury & Gateway | wallets and runtime/policy tools |

NOEMA itself remains the central persistent identity. A visible connection must still come from
a configured provider route, observed venue/wallet state, persisted registration, recorded tool
invocation, mission/session/experiment relationship, immutable forecast relationship, or
persisted specialist handoff.

Historical replay continues to hide current-only provider, venue, wallet, and gateway state.

## Design reference boundary

The habitat direction is informed by the general idea of spatial, runtime-backed agent interfaces
such as StarNet (https://github.com/androoAGI/starnet), but this first implementation is
NOEMA-native and does not import StarNet artwork, sprites, logos, brand identity, or source files.

If future work copies or adapts substantial MIT-licensed StarNet source code, preserve the
upstream copyright/license notice for that copied code. Do not import StarNet brand assets; its
repository explicitly reserves the StarNet name, logo, station artwork, and sprites.

## Contract

1. **Runtime first.** Visual state follows canonical NOEMA records/checks.
2. **Unknown stays unknown.** Layout must never upgrade missing state into a positive claim.
3. **Authority stays separate.** Seeing a wallet, venue, tool, or path never grants execution.
4. **Handoffs are literal.** Specialist-to-specialist paths are rendered only from persisted handoffs.
5. **Replay is time-honest.** Later state cannot leak into an earlier replay frame.
6. **The habitat is replaceable.** Spatial layout is pure and separate from the runtime model.


## Living inhabitants

Phase 2 projects NOEMA specialists as inhabitants rather than generic graph points. Their activity
badges are evidence labels, not animation state:

- **active work** requires a canonical active specialist/mission state,
- **receiving/sending handoff** requires a recent persisted specialist handoff,
- **degraded** follows recorded degraded/offline/quarantined state,
- **past activity only** means a persisted event exists but no current work is proven,
- **unknown** means NOEMA has no evidence for a current activity claim.

Recent persisted handoffs may render a single one-way transfer marker for 90 seconds from the
recorded handoff timestamp. The marker never loops, does not appear for unrelated links, and is
disabled as live motion during historical replay.
