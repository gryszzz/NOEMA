# Trench specs from `proven_edges.md` — preliminary run (2026-10-22)

**Status: PRELIMINARY / UNDERPOWERED. Nothing passes. No paper desk wired.**

Each spec needs ≥ 72 h of tape. This run has **7.44 h** (3,112 gap-guarded coins).
Held-out fold starts 2026-10-22 23:11 UTC (19:11 EDT). Code: `noema/trench_specs.py`. Raw output: `trench-specs-2026-10-22.json`.

## Method

- **Costs:** 1.25% curve fee per side, plus a 0.0003 SOL priority fee per transaction. Trade size is $5, with SOL at $104.
- **Latencies:** 0.5 s, 1.5 s and 3 s.
- **Gap guard:** the same capture-outage guard as `trench-edges-2026-10-22.md`. Each coin's coverage is cut at the first global silence longer than 10 s.
- **Folds:** 5 time-ordered folds. Folds 1–4 are discovery and fold 5 is held out.
- **Multiple testing:** Bonferroni across the grid, with m = 6 for S1 and m = 6 for S2.
- **No lookahead:**
  - Originality features use only launches recorded earlier.
  - The creator track record uses only prior coins whose 1 h outcome was already known at entry.
  - Manipulation features use only trades at or before entry.
- **NOEMA bar:** ≥ 150 trades, CI95 lower bound > 0, and mean > 0 with the top 3 trades removed.

## S1 — originality / creator pre-filter (merged with the creator track-record veto)

The `orcACR` rule vetoes a coin if any of these holds:

- the creator launched before,
- the name+symbol was seen before,
- the dev's initial buy is over 4 SOL.

Base rates across all 9,930 recorded launches:

- 70% are from repeat creators.
- 57% reuse a name+symbol.
- 7% have a dev buy over 4 SOL.

**Completion within coverage** (applies to every launch):

| rule | passed n | passed half-curve / complete | vetoed n | vetoed half-curve / complete |
|---|---|---|---|---|
| orcACR | 854 | 2.1% / 0.7% | 2,258 | 5.7% / 2.7% |
| creator track record (≥1 known prior, all dead) | 2,446 | 5.6% / 2.7% | 666 | 1.35% / 0.3% (p < 1e-4) |

- **orcACR goes the wrong way.** The coins it vetoes complete *more* often. On this tape, repeat creators and copycat names are the ones that run.
- **The creator track record does separate completion rates.** That repeats the label effect found earlier.

**FRESH desk PnL** (fresh_g1 exit): there are only 11 discovery and 3 held-out entries in total, so the result is meaningless.

- orcACR at 1.5 s: passed +2.6% (n = 7) vs vetoed −21.5% (n = 4). Bonferroni p = 0.40.
- The creator veto fires on 1 entry.
- **FAIL.**

**Secondary check** (all coins at 15 s, 60 s time exit):

- **orcACR at 0.5 s, discovery:** passed −5.0% (n = 156) vs vetoed −13.0% (n = 609). Bonferroni p = 0.15.
- **orcACR at 0.5 s, held out: the ordering reverses.** Passed −10.8% (n = 57) vs vetoed +9.3% (n = 235).
- **Creator veto:** no discovery lift (Bonferroni p = 1.0).
- **FAIL.**

## S2 — manipulation veto (6-cell grid, Bonferroni m = 6)

The grid is wash score {>10, >25, >50} × atomic round-trips {≥1, ≥3}, at 1.5 s latency.

- **FRESH base:** of 61 entries, the veto fires on **0**. **FAIL** (nothing to test).
- **All-coins base:** of 2,382 entries, 40 are flagged by any cell, and at most 11 in discovery. All Bonferroni p = 1.0.
- **Rate of hitting −50% within coverage:** vetoed 7–8% vs passed 14%. The veto is not even selecting the dumps. **FAIL.**
- **Not computed: creator-funded creation-block snipers.** This needs a Helius funder hop and slot data, and we have neither.
- **The wash score is a proxy:** flips / (|net tokens| + 1).

## S3 — re-entry after the sniper overhang clears

**Cohort:** the creator plus buyers in the first 2 s. There is no slot data, so this replaces "first 5 slots".

**Trigger at 60 s.** All of these must hold:

- the cohort holds ≤ 20% of what it bought,
- ≥ 10 new buyers in 60 s,
- net inflow > 0,
- the curve is 25–70% full.

**Exits:** TP +40%, SL −20%, a 10 min time stop, or 95% curve progress.

| arm | triggers | spec trades (full 10 min covered) |
|---|---|---|
| treatment (inventory cleared) | 108 | **0** |
| control (no inventory condition) | 169 | **0** |

- **The spec cannot be priced.** `launch_stream` records only about 170 s per coin, so no trigger has 10 minutes of coverage.
- **Diagnostic (exit at the end of coverage; NOT the spec):**
  - Treatment: means of +80% / −11% / +440% at 0.5 / 1.5 / 3 s latency.
  - With the top trade removed, all three are negative: −18% / −13% / −15%.
  - Control looks the same.
  - The swings between latencies come from one or two outlier fills.
- **FAIL / unpriceable.**

## What a 72 h rerun needs

1. **≥ 72 h of continuous tape with the capture outages fixed.** About 112 of 442 min were silent, and DB-lock gaps also need fixing.
2. **Trade coverage of ≥ 10 min per coin.** S3 needs it, and so do the long horizons in PR 187. Today, per-coin trade following stops at about 180 s.
3. **Slot numbers on `launch_trades`.** This is for creation-block and first-5-slot cohorts.
4. **A funder hop (Helius) for early buyers.** This supplies `funder_is_creator` and the fund-linked snipers for S2.
5. **Sample size.** At the current rate, FRESH produces about 8 entries an hour. Reaching ≥ 150 held-out FRESH trades needs roughly 90+ h. All-coin entry sets run at about 320 an hour, so they clear the bar easily in 72 h (about 4.6k held-out entries).
