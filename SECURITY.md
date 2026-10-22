# Security Policy

This repository is **public** and **paper-trading only** by default. It contains **no API keys, private keys, or credentials**.

- Secrets (exchange keys, RPC keys, signer keys) are supplied only through the runtime environment and are never committed.
- Owner-specific config (wallet addresses, signer pins, tuned thresholds, smart-wallet lists, hostnames) lives in a gitignored private env file. See `.env.example` / `config/private.env.example` for the template.
- Live execution flags (`NOEMA_ALLOW_LIVE_ORDERS`, signer enables, owner-review flags) default off and are set only by the owner.
- Swap signers fail closed: with no `NOEMA_SOLANA_SIGNER_PIN` / `NOEMA_EVM_SIGNER_PIN` configured they refuse to sign.

## Reporting a vulnerability

Please use **GitHub private vulnerability reporting** (Security tab → "Report a vulnerability"). Do not open a public issue for security problems. We aim to acknowledge within 72 hours.

Wallet addresses that appear in older git history are public on-chain data, not secrets.
