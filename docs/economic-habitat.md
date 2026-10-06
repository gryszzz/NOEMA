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


## Runtime-backed workstations

Canonical non-agent entities now receive presentation-only workstation archetypes. A provider can
look like a cognition terminal, a market like a market console, a mission like a command table,
a wallet like a treasury vault, the deterministic execution gateway like gateway hardware, and
research/evidence records like benches or archives.

The archetype is **not** an operational claim. Its stroke/status still comes from the canonical
runtime state. A rendered gateway does not imply execution authority; a rendered wallet does not
imply signing; a rendered provider does not imply reachability; a rendered market console does
not imply executable liquidity.


## Mission occupancy and recorded outputs

Active mission occupancy is derived from persisted coordination relationships, not free-running
character animation. A specialist can relocate from the Agent Colony to an active mission table
only when the graph contains an exact `assigned specialist` relationship for a mission whose
state is `claimed`, `running`, or `waiting`. A handoff recipient can join that mission only
while a persisted handoff is in `requested`, `accepted`, or `running` state.

Habitat zones brighten only when they contain an entity in the persisted lineage of an active
mission. This may include Mission Control, the Agent Colony, and any linked session, experiment,
tool, or evidence station. A healthy provider or visible wallet alone does not make a room
"active."

A deliverable/archive object is created only when a mission is `passed` or `completed` and its
persisted `result_json` explicitly records a non-false `deliverable_produced` value. The object
records whether `delivery_tested=true` was also observed. It never implies a downloadable file,
published artifact, customer delivery, revenue, or economic value unless separate evidence exists.


## Phase 3: Autonomous Desk

The Autonomous Desk turns NOEMA's existing specialist registry, mission assignments, handoffs, and
deterministic gateway into a bounded desk-style workflow surface.

The desk uses eight presentation roles:

1. **Chief** — the persistent NOEMA identity; coordination is shown separately from execution.
2. **Scout** — discovery / scan / trench-like specialists when persisted metadata supports the role.
3. **Map / Context** — world-intelligence or context specialists when such an entity is actually registered.
4. **Vet** — evidence critics, validators, or quality reviewers.
5. **Odds** — forecast, quant, probability, or prediction specialists.
6. **Size** — allocation / budget / capital-sizing specialists.
7. **Execution** — always the deterministic execution-gateway projection, never an inferred agent.
8. **Risk / Exit** — risk or exit specialists when registered.

Specialist-to-seat mapping is presentation-only and is derived from persisted specialist
name/family/capability metadata. A seat with no defensible match remains **VACANT / UNBOUND**.
Binding a seat never creates a capability, changes a specialist's mission, enables a wallet,
changes execution policy, or grants order authority.

The Active Work Packets view is built only from persisted active missions, exact assigned-specialist
links, and persisted handoffs. The Shift Tape is a bounded newest-first view of the canonical event
timeline. Historical replay therefore changes the desk using the same time-honest world model.

This design is inspired by the general operating principle that a multi-agent system is easier to
audit when each role has one bounded responsibility and work is explicitly handed to the next role.
Reported performance claims from external examples are not treated as evidence for NOEMA's expected
returns or strategy quality.
