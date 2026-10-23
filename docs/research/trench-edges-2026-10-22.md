# Trench edge research — 2026-10-22 (paper, no lookahead)

**Result: none of the six ideas passes, and no paper desk was wired.** Numbers below are from the gap-guarded rerun (see Data quality).
The tool is `python -m noema.trench_edges report --db data/noema.db [--include-rpc] [--lat 3.0] [--out f.json]`. The raw results are the JSON files next to this one.

## Protocol
- **Data:** pump.fun trades from `launch_stream`, Helius-decoded, from 2026-10-22 17:16 UTC to about 00:30 UTC on Oct 23.
  - The **primary** run uses 3,103 launches that have a create row (384k trades).
  - The **rpc** run adds about 5k mints that have Helius trades but no create row (8,172 coins, 881k trades). For those coins, t0 is the first trade.
- **Folds:** 5 equal-count folds ordered by launch time.
  - Folds 1–4 are discovery.
  - Fold 5 is held out, from about 23:00 UTC. It is run **once**, for the best discovery variant only.
  - Copy-wallet selection is walked forward with purging. Wallets are scored only on trades that finished before the fold being traded.
- **Costs:** these come from the trench_hft curve math (a cross-check found 0 of 800 mismatches against `trench_hft.simulate`).
  - 1% fee per side.
  - 0.0003 SOL priority fee per transaction.
  - $5 size, SOL at $104.
  - 1.5 s latency, with a 3 s sensitivity run.
  - The round-trip drag is about 3.3% before slippage.
- **PASS rule:** discovery Bonferroni p < 0.05, held-out mean > 0 with one-sided p < 0.05, and held-out n ≥ 30.

## Critical data limit (also affects PR #187)
`launch_stream` records a coin's trades only for `TRADE_FOLLOW_S = 180 s` after create. WATCH-wallet buys extend this, but that extension is selection-biased.
- Every simulated trade here must close by t0 + 170 s. Windows past that are skipped, not marked at a frozen price.
- **The 600 s horizons in PR #187 were partly marking frozen prices**, so those long-horizon numbers are unreliable.
- Migration events and PumpSwap prices are not stored at all.

