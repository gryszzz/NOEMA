# Weaker prediction-market categories, 2026-10-22 (paper only, `noema.pm_weak`)
Walk-forward (fit only on markets closed before each snapshot), event-clustered bootstrap CIs, Kalshi taker
fee, 1c fee+buffer gate. Diff = model minus market (negative is better).

1. Weather (Kalshi daily high/low, NWS station coords; Open-Meteo previous-run forecasts issued before entry)
   - Forecast-only model: much worse than market (Brier +0.039 eve / +0.059 morn, P&L -4.0c / -2.8c, CIs < 0).
   - Market + forecast stack: eve n=3041, Brier -0.00035 [-0.00082, +0.00015], P&L +3.7c [-0.9, +8.7] (220 trades);
     morn n=3239, Brier -0.00043 [-0.00091, +0.00008], P&L +0.1c [-4.1, +4.2]. Tie. Rain not modelled.
2. Economic releases: not modelled - 16 CPI + 8 payrolls settled markets locally, no free archived consensus
   with issue times; gas/diesel/claims already tie the market (pm_models).
3. Thin markets (spread >= 3c at entry, no look-ahead): n=13,400, Brier +0.00002 [-0.00028, +0.00033],
   P&L -0.6c [-3.9, +2.5]. No edge in any category. Lifetime-volume bucket shows +1.8c [1.1, 2.6] but is
   look-ahead (low final volume leaks the outcome path) - discarded.
   Polymarket US: no settled history with quotes available - not tested.
4. Long-dated politics (14,565 settled Politics markets, 3,443 open >= 45 days):
   - 30d to close, calibration: P&L +4.5c [-0.9, +9.4]; drift mean-reversion: +6.1c [+0.9, +10.7]
     (509 trades, 736 events; by-series clustering +6.1c [+1.0, +11.2]; halves +5.2c [-5.4, +13.5], +6.6c
     [+1.0, +12.4]); annualised +146% [+33%, +248%]. Brier not better than market.
   - 90d: calibration -0.3c [-6.1, +4.6]; drift -9.8c [-18.7, -1.2] (opposite sign).
   One positive result out of 4 politics tests (+ ~20 overall) - treat as a candidate, not proven.
