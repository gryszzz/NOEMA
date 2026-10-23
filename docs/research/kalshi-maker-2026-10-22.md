# Results: Kalshi maker (resting limit) variants of the pm_models shadow agents

Pre-registration: [kalshi-maker-prereg-2026-10-22.md](kalshi-maker-prereg-2026-10-22.md) (committed afa7d72,
2026-10-22 22:02 ET, before any maker number was computed). Backtest run 2026-10-22 ~22:05 ET with
`python -m noema.kalshi_maker --backtest` (read-only on `noema.db`, `pm_train.db`, `edge_avenues.db`).
Raw output: [kalshi-maker-2026-10-22.json](kalshi-maker-2026-10-22.json). Paper/research only.

## Verdict

**0 of 32 pass.** Resting limits do not rescue these agents on the recorded data — they make them much worse.
Apart from the one exception below, every primary test with at least 30 fills loses 4–24¢ per filled contract as a
maker, against roughly −0.5¢ as a taker.
The losses come from adverse selection: under the trade-through fill rule an order fills only when the market
moves through its price, and those are mostly the markets where the agent was wrong. Fees barely matter
(M = 0 changes results by about 0.3¢), and so does queue position (at-price fills change them by 1–4¢).

Under the pre-registered rule, two tests are **"promising, under-sampled"** (mean > 0, minimum sample not met),
so the 7-day paper forward run is triggered:
- `pm-markets-stack-396545 / improve`: +9.41¢ on 113 fills, 61 events, 1 day; 95% CI [−3.88, +22.67]¢. The
  secondary live-forecast data **does not support it**: −2.90¢ on 669 fills, CI [−7.69, +1.66]¢.
- `pm-economic-market_cal-3fd698 / improve`: +5.40¢ on **1** fill. This is noise.

On the secondary live-forecast data, which is a sensitivity and not used for the verdict, `economic-3fd698`
(5–6 fills) and the three weather `market_cal` agents (75–88 fills, +1 to +5¢, every CI spans 0) are also
positive but under-sampled. The forward run tests all 32 again, on forward data only.

Coverage caveats: there is about 1.5 days of trade/quote evidence, almost all of it on Oct 22 UTC, so no test
can reach the required 3 days with ≥ 30 fills. Only 1–4% of primary orders were evaluable (most snapshot markets
predate the recorded tape). Weather and economics have almost no tape in the primary set.

Structural observation: with a 1¢ spread, JOIN almost always quotes one side. When p is between bid and ask, one
side's edge is at least half the spread, which beats the ≤ 0.44¢ maker fee. That is why JOIN order counts nearly
equal decision counts, and why the maker rule's "edge" is mostly the spread, not the model.

### Primary (verdict): walk-forward OOS snapshots, pm_train

176105 agent×market cases. Cents per filled contract, net of maker fee (M=1) and 0.5¢ penalty.

