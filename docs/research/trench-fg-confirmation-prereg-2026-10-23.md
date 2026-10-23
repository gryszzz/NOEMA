# Pre-registration: confirmation test for trench ideas (f) and (g) on fresh epochs (2026-10-23)

Registered 2026-10-23 ~03:20 ET (07:20 UTC), **before any trade, launch or block row from epochs 1052–1061 was
opened**. What had been seen of those epochs at registration: the directory listing (an `epoch=1061` partition
exists) and the per-epoch row counts and first/last block times that `pumpfun/README.md` prints for epoch 1061.
No return, signal, veto or lift on those epochs was computed. Paper/research only: nothing here places orders,
reads keys, touches wallets or live flags.

Source study (not changed in any way): `docs/research/trench-72h-prereg-2026-10-23.md` (commit `15d3b57`),
results `docs/research/trench-72h-2026-10-23.md` / `.json`, code `noema/trench72.py` +
`noema/trench72_kernels.py` (merged in PR #264, `e6438dd`). In that study (f) was "promising" (held-out Holm
0.003, but early Holm 0.82) and (g) had a +3.9 pp early lift that shrank to +0.5 pp held-out. This test asks
whether either replicates on data nobody has looked at.

Script: `noema/trench_fg_confirm.py` (unit tests `tests/test_trench_fg_confirm.py`). It imports every
definition, parameter, fill model and control from `trench72.py` / `trench72_kernels.py` unchanged; it only
re-points the data at the new epochs and applies the pass bar below.

## 1. Data (frozen)
God Of Market Data's free Old Faithful backfill, `/workspace/marketdata/noema/pumpfun/` (bonding-curve tape;
$0, no Helius).
- **Set A:** epochs **1058–1061** (slots 457,056,000–458,783,999). Expected ~08:00 ET 2026-10-23.
- **Set B:** epochs **1052–1057** (slots 454,464,000–457,055,999). Expected ~17:00 ET 2026-10-23.
- Each set is prepped and evaluated **on its own** (its own trades, blocks, wallet ids and window end). No
  trade from another set or from 1062–1064 is used.
- A set is run only when every one of its epochs has `epoch-N.summary.json` with `parse_complete = true`, its
  trades and blocks partitions, slots inside the epoch, and no hole > 1,000 slots between consecutive epochs
  (`check_set_ready`). Otherwise the script refuses; it never runs on a partial set.

**Universe U (study §1, verbatim):** launches with `created_in_window`, SOL-quoted, `is_mayhem_mode = 0`,
standard initial curve (30 virtual SOL, 1.073e15 raw virtual tokens), **create slot inside the set's slot
range**. Coins created before the set's first slot are excluded. Trades are only those of the set's epochs.

## 2. Execution model (study §3, verbatim)
- Signal at slot `s`; entry lands at `s + L`, **L = 5 slots**. Fill on the curve's virtual reserves after the
  last trade with slot ≤ `s + L` (we are last in our slot).
- Buy 0.1 SOL: curve receives `x·(1 − 0.0125)`; constant-product tokens; then a **1 % slippage buffer**
  (tokens × 0.99). Sell: reserves shifted by our own position; net = gross × (1 − 0.0125) × (1 − 0.01).
- **Fee 1.25 % per side; priority + base fee 0.0005 SOL per tx; size 0.1 SOL.**
- Exits evaluated on every observed trade's post-trade price, landing at that trade's slot + L. Curve
  completion while held → sold into the final curve reserves. An exit after the set's last slot → trade
  **dropped** (never marked).
- Return per trade = (SOL out − SOL in − 0.0005 × txs) / SOL in.
- The B2 slot clock (t0 + Δslot × slot_s) uses slot_s measured on the set's own blocks, exactly as the study
  measured it on its blocks (study deviation 2).

## 3. The two ideas (exact study definitions)
**(f) Manipulation veto — tested only as a filter.** The single confirmatory cell is the one the study selected
on its early split: **`B2|sniper_share>0.3`**.
- Base strategy B2 = the unchanged FRESH desk pre-filter (`trench_fresh.evaluate` on `trench_hft.features` at
  15 s, then 45 s; signal slot = create slot + int(age / slot_s)), exit `fresh_g1_tp30_sl7.9_t120`
  (TP +30 % full, SL −7.9 %, 120 s max).
- Feature (`manip_feats`, trades with slot ≤ signal slot only): launch-block sniper share = non-creator buy SOL
  in the create slot / all buy SOL up to the signal. **Veto if > 0.3.**
- **Control = the unfiltered B2 strategy** in the same set. Lift = kept mean − unfiltered mean.
- Test statistic (study §6): **Welch one-sided t-test, vetoed mean < kept mean** (`welch_less`).
- The other 15 cells of the study's (f) grid (wash/atomic and sniper 0.5, on B1 and B2) are reported
  **descriptively only**; they are not tests and cannot pass.

