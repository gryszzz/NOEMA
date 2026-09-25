# Connect NOEMA to Kalshi Safely

Start with Kalshi's **demo environment** and keep production execution disabled while validating data, account reconciliation, and model evaluation.

## Demo credentials

Kalshi demo and production credentials are separate. Create a demo API key following Kalshi's official API-key documentation.

Keep the private key outside the repository. Never commit it, paste it into chat, or print it in logs.

Example environment:

```bash
export NOEMA_KALSHI_ENV=demo
export KALSHI_API_KEY_ID='YOUR_DEMO_KEY_ID'
export KALSHI_PRIVATE_KEY_PATH="$HOME/.config/noema/kalshi-demo.pem"
export NOEMA_ALLOW_LIVE_ORDERS=0
export NOEMA_MASTER_HALT=1
```

## Validate the connection

```bash
noema check-config
noema account
noema markets --limit 20
noema stream
noema sync-outcomes --limit 1000
noema evaluate
```

The account command mirrors balance, portfolio value, positions, orders, fills, API limits, and Kalshi's user-data freshness timestamp for reconciliation.

## Production

Production activation is intentionally not part of this setup guide. Keep the master halt enabled until the research system has passed demo reconciliation, evaluation, and survival testing.

## Visibility

NOEMA defaults to primary subaccount 0. Kalshi numbered subaccounts are API-only today, so the primary account is the clearest default for visibility during validation.
