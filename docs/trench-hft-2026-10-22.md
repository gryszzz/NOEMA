# TRENCH-HFT research — 2026-10-22 (paper only)

**Verdict: no edge after costs. No TRENCH-HFT-g0 agent added.**

Data: our own captured pump.fun ticks (`launch_stream` + `launch_trades`, receive timestamps, Helius+PumpPortal),
848 coins with ≥3 trades, 1,894 decision samples, ~2.4 h of tape (prune keeps 72 h; capture started today).
Code: `noema/trench_hft.py` (stdlib; `python -m noema.trench_hft report`).

Features at decision age 15/45/120 s: order-flow imbalance, buy/sell SOL, unique-buyer rate and acceleration,
top-3 buyer share, first-1s share and <0.3 s buy clustering (bundle/staged proxy), dev sold / dev sell ratio,
smart-wallet buyers (PR 179 watchlist), bonding-curve progress + 10 s velocity, return since launch / 10 s,
drawdown from peak, median and max buy size, sell-pressure acceleration, distinct sellers.

Models: L2 logistic and boosted stumps (60 rounds) for P(+3% at +30 s / +2 m / +10 m) and P(rug: −50% within 10 m).
5-block time walk-forward, expanding train, purged (train samples whose label window overlaps the test start dropped).
Entry only when up-score is in the top 10/20% of the train distribution and rug-score < 0.5.

Execution: fill at curve state `lat` s after the decision tick (and after each exit trigger), constant-product curve
math with our own size, 1% fee each side, priority fee per tx, SOL=$105. Exits: TP 30/60% sells half, trailing 20% on
rest, SL 20/35%, time 120/600 s.

| scenario | best variant | n | hit | mean net/trade | 95% bootstrap CI | p (raw) | Bonferroni |
|---|---|---|---|---|---|---|---|
| base: 1.5 s, $5, 0.0003 SOL prio | age45 gbm h600 top10 | 48 | 37.5% | −1.0% | −10.2% … +9.4% | 0.58 | 1.0 |
| optimistic: 0.5 s, $10, 0.0001 | age120 gbm h30 top20 | 87 | 23% | +25% (one outlier) | −15% … +102% | 0.24 | 1.0 |
| pessimistic: 3 s, $2.50, 0.0005 | age120 gbm h30 top10 | 33 | 30% | +2.3% | −14% … +24% | 0.41 | 1.0 |

Variants counted: 288 per scenario × 3 = **864**; 0 survive Bonferroni (α=0.05/864). Enter-everything baseline is
−5% to −7% per trade in base costs, so the models filter out some losers but do not get above zero.

## Pre-registration (frozen before any forward test)
Only if we forward-test, the single candidate is: age 120 s, GBM, label +3% at +30 s, top 20% score, rug < 0.5,
exit TP 30% (half) / trail 20% / SL 20% / 120 s, 1.5 s latency, $5. Pass = ≥150 fresh trades, mean net > 0 with
bootstrap 95% CI lower bound > 0, and not driven by one trade (still > 0 with top 3 removed). Re-run this report
after ≥3 days of tape before deciding; no parameter changes allowed.
