# NOEMA Agent Ecosystem

NOEMA is an ecosystem of specialist research agents coordinated by a persistent runtime and a
deterministic economic/risk boundary.

The ecosystem kernel answers a different question from a trading strategy:

> **Which specialist deserves NOEMA's limited research attention right now?**

It allocates research attention only. It does not move capital, sign transactions, or override
wallet/autonomy policy.

## Current ecosystem

```text
                         NOEMA RUNTIME
                              |
        +---------------------+---------------------+
        |                     |                     |
  Kalshi specialist       Trench-1            Future specialists
  paper research          shadow research      start in shadow
        |                     |                     |
        +---------------------+---------------------+
                              |
                    ECOSYSTEM ALLOCATOR
                              |
              +---------------+---------------+
              |                               |
        evidence-weighted                bounded exploration
          exploitation                    for new ideas
              |                               |
              +---------------+---------------+
                              |
                      family concentration cap
                              |
                      idle / collect / research
                              |
                         AGENT PLANNER
                              |
                   persistent heartbeat/status
```

## Specialist states

The registry uses the existing specialist lifecycle:

- `shadow` — new specialty; limited exploration only;
- `paper` — has a paper research loop but no live authority;
- `active_research` — evidence supports more research attention;
- `quarantined` — receives zero automatic attention.

State is persistent in SQLite. Runtime bootstrap never overwrites an existing state.

## Attention allocation

Established specialists compete using NOEMA's existing
`specialist_attention_multiplier`, which incorporates:

- resolved sample size;
- reliability;
- calibration error;
- drawdown.

Shadow specialists receive a separate exploration pool.

Default policy:

```text
15% bounded exploration pool
65% maximum attention to any one specialist family
remaining unsupported capacity may stay idle
```

Idle capacity is intentional. NOEMA should not invent work merely to reach 100% utilization.

## Why cap families

Five agents built on the same data/strategy family are not five independent edges.

The family cap prevents one family from consuming the entire research loop merely because it has
more agents or variants. Excess attention is left idle rather than force-allocated to weaker
work.

## Persistent audit

`EcosystemStore` records:

- specialist name/family/state;
- resolved observations;
- reliability;
- calibration error;
- after-cost return;
- drawdown;
- ecosystem attention plans.

Identical consecutive plans are deduplicated.

This makes attention allocation auditable and gives later versions a history of why one specialty
received more research than another.

## Runtime integration

Each normal agent cycle now:

1. runs the existing market perception;
2. reads the Economic OS;
3. reviews the specialist ecosystem;
4. selects the dominant research focus;
5. passes that focus into the goal planner;
6. records ecosystem state/focus in the heartbeat.

Urgent perception repair and high-attention validated market research still outrank the generic
ecosystem focus.

## What this does not yet do

The kernel does **not** yet automatically:

- update specialist evidence from Trench/Kalshi outcomes;
- schedule Trench-1 network collection;
- spawn new strategy variants;
- retire a specialist from measured decay;
- decide research/API/compute purchases;
- transfer strategy capital;
- execute a swap;
- change wallet limits.

Those are intentionally separate promotion steps.

## Closed-loop evolution

The outcome-to-specialist loop is now implemented for the default ecosystem.

```text
specialist observation
    -> immutable predictions / research decisions
    -> later outcomes
    -> calibration + after-cost evidence
    -> specialist evolution review
    -> registry update
    -> ecosystem attention reallocation
    -> bounded challenger experiments
```

Kalshi history has a forward-evidence adapter today. Trench-1 advances from counterfactual label
maturity until its first trained survival model exists. Reviews are event-driven: unchanged
evidence cannot advance a streak.

See [Specialist evolution](specialist-evolution.md).

The next milestone is continuous Trench-1 trajectory collection and survival-model training,
followed later by resource-proposal integration with the deterministic Economic OS.

## Standard

NOEMA should become more autonomous by becoming **more accountable to evidence**.

The ecosystem is successful when it can:

- discover a new specialty;
- allocate a small exploration budget;
- learn from forward outcomes;
- increase attention when evidence improves;
- throttle or quarantine decaying work;
- keep unrelated specialists independent;
- justify paid data/model/compute requests;
- leave resources unused when no proposal clears the evidence threshold.

Only later, and through the existing earned-autonomy/wallet gates, can proven economic authority
expand.
