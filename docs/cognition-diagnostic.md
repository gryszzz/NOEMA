# Owner-triggered cognition diagnostic

The hosted worker accepts a one-shot owner trigger through
`NOEMA_COGNITION_DIAGNOSTIC_TRIGGER_ID`. Set it to a new unique ID on the
worker to request one diagnostic from persisted market/research snapshots. The
worker claims the ID in its durable SQLite database before any model request;
restarts and repeated cycles cannot replay a claimed ID. A failed or blocked
trigger is also consumed, so a retry requires a new owner-chosen ID and must
pass the same budget checks.

The diagnostic is separate from automatic cognition qualification. It does not
change edge, freshness, uncertainty, attention, or research thresholds. Its
request is constrained to persisted observations, `gpt-5.6-luna`, and a
structured `observe_only` result. It cannot call tools or create orders,
trades, transfers, wallet signatures, or execution authority. It still shares
the persisted model reservations and is capped at one request per hour, the
configured daily cognition spend (never above USD 0.01), the remaining monthly
model budget, and at most 1,000 output tokens.

After the result is durably written, clear the trigger variable in the worker
environment. The durable trigger record remains in `cognition_diagnostics`
with its prompt-context summary and hash, result, subsystem, token usage,
estimated cost, budget remaining, execution classification, and completion
status. Logs contain the returned diagnostic result and safe accounting
metadata, but never the API key or request authorization headers.

The automatic cognition policy continues to use its existing production gates.
The diagnostic does not change those gates or enable financial execution.
