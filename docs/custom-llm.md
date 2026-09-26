# NOEMA's custom language model

NOEMA has two different model jobs. The probability engine must produce
auditable, calibrated forecasts and beat market-price baselines on later
resolved events. The language model reads verified evidence, challenges a
thesis, identifies missing facts, and requests research. Its prose and
confidence do not authorize an order, set risk, or become a market probability.

## Build sequence

1. **Research analyst now:** Connect a capable hosted model through the
   existing Foundry Responses integration. The model receives bounded market
   telemetry and verified evidence summaries; the full raw settlement sample
   and any wallet/account secrets stay out of prompts. Calls and tokens are
   capped. Keep the deployment and API key in the local environment only.
2. **Evaluation set:** Have a reviewer approve research packets for distinct,
   time-stamped events. Include adversarial cases: missing or conflicting
   evidence, misleading market titles, stale quotes, incomplete event groups,
   and apparent edges erased by spread and fees. Keep later events entirely
   outside the training set. Measure unsupported factual claims, valid evidence
   references, appropriate abstention, research usefulness, latency and cost.
3. **Own weights:** Once enough reviewed examples exist, compare a small
   open-weight model adapted with LoRA against the hosted model on untouched
   events. Rent GPU time only for the training job; keep the adapter and base
   license/version pinned. A Foundry fine-tune is another option after checking
   its current model availability and hosting bill. Do not train on generated
   text as if it were a verified label.
4. **Promotion:** Only replace the hosted reviewer if the custom model passes
   the same evidence tests and improves quality or cost on the held-out set.
   Neither reviewer may bypass NOEMA's deterministic risk rules. Trading
   eligibility is evaluated separately using resolved forecast performance.

`noema model-audit` evaluates the distinct statistical forecast blend, not the
language model. The LLM research path currently has no Foundry credentials in
the checked environment and no approved fine-tuning dataset. The first step
that needs money is chosen only after a concrete model, labeled dataset,
evaluation result, and spending limit are reviewed.
