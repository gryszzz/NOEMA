# Polymarket US live trading

NOEMA trades Polymarket US only through the official `polymarket-us` SDK and `https://api.polymarket.us`. The international CLOB is not a live venue. The read-only adapter in `noema/venues/polymarket_us.py` still never submits orders.

`PolymarketUSTradingVenue` reports `supports_live_execution` only when all of the following are true:

- `POLYMARKET_US_KEY_ID` is configured
- `POLYMARKET_US_SECRET_KEY` or a readable `POLYMARKET_US_SECRET_FILE` is configured
- `NOEMA_ALLOW_LIVE_ORDERS=1`
- `NOEMA_MASTER_HALT` is not `1`

Those defaults stay off. Setting the flag on the adapter does not by itself send an order. The execution gateway also requires:

- `NOEMA_EXECUTION_GATEWAY_ENABLED=1`
- `NOEMA_LIVE_VENUES` containing the exact name `polymarket_us`
- `NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD` set to a positive cap
- mission execution authority, a live risk decision, and the usual evidence checks
- `prepare_live_execution(slug)` having verified the book, a non-negative fee preview, buying power, positions, and open orders

`prepare_live_execution` calls the order-preview endpoint. It does not insert an order.

## Enable

Generate a key at [polymarket.us/developer](https://polymarket.us/developer) after the US app account is approved. Put the key id and secret in the runtime environment, or point `POLYMARKET_US_SECRET_FILE` at a mounted file. Do not commit either value.

```bash
export POLYMARKET_US_KEY_ID="..."
export POLYMARKET_US_SECRET_KEY="..."
# or: export POLYMARKET_US_SECRET_FILE=/etc/secrets/polymarket-us.secret
export NOEMA_ALLOW_LIVE_ORDERS=1
export NOEMA_MASTER_HALT=0
export NOEMA_EXECUTION_GATEWAY_ENABLED=1
export NOEMA_LIVE_VENUES=polymarket_us
export NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD=5
```

`NOEMA_MASTER_HALT=1` blocks new orders. Cancel remains available so an owner can reduce risk while entry is stopped.

## Commands

`noema pm-account` prints balances, positions, and open orders. It does not place orders.

`noema pm-order --slug SLUG --side buy_yes --price 0.50 --qty 2` records a proposal and does not call the venue. Add `--live` to place that limit and cancel it in the same command. `--live` still refuses to run unless every gate above is set. `--dry-run` is the default; passing it together with `--live` is an error.

The retail create-order schema has no client-order field ([Create Order](https://docs.polymarket.us/api-reference/orders/create-order)). The gateway's stable id is stored locally and is not sent as an extra body field. A retry with the same id returns the saved venue order id. If a dispatch outcome was never confirmed, the adapter does not submit a second order.

Authenticated reads and the private websocket (`wss://api.polymarket.us/v1/ws/private`) use the same Ed25519 headers the SDK already applies. Secret values and signature headers are not written to logs or gateway records.
