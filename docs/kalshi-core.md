# Kalshi Core

NOEMA uses Kalshi's documented external Trade API.

## Environments

Prediction-market REST:
- Production: `https://external-api.kalshi.com/trade-api/v2`
- Demo: `https://external-api.demo.kalshi.co/trade-api/v2`

Perpetual-futures REST uses the `/margin/` namespace and is intentionally treated as a separate engine.

## Current integration

The initial adapter supports:

- public open-market discovery;
- cursor pagination with a persisted page cursor across agent cycles, so a
  bounded worker advances through open markets instead of revisiting the first page;
- exchange status;
- current orderbook retrieval;
- normalized binary quotes;
- settlement-rule capture;
- top-of-book liquidity approximation;
- request signing for Ed25519 or RSA keys;
- authenticated demo order submission;
- production order submission only behind an explicit live flag.

## Credentials

Use environment variables:

```bash
NOEMA_KALSHI_ENV=demo
KALSHI_API_KEY_ID=...
KALSHI_PRIVATE_KEY_PATH=/secure/path/kalshi.key
NOEMA_ALLOW_LIVE_ORDERS=0
```

Demo and production credentials are distinct.

NOEMA signs authenticated requests with:

```text
timestamp_ms + HTTP_METHOD + request_path_without_query
```

and sends the three Kalshi access headers.

## Safety boundary

Production execution requires all of the following:

1. `NOEMA_KALSHI_ENV=production`
2. valid production credentials
3. `NOEMA_ALLOW_LIVE_ORDERS=1`
4. an action approved by the deterministic risk engine

The default remains demo/paper.

## Next

- WebSocket market/orderbook streaming;
- historical candles and resolved-market datasets;
- outcome resolver;
- calibration tables by series/category;
- specialized first niche;
- separate perps engine under Kalshi's margin API.
