# Structural-edge strategies (paper only)

`noema/edge_strategies.py` runs six strategies in PAPER. Each signal is costed (Kalshi fee 0.07·p·(1−p),
Polymarket US fee assumption `NOEMA_PMUS_FEE_PER_CONTRACT=0.01`, slippage `NOEMA_STRATEGY_SLIPPAGE=0.005`
per leg), recorded in the accuracy ledger as `strategy:<name>`, and settled from official outcomes or
the current bid. `LIVE_SUPPORTED = False`. Every live flag stays 0.

| # | Strategy | Edge source | Guard rails |
|---|---|---|---|
| 1 | `pm_arb` | Kalshi strike-ladder monotonicity, mutually-exclusive bracket NO-sum, Kalshi↔Polymarket US gaps | direction read from each market's own title (mixed `<`/`>` ladders are skipped), legs quoted within 90 s, market still open, $25 cap (top-of-book only), cross-venue = resolution-review flag |
| 2 | `news_latency` | X `not_priced_in` bursts with a direction; primary-source articles (Fed, BLS, WH, Fed Register, courts…) with a confident stance | skipped if price already moved 3c toward the news; 6 h time stop |
| 3 | `fav_longshot` | price-bucket bias by market family | walk-forward (fit on first 60 %, test on last 40 %); only rules positive out of sample trade |
| 4 | `smart_money` | wallets scored on realized round-trip PnL from public GeckoTerminal pool trades | followed only with ≥ 15 round trips, t ≥ 2, PnL > 0; 2-min delay; exit when the wallet sells or after 24 h |
| 5 | `meme_catalyst` | alpha discovery now needs a catalyst, not just clean screens | no wash-trading fingerprint, no bundled launch, buys/sells 1.1–3×, h1 volume ≥ 1.5× average, ≥ 2 of: smart money buying, credible accelerating X narrative, rising unique buyers, second-leg breakout on a > 3-day, > $500k-liquidity token |
| 5b | `majors_momentum` | BTC/ETH/SOL above SMA20 with positive 7-day momentum | 30 bps per switch |
| 6 | allocator | weight ∝ shrunk Sharpe, n/(n+30) | exploration weight under 30 settled; benched on negative shrunk EV or a no-edge backtest; benched signals are kept as `shadow` rows so the ledger still scores them |

Sizing: allocator weight × bankroll (`NOEMA_STRATEGY_PAPER_BANKROLL_USD`, default $1,000), capped at the
capital guard's unproven 3 % tier per trade.

## Backtests (honest numbers, 21 Oct 2026)

* **Favourite-longshot (Kalshi, 2,460 resolved markets with a prior-day 1–10c-spread quote).**
  The bias is real in calibration: 5–10c contracts priced 6.8c resolved YES 4.4 %; 80–90c priced 87c resolved 93.4 %.
  But out of sample after fees and the spread: **n = 131, EV −$0.026 per contract, hit rate 75.6 %, t = −0.8 →
  no edge after fees → benched.** Caveats: prior-day quote, sports-heavy sample.
* **Majors momentum (Coinbase daily, 328 days per coin).** EV −0.04 % per day for BTC, ETH and SOL; in-market hit rate 44–47 %.
  BTC −15.3 % against buy-and-hold −7.4 %, ETH −17.5 % against −17.5 %, SOL −20.0 % against −23.6 %. **No edge → benched (shadow only).**
* **Arb, news latency, smart money, meme catalyst:** there's no historical order-book or wallet history to replay, so these are forward-only and get scored as they settle.

## Surfaces

* `GET /api/strategies` returns the read-only board.
* `/static/strategies-panel.js` is the STRATEGIES panel. It mounts automatically in the room, and on laptops it's dock tab 7.
* To run the loop: `python -m noema.edge_strategies --loop` (default every 5 min; backtests refresh hourly).
