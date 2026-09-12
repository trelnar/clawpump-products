# The short leg — Hyperliquid perps at 1x

**Status: built, OFF until funded and probed** (`SHORTS_ENABLED=0`).

## Why and what

Most of what the bot sees goes down. The tokens it trades can't be shorted (no borrow, no
futures), but the ~150 tokens with perpetual futures on Hyperliquid can. The short leg
mirrors the long thesis on that universe: **a token that pumped over 24h and is fading; will
it fall 8% within 6 hours?** The model reports `p30` for each; the core shorts at
`p30 ≥ 0.35`, stops 4% above the fill, and a mirrored ratchet covers 75% once −5% has held
and the price bounces. Everything is 1x: a squeeze costs the stop, never the account.
Positions are covered at 12h regardless.

It keeps its own book (`shorts` table), its own monitor pass every 10s, and its own exits,
sharing with the long leg only discovery/research, the approval flow (kind `short`), AUTO
mode, the cooldowns, `PNL`/`HOLDING`, and the alerts ("I shorted ETH for $10.00 at 2600
(1x). Exit plan: …", "I covered 75% of my ETH short for a gain of $0.42 because it bounced
off the low, so I banked it.").

Files: `bot/tradebot/exchanges/hyperliquid.py` (SDK wrapper), `bot/tradebot/shorts.py`,
`bot/tests/test_shorts.py`. Parameters: `HL_*` in `config.py`.

## The rule on US persons

Hyperliquid's terms exclude US persons. The bot talks to its API from a US VPS with a
self-custody wallet. Whether to do that is the operator's decision, made knowingly.

## Funding (one time, ~$50–100)

Hyperliquid is funded through its Arbitrum bridge. The bot's EVM address is the same on
every chain (`hl_probe.py` prints it).

1. On Coinbase, withdraw USDC to the bot's EVM address, **network: Arbitrum**. Also send
   about $2 of ETH to the same address, network Arbitrum, for gas.
2. On the VPS, as root, install the SDK and move the USDC into Hyperliquid:
   ```
   cd /opt/tradebot && venv/bin/pip install -q -r bot/requirements.txt
   sudo -u bot venv/bin/python scripts/hl_deposit.py
   ```
3. Check it landed and the venue answers:
   ```
   sudo -u bot venv/bin/python scripts/hl_probe.py
   ```
4. Turn it on (`setup-secrets.py` does not know this key; it is not a secret):
   ```
   printf 'SHORTS_ENABLED=1\n' >> /etc/tradebot/secrets.env
   bash scripts/split-credentials.sh && systemctl restart tradebot-core tradebot-agent
   ```

`HOLDING` lists shorts as "SHORT ETH: $10.00 in, now +0.30 (+3% in our favour) …".
`SCORE` scores SHORT_NOW rows on the low inside the window, so "+30%/6h" for those rows
means "fell 8%".
