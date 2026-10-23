# Pre-registration: 72 h on-chain rerun of the trench edge study (2026-10-23)

Registered 2026-10-23 ~02:45 ET (06:45 UTC), **before any strategy return, veto lift or META outcome was computed**
on this data. What had been looked at: the dataset READMEs, schemas, row counts, the fee rate (1.25 % per side,
confirmed from `fee_lamports + creator_fee_lamports`), quote-mint and mayhem shares, PumpSwap coverage (epoch 1064
only) and the `meta_check/analyze.log` verdict counts (frozen-feed diagnosis, not returns of any rule here).
Paper/research only. Nothing here places orders, reads keys or touches live flags.

Red-team bar agreed with Mad Scientist Bot: pre-registration, realistic fills, META re-score on on-chain prices,
multiple-testing correction over every variant, per-day and held-out pass. Results doc:
`docs/research/trench-72h-2026-10-23.md` (+ JSON). Code: `noema/trench72.py`.

## 1. Data (frozen)
- `/workspace/marketdata/noema/pumpfun/` **epochs 1062, 1063, 1064 only** (slots 458,784,000–460,079,999;
  2026-10-19 09:30:15 UTC → 2026-10-22 15:06:37 UTC, 77.6 h). Older epochs being appended by `run_epochs.sh` are
  ignored even if they appear while the study runs.
- PumpSwap (`../pumpswap/`): epoch 1064 only (the only AMM epoch parsed at registration).
- META: `../meta_check/meta_positions_onchain.parquet` + `meta_onchain_trades.parquet` (as of 2026-10-23 02:27 ET),
  and `NOEMA/data/meta_launch.db` (`meta_matches`, `meta_positions`) opened read-only.
- SOL/USD: `../sol_usd/sol_usd_1m.parquet` (only for USD sensitivity sizes).

**Universe U:** launches with `created_in_window`, SOL-quoted (`quote_mint` = system program), `is_mayhem_mode = 0`,
standard initial curve (30 virtual SOL, 1.073e15 raw virtual tokens). Coins created before the window are excluded
(no create row). Non-SOL-quoted curves are excluded.

## 2. Split (fixed)
- **Early / train:** entry slot < 459,648,000 (epochs 1062–1063, until 2026-10-21 13:36:05 UTC).
- **Late / held-out:** entry slot ≥ 459,648,000 (epoch 1064). Held-out is evaluated **once**, for the single
  variant per idea selected on the early split.
- **Days for the per-day test:** each epoch (1062, 1063, 1064; ≈ 25.6 h each) **and** each UTC calendar day
  (Oct 19 partial from 09:30, Oct 20, Oct 21, Oct 22 partial to 15:06) that has ≥ 50 trades for the variant.
  A day is assigned by the entry slot's block time / epoch.

## 3. Execution model (all ideas)
- Signal at slot `s` (the slot of the last information used). Entry tx lands in slot `s + L`, **L = 5 slots**
  primary (≈ 1.1 s at the observed ~0.22 s/slot). Sensitivities L = 2 and L = 10 (reported, not selectable).
  Entry is never in the launch block (`s + L > create_slot` always, and enforced).
- Fill against the curve's virtual reserves **after the last trade of that mint with slot ≤ s + L** (we are
  assumed last in our slot: every same-slot trade goes first).
- Buy `x` SOL: curve receives `x·(1 − 0.0125)`; tokens = `vT − vS·vT/(vS + x_net)`; then an **extra 1 % slippage
  buffer** (tokens × 0.99).
- Sell `q` tokens: reserves at the exit fill are shifted by our own position (`vS + x_net`, `vT − q_bought`),
  gross = constant-product out, net = gross × (1 − 0.0125) × (1 − 0.01 buffer).
