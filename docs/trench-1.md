# NOEMA Trench-1

Trench-1 is NOEMA's first deliberately narrow crypto research specialty:

> **Early Solana token intelligence: learn which newly tradeable tokens survive, which are
> manipulated or structurally unsafe, and which early trajectories deserve further research.**

It is not a promise of profitability and it does not place live trades.

## Why start here

Current evidence supports a narrow, data-heavy approach instead of a generic "AI crypto bot":

- **Catching the Rug (2026)** studies 6.4M Solana tokens and reports that classic ML models can
  detect rug-like behavior using only the first five minutes of trading data:
  https://arxiv.org/abs/2608.20271
- **SolRugDetector (2026)** finds Solana rug behavior is expressed heavily through on-chain
  market manipulation and organized transaction/state patterns rather than only contract code:
  https://arxiv.org/abs/2603.24625
- **A Midsummer Meme's Dream** finds widespread artificial-growth behavior among high-return
  memecoins, including wash trading and liquidity-pool-based price inflation:
  https://arxiv.org/abs/2507.01963
- A 2026 autonomous Solana memecoin paper-trading study shows why tail dependence must be
  measured: removing its top three trades flipped the reported strategy unprofitable:
  https://arxiv.org/abs/2606.08232

The design response is:

1. collect early-life data;
2. classify survival/manipulation risk;
3. rank opportunities separately from safety;
4. save every decision;
5. follow rejected candidates forward;
6. promote only after leakage-aware, multiple-testing-aware evaluation.

## Data surface

### Jupiter

Trench-1 can read:

- `/tokens/v2/recent` for newly tradeable tokens;
- `/tokens/v2/toporganicscore/5m` for current organic-activity ranking.

Jupiter documents that "recent" refers to first pool creation, not mint creation. That makes it a
useful tradeability clock. Jupiter also warns that very fresh organic scores are volatile and
should be treated cautiously.

Current platform docs:

- https://developers.jup.ag/
- https://developers.jup.ag/blog/what-is-organic-score

### Solana RPC

The read-only research client uses:

- `getTokenSupply`;
- `getTokenLargestAccounts`.

These let NOEMA normalize top-account balances by total supply instead of treating raw balances
as concentration.

Current RPC docs:

- https://solana.com/docs/rpc/http/gettokensupply
- https://solana.com/docs/rpc/http/gettokenlargestaccounts

### Token controls

Token authority and Token-2022 extension state must eventually be collected from parsed mint
state. The Trench-1 model already has fields for:

- mint authority;
- freeze authority;
- permanent delegate;
- transfer hook;
- transfer fee.

Solana documents that retained freeze authority can prevent token-account transfers and that
Token-2022 can add transfer fees and other extensions. These are risk inputs, not automatic
proof of fraud.

- https://solana.com/docs/tokens/basics/set-authority
- https://solana.com/docs/tokens/extensions

## Feature contract

`extract_trench_features` currently produces an auditable early-life vector:

- price return;
- peak-to-trough drawdown;
- liquidity growth;
- signed buy/sell flow imbalance;
- unique-buyer growth;
- buyer acceleration;
- buyer/seller participation balance;
- top-holder and top-five concentration;
- holder HHI;
- creator supply share;
- Jupiter organic score when available;
- upstream wash-trade probability when available.

No single feature is treated as "alpha."

## Survival before opportunity

`assess_trench_candidate` produces two separate numbers:

- **survival risk** — structural/manipulation danger;
- **opportunity score** — early activity deserving research attention.

The result is one of:

- `quarantine`;
- `observe`;
- `research_candidate`.

It intentionally does **not** return BUY/SELL.

The default thresholds are research priors only. They must be replaced or calibrated from
forward data before they can influence autonomous capital.

## Counterfactual rejection tracking

Filtering only tells half the story.

`TrenchResearchStore` records every candidate and later outcomes at explicit horizons. For
quarantined tokens, NOEMA can measure both:

- avoided deep drawdowns;
- missed large winners.

A filter that avoids rugs but also deletes every right-tail winner is not useful.

Suggested initial horizons:

`5m -> 15m -> 1h -> 6h -> 24h`

These are dataset labels, not holding-period recommendations.

## Research-trial memory

`ResearchTrialStore` hashes the complete hypothesis + parameter set + feature version.

Trying:

- buyer growth > 1.5;
- buyer growth > 2.0;
- buyer growth > 3.0;

counts as three research trials rather than one vague idea. Failed variants remain in the
database.

This is needed because searching many strategies creates selection bias.

## Leakage and overfit controls

Trench-1 includes:

- label-overlap-aware purged expanding walk-forward folds;
- Probabilistic Sharpe Ratio (PSR) diagnostic;
- combinatorial symmetric cross-validation Probability of Backtest Overfitting (PBO).

These complement NOEMA's existing research guard.

