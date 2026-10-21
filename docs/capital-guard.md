# Capital guard

The capital guard is NOEMA's last line of defence for real money. It applies to
every live path: Kalshi, Polymarket US, Solana (Jupiter) and EVM (KyberSwap).
It fails closed: with no fresh equity snapshot, every live order is refused.

| Protection | Default | Setting |
|---|---|---|
| Rising capital floor | 75% of peak equity, never decreases | `NOEMA_CAPITAL_FLOOR_FRACTION` (clamped 0.50–0.95) |
| Daily loss stop | 10% below the day's opening equity | `NOEMA_DAILY_LOSS_STOP_FRACTION` (clamped 0.01–0.10) |
| Equity freshness | snapshot older than 15 min blocks trading | `NOEMA_EQUITY_MAX_AGE_SECONDS` |
| Trip latch | floor or daily-stop breach halts until owner reset | `noema capital-guard-reset --confirm "I ACCEPT THE LOSS"` |

Deposits raise the peak and the floor (`noema capital-guard-record --equity X --deposit D`).
New equity highs ratchet the floor up: profit is locked in, never given back below 75% of peak.

## Edge-scaled sizing

Per-trade risk is a share of current equity, capped by **both** the autonomy
ladder and the agent's measured accuracy:

| Tier | Max per trade | Accuracy proof required |
|---|---|---|
| MICRO / PROVEN | 3% | none / ≥30 resolved forecasts, Brier ≤ 0.22, EV after cost > 0 |
| SELF_FUNDED | 5% | ≥100 resolved, Brier ≤ 0.20 and better than the market |
| EXPANSION | 8% | ≥200 resolved, Brier ≤ 0.18 and better than the market |

Within a tier, quarter-Kelly sizes each pick (half that for agents not yet
calibrated), so strong, high-confidence picks use the top of the range and weak
ones far less. No trade may exceed the headroom above the floor or the remaining
daily loss budget.

## Starter token allowlist (proposed, not live)

Until a memecoin desk proves itself in paper, live swaps should be limited to:

* Solana: SOL ⇄ USDC (`EPjFWdd5AufqSSqeM2qN1xzybkapC8G4wKGGkZwyTDt1v`)
* Base (8453): WETH (`0x4200000000000000000000000000000000000006`) ⇄ USDC (`0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`)

No memecoins until a desk has 30+ resolved paper trades with positive EV after costs.