- **Priority + base fee 0.0005 SOL per tx.** Primary size **0.1 SOL** (≈ $10.4). Sensitivity: $5 (0.048 SOL).
- Exit triggers are evaluated on every observed trade's post-trade price; the exit lands at that trade's slot
  + L and fills at the reserves after the last trade with slot ≤ that slot. Time stops exit at the first slot whose
  block time ≥ entry time + tmax (+ L slots).
- If the curve completes while we hold, the position is sold into the final curve reserves at the complete slot
  (same fee + buffer). If the exit time is after the window end, the trade is **dropped** (never marked).
- Return per trade = (SOL out − SOL in − 0.0005 × txs) / SOL in.

Exit rule library (`EXITS`) = the 26 rules of `trench_edges.EXITS` plus four longer rules made possible by full
tapes: `time300`, `time600`, `trail20_t300` (trail 20 % from peak, armed at once, 300 s max), `tp50_sl20_t600`.

## 4. Baselines used by exits/veto ideas
- **B1 `all15`:** every U coin with ≥ 5 trades by 15 s after create (block time); signal slot = the last slot with
  block time ≤ create + 15 s; exit `time60`.
- **B2 `fresh`:** the unchanged FRESH desk pre-filter (`trench_fresh.evaluate` on `trench_hft.features` at 15 s,
  then 45 s); exit `fresh_g1_tp30_sl7.9_t120`.

## 5. Hypotheses and variants (every variant counted)
**(a) Copy proven wallets.** Signal = a wallet's first buy of a U coin within 120 s of create (creator excluded).
Wallet score = copy return at L with `time30`, only on scoring trades whose exit closed before the test segment
starts (purged). Grid: min_n {5, 10, 20} × min_t (t-stat) {1, 2, 3} × exit {time30, time60, trail20_t60,
ladder15_40_trail15_sl15_t60} = **36**. One copy per coin (first chosen-wallet buy). Discovery (early): wallets
scored on 1062, copied in 1063. Held-out: scored on 1062–1063, copied in 1064. Per-day unit 1062: scored on
slots < 459,000,000, copied in the rest of 1062. (Per-day test for (a) uses these three segments.)

**(b) Exit rules.** 30 exits × bases {B1 entries, B2 entries} = **60**.

**(c) Graduation plays.**
- (c1) curve-progress cross: signal = first trade with mcap ≥ {100, 150, 250, 350} SOL; exits {time10, time30,
  time60, trail10_t60, trail20_t60, tp20_sl10_t120, tp50_sl20_t120, ladder15_40_trail15_sl15_t60} = **32**.
- (c2) post-migration PumpSwap buy (epoch 1064 migrations only): entry at the first pool trade slot ≥ migration +
  {0, 60, 300} s + L, on effective reserves (vault + virtual quote), fee 1.25 %/side + 1 % buffer + 0.0005 SOL/tx;
  exits {time300, time1800, time21600, trail20_t21600, tp50_sl20_t21600} = **15**. Early = migrations before
  2026-10-22 00:00 UTC, late = after. **Pre-declared: c2 cannot meet the per-day rule (one epoch of AMM data), so
  it can at most be "promising", never PASS.**

**(d) Creator track-record veto.** Outcome of a prior coin = max curve progress within 1 h of its create (full tape
now), known only at create + 1 h. Rules: veto if ≥ 1 known prior and all died (< 50 %); veto if ≥ 2 known priors all
died; keep only creators with a known prior runner. × {B1, B2} = **6**.

**(e) Originality / creator pre-filter.** Veto if: orcACR (any of the next three); creator launched before in
window; name+symbol seen before in window; creator initial buy > 4 SOL. × {B1, B2} = **8**.

**(f) Manipulation veto** (features from trades with slot ≤ signal slot). wash_max (flips / (|net tokens| + 1))
> {10, 25, 50} OR atomic buy+sell signatures ≥ {1, 3} (6 cells); launch-block sniper share (non-creator buy SOL
in create slot / all buy SOL to signal) > {0.3, 0.5} (2 cells). × {B1, B2} = **16**.

