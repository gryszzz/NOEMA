# Hosted cognition billing and cost attribution

Hosted cognition is opt-in and budget-gated. An OpenAI API key, an available
provider credit balance, or a selected model does not grant NOEMA a spending
budget. Missing configuration must continue to block hosted calls.

## Render configuration

The worker's `render.yaml` declares the following as `sync: false`. These are
owner-supplied Render service environment values, not values inferred from the
OpenAI account, Render plan, local `.env`, or model choice:

| Variable | Meaning |
| --- | --- |
| `NOEMA_HOSTED_BILL_BUDGET_HOSTING_USD` | Owner-entered monthly hosting budget used by NOEMA's bill ledger. |
| `NOEMA_HOSTED_BILL_BUDGET_OTHER_USD` | Owner-entered monthly non-hosting expense ceiling; model spend is included here. |
| `NOEMA_HOSTED_BILL_BUDGET_MODEL_USD` | Monthly ceiling for model usage; must be no greater than `OTHER_USD`. |
| `NOEMA_HOSTED_BILL_BUDGET_OWNER_LIMIT_USD` | Owner-authorized monthly aggregate spending limit. |

Values must be finite, non-negative USD with at most two decimal places. The
hosted worker initializes the durable `bill_budget` row only when all four
values are present and valid and the database has no row yet. Bootstrap is
insert-only: an existing persisted operator budget takes precedence over
environment changes. To change an existing budget, use the existing explicit
operator budget-configuration path; do not expect a redeploy to silently replace
it.

`hosted_bill_budget_bootstrap` reports each setting as `missing`, `blank`,
`invalid`, or `valid` without logging its amount, and separately reports whether
a persisted budget is retained. A ready hosted budget requires the durable
budget check to pass; the presence of the four environment variables alone is
not proof of readiness.

## OpenAI configuration and gates

The OpenAI Responses API path additionally requires `OPENAI_API_KEY`,
`NOEMA_OPENAI_ENABLED=1`, `NOEMA_COGNITION_ENABLED=1`, a configured model, a
matching `NOEMA_OPENAI_PRICING_MODEL`, and both model-rate variables:

- `NOEMA_OPENAI_INPUT_USD_PER_MILLION`
- `NOEMA_OPENAI_OUTPUT_USD_PER_MILLION`

The per-call upper-bound reservation uses the configured model rates, UTF-8
input bytes as a conservative input-token proxy, and the configured maximum
output tokens. The request is rejected if price/model configuration is
missing, the persisted monthly budget is unavailable or exceeded, or a daily,
hourly-call, hourly-token, freshness, evidence, uncertainty, or cooldown gate
fails. No API credit balance is treated as authorization.

For a GPT-5.6 Luna test, the operator must explicitly approve the monthly model
budget and daily/call limits. Keep the first test to one naturally eligible
request with a small output cap. Idle work with no eligible research candidates
is deterministic and does not invoke OpenAI. Stale or below-threshold evidence
is rejected by the existing cognition policy.

## Per-call attribution

OpenAI requests originate from the `market_cognition` and `research_allocator`
subsystems. Each request emits safe lifecycle metadata (subsystem, model,
decision ID, HTTP outcome, and trace metadata); prompts, request bodies,
authorization headers, API keys, and signatures are excluded. For successful
Responses API replies, usage and cost are persisted idempotently by response ID
to the economic ledger before output parsing. Cost is calculated from the
configured rates and reported as an estimate; input is charged at the uncached
rate as a conservative upper bound. Provider invoices remain authoritative for
actual billing. Failed/ambiguous requests retain their pre-call budget
reservation, preventing retries from bypassing the cap.

The logged `remaining_model_budget_usd` is the persisted monthly model cap less
all conservative model reservations for the current UTC month. It is not the
OpenAI account credit balance. Calls outside NOEMA are not visible to this
ledger; provider-side usage reports are needed to reconcile account-wide
credits.

Hosted execution authority is independent of cognition billing. This
configuration does not enable live orders, wallet signing, transfers, or
execution.
