# Agent lifecycle, edge mining, adaptive exits, fresh setups (paper/shadow only)

Nothing here places orders or changes live/owner flags, the capital guard or secrets.

* `noema/agent_lifecycle.py` — roster of rule agents (genome = entry filters + exit params) in
  `data/agent_lifecycle.db`. Scored prospectively on pool sweeps captured after hire. Fired when
  n >= 30 and the 95% upper bound of mean net return < 0 (or n >= 90 and mean < 0). A post-mortem
  (losses by liquidity / age / disposition) is written; a replacement is a mutation of the best
  surviving agent plus the post-mortem fix. New hires start in shadow with weight 0 and earn weight
  only with n >= 30 and a 95% lower bound > 0. Population cap 12. Forecast agents from the accuracy
  ledger are scored on Brier vs the market price (advisory verdict). Run: `python -m noema.agent_lifecycle --loop`.
  API: `GET /api/roster`.
* `noema/edge_mining.py` — walk-forward (first 60% by time = train, last 40% = holdout), costs
  (2% round trip + constant-product slippage on a $25 clip, upside capped at +300%), bootstrap
  95% CI, Benjamini-Hochberg across rules carried to the holdout. Survivors seed shadow agents only.
* `noema/adaptive_exits.py` — partial TP then break-even stop, volatility-trailed runner,
  conditional time-stop extension, liquidity-pull / volume-dry / holder-selling / X-flip exits,
  post-exit price logging (`alpha_exit_paths`). Default for the alpha paper book
  (`NOEMA_ALPHA_ADAPTIVE_EXITS=0` restores the old fixed ladder).
* `noema/setup_universe.py` + `config/setup_universe.json` — fresh-setup source in
  `/api/opportunities`: majors/blocklist/mcap ceiling excluded, novelty cooldown unless material change.
