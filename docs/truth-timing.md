# NOEMA Truth + Timing Layer

This layer exists to make NOEMA harder to hallucinate, harder to fool with stale data, and easier to audit after the fact.

It does not guarantee profit or perfect timing.

## Truth before intelligence

A forecast may reference evidence IDs. Evidence records contain:

- source;
- source type;
- observation timestamp;
- retrieval timestamp;
- canonical payload hash;
- immutable payload JSON.

A grounded forecast can be rejected when:

- evidence is missing;
- the payload hash no longer matches;
- evidence is stale;
- evidence is future-dated;
- the source type is not permitted.

## AI reasoning packets

If an LLM is used to interpret unstructured evidence, each factual claim must carry explicit evidence IDs.

A reasoning packet is invalid when a factual claim:

- has no evidence IDs;
- references evidence that does not exist;
- references evidence whose integrity check fails.

Unknowns are first-class. The model is allowed to say it does not know.

## Raw realtime journal

Every captured WebSocket frame is timestamped on receipt and stored append-only with a payload hash.

This enables later reconstruction of:

- what NOEMA actually knew at a given moment;
- whether the feed was stale;
- whether a claimed timing edge existed before the outcome;
- whether reconnects or gaps damaged data quality.

## Sequence-aware order books

Incremental books must start from a snapshot.

A missing sequence number marks the local book unsynchronized. NOEMA must stop using that book until a fresh snapshot restores state.

Binary-book asks are derived from the opposite side's bids:

```text
YES ask = 1 - best NO bid
NO ask  = 1 - best YES bid
```

This follows Kalshi's binary-book mechanics.

## Timing gate

Timing eligibility requires:

- synchronized order book;
- fresh market event;
- acceptable feed latency;
- acceptable processing latency;
- acceptable spread;
- edge persistence for a minimum duration.

A single-tick apparent edge is not considered stable evidence.

## Entry-quality research

The research score combines:

- after-cost edge;
- spread;
- latency;
- edge persistence;
- order-book depth stability;
- short-horizon price-shock stability.

The score is for retrospective/paper research. It is not a guarantee that an order will fill or make money.

## Source authority

Source types are deliberately unequal.

Official exchange, official resolution, primary/government, licensed, and reputable secondary sources receive different admissibility.

Unknown sources carry zero authority by default.

## Latency

NOEMA measures separately:

- event -> receipt latency;
- receipt -> processed latency;
- event -> processed latency.

Timing claims should be evaluated against recorded latency distributions, not assumed network speed.

## Kalshi realtime design

Kalshi's current API documentation exposes:

- authenticated WebSocket connections;
- incremental order-book updates;
- market ticker updates;
- public trades;
- user fills;
- user orders;
- market positions;
- market/event lifecycle updates;
- queue-position REST endpoints;
- user-data freshness timestamps.

NOEMA should reconcile request responses, WebSocket state, and portfolio state instead of trusting any single view blindly.

## Standard

The timing system should prefer:

```text
MISS A TRADE
```

over:

```text
ACT ON UNKNOWN OR STALE STATE
```

The system is considered better when it passes more bad opportunities, not when it trades more often.
