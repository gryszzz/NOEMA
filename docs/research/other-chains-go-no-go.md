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

## v2 arm (separate registration, Base + Robinhood)

v1 above stays frozen. Its rule, sha256 and verdict are unchanged, and v2 never writes to `other_chains.db`.
v2 (`noema/otherchain_v2.py`, `data/other_chains_v2.db`) is a separate study. It has its own registration row
(timestamp and sha256, stored when the process first starts) and uses the same pass bar, costs and paper rule.
It runs on the funded chains Base and Robinhood only, and Bonferroni k = 2. It changes only these things:

1. **GeckoTerminal fallback.** If DexScreener has no listing for a pair, vetting and every follow-up snapshot use
   GeckoTerminal `pools/multi` (price, reserve, m5 buys/sells/volume) instead of skipping the pair as
   `not_indexed`. GT snapshots run at most once a minute per chain, and v2 makes at most 4 GT calls a minute.
   Together with timing's 10/min and v1's 4/min, that stays inside the free ~30/min.
2. **Tax unknown.** v2 first asks honeypot.is, a free buy+sell simulation that covers Base but not Robinhood. If the
   tax is still unknown, it re-checks GoPlus 30, 60 and 120 s after the first unknown. A pair is followed only
   once both taxes are known, it isn't a honeypot and both a buy and a sell route exist. A signal can only fire
   after the pair is followed, so the whole wait is charged as entry lag (`vet_wait_s`, `entry_lag_median_s`).
   A Kyber round-trip quote can't stand in for a tax check, because it prices the pool and doesn't simulate the
   token's transfer.

Discovery reuses v1's feed read-only, so it adds no API calls. v2 picks up each new Base/Robinhood pair within
about 15 s, which adds that much discovery lag. You can read v2 with `python -m noema.otherchain_v2 --board` or
under `v2` in `/api/other-chains`.

## feed:geckoterminal heartbeat (2026-10-22 diagnosis)

The heartbeat read only `timing.db pools.fetched_at`. That value moves only when the timing agent looks up a
pool for a mint it hasn't seen before (a cache miss). After the last new candidate at 9:11 PM EDT, every
GeckoTerminal call was a candle refresh, so the heartbeat went red even though GeckoTerminal kept answering.
Candles were 148 s fresh. GeckoTerminal was never down. Upstream, no new trench `observe` candidates have been
written since 8:55 PM EDT, and that's tracked separately. The probe now takes the newest successful
GeckoTerminal response from any reader: timing pools, timing candles, v1 discovery polls and v2 GT calls.

## Chain allowlist (2026-10-22)

Tony narrowed the scope to chains NOEMA can use with what it holds: Base and Robinhood (funded) and Ethereum.
Solana is the main desk and isn't part of this scanner. `NOEMA_OTHERCHAIN_CHAINS` (or `--chains`, default
`base,ethereum,robinhood`) is now the allowlist. Chains outside it aren't discovered, vetted or shown. Rows
still open on dropped chains (BSC, Arbitrum, Polygon) are closed as `chain_dropped` with no result booked,
so they never count as trades. v1's registered rule and sha256 are unchanged. Its Bonferroni k follows the
registered wording ("k = chains tested"), so it is now 3. Ethereum stays bridge-free only while the treasury
reads at least $1 of ETH on mainnet. Otherwise the real Relay bridge quote is charged both ways, as before.
