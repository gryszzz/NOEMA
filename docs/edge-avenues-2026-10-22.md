# Edge avenues, 2026-10-22 (paper only, `noema.edge_avenues`, data/edge_avenues.db)

All entries pass the fee-plus-buffer gate. Outcomes feed the hire/fire roster (kinds funding_carry,
kalshi_mm, news_lag), scored prospectively after hire. The roster file lists results as of first run.

1. Funding carry (Kraken PF_* + Hyperliquid, ~1y hourly, decisions use strictly-past 72h mean).
   7-day amortisation: gate never clears (n=0). 30-day: n=40 episodes, mean -0.03% of notional
   [-0.25%, +0.21%]. Best leg HL ETH +3.3%/yr net, CI includes 0. Funding ~3-6%/yr gross does not
   cover spot+perp taker round trip (0.43-0.82%) by a margin.
2. Kalshi passive MM (fs_quotes snapshots, public trades, fills only strictly through, maker fee,
   |inv|<=10). First 150 settled markets: 18 with fills, 85 fills, -$0.147/market [-0.66, +0.34],
   -3.1c/fill. Adverse selection 60s after fill -3.3c [-6.7, +0.1]: fills are toxic.
3. News/X lag (news_quote_marks). 28 items with t0 mark (X: 11 of 173 - X posts arrive in the inbox
   ~30 min late, so most marks are missed). |mid move| 0 at +60s/+300s, 0.3c at +60m. No 2c move at
   +5m ever happened: no continuation trades. Markets do not react on these timescales; no lag to trade.
4. X queries: `$TICKER` is stripped by X search; x_edge.precise_query rewrites cashtags to quoted names.
5. Meta-launch shield pass & liq>=5k (all captured, in-sample): n=130, +22.8%/trade [+8.9%, +38.2%],
   win 55%; dropping top 5 winners +12.8% [+0.3%, +25.9%]; second half alone +12.0% [-8.4%, +33.5%].
   Prospective (after hire) n=5. One day of data, probe-size slippage model: promising, unproven.