| agent | variant | orders | evaluable | fill rate | fills | events | mean ¢ | 95% CI ¢ | Holm p | per day (n, ¢) | M=0 ¢ | at-price fills ¢ | taker same mkts (n, ¢) | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| crypto-market_cal-1eb3c0 | join | 21926 | 828 | 0.61 | 507 | 271 | -11.68 | [-16.05, -7.59] | 1.00 | 10-22: 450, -9.61; 10-23: 57, -27.99 | -11.40 | -10.79 | 152, -0.65 | fail |
| crypto-market_cal-1eb3c0 | improve | 4737 | 365 | 0.65 | 237 | 111 | -11.70 | [-17.95, -5.70] | 1.00 | 10-22: 216, -9.54; 10-23: 21, -33.94 | -11.43 | -10.48 | 0, — | fail |
| crypto-market_cal-25afbc | join | 21928 | 828 | 0.61 | 507 | 271 | -11.68 | [-16.05, -7.59] | 1.00 | 10-22: 450, -9.61; 10-23: 57, -27.99 | -11.40 | -10.79 | 152, -0.65 | fail |
| crypto-market_cal-25afbc | improve | 4737 | 365 | 0.65 | 237 | 111 | -11.70 | [-17.95, -5.70] | 1.00 | 10-22: 216, -9.54; 10-23: 21, -33.94 | -11.43 | -10.48 | 0, — | fail |
| crypto-market_cal-40c622 | join | 21926 | 828 | 0.61 | 507 | 271 | -11.68 | [-16.05, -7.59] | 1.00 | 10-22: 450, -9.61; 10-23: 57, -27.99 | -11.40 | -10.79 | 152, -0.65 | fail |
| crypto-market_cal-40c622 | improve | 4738 | 365 | 0.65 | 237 | 111 | -11.70 | [-17.95, -5.70] | 1.00 | 10-22: 216, -9.54; 10-23: 21, -33.94 | -11.43 | -10.48 | 0, — | fail |
| crypto-market_cal-4c72c6 | join | 21928 | 828 | 0.61 | 509 | 273 | -11.78 | [-16.00, -7.58] | 1.00 | 10-22: 452, -9.73; 10-23: 57, -27.99 | -11.50 | -10.89 | 166, -0.83 | fail |
| crypto-market_cal-4c72c6 | improve | 4747 | 370 | 0.65 | 241 | 112 | -11.15 | [-17.77, -5.18] | 1.00 | 10-22: 220, -8.98; 10-23: 21, -33.94 | -10.88 | -9.98 | 0, — | fail |
| economic-market_cal-3fd698 | join | 326 | 7 | 0.57 | 4 | 2 | -37.41 | — | 1.00 | 10-22: 4, -37.41 | -37.25 | -37.41 | 6, -2.55 | fail |
| economic-market_cal-3fd698 | improve | 138 | 2 | 0.50 | 1 | 1 | +5.40 | — | 1.00 | 10-22: 1, +5.40 | +5.50 | +5.40 | 2, +3.26 | promising, under-sampled |
| markets-market_cal-359eb4 | join | 21410 | 298 | 0.77 | 229 | 170 | -17.57 | [-25.21, -9.71] | 1.00 | 10-22: 229, -17.57 | -17.27 | -16.22 | 0, — | fail |
| markets-market_cal-359eb4 | improve | 9107 | 124 | 0.77 | 96 | 39 | -23.72 | [-38.58, -8.05] | 1.00 | 10-22: 96, -23.72 | -23.41 | -21.74 | 0, — | fail |
| markets-stack-396545 | join | 19962 | 300 | 0.77 | 232 | 185 | -4.21 | [-10.36, +2.46] | 1.00 | 10-22: 232, -4.21 | -3.94 | -0.82 | 0, — | fail |
| markets-stack-396545 | improve | 8250 | 149 | 0.76 | 113 | 61 | +9.41 | [-3.88, +22.67] | 1.00 | 10-22: 113, +9.41 | +9.69 | +10.55 | 0, — | promising, under-sampled |
| markets-stack-4fb13f | join | 19967 | 300 | 0.61 | 182 | 125 | -15.35 | [-25.11, -5.58] | 1.00 | 10-22: 182, -15.35 | -15.03 | -14.59 | 0, — | fail |
| markets-stack-4fb13f | improve | 8347 | 139 | 0.73 | 101 | 44 | -20.77 | [-35.15, -6.31] | 1.00 | 10-22: 101, -20.77 | -20.47 | -20.90 | 0, — | fail |
| other-market_cal-a30995 | join | 3650 | 80 | 0.68 | 54 | 54 | -8.72 | [-19.73, +2.49] | 1.00 | 10-22: 54, -8.72 | -8.41 | -5.22 | 4, +3.18 | fail |
| other-market_cal-a30995 | improve | 2939 | 59 | 0.71 | 42 | 42 | -9.93 | [-23.96, +4.90] | 1.00 | 10-22: 42, -9.93 | -9.57 | -7.91 | 0, — | fail |
| other-stack-248d77 | join | 3443 | 80 | 0.62 | 50 | 50 | -11.73 | [-23.97, +0.24] | 1.00 | 10-22: 50, -11.73 | -11.40 | -7.47 | 32, +0.88 | fail |
| other-stack-248d77 | improve | 2741 | 54 | 0.69 | 37 | 37 | -15.67 | [-30.50, -0.89] | 1.00 | 10-22: 37, -15.67 | -15.31 | -13.09 | 25, +2.18 | fail |
| other-stack-51783d | join | 3443 | 80 | 0.70 | 56 | 56 | -17.07 | [-26.04, -7.21] | 1.00 | 10-22: 56, -17.07 | -16.77 | -13.13 | 33, +7.05 | fail |
| other-stack-51783d | improve | 2720 | 48 | 0.69 | 33 | 33 | -13.64 | [-27.84, +0.81] | 1.00 | 10-22: 33, -13.64 | -13.29 | -10.88 | 23, +8.94 | fail |
| other-stack-f65274 | join | 3443 | 80 | 0.70 | 56 | 56 | -13.96 | [-23.59, -3.30] | 1.00 | 10-22: 56, -13.96 | -13.66 | -10.23 | 30, +6.86 | fail |
| other-stack-f65274 | improve | 2714 | 48 | 0.71 | 34 | 34 | -15.94 | [-30.21, -1.57] | 1.00 | 10-22: 34, -15.94 | -15.59 | -13.13 | 21, +8.41 | fail |
| weather-market_cal-3e1a7c | join | 670 | 18 | 0.44 | 8 | 3 | -16.90 | — | 1.00 | 10-22: 8, -16.90 | -16.62 | -12.05 | 12, -0.12 | fail |
| weather-market_cal-3e1a7c | improve | 462 | 12 | 0.58 | 7 | 3 | -23.76 | — | 1.00 | 10-22: 7, -23.76 | -23.50 | -23.76 | 7, -5.85 | fail |
| weather-market_cal-52ac9d | join | 670 | 18 | 0.44 | 8 | 3 | -16.90 | — | 1.00 | 10-22: 8, -16.90 | -16.62 | -12.05 | 8, +7.99 | fail |
| weather-market_cal-52ac9d | improve | 462 | 12 | 0.58 | 7 | 3 | -23.76 | — | 1.00 | 10-22: 7, -23.76 | -23.50 | -23.76 | 3, +8.13 | fail |
| weather-market_cal-b2469c | join | 670 | 18 | 0.44 | 8 | 3 | -16.90 | — | 1.00 | 10-22: 8, -16.90 | -16.62 | -12.05 | 12, -0.12 | fail |
| weather-market_cal-b2469c | improve | 463 | 12 | 0.58 | 7 | 3 | -23.76 | — | 1.00 | 10-22: 7, -23.76 | -23.50 | -23.76 | 7, -5.85 | fail |
| weather-stack-80285d | join | 466 | 11 | 0.36 | 4 | 2 | -41.84 | — | 1.00 | 10-22: 4, -41.84 | -41.50 | -41.84 | 7, -8.27 | fail |
| weather-stack-80285d | improve | 352 | 9 | 0.44 | 4 | 2 | -42.83 | — | 1.00 | 10-22: 4, -42.83 | -42.50 | -20.64 | 5, -12.32 | fail |

