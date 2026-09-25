# NOEMA Hardening

NOEMA's operating priority is:

```text
SURVIVE -> VERIFY EDGE -> PROFIT -> SCALE
```

Profit is never allowed to bypass survival controls.

## Kalshi-specific safeguards

### Primary account by default

NOEMA uses subaccount `0` by default so account activity remains visible in the normal Kalshi account experience. Numbered Kalshi subaccounts are API-only today and should be introduced only if isolation benefits outweigh reduced UI visibility.

### Account mirror

Authenticated account state should reconcile:

- balance / portfolio value
- positions
- resting/executed/canceled orders
- fills
- API usage tier / limits
- Kalshi's user-data validation timestamp

Kalshi documents a short delay between exchange events and some REST portfolio views. NOEMA therefore uses WebSocket events for immediacy and the user-data timestamp to decide whether REST state is fresh enough for risk decisions.

### 429 handling

Kalshi uses token-bucket rate limits and does not currently return Retry-After headers on 429. NOEMA applies exponential backoff and should prefer WebSocket streams over aggressive REST polling.

### Order groups

Before live automation, NOEMA should create a dedicated Kalshi order group and attach every autonomous event-market order to it. Order groups are exchange-enforced rolling 15-second contract limits and cancel all resting orders when triggered. This is an additional venue-side guard, not a replacement for NOEMA's own risk engine.

## Capital guardian

Default hard stops:

- 8% peak-to-trough equity drawdown
- 3% daily equity loss
- 10% maximum aggregate open exposure
- 2% maximum single-market exposure
- 50% minimum cash reserve
- five consecutive losses
- portfolio/account data older than 15 seconds

These are conservative research defaults, not claims of optimal sizing.

## Strategy quarantine

A specialist is not allowed to allocate capital merely because it is profitable over a short window.

Default promotion requires:

- at least 100 resolved out-of-sample forecasts
- positive performance after costs
- lower Brier score than the market-price baseline
- drawdown below the strategy cap

A narrow edge remains throttled.

## Next live-trading hardening

Before production execution:

1. account reconciliation tests against Kalshi demo;
2. exchange pause / maintenance handling;
3. exchange-enforced order group creation and attachment;
4. order / fill idempotency tests;
5. cancel-all emergency path;
6. WebSocket fills/orders reconciliation;
7. stale-book and crossed-book detection;
8. execution slippage measurement;
9. daily risk reset with explicit timezone;
10. manual master halt controlled outside the model.
