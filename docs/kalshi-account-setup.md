# Connect NOEMA to a Kalshi Account

NOEMA should be connected in **demo first**, then production only after account reconciliation and risk tests pass.

## 1. Create an API key in Kalshi

Use Kalshi's API-key flow for the environment you intend to use.

Demo and production credentials are separate. A demo API key cannot authenticate against production and a production key cannot authenticate against demo.

Keep these two values:

- API key ID
- downloaded private key file

The private key is secret. Do not paste it into chat, commit it to GitHub, store it in README files, or print it in logs.

## 2. Put the private key outside the repository

Example:

```text
~/.config/noema/kalshi-demo.pem
```

Restrict local file permissions where your operating system supports it.

## 3. Configure demo

```bash
export NOEMA_KALSHI_ENV=demo
export KALSHI_API_KEY_ID='YOUR_DEMO_KEY_ID'
export KALSHI_PRIVATE_KEY_PATH="$HOME/.config/noema/kalshi-demo.pem"
export NOEMA_ALLOW_LIVE_ORDERS=0
export NOEMA_MASTER_HALT=1
```

Starting with `NOEMA_MASTER_HALT=1` is intentional.

## 4. Verify configuration without leaking secrets

```bash
noema check-config
```

The diagnostic reports only whether required values exist. It never prints the private key.

## 5. Inspect the authenticated account mirror

```bash
noema account
```

This reads the primary account:

- balance
- portfolio value
- positions
- orders
- fills
- API usage tier / limits
- Kalshi user-data freshness timestamp

## 6. Run data collection

```bash
noema markets --limit 20
noema stream
noema sync-outcomes --limit 1000
noema evaluate
```

No production order should be enabled during this phase.

## 7. Production later

Production requires all of these simultaneously:

```text
NOEMA_KALSHI_ENV=production
valid production API key
valid production private key
NOEMA_ALLOW_LIVE_ORDERS=1
NOEMA_MASTER_HALT=0
survival gate approved
strategy promotion gate approved
deterministic risk engine approved
```

Missing any condition must result in no trade.

## Primary account visibility

NOEMA defaults to Kalshi subaccount 0. This keeps activity in the primary account rather than a numbered API-only subaccount.

## Emergency stop

Set:

```bash
export NOEMA_MASTER_HALT=1
```

and restart/reload the worker. Production execution support becomes false even when all other live settings are present.

A future deployment should put the master halt in an external secret/config system so it can be changed without editing code.
