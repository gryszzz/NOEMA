# NOEMA: the real life ladder

Run `noema ladder` or open NOEMA OPS to see observed state. Each rung reports
evidence and a concrete next action. A configured secret is never reported as a
successful connection until a cycle actually checks it.

1. **Observe:** `noema agent-once` records public market snapshots and PASS-only
   market-price baselines. No Kalshi key or wallet is needed. The host must reach
   the public Kalshi API. If data is invalid or unavailable, this rung remains pending.
2. **Stay online:** `noema-agent` runs recurring cycles. `noema-dashboard` opens
   the local console at `http://127.0.0.1:8787`. The online indicator requires a
   recent heartbeat; a past one-shot run does not qualify.
3. **Forecast and score:** Baselines create an auditable benchmark. `noema
   sync-outcomes` and `noema evaluate` score them when contracts resolve. The
   ladder shows this rung as observed only when a forecast has a later settlement.
4. **Independent forecast:** After at least 30 previously seen, resolved,
   homogeneous one-market events or complementary two-market events in a series,
   NOEMA records an exploratory frequency
   forecast based on YES/NO outcomes rather than the current market quote.
   `noema compare` scores the earliest candidate for each settled market against
   its baseline from the exact same snapshot. No forecast, no pair, or a negative
   Brier improvement is an honest result. All such forecasts PASS.
5. **Model research:** `noema setup` optionally configures a Foundry deployment.
   The model only reviews eligible independent research rows under call and token
   budgets; baseline forecasts do not qualify. It cannot set stake size.
6. **Account and wallet observation:** Setup can add Kalshi read-only account
   telemetry and a dedicated EVM *public address*. Keep seed phrases and private
   wallet keys out of NOEMA and the repository.
7. **Paper evaluation:** Collect many independent, later-resolved markets,
   compare to the market baseline over time, and measure costs and drawdown.
   Repeated observations of one market count once in the comparison.
8. **Live capital:** Locked in the ladder. It needs a separate review of real
   out-of-sample performance, venue rules, execution behavior, and bounded risk.

The dashboard binds to localhost by default. Running the dashboard publicly
requires access controls because its account telemetry is sensitive.

The persistent agent syncs up to 2000 outcomes every 15 minutes by default.
For one-shot testing, first run `noema sync-outcomes --limit 2000`; history
arriving after a snapshot is never allowed to influence that earlier forecast.
