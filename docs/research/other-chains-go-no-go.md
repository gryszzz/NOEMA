# Other-chain launches: pre-registered go/no-go (paper only)

Study `other_chains_v1`, scanner `noema/other_chains.py`, data `data/other_chains.db`.
The rule below is written and hashed (`oc_preregistration`, sha256) before any outcome exists.
Changing a parameter means starting a new study with a new registration. Nothing in this study signs,
swaps, bridges or moves funds; GO only makes a chain eligible for a proposal Tony reviews.

## Question
Can NOEMA make money buying freshly launched pairs on non-Solana chains at the speed we actually
discover them, after every real cost?

## Chains
| chain | money | signer swap path | how a paper entry is priced |
|---|---|---|---|
| Base | funded (treasury >= $1) | yes (Kyber) | gas + slippage + fees + tax |
| Robinhood | funded | yes (Kyber) | same |
| Ethereum | bridge-dependent | yes | + Relay bridge from the richest funded chain (time and cost, in and out) |
| Polygon | bridge-dependent | yes | same |
| BSC | bridge-dependent | not in signer `SWAP_CHAINS` | same; would also need signer support |
| Arbitrum | bridge-dependent | not in signer `SWAP_CHAINS` | same; would also need signer support |

"Funded" is re-read (amounts only) from the treasury every 5 minutes; a chain that drops below $1
becomes bridge-dependent. Bridge time and cost come from a real Relay quote refreshed hourly (no
wallet in the request; a burn address is used as the quoting user).

## Funnel and buyability (skips counted by reason)
Discovery: GeckoTerminal `new_pools` per chain (round-robin, 4 req/min) plus DexScreener latest
profiles and boosts. Discovery lag = time we first saw the pair - pair creation time; the board reports
the steady-state median and p10 (pairs created after the scanner started watching that chain).

A pair is followed only if it passes, in order: `too_old` (> 1 h at discovery), `not_indexed`,
`low_liquidity` (< max($5k, 200 × size)), `stale_native_price`, `unroutable` / `router_not_pinned` /
`unroutable_sell` (live Kyber buy and sell-back quotes through the signer's pinned router),
`slippage_too_high` (round trip > 10 %), GoPlus `honeypot` / `cannot_trade` / `tax_unknown` /
`high_tax` (> 10 %) / `security_unknown`, and `no_bridge_quote` for unfunded chains. Quote currency
and existing holdings are not filters; the aggregator routes from the native token.

## Paper rule (fixed)
- Size $25. Follow each buyable pair for 15 min with DexScreener snapshots every 30 s.
- Signal: m5 buys >= 10, buys >= 1.5 × sells, m5 volume >= $1k, liquidity >= 70 % of first reading.
- Entry: first fresh snapshot at signal + 20 s execution delay + bridge time. A signal that goes stale
  through a data gap is dropped, never filled late.
- Exit: -35 % stop or 10 min hold. A pair that disappears is booked at -100 %.
- Dead before signal (liquidity < 50 % of first reading) and no-signal pairs are counted, not traded.

## Costs in every trade
Buy and sell tax (GoPlus) + Kyber round-trip loss (slippage + DEX fees; exit leg scaled up as liquidity
shrinks) + gas × 2.5 (buy, sell, approve; Kyber `gasUsd`) + for unfunded chains the Relay bridge cost
twice (in and out) and its time added to the entry delay.

## Decision rule (per chain)
- Minimum sample: 100 closed trades and 7 UTC days, with >= 30 trades in the held-out part.
- Split: chronological by UTC day; first 60 % of days discovery, last 40 % (rounded up) held-out.
- GO requires all of: held-out mean net return > 0; held-out bootstrap lower bound > 0 at
  1 - 0.05/k (k = 6 chains, Bonferroni); each day holds up (among days with >= 5 trades, at most one
  losing day overall and no losing held-out day).
- Early NO-GO: at >= 100 trades, the 95 % upper bound of mean net < 0.
- Decision point: the first UTC day end where the minimum sample is met; that verdict is final for v1.
- No tuning after registration.

## When we can decide
Not before 7 days of collection. Each chain needs ~15 entries/day to reach 100 trades in 7 days; chains
with fewer signals take proportionally longer (the board shows entries so far).