**(g) Re-entry after snipers exit, vs matched control.** `reentry_signals`, unchanged:
- Cohort = creator + every buyer in slots create..create+4. Checked at each trade between 60 s and 30 min
  after create: ≥ 10 distinct non-cohort buyers in the last 60 s, net SOL inflow over 60 s > 0, curve progress
  25–70 %. First qualifying trade per coin is the signal.
- **Treatment:** cohort still holds ≤ 20 % of the tokens it bought. **Matched control:** first moment the same
  conditions hold while the cohort still holds > 20 %. (No-inventory control also reported, not tested.)
- Exit `reentry_tp40_sl20_t600_p95`: TP +40 %, SL −20 %, 600 s, sell at 95 % curve progress.
- Lift = treatment mean − matched-control mean. Test statistic (study): **Welch one-sided, control mean <
  treatment mean**.

## 4. Pass bar
- 4 tests: {f, g} × {Set A, Set B}. **Holm across the 4 raw Welch p-values, α = 0.05.**
- A test passes if its **lift > 0 and Holm-adjusted p < 0.05**.
- An idea **PASSES** only if it passes in Set A **and** in Set B, each set evaluated on its own (no pooling).
- No re-selection: the cells and arms above are fixed; nothing is chosen on A or B.
- Primary analysis uses **all coins**. As a sensitivity, the same 4 tests are redone with **gapped coins
  dropped** (own Holm over 4): a coin is gapped if any row of `events/epoch=N/gap.parquet` (log-truncated,
  multiframe-skipped or undecodable records) has a slot between its create slot and its last trade slot in the
  set. An idea that passes **only** once gapped coins are dropped is marked **"needs fuller data"**, never PASS.
  (Bonding-curve trades have no PumpSwap pools; this is the curve-tape analogue of "gapped pools".)
- **Per-day breakdowns are reported** for both ideas: every epoch and every UTC day of each set (n, means,
  lift; for f also n vetoed). They are reported, not part of the pass bar.

## 5. Consequences
- **f is a filter only.** Even if it passes it gets **no desk**; at most it is recorded as a confirmed filter
  for future pre-registered trench hypotheses.
- **g gets no paper desk while the mean net return of the trades it keeps (the treatment trades) is not above
  0** — checked in Set A and in Set B separately — even if it beats its matched control. If g passes and the
  treatment mean is > 0 in both sets, it becomes desk-*eligible* only; wiring a paper desk (paper only, default
  off) is a separate PR. No scaling, no live trading follows from this test.
- Results: `docs/research/trench-fg-confirmation-2026-10-23-setA.json`, `-setB.json`, and the combined
  verdict in `docs/research/trench-fg-confirmation-2026-10-23.json`, plus a short results note. Any deviation
  from this document is labelled **DEVIATION** there.

## 6. Run procedure
```
PYTHONPATH=. python -m noema.trench_fg_confirm check --set A     # refuses until all of 1058-1061 landed
PYTHONPATH=. python -m noema.trench_fg_confirm prep  --set A     # -> /workspace/trench_fg_work/A
PYTHONPATH=. python -m noema.trench_fg_confirm run   --set A
# same three steps for --set B once 1052-1057 have landed, then:
PYTHONPATH=. python -m noema.trench_fg_confirm verdict           # Holm over the 4 tests; PENDING until both sets
```
(Research venv with numpy, numba, scipy, pyarrow, duckdb, pandas; these are not NOEMA runtime deps.)

## 7. Replication check done before registration (already-published data only)
To show the script is the study's code and not a re-implementation, it was run once on the study's own epochs
1062–1064 (`--set R`, never part of the verdict). The prepped tape matched the study's (7,333,952 trades,
73,641 coins, 408,044 wallets, 1,294,515 blocks; 0 differing trade rows). All 16 (f) cells reproduced the
study's per-epoch and per-day n, n vetoed and lift exactly (0 mismatches), and the (g) arms reproduced the
study's counts and per-epoch means exactly (treatment 898/801/736, matched control 589/574/467). No gap
records exist in 1062–1064.

## Amendment 1 (registered 2026-10-23 ~03:30 ET / 07:30 UTC, before any Set A data was read)
Side check (does not change the pass bar): Set A results will also be reported with epoch 1061 excluded, i.e.
epochs 1058–1060 only, because 1061 files were on disk before registration.
Implementation: `run --set A` also stores `excl_epoch1061` (the f cell and the g treatment/matched-control
tests recomputed on trades whose entry slot is outside epoch 1061); `verdict` reports it under
`side_checks.A_excl_1061` with raw Welch p-values only. It is not in the Holm family and cannot pass or fail
an idea.
