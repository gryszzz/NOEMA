# Trench 72h, idea (c2) coverage robustness rerun (paper only), 2026-10-23

**Result: the coverage filter changes nothing on held-out data. c2 is still −9.5 %/trade (n 208), it still
loses on both days, and the verdict stays FAIL.** This is a robustness check only. It is not a new test and
does not add to the 178-test family.

- Original study: `docs/research/trench-72h-2026-10-23.md` / `.json` (PR 264). Pre-registration
  `docs/research/trench-72h-prereg-2026-10-23.md` (commit `15d3b57`).
- Code: `noema/trench72_c2_covered.py`. It re-implements `noema.trench72.idea_c2` line for line and
  imports the same `Data`, `Sim`, exits, `describe` and the numba `sim_one` kernel. It also records
  per-trade rows so the pool filters can be applied. Output: `docs/research/trench-72h-c2-covered-rerun-2026-10-23.json`.
- Reproduce (about 10 s): `PYTHONPATH=. python -m noema.trench72_c2_covered --work /workspace/trench72_work --pools <pools.parquet>`.
- Inputs are unchanged since PR 264. `pools.parquet` was snapshotted at about 03:28 ET (sha256 `4716a3ebb3b0d64c4a3589b5dd24bd96401b44f63c08df9e07547a8e42fd6130`,
  mtime 02:46 ET, before the PR 264 run). The epoch-1064 PumpSwap trades have mtime 02:46 ET. The same compact
  curve tape is in `/workspace/trench72_work`.
- Nothing was changed: same universe (U, SOL-quoted epoch-1064 migrations), same entry (first pool trade at
  or after migration + delay, then + L=5 slots), same fills (effective reserves = vault + virtual quote,
  1.25 %/side fee + 1 %/side buffer + 0.0005 SOL/tx, own impact on exit), size 0.1 SOL, same 15 variants, same
  split (held-out = migrations at or after 2026-10-22 00:00 UTC = Oct 21 20:00 ET). Selected variant
  **d0_tp50_sl20_t21600** (chosen on the early split in PR 264; not re-selected).
- The **only** change is a pool filter from `pools.parquet`: cut A `hours_covered_post_migration >= 6`,
  cut B `covered_24h`.
- **Reproduction check:** with no filter the script reproduces PR 264 exactly for all 15 variants, including
  held-out n 208 / mean −0.09536 / CI [−0.1621, −0.0246].

## Held-out results, selected variant (net return per trade, 0.1 SOL, L=5)
| cut | n | mean [95 % CI] | median | mean ex-top-3 | hit rate |
|---|---|---|---|---|---|
| PR 264 original (no filter) | 208 | −9.5 % [−16.2, −2.5] | −19.3 % | −10.7 % | 46.6 % |
| A: `hours_covered_post_migration >= 6` | **208** | **−9.5 % [−16.2, −2.5]** | **−19.3 %** | **−10.7 %** | 46.6 % |
| B: `covered_24h` | **0** | — | — | — | — |

Per day, cut A (identical to the original):
- By UTC day of entry, there is one day only, 2026-10-22: n 208, −9.5 %.
- By ET day of migration:
  - Oct 21 ET (20:00–23:59): n 93, mean −6.2 %, median +10.8 %, ex-top-3 −8.4 %.
  - Oct 22 ET (00:00–05:06): n 115, mean −12.3 %, median −20.9 %, ex-top-3 −14.2 %.
- It is negative on both days.

## Matched baselines on the same covered pools (cut A, held-out, same entry and fills)
| passive hold, d0 | n | mean | median | ex-top-3 |
|---|---|---|---|---|
| time21600 (6 h, the same horizon as c2) | 208 | −58.2 % | −96.8 % | −86.2 % |
| time1800 (30 min) | 208 | −18.3 % | −96.0 % | −37.2 % |
| time300 (5 min) | 208 | −4.4 % [−11.7, +3.6] | +7.7 % | −7.5 % |

- Compared with the 6 h hold on the same pools, c2's tp50/sl20 exit is better by +48.7 pp mean (paired,
  n 208) and +71.7 pp median. That is the stop-loss cutting off the post-migration collapse; it is not an edge.
- **A plain 5-minute hold on the same 208 pools loses less (−4.4 %) than c2 (−9.5 %).** Every one of the 15
  variants is negative on cut A. The best is d0_time300.

## Why the filter is a no-op, and what the censoring actually is
- `sim_one` returns NaN when entry + 6 h falls after the end of the block tape, which is the end of epoch
  1064 (Oct 22 11:06 ET). So the PR 264 held-out set already contained only pools with at least 6 h of
  observed post-migration trading.
- No pool with less than 6 h of coverage was marked at its last price. Every held-out c2 trade is in a pool
  with `hours_covered_post_migration >= 6`.
- The real censoring is different: **held-out c2 covers only migrations from Oct 21 20:00 ET to Oct 22
  05:06 ET.** Of the 521 held-out-window SOL pools, 328 have at least 6 h of coverage. The 193 that migrated
  later are missing until epoch 1065 is archived. That is a selection on migration time, not on outcome.
  Fuller data could still move the mean, but the 6 h-exit variants cannot be scored on those pools until
  epoch 1065 arrives.
- `covered_24h` needs migration before Oct 21 11:06 ET, which falls entirely in the early split, so held-out
  n = 0 by construction. For information only (this is not held-out): early-split covered_24h pools give c2
  n 44, mean −5.9 %, median −21.2 %, ex-top-3 −10.7 %. The 6 h hold on the same pools gives −89.6 %.

## Verdict
- Unchanged: **c2 FAIL.** As pre-declared, c2 could never PASS with one epoch of AMM data.
- The coverage check confirms the PR 264 number was not inflated by marking censored pools at their last
  price. The −9.5 % held-out figure stands on fully observed 6 h windows.
- Next step: rerun when epoch 1065 (Oct 24) and epochs 1062–1063 PumpSwap data land, using the same script
  and the same frozen variant. Treat that run as a confirmation, not a re-selection.
- Paper research only. No live trading, wallets, keys or signer were touched.
