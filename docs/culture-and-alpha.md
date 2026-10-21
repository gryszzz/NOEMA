# Culture radar and alpha discovery (paper)

`python -m noema.culture_crawler --loop` runs every 10 minutes. Read-only. It respects robots.txt, spaces requests per host, and sanitizes every string as untrusted.

## Sources
| Domain | Live source |
|---|---|
| crypto | DexScreener top/latest boosts and token profiles, Jupiter top-trending 1h, pump.fun live coins, ApeWisdom crypto mentions (Reddit) |
| stocks | ApeWisdom r/wallstreetbets and all-stocks mentions (Reddit), StockTwits trending |
| prediction | Kalshi open markets (biggest movers × volume), Polymarket top 24h volume and newest markets, Polymarket daily PnL leaderboard |

Blocked sources are listed in `BLOCKED_SOURCES`, with the reason, and shown on the room. They include Reddit JSON (robots plus 403), GeckoTerminal and CoinGecko trending (robots), Birdeye (needs a key), Yahoo (robots), Nasdaq (timeout), SEC Form 4 (403 without a contact UA), and Polymarket comments.

## Culture signal
Each token, ticker or market gets the following fields:
- `buzz`: summed per-source buzz over the last 20 minutes.
- `buzz_velocity`: change against the trailing 24h mean. On first sight it uses the source's own 24h acceleration, otherwise 0.
- `sentiment`: buy/sell flow for tokens, or the price move for markets.
- `novelty`: a 6h half-life.
- `score`: a blend of the above.

Hot markets go into the accuracy ledger as agent `culture`, at most once per 6h. The forecast is the market price nudged by up to ±4% toward the crowd's move, so the accuracy engine can show whether culture beats the price.

## Alpha discovery
Candidates are the culture radar's tokens, priced from DexScreener pairs. Every screen must pass, and missing data fails closed:

- **Market:** liquidity ≥ $50k, 24h volume ≥ $100k, pair age ≥ 6h, 24h change > −50%.
- **Solana (RugCheck):** mint and freeze authority null, LP ≥ 90% locked or burned, top-10 holders ≤ 30% excluding pools, no transfer tax, not rugged, no danger risks, creator ≤ 5%.
- **EVM (GoPlus):** not mintable, no hidden or reclaimable owner, no pause or blacklist, LP ≥ 90% locked, top-10 ≤ 30%, not a honeypot, buy/sell tax ≤ 5%, creator ≤ 5%.

Limits can be changed with the `NOEMA_ALPHA_*` env vars.

**Ranking:** 35% momentum (1h/6h), 15% 1h buy/sell flow, 30% buzz acceleration, 20% 1h liquidity growth.

**Sizing:** mirrors the capital guard. It uses the micro-tier 3% of equity × conviction, inside the 75% rising floor and the 10% daily stop. Memecoins (mcap < $50M) trade at half size, with total exposure ≤ 10% of equity. Mid caps are capped at 40%.

**Exits:**
- Take profit at +30% (sell ⅓), +75% (sell ½ of the rest) and +150% (sell all).
- A 20% trailing stop from the peak, armed after the first take-profit.
- A −15% hard stop before that.
- A 24h time stop, and an exit at 6h if the position is not up 5%.

The book is a separate $1,000 paper book (`NOEMA_ALPHA_PAPER_BANKROLL_USD`). Entries are recorded as `alpha` / `paper_trade` token_up forecasts. Screened-out tokens are recorded as `veto`, so the screens themselves get scored too.

**Live:** there is none. `LIVE_SUPPORTED = False`. Passing tokens are written to `alpha_allowlist` with `owner_reviewed=0`, and a token that later fails is removed. Live use still needs the autonomy ladder, owner review, the capital guard and the signer pins.
