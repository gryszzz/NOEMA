# Pre-registration: Kalshi FX 15-minute "spot-volatility" agents (paper only)

Committed 2026-10-22 ~22:10 ET, **before** any new historical re-score or forward data was computed.
Approved by Tony (owner) as a strict paper test. Nothing here touches real money or any live flag;
`NOEMA_ALLOW_LIVE_ORDERS=0` / `NOEMA_EXECUTION_GATEWAY_ENABLED=0` stay as they are. Passing does **not**
promote anything; it only earns a proposal to Tony.

## 1. Hypothesis

The two agents fired on 2026-10-22 02:48 UTC for worse-than-market Brier (`pm-other-spotvol-2d3462`,
`pm-other-model_cal-364537`) showed +6.9¢ / +7.1¢ per contract (95% CI ≈ 5.5–8.7¢, n ≈ 3.1k / 2.9k) in the
`pm_models` walk-forward backtest. That backtest entered at the snapshot quote with no delay, no slippage
and no quote-freshness check. **H1:** when entries are priced at the ask ~1 minute after the decision, with
taker fees, 1¢ slippage and freshness gates, these agents still earn a positive mean P&L per contract.
**H0:** the edge is a backtest artifact (stale/at-snapshot pricing, look-ahead) and the mean is ≤ 0.

## 2. Agents and fixed parameters (frozen for the whole window)

| Test agent | Source agent | Probability |
|---|---|---|
| `fx-spotvol` | `pm-other-spotvol-2d3462` (genome `{category: other, model: spotvol, window_days: 999, ridge: 1.0}`) | `pm_models.spotvol_prob` unchanged: lognormal from reference spot, per-5m realized vol over prior 24h × hour-of-day profile over prior 30d (bars strictly before the decision), sd × 1.1, clipped to [0.01, 0.99] |
| `fx-model_cal` | `pm-other-model_cal-364537` (genome `{category: other, model: model_cal, window_days: 999, ridge: 1.0}`) | `sigmoid(w0 + w1·logit(p_spotvol))` with **frozen** `w0 = -0.23540, w1 = 0.56220` (ridge-1 Platt fit on all 3,678 `category='other'` rows with a model prob in `pm_train` through close 2026-10-22 20:00 UTC; no refits during the window) |

- **Universe:** Kalshi `KXGBPUSD15M`, `KXAUDUSD15M`, `KXUSDCAD15M` (the "other" category). One binary
  `greater_or_equal` market per 15-minute event.
- **Reference spot:** Yahoo 5-minute bars `GBPUSD=X`, `AUDUSD=X`, `CAD=X` (bars are stamped at their start;
  a bar is usable only once `start + 300 ≤ decision time`).
- **Decision time** `t_d = close − 450 s` (identical to the training snapshot lead for 15-minute markets).
- **Signal quote:** Kalshi yes bid/ask observed at or before `t_d`, at most 60 s old (live: REST read at `t_d`;
  history: the 1-minute candle ending in `[t_d − 60, t_d]`). Require `0 < bid < ask < 1` and `ask − bid ≤ 0.20`.
- **Spot freshness:** newest usable bar must have closed within 15 minutes of `t_d`, and ≥ 30 non-zero 5m
  returns in the prior 24h; otherwise skip (logged `stale_spot`).
- **Decision rule (unchanged from the backtest):** buy YES if `p − ask_d > fee(ask_d)`; else buy NO if
  `(1 − p) − (1 − bid_d) > fee(1 − bid_d)`; else no trade.
- **Entry / fill:** re-quote at `t_d + 60 s` (accept a quote timestamped in `[t_d + 60, t_d + 120]`; history:
  first 1-minute candle ending in that range). Fill = ask (YES) or `1 − bid` (NO) at that quote **+ 1¢
  slippage**, capped at 0.99. The edge is **not** re-checked at the fill (conservative: we eat the delay).
  No fresh fill quote → skip (logged `stale_fill`). Only data timestamped before `t_d` drives the decision.