**(g) Re-entry after snipers exit, with matched control.** Cohort = creator + every buyer in slots
create..create+4. Checked at each trade between 60 s and 30 min after create: ≥ 10 distinct non-cohort buyers in
the last 60 s, net SOL inflow over 60 s > 0, curve progress 25–70 %. **Treatment:** cohort still holds ≤ 20 % of the
tokens it bought. **Matched control:** first moment the same conditions hold while the cohort still holds > 20 %.
Also reported: control without any inventory condition. Exit: TP +40 %, SL −20 %, 600 s, sell at 95 % progress.
Variants: **3** arms (treatment counted as the 1 selectable variant; both controls counted).

**(h) META shield re-score** (on-chain prices, not NOEMA marks).
- (h1) every META paper position with on-chain coverage: on-chain open→close return minus costs (2 × 1.25 % fee,
  2 × 1 % buffer, 2 × 0.0005 SOL at the position's USD size) vs NOEMA's recorded `realized_ret`.
- (h2) shield discrimination: `meta_matches` inside the window whose mint is in U; on-chain 30 min forward return
  from `matched_at` (first slot with block time ≥ matched_at, + L) with the §3 model; shield-pass vs shield-fail
  (all ranks; secondary: rank=first). **2** tests. Matches only start 2026-10-21 17:29 UTC, so only epoch 1064
  overlaps: **pre-declared that (h) cannot meet the per-day rule; it is reported, not passable.**

**Total pre-registered selectable variants: 36 + 60 + 32 + 15 + 6 + 8 + 16 + 3 + 2 = 178.** Latency/size
sensitivities are extra evaluations (reported and counted in the "evaluations" number), not selectable.

## 6. Statistics and pass criteria
- Per-trade one-sided t-test for mean > 0; bootstrap 95 % CI (2,000 resamples). Veto tests: Welch one-sided
  (vetoed mean < kept mean). Lift = kept mean − unfiltered baseline mean.
- **Multiple testing:** Holm (primary) and Benjamini–Hochberg (reported) across **all 178** early-split tests
  as one family; Bonferroni-style number = min(1, p × 178) also reported. Held-out tests: Holm across the ideas
  that reach held-out.
- **Selection:** per idea (per sub-idea c1/c2) the variant with the highest early mean (strategies, n ≥ 100) or the
  smallest early veto p (vetoes, ≥ 30 vetoed trades).

**Strategy PASS (a, b, c1, c2, g):** all of
1. Early: mean > 0, n ≥ 150, Holm-adjusted p < 0.05.
2. Held-out: n ≥ 150, mean > 0, CI95 low > 0, mean without top 3 trades > 0, Holm-adjusted held-out p < 0.05.
3. Every epoch and every UTC day with ≥ 50 trades: mean > 0 and mean without top 3 > 0 (each epoch must have
   n ≥ 50).
4. Held-out mean still > 0 at L = 10.
5. (g only) treatment − matched control lift > 0 with Welch p < 0.05 in early and held-out, and > 0 every epoch.

**Veto PASS (d, e, f):** early Holm-adjusted p < 0.05; held-out Welch p < 0.05 (Holm across held-out veto tests)
with lift > 0; lift > 0 in every epoch and every UTC day with ≥ 50 trades. A veto is **desk-eligible** only if
additionally the kept strategy meets strategy criteria 2–3.

**(h) PASS:** shield-pass on-chain mean > 0 with CI low > 0 and > shield-fail (Welch p < 0.05) — but see the
per-day pre-declaration above.

## 7. Actions tied to outcomes
- A paper desk (paper only, following `trench_desks`/`trench_fresh` wiring, default disabled behind an env flag)
  is wired **only** for an idea that meets every criterion. None otherwise.
- For any pass: scaling check at sizes 0.1/0.5/1/2/5/10 SOL with own-impact fills, plus concurrency (signals per
  hour, overlapping positions) → max size and markets before net mean ≤ 0.
- Any deviation from this document will be labelled **DEVIATION** in the results doc.