### Secondary (sensitivity only): agents' recorded live forecasts

8535 agent×market cases. Cents per filled contract, net of maker fee (M=1) and 0.5¢ penalty.

| agent | variant | orders | evaluable | fill rate | fills | events | mean ¢ | 95% CI ¢ | Holm p | per day (n, ¢) | M=0 ¢ | at-price fills ¢ | taker same mkts (n, ¢) | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| crypto-market_cal-1eb3c0 | join | 1287 | 1286 | 0.62 | 798 | 397 | -9.48 | [-12.59, -6.72] | 1.00 | 10-22: 609, -9.30; 10-23: 189, -10.04 | -9.18 | -8.31 | 34, -6.12 | fail |
| crypto-market_cal-1eb3c0 | improve | 769 | 768 | 0.61 | 469 | 198 | -6.57 | [-10.24, -3.26] | 1.00 | 10-22: 368, -6.72; 10-23: 101, -6.03 | -6.29 | -5.74 | 0, — | fail |
| crypto-market_cal-25afbc | join | 817 | 817 | 0.65 | 531 | 252 | -9.79 | [-13.35, -6.50] | 1.00 | 10-22: 342, -9.65; 10-23: 189, -10.04 | -9.51 | -8.75 | 24, -11.49 | fail |
| crypto-market_cal-25afbc | improve | 475 | 475 | 0.64 | 304 | 125 | -8.80 | [-12.70, -4.55] | 1.00 | 10-22: 203, -10.18; 10-23: 101, -6.03 | -8.53 | -7.77 | 0, — | fail |
| crypto-market_cal-40c622 | join | 817 | 817 | 0.65 | 531 | 252 | -9.79 | [-13.35, -6.50] | 1.00 | 10-22: 342, -9.65; 10-23: 189, -10.04 | -9.51 | -8.75 | 24, -11.49 | fail |
| crypto-market_cal-40c622 | improve | 475 | 475 | 0.64 | 304 | 125 | -8.80 | [-12.70, -4.55] | 1.00 | 10-22: 203, -10.18; 10-23: 101, -6.03 | -8.53 | -7.77 | 0, — | fail |
| crypto-market_cal-4c72c6 | join | 817 | 817 | 0.65 | 530 | 252 | -8.28 | [-11.81, -4.80] | 1.00 | 10-22: 341, -8.14; 10-23: 189, -8.53 | -7.99 | -7.32 | 24, -11.49 | fail |
| crypto-market_cal-4c72c6 | improve | 471 | 471 | 0.64 | 300 | 121 | -7.25 | [-11.66, -3.10] | 1.00 | 10-22: 201, -9.27; 10-23: 99, -3.15 | -6.98 | -6.30 | 0, — | fail |
| economic-market_cal-3fd698 | join | 124 | 124 | 0.05 | 6 | 6 | +22.81 | [-20.32, +53.92] | 1.00 | 10-22: 6, +22.81 | +23.17 | +21.73 | 110, -0.29 | promising, under-sampled |
| economic-market_cal-3fd698 | improve | 110 | 110 | 0.05 | 5 | 5 | +41.55 | [+21.89, +57.28] | 0.02 | 10-22: 5, +41.55 | +41.90 | +41.55 | 96, -0.78 | promising, under-sampled |
| markets-market_cal-359eb4 | join | 1089 | 1072 | 0.82 | 875 | 305 | -9.15 | [-12.56, -5.34] | 1.00 | 10-22: 768, -9.07; 10-23: 107, -9.74 | -8.89 | -7.25 | 0, — | fail |
| markets-market_cal-359eb4 | improve | 678 | 669 | 0.86 | 572 | 105 | -8.70 | [-13.50, -2.86] | 1.00 | 10-22: 511, -8.50; 10-23: 61, -10.37 | -8.43 | -8.01 | 0, — | fail |
| markets-stack-396545 | join | 1027 | 1019 | 0.82 | 837 | 296 | -5.33 | [-8.80, -1.95] | 1.00 | 10-22: 729, -4.44; 10-23: 108, -11.32 | -5.07 | -3.50 | 2, -7.08 | fail |
| markets-stack-396545 | improve | 777 | 771 | 0.87 | 669 | 148 | -2.90 | [-7.69, +1.66] | 1.00 | 10-22: 595, -1.20; 10-23: 74, -16.60 | -2.64 | -2.02 | 0, — | fail |
| markets-stack-4fb13f | join | 1077 | 1069 | 0.58 | 623 | 265 | -8.12 | [-15.30, -1.00] | 1.00 | 10-22: 519, -7.12; 10-23: 104, -13.13 | -7.81 | -5.80 | 13, +2.12 | fail |
| markets-stack-4fb13f | improve | 743 | 737 | 0.60 | 443 | 108 | -6.95 | [-15.75, +2.25] | 1.00 | 10-22: 376, -5.57; 10-23: 67, -14.67 | -6.64 | -5.40 | 2, +7.09 | fail |
| other-market_cal-a30995 | join | 513 | 485 | 0.72 | 350 | 282 | -3.33 | [-9.21, +1.43] | 1.00 | 10-22: 302, -2.54; 10-23: 48, -8.28 | -3.01 | -2.96 | 7, +7.81 | fail |
| other-market_cal-a30995 | improve | 380 | 352 | 0.70 | 247 | 184 | -1.87 | [-8.57, +4.20] | 1.00 | 10-22: 218, -1.45; 10-23: 29, -5.04 | -1.55 | -0.86 | 0, — | fail |
| other-stack-248d77 | join | 123 | 123 | 0.76 | 94 | 94 | -0.94 | [-10.43, +9.08] | 1.00 | 10-22: 72, +2.55; 10-23: 22, -12.37 | -0.58 | -0.60 | 52, +12.20 | fail |
| other-stack-248d77 | improve | 91 | 91 | 0.77 | 70 | 70 | -3.92 | [-15.38, +6.67] | 1.00 | 10-22: 53, -0.46; 10-23: 17, -14.73 | -3.54 | -1.08 | 38, +14.40 | fail |
| other-stack-51783d | join | 132 | 132 | 0.80 | 105 | 105 | -1.76 | [-10.41, +6.83] | 1.00 | 10-22: 84, +0.18; 10-23: 21, -9.51 | -1.41 | -1.44 | 61, +7.35 | fail |
| other-stack-51783d | improve | 91 | 91 | 0.82 | 75 | 75 | -0.53 | [-11.24, +10.09] | 1.00 | 10-22: 61, +0.64; 10-23: 14, -5.66 | -0.15 | +1.31 | 42, +7.51 | fail |
| other-stack-f65274 | join | 123 | 123 | 0.80 | 98 | 98 | -0.43 | [-9.36, +9.00] | 1.00 | 10-22: 77, +1.67; 10-23: 21, -8.13 | -0.07 | -0.10 | 54, +7.55 | fail |
| other-stack-f65274 | improve | 91 | 91 | 0.82 | 75 | 75 | -0.45 | [-11.71, +10.22] | 1.00 | 10-22: 60, +0.71; 10-23: 15, -5.08 | -0.06 | +1.39 | 41, +8.28 | fail |
| weather-market_cal-3e1a7c | join | 198 | 198 | 0.43 | 86 | 51 | +1.89 | [-6.61, +9.88] | 1.00 | 10-22: 73, +2.55; 10-23: 13, -1.84 | +2.15 | +2.28 | 172, +0.97 | promising, under-sampled |
| weather-market_cal-3e1a7c | improve | 172 | 172 | 0.44 | 75 | 50 | -0.01 | [-9.75, +8.26] | 1.00 | 10-22: 60, +0.81; 10-23: 15, -3.32 | +0.23 | +0.54 | 149, +0.68 | fail |
| weather-market_cal-52ac9d | join | 187 | 187 | 0.47 | 88 | 51 | +3.56 | [-4.89, +11.06] | 1.00 | 10-22: 75, +4.49; 10-23: 13, -1.84 | +3.82 | +3.83 | 116, +4.63 | promising, under-sampled |
| weather-market_cal-52ac9d | improve | 161 | 161 | 0.47 | 76 | 49 | +1.34 | [-7.63, +9.65] | 1.00 | 10-22: 61, +2.48; 10-23: 15, -3.32 | +1.58 | +2.63 | 95, +4.29 | promising, under-sampled |
| weather-market_cal-b2469c | join | 187 | 187 | 0.47 | 88 | 51 | +5.02 | [-3.35, +12.40] | 1.00 | 10-22: 75, +4.75; 10-23: 13, +6.62 | +5.28 | +5.20 | 158, +2.95 | promising, under-sampled |
| weather-market_cal-b2469c | improve | 161 | 161 | 0.47 | 76 | 49 | +1.63 | [-8.01, +9.51] | 1.00 | 10-22: 61, +1.04; 10-23: 15, +4.01 | +1.87 | +2.87 | 136, +2.12 | promising, under-sampled |
| weather-stack-80285d | join | 12 | 12 | 0.17 | 2 | 1 | -85.23 | — | 1.00 | 10-22: 2, -85.23 | -85.00 | -85.23 | 7, -16.72 | fail |
| weather-stack-80285d | improve | 12 | 12 | 0.17 | 2 | 1 | -86.22 | — | 1.00 | 10-22: 2, -86.22 | -86.00 | -86.22 | 7, -16.72 | fail |

## Forward paper run
`python -m noema.kalshi_maker --loop` (started from `noema-run/start.sh`) re-applies the same frozen rules every
15 min to the agents' live forecasts recorded after its start. After each quoted market settles, it fetches that
market's public trade prints once with a read-only GET to `/markets/trades` into `data/kalshi_maker.db`. Those
prints are a complete history, so non-fills are evaluable. The window is 7 days from first start. The verdict
freezes 24 h after the window ends, so the last markets can settle, and is stored in `km_reports` as
`forward_final`. Same criteria, same Holm over 32, forward data only. Heartbeat: `agent:kalshi-maker-sim`.
It never places orders, reads keys, or touches live/owner flags.
