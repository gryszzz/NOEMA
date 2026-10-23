# Pre-registration: Kalshi maker (resting limit) variants of the pm_models shadow agents

Registered 2026-10-22 22:02 ET (2026-10-23 02:02 UTC), **before any maker P&L was computed**.
What had been seen at registration: the taker leaderboard (`pm_leaderboard` at 2026-10-22T20:14Z — best
shadow agents roughly −0.5¢/contract after the taker fee, CIs spanning 0) and data *availability* counts
(which tables exist, row counts, date ranges). No fill or P&L numbers for any maker rule had been computed.
Paper/research only. Nothing here places orders or touches live flags.

## Question
The shadow agents fail mostly on cost: they pay the Kalshi taker fee `0.07·P·(1−P)` (≤1.75¢ at 50¢)
and cross the spread. Does resting a limit order at the bid (or 1¢ inside it) — paying the maker fee
instead — make any of them profitable after costs, once only fills the tape proves are counted and
adverse selection is charged?

## Fees (checked 2026-10-22 against kalshi.com/fee-schedule and the fee-schedule PDF, effective 2026-07-07)
- Taker: `round_up(M · 0.07 · C · P · (1−P))`.
- Maker: `round_up(M · 0.0175 · C · P · (1−P))`, charged only when a resting order fills. M is 0 for most
  series (no maker fee) and 1 for series listed with maker fees.
- **Primary analysis charges the maker fee on every fill (M = 1), rounded up to the centicent per 1-lot**,
  i.e. an upper bound regardless of each series' current multiplier. M = 0 is a reported sensitivity only.

## Agents and variant count (frozen)
The 16 non-fired `pm_agents` genomes in `data/pm_train.db` at registration (walk-forward weights are refit per
test day from strictly earlier days, exactly as `pm_models.walk_forward`):

| agent | category | model | window_days | ridge |
|---|---|---|---|---|
| pm-crypto-market_cal-1eb3c0 | crypto | market_cal | 999 | 1.0 |
| pm-crypto-market_cal-25afbc | crypto | market_cal | 999 | 3.0 |
| pm-crypto-market_cal-40c622 | crypto | market_cal | 999 | 0.3 |
| pm-crypto-market_cal-4c72c6 | crypto | market_cal | 21 | 3.0 |
| pm-economic-market_cal-3fd698 | economics | market_cal | 999 | 1.0 |
| pm-markets-market_cal-359eb4 | markets | market_cal | 999 | 1.0 |
| pm-markets-stack-396545 | markets | stack | 30 | 3.0 |
| pm-markets-stack-4fb13f | markets | stack | 999 | 1.0 |
| pm-other-market_cal-a30995 | other | market_cal | 999 | 1.0 |
| pm-other-stack-248d77 | other | stack | 7 | 1.0 |
| pm-other-stack-51783d | other | stack | 999 | 1.0 |
| pm-other-stack-f65274 | other | stack | 14 | 3.0 |
| pm-weather-market_cal-3e1a7c | weather | market_cal | 999 | 1.0 |
| pm-weather-market_cal-52ac9d | weather | market_cal | 14 | 10.0 |
| pm-weather-market_cal-b2469c | weather | market_cal | 30 | 1.0 |
| pm-weather-stack-80285d | weather | stack | 999 | 1.0 |

Two price variants per agent → **32 tests**. No other variants are tested for the verdict.
- **JOIN**: buy YES with a resting bid at the snapshot best YES bid `L = bid`; or buy NO by resting a YES offer at
  the snapshot best ask `A = ask` (NO price `1 − ask`).
- **IMPROVE**: one tick inside: `L = bid + 0.01` / `A = ask − 0.01`, only if that is still strictly inside the spread
  (spread ≥ 2¢); otherwise no order.

## Decision rule (per agent, per market, one order max)
With agent probability `p` (out-of-sample walk-forward prediction at the snapshot):
- YES edge `p − L − maker_fee(L)`; NO edge `(1 − p) − (1 − A) − maker_fee(1 − A)`.
- Place the side with the larger edge if it is > 0. Prices must be in [0.01, 0.99].

## Fill model (trade-through only)
- **Placement time** = end of the snapshot quote's 1-minute candle (`floor(snap_at/60)·60 + 60s`) — the quote is not
  known earlier. **Cancel time** = close − 60 s. One contract (1-lot); hold to settlement.
- A resting YES bid at `L` is filled only if, strictly after placement and before cancel, a recorded trade prints at
  `yes_price < L`, or a recorded quote shows best ask `< L` (the book traded *through* our level).
  A resting YES offer at `A` (our NO buy) fills only on a trade at `yes_price > A` or a quote with best bid `> A`.
- **Queue position: we are assumed last in the queue at our price.** Trades *at* our price never fill us in the
  primary analysis (we do not know depth ahead). At-price fills are a reported optimistic sensitivity only.
- Evidence sources (all already recorded, read-only): `edge_avenues.db:mm_trades` (Kalshi public trade prints),
  `noema.db:fs_trades` (live trade capture), `noema.db:fs_quotes` (≈1-min BBO snapshots), `noema.db:fs_books`
  (top of book). An `mm_trades` ticker with 10,000 rows whose earliest print is after placement is treated as
  truncated and its trade evidence is not used for that window (quotes still are).
- Markets with no fill evidence at all for the window are "not evaluable", not "unfilled".

## Adverse selection
- Settlement P&L on filled contracts already carries adverse selection (we are only filled when price moves
  through us). On top of that, every fill is charged an **extra 0.5¢ per contract** penalty (latency /
  queue-jumping / stale-quote haircut).
- Net per filled contract: YES `result − L − maker_fee(L) − 0.005`; NO `(1 − result) − (1 − A) − maker_fee(1 − A) − 0.005`.

## Pass criteria (all required, per agent × variant)
1. Mean net P&L per filled contract > 0 and the **95% event-clustered bootstrap CI lower bound > 0**
   (2,000 resamples of events, seed 7).
2. **Holm–Bonferroni** over all 32 tests: one-sided bootstrap p-value (share of resampled means ≤ 0) must be
   below 0.05 after Holm adjustment.
3. **Minimum sample**: ≥ 300 filled contracts, ≥ 50 distinct events, and ≥ 3 distinct UTC days (by close time)
   with ≥ 30 fills each.
4. **Every day holds**: mean net P&L > 0 on each UTC day with ≥ 30 fills.

**Promising but under-sampled** = mean > 0 after all costs with criterion 3 unmet (or CI including 0 only because
n is below the minimum). If any agent passes or is promising, a paper-only forward maker simulation runs for 7 days
using the same rules on the agents' live forecasts, and is judged with the same criteria on forward-only data.
If nothing passes and nothing is promising, the verdict is "maker execution does not rescue these agents".

## Reported, not used for the verdict
Maker fee M = 0; at-price (queue-front) fills; no 0.5¢ penalty; fill rate; taker comparison on the same markets;
the same rules on the agents' recorded live forecasts (`accuracy_forecasts`, Oct 22 onward).
