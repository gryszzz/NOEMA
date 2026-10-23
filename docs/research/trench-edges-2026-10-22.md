# Trench edge research — 2026-10-22 (paper, no lookahead)

**Result: none of the five ideas passes, and no paper desk was wired.**
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

## Results
Returns are net per trade. The 95% CI is a bootstrap of the mean, and p is one-sided for mean > 0. Means are dominated by outliers, so also look at the median and the mean excluding the top trade (in the JSON).

| # | Idea (best discovery variant) | Discovery n / mean / p (Bonf) | Held-out n / mean / CI / p | Verdict |
|---|---|---|---|---|
| 1 | Copy proven wallets, primary (min_n 5, t ≥ 1, ladder15/40+trail15, sl15, 60 s) | 335 / +6.9% (median −8.1%) / 0.20 (1.0) | 203 / **−7.6%** / [−10.7, −4.4] / 1.00 | FAIL |
| 1 | Copy wallets, rpc (min_n 20, t ≥ 1, time60) | 423 / +25% (median −58%) / 0.23 (1.0) | 94 / +29% / [−24, +99] / 0.18; median −51% | FAIL |
| 1 | Copy wallets, rpc, **3 s latency** | 106 / +15% / 0.30 (1.0) | 62 / **−16%** / [−41, +17] / 0.85 | FAIL |
| 2 | Exits, all coins entered at 15 s (best of 26: time10) | 1,890 / −4.4% / 1.0 | 458 / −2.3% / [−8.2, +6.0] / 0.74 | FAIL; **all 26 exits negative** |
| 2 | Exits on fresh-filter entries (tp20/sl10/120 s) | 53 / +3.1% / 0.27 (1.0) | 8 / +2.0% / — | FAIL (n too small) |
| 3 | Graduation: cross 100 SOL mcap, exit after 30 s | 309 / +124% (median −6%, ex-top1 −6.7%) / 0.17 (1.0) | 52 / **−5.9%** / [−31, +24] / 0.67 | FAIL |
| 3 | Graduation, 3 s latency (cross 350 SOL, trail20) | 70 / +44% / 0.03 (Bonf 1.0) | 9 / −12% | FAIL |
| 4 | X mention velocity (349 pump mints mentioned) | — | only 4–5 signals priceable, mean ≈ +1% | UNTESTABLE |
| 5 | Avoid-side filters (15 filters; baseline is all coins at 15 s with a 60 s time stop, disc. −8.3%) | best is bundle_cluster > 0.5: kept −4.7% vs removed −9.7%, p = 0.19 (Bonf 1.0) | kept +12.8% (n 193, p 0.29) vs removed −9.6%, p = 0.17 | FAIL |

Notes:
- **Copy wallets is the only lead.** It flips sign between runs and turns negative at 3 s latency. It is high variance and latency-bound, and not tradable on this evidence.
- **Avoid filters:** "risky-looking" coins (top-3 holder share > 0.5/0.7, dev sold, heavy 1 s sniping) did **not** do worse than the coins we kept. In several cases they did better.
- **Graduation** means come from one or two moonshots. The medians are negative.

## What would let us test properly (data collection, not trading)
1. Extend trade coverage past 180 s, at least for a random sample of coins (unbiased) and for every coin that crosses about 100 SOL mcap.
2. Store PumpPortal migration events and post-migration PumpSwap prices.
3. Start following the price path of any mint when it is first mentioned on X.
4. Rerun this report after 48–72 hours of data (more folds, larger held-out n).
5. Optionally, shadow-log the copy-wallet signal in paper at ≤1.5 s latency to measure real fill delay.
