# tradebot

The runnable implementation of the 17 skills in `.agents/skills/`. Two
services on the VPS (runtime skill):

- **tradebot-core** — deterministic layer. Telegram approval gate, risk
  enforcement, execution (Coinbase / Solana via Jupiter / Base via KyberSwap),
  position monitoring with mechanical invalidation exits, portfolio state,
  the 20%/24h halt, heartbeat. No model calls.
- **tradebot-agent** — research layer. Discovery (DexScreener, Coinbase
  movers) -> Claude API with the short-horizon-research skill as its strategy
  spec -> structured forecasts -> tickets. Holds no venue credentials.

State and journal: SQLite at /var/lib/tradebot/tradebot.db (append-only
journal tables per trade-journal skill). Secrets: /etc/tradebot/secrets.env.

Deploy/update: `scripts/deploy.sh` (root). First-run checklist:
`scripts/phase0.py` (bot user). Sizing follows go-live phases: 0 = no orders,
1 = $5 venue-minimum orders, 2-4 = 25/50/100% of risk-computed size; each
advance requires a Telegram-approved code.

Telegram commands: YES/NO <code>, STOP, RESUME, FLATTEN, REVOKE <asset>,
STATUS, REPORT, WHY <asset>.