References:

- Bailey & Lopez de Prado, Deflated Sharpe Ratio:
  https://doi.org/10.3905/jpm.2014.40.5.094
- `purgedcv` 2026 paper/implementation overview:
  https://github.com/eslazarev/purged-cross-validation

The goal is not to celebrate the best backtest. It is to estimate how likely the research process
is to have selected a lucky configuration.

## Continuous collector

Trench-1 now has a persistent, read-only launch collector that can run inside the normal NOEMA
agent process.

Enable it explicitly:

```text
NOEMA_TRENCH_ENABLED=1
NOEMA_JUPITER_API_KEY=...
NOEMA_SOLANA_RPC_URL=https://...
```

The API key is optional for Jupiter keyless prototyping, but the default request pause is
conservative for the current 0.5 RPS keyless tier. Configure the pause for the rate limit of the
connected Jupiter plan.

Operator commands:

```bash
noema trench-once
noema trench-show
```

The Ops API exposes `GET /api/trench`.

### Time-honest horizon schedule

Default target ages from first pool creation:

```text
30s -> 1m -> 2m -> 5m -> 15m -> 1h -> 6h -> 24h
```

A target that is already too stale is recorded as `missed`. NOEMA does not backfill a five-minute
state and pretend it was a 30-second observation.

Each collection attempt is separate from successful immutable observations, so API/RPC failures
remain missing data rather than becoming fake zeroes.

Only one target horizon per token is collected in a cycle. The five-minute snapshot is the first
fixed assessment point. Later launch-age observations are attached as counterfactual outcomes
against that frozen five-minute reference.

### Jupiter field semantics

The collector preserves provider semantics rather than inventing features:

- `numOrganicBuyers` is stored as organic net buyers, not generic unique buyers;
- `numTraders` remains total traders;
- regular buy/sell volume and organic buy/sell volume remain distinct;
- `organicScore` is stored raw;
- absent token-control audit fields remain unknown;
- an upstream suspicious flag is preserved;
- Token API price/liquidity unavailability causes a failed/missing snapshot, never a zero price.

For launches younger than 24 hours, `stats24h` is used as a life-to-date approximation because
their first pool did not exist before the rolling window. Raw Jupiter payloads are saved alongside
the normalized observation for later replay.

## Initial collection protocol

A practical first dataset should snapshot a token repeatedly from first tradeability.

For example:

`30s, 1m, 2m, 5m, 15m, 1h`

At each observation, save the immutable raw response alongside derived features. Later outcome
labels must be added only after their horizon expires.

The first supervised task should be **survival/risk classification**, not return maximization.

Candidate labels can include:

- liquidity collapse;
- >=50% drawdown;
- extreme holder concentration increase;
- transfer/sell restriction appearing;
- creator-linked exit;
- survival to horizon.

Only after that classifier has forward evidence should the lab train a separate right-tail
opportunity model.

## Future model stack

A sensible progression is:

1. deterministic safety gates;
2. logistic/tree baseline;
3. gradient-boosted survival classifier;
4. right-tail opportunity classifier/regressor;
5. calibrated ensemble;
6. wallet-cluster specialist;
7. microstructure specialist;
8. selective LLM researcher for unusual evidence.

The LLM should not replace the numerical classifier.

## Future execution path

Live execution remains outside Trench-1.

If it is eventually earned, use the existing NOEMA wallet-policy boundary and add, in order:

1. current Jupiter quote/order integration;
2. transaction simulation;
3. price-impact + route sanity checks;
4. bounded programmable signer;
5. MEV-aware submission;
6. post-fill reconciliation;
7. per-strategy realized-cost attribution.

Jupiter's 2026 platform exposes Swap V2 plus optimized transaction submission/MEV protection,
while Jito documents protected low-latency transaction and bundle submission.

- https://developers.jup.ag/
- https://docs.jito.wtf/

## Manipulation boundary

NOEMA may detect and model manipulation. It must not create it.

Trench-1 must never:

- wash trade;
- generate fake volume;
- coordinate pumps;
- fabricate social interest;
- spoof liquidity;
- create deceptive tokens;
- dump promoted tokens onto other participants.

Its edge must come from observation, forecasting, filtering, and execution quality.

## Success standard

The initial Trench-1 loop is:

```text
DISCOVER
  -> SNAPSHOT
  -> FEATURE
  -> SURVIVAL SCREEN
  -> RESEARCH RANK
  -> RECORD DECISION
  -> FOLLOW FORWARD
  -> LABEL OUTCOME
  -> PURGED WALK-FORWARD EVALUATION
  -> OVERFIT AUDIT
  -> CALIBRATION
  -> KEEP / REVISE / KILL
```

Trench-1 becomes eligible for a later paper-execution stage only when the forward evidence says
the specialty adds information beyond transparent baselines after realistic execution costs.