## Data quality: capture outages = frozen prices (same failure class as META)
META's marks froze: 150 of 176 META closes were exactly −2.00% with no price movement. Our marks are the market cap printed by each recorded trade, so a mark can only look frozen when no trades were recorded. That happens either because the coin was dead (real) or because capture was down (fake).
- Across all coins there are **248 tape-wide silences longer than 10 s**, totalling about **112 min of a 442-min tape**. 124 of them are longer than 20 s. The largest were 354 s at 23:59 UTC, 239 s at 22:29 and 179 s at 22:51. They line up with the red `launch_stream` heartbeat, the RPC timeouts and the 66–140 s SQLite lock holds.
- **The first version of this study (PR #234) did not guard against this.** Some of its exit windows spanned outages and were marked at frozen prices. PR #234's conclusion (no pass) is unchanged, but its numbers are superseded by the ones below.
- **Fix:** `apply_gap_guard` treats a silence across all coins longer than 10 s as an outage. It cuts each coin's usable coverage at the first outage after its create, and drops coins created during an outage.
- **After the guard:** 0–2 windows have a flat mark despite trades, and 14–16% of 60 s windows have no trades at all. That 14–16% is dead coins (genuine, not capture loss).
- The cost is a much smaller usable sample. For example, the primary all-coin 15 s entries fell from 1,890 to 461 in discovery.

## Results (gap-guarded; primary = launches with a create row, 1.5 s latency unless noted)
Returns are net per trade. The CI is a 95% bootstrap of the mean, and p is one-sided for mean > 0. The medians and the mean with the top trade removed are in the JSON. Most positive means vanish once the top trade is removed.

| # | Idea (best discovery variant) | Discovery n / mean / p (Bonf) | Held-out n / mean / CI / p | Verdict |
|---|---|---|---|---|
| 1 | Copy proven wallets (min_n 5, t ≥ 1, trail20/60 s) | 84 / +15% (median −15%, ex-top1 −0.7%) / 0.23 (1.0) | 57 / **−12.1%** / [−17, −6.5] / 1.00 | FAIL |
| 1 | Copy wallets, with RPC-only coins (min_n 20, trail20/60 s) | 79 / +8.9% / 0.31 (1.0) | 79 / −7.8% / [−19, +7.2] / 0.88 | FAIL |
| 1 | Copy wallets, RPC-only coins, 3 s latency | 98 / +7.2% / 0.32 (1.0) | 56 / +3.8% / [−12, +23] / 0.34 | FAIL |
| 2 | Exits, all coins at 15 s (best of 26: tp20/sl35/120 s) | 461 / −3.0% / 0.85 | 225 / −10.3% / [−14, −7.1] / 1.00 | FAIL |
| 2 | Exits, all coins at 15 s, RPC-only coins included (time20) | 3,098 / −2.6% / 0.96 | 997 / −3.4% / [−8.1, +2.3] / 0.90 | FAIL |
| 2 | Exits on fresh-filter entries (time90) | 13 / +0.4% / 0.49 | 2 / −63% | FAIL (n too small) |
| 3 | Graduation (cross 250 SOL, tp20/sl10/120 s) | 24 / +54% (ex-top1 +0.2%) / 0.16 (1.0) | 6 / +33% / p 0.09 | FAIL (n too small) |
| 4 | X mention velocity (349 pump mints mentioned) | — | 2 signals priceable | UNTESTABLE |
| 5 | Avoid filters (15; baseline is all coins at 15 s with a 60 s stop: disc. −7.9%) | best is few_buyers < 5: kept −6.3% vs removed −12.8%, p 0.18 (Bonf 1.0) | kept −12.6% (n 187) vs removed +43%, p 0.91 | FAIL |
| 6 | **Creator track-record veto** (see below) | veto lift p 0.004 (**Bonf 0.097**); kept −1.4% | lift p 0.53; kept 182 / −10.1% / [−13.6, −6.8] | FAIL |

### 6. Creator track-record veto (added on request)
- **Rule:** for each new coin, look only at that creator's earlier coins whose 1 h outcome (curve ≥ 50%) was already known at entry, meaning the prior coin was created ≥ 1 h before entry.
- **Variants (24 in total, Bonferroni m = 24):**
  - Bases: FRESH-filter entries and all coins at 15 s.
  - Rules: veto when all known priors died (≥1 prior, or ≥2 priors), or keep only creators with a prior runner.
  - Exits: time30, time60, tp20/sl10/120 s and the fresh-g1 exit.
  - The selected variant is the one whose vetoed trades looked most clearly worse in discovery.
- **The label effect is real and reproduces** on our coins. P(reach half the curve) is 3.4% when all priors died (n = 653), 8.9% with a prior runner (n = 247) and 7.0% for first-time creators (n = 2,177). The other analysis found 3.9%, 8.3% and 6.8%.
- **It does not convert into tradable returns at 15 s:**
  - Best variant: all coins, veto when ≥2 priors all died, tp20/sl10/120 s.
  - Discovery: vetoed −12.3% (n 78) vs kept −1.4% (n 383). One-sided p 0.004, Bonferroni 0.097, which misses 0.05.
  - Held-out: vetoed −9.9% (n 43) vs kept −10.1% (n 182), p 0.53. **No lift out of sample, and the kept trades still lose money.**
  - With RPC-only coins added: discovery p 0.003 (Bonf 0.075), held-out p 0.52.
  - At 3 s latency the best variant has no discovery lift at all (p 0.36).
- **On FRESH entries the veto almost never fires:** in discovery it vetoes 0–2 entries in total, because the FRESH pre-filter already excludes most serial-deployer launches. Adding it to the fresh desk would change almost nothing.
- **Caveat:** we record only about 180 s of trades per coin, so a prior coin's "1 h outcome" is in practice its outcome in the first 3 minutes.
- **Verdict:** FAIL as an edge and FAIL as a veto. It is worth re-testing as a cheap avoid-side prior once trade coverage is extended and there are more days of data.

## What would let us test properly (data collection, not trading)
0. Fix capture outages first: about 25% of the tape was lost to >10 s feed or DB-lock gaps. Move trade writes off the noema.db hot lock, or into a separate DB, and add a gap metric to the heartbeat.
1. Extend trade coverage past 180 s, at least for a random sample of coins (unbiased) and for every coin that crosses about 100 SOL mcap.
2. Store PumpPortal migration events and post-migration PumpSwap prices.
3. Start following the price path of any mint when it is first mentioned on X.
4. Rerun this report after 48–72 hours of data (more folds, larger held-out n).
5. Optionally, shadow-log the copy-wallet signal in paper at ≤1.5 s latency to measure real fill delay.