- **Fees:** Kalshi taker fee `ceil(100 · 0.07 · C · P · (1 − P)) / 100` dollars per order (`P` = fill price,
  `C` = contracts), i.e. `0.07·P·(1−P)` per contract rounded up per order.
- **Size:** fixed **10 contracts** per trade, paper only.
- **P&L per contract** = settlement (1/0 for the side held) − fill − fee/C. Settlement = Kalshi official result.

## 3. Window

Forward test: trades whose decision time falls in **2026-10-23 00:00:00 ET → 2026-10-29 23:59:59 ET**.
Kalshi FX 15M markets run Sun evening → Fri ~17:00 ET, so the window has 6 market days (Fri 23, Sun 25 (short),
Mon 26 – Thu 29). Trades logged before Oct 23 00:00 ET are burn-in and excluded.

## 4. Pass criteria (each agent separately; all must hold)

1. **Sample size:** ≥ **500** settled trades. Justification: per-contract P&L on a binary has sd ≈ 0.5; with
   z = 2.24 (Bonferroni, below) a true +5¢ edge needs n ≈ (2.24·0.5/0.05)² ≈ 500 to clear zero. Expected
   volume is ~250 markets/full day × ~0.8 trade rate × 5–6 days ≈ 1,000–1,200, so 500 is reachable.
2. **Mean after fees and slippage > 0** and the **97.5% clustered bootstrap CI** (10,000 resamples, clusters =
   15-minute close time, because the three USD pairs are correlated) **excludes 0**.
3. **Daily consistency:** mean P&L > 0 on **≥ 5** of the ET days that have ≥ 30 settled trades for the agent
   (Tony's "5 of 7"; only 6 days can have markets, so this is ≥ 5 of ≤ 6).
4. **Multiple-variant correction:** 2 agents are tested → Bonferroni α = 0.05/2, hence the 97.5% CI in (2).
   (They are near-monotone transforms of the same signal; Bonferroni is conservative.) The historical
   leaderboard tested ~25 agents and BH-corrected; the forward test is the clean out-of-sample confirmation.

Reported but **not** gating: per-pair breakdown, 0¢ / 2¢ slippage sensitivity, a "re-check edge at fill"
variant, skip counts by reason, Brier vs market mid.

## 5. Kill / fail rules

- **Early kill (per agent):** once ≥ 200 settled trades, if the 97.5% CI upper bound < 0 → stop that agent, FAIL.
- **Drawdown kill:** cumulative paper P&L ≤ **−$300** (10-contract trades) → stop that agent, FAIL.
- **Data-integrity pause:** if > 25% of eligible markets skip for stale data on a market day, the day does not
  count; the window extends by that day (max +3 days). Logged, not a pass.
- **No mid-window changes:** any change to model, parameters or decision/fill logic voids the run; it restarts
  with a new pre-registration.
- **End of window:** any criterion unmet → FAIL: agents stay fired and are never wired to money. PASS → still
  paper; I bring Tony the numbers and he alone decides whether anything further happens.

## 6. Historical red-team (same rules, run after this commit)

Re-score all `category='other'` rows in `pm_train` with the rules above using Kalshi public 1-minute
candlesticks (signal candle ending in `[t_d−60, t_d]`, fill candle ending in `[t_d+60, t_d+120]`, +1¢,
fees), walk-forward for `model_cal` exactly as the original backtest. Also check for look-ahead in the
reference bars (bar stamp vs Kalshi `expiration_value`). **Verdict rule:** the +7¢ "survives" only if the mean
is > 0 with the 97.5% clustered CI excluding 0; otherwise it "collapses".

## 7. Logging

Every decision (trade or skip) is written to `data/fx_spotvol.db` with: agent, ticker, decision time, signal
quote time/bid/ask, spot bar time/value, model prob, side, fill quote time, fill price (incl. slippage),
fee, contracts, settlement, P&L, skip reason. Heartbeat `agent:fx_spotvol` in `/api/heartbeats`.
