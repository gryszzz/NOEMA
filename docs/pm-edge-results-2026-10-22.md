# Prediction-market edge check, 2026-10-22 (paper only)

## Kalshi crypto near-close (PR #168 pipeline, walk-forward, no look-ahead)
23,029 snapshots / 15,705 events (5m 17,685; 15m 2,620; 30m 2,601). Index mirror vs Kalshi
expiration value: median 0.5 bps (BTC) to 2.0 bps (DOGE).
No model beats the market price. Best (stack, 5m): Brier diff +0.00004 [-0.00017, 0.00024],
paper P&L after Kalshi fee -0.34c/contract [-1.01, +0.39] (n=6,910 trades).
Raw spot-index model is significantly WORSE than market at 5m (Brier +0.0040 [0.0032, 0.0048]).
Confidence-filtered top 5/10/20% slices by post-fee edge: no slice has a P&L CI lower bound > 0;
the spot model is worst exactly where it disagrees most with the market. Paper trading stays off.

## Cross-venue Kalshi <-> Polymarket US (188 matched pairs, 104 rules-equivalent)
Snapshot: 102 pairs quotable; after both taker fees + 0.5c slippage/leg, median edge -2.2c,
max +0.47c, 1 pair > 0, none >= 1c buffer. 12 of 19 matcher runs since 2026-10-22 00:17 UTC had
a pair > 0 after fees (incl. caveat pairs); per-pair history now kept in `xv_gap_history`,
`python -m noema.xv_match --stats` reports frequency vs fees + buffer.

## Fee-plus-buffer gate
`NOEMA_ENTRY_BUFFER` (default 0.01): strategy signals and fast_settle paper trades only enter when
post-fee expected gain >= buffer; otherwise shadow-only.
