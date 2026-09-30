# Live economic workstation

The operating console at `/` uses a single observed Agent Balance. Every monetary
contribution comes from `/api/capital-history`; the browser does not construct a
second aggregate. Selecting a category or entity never changes the overall total.

## Balance sources and units

| Source | Included amount | Observation source |
| --- | --- | --- |
| Kalshi | Authenticated account cash in USD | Official portfolio balance; decimal dollars, with explicit cents fallback |
| Polymarket US | Authenticated current USD balance | Official account balances |
| Solana | Observed native SOL multiplied by observed SOL/USD | Owner wallet read + Coinbase public spot |
| Ethereum / Base | Native ETH on each distinct chain multiplied by ETH/USD | Owner wallet reads + Coinbase public spot |
| Polygon | Observed native POL multiplied by POL/USD | Owner wallet read + Coinbase public spot |
| Bitcoin | Observed native BTC multiplied by BTC/USD | Owner wallet read + Coinbase public spot |
| Stripe | Available USD funds from a confirmed live-mode account observation | Persisted Stripe balance snapshot |

Zero native balances are measurable zero without requiring a price. Missing,
malformed, unavailable, stale or unpriced observations are excluded from the
current sum and remain visible in source coverage. Positive token balances without
a supported price are counted separately as unpriced assets while valued native
balances remain in the partial total. Native marks are indicative spot values,
before liquidation fees. The UI labels funded but unpriced sources. Ethereum and
Base are separate chain accounts even when the address is identical. The
reserve/treasury projection is never added to these balances again.

Open positions, unpriced tokens, pending Stripe funds, historical gross payments,
and unreconciled liabilities are not added. The headline is the known valued
balance within that stated scope, not an assertion of complete net worth or
spendable execution authority. Deposits and spot-price changes do not create P&L.

Official unit/price references:
- https://docs.kalshi.com/api-reference/portfolio/get-balance
- https://docs.cdp.coinbase.com/coinbase-business/track-apis/prices

## History and freshness

`live_balance_observations` stores immutable, fingerprint-deduplicated observations
with per-source contributions and the raw observed native/token balance rows in
the existing database. Wallet history keys use the same chain and address identity
as the account cards, including the explicit `unknown` address case. No prior
values are invented. Time windows are 1H, 24H, 7D, 30D and ALL; rendering is
bounded to 720 actual samples. Gaps and changes to the included account set break the line. A
24-hour change requires a comparable source set and a baseline within five minutes
of the 24-hour boundary. It is a balance change, including transfers, not
investment return. Deposit/withdrawal totals remain unknown without reconciled
transfer records. Missing/read-only databases keep history unavailable without
inventing a replacement.

The dashboard server samples valued account state every 30 seconds by default,
even with no browser connected. The interval is bounded to 15–300 seconds with
`NOEMA_CAPITAL_SAMPLE_INTERVAL_SECONDS`; the sampler can be disabled with
`NOEMA_CAPITAL_HISTORY_SAMPLER_ENABLED=0`. Wallet and venue provider caches are
30 seconds, and the browser refreshes those sources on the same bounded cadence.
Operating records still refresh through commit SSE notifications with a 15-second
poll fallback. Balance evidence older than 120 seconds is excluded. Reading pause freezes displayed data and rejects
in-flight responses; it does not pause NOEMA's runtime. Slow optional providers
cannot block successful capital or operating updates. Capital sources and category
headers expose their own status and observation time.

Market-data qualification reports historical forecast and outcome row totals
separately from the paired-evaluation cohort. A historical forecast or outcome
does not count as a qualified pair: evaluation requires the candidate forecast
and market baseline for the same venue, ticker and decision timestamp, before
the recorded resolution. Historical totals remain available if current snapshot
quality data is unavailable; missing source tables remain unknown.

Web3 chain cards keep native amounts, valued USD contributions, and any observed
token amounts visible in the compact view. Source address, freshness, pricing,
P&L attribution, execution authority and the full token list sit in expandable
diagnostics. Unpriced token amounts remain visible and are recorded in each
persisted source observation; they are excluded from USD totals until an observed
valuation is available.

## Economics and categories

The primary P&L is explicitly NOEMA live realized P&L. It reports no live
executions when the persisted gateway ledger has no submitted or confirmed orders;
once orders exist, it stays unavailable until fills are reconciled. It never
promotes paper economics to live results. Canonical economic-ledger subtotals
appear in Economics; paper evidence appears in Research / Validation. Volume, fees
and exposure from Kalshi position rows are labeled bounded, account-wide
reported subtotals; they are not NOEMA-attributed totals. Unrealized P&L and
strategy-level metrics remain unknown without exact evidence.

Prediction Markets, Web3 / Wallets, Strategies, Markets / Assets, Economics,
Revenue, Research / Validation and System / Records are mounted views in the same
page. They share selection with topology, inspector, decisions and the terminal.
Filtering uses exact persisted identifiers; absent asset links produce empty
scoped results rather than guessed attribution. Paper results remain labeled in
Research / Validation. Category navigation supports keyboard arrows, Home/End,
and hash links without losing selected state.

The terminal merges persisted mission, runtime, decision, cognition, specialist
handoff, wallet and economic records with official fill records and recorded
execution requests. Repetitive PASS decisions are summarized by shared venue,
reason and model, with expandable immutable forecasts. It labels confirmed balance changes,
reconciled realized P&L, accepted orders, fills and promotions from their source
events. It does not emit fake market ticks or simulate order activity. Worker,
console stream, market-feed and account-feed freshness are displayed separately.
Text is rendered inertly, and reading scroll anchors are retained as records
arrive.

Live observation does not enable execution. No live flags, signing authority,
risk limits, or execution prerequisites are changed by this workstation.
