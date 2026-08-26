# GATEKEEPER — the token screen that says no

A ClawPump agent skill that runs any Solana token through a 12-check scam-and-quality gauntlet and returns one word you can act on: **PASS**, **DEAD**, or **WALK**. Raw numbers included. Opinions not.

## What it catches

- Devs who can still print supply or freeze your wallet (mint/freeze authority)
- Ten wallets holding the whole float
- Unlocked liquidity — the classic rug
- Wash-traded volume dressed up as demand
- Day-old contracts, ghost-town holder counts, ticker impersonators

## What it will not do

Trade. Ever. Gatekeeper ships with every execution capability disabled — it can look, it cannot touch. A DEAD verdict cannot be argued with, including by you. That's the product.

## Install

**Into a ClawPump agent:** Skills → import from GitHub → point at this repo. Enable Market Intelligence, Token Sniper, and Bitget Intel on the agent. Disable everything else.

**Any SKILL.md-compatible client:**

```bash
npx skills add trelnar/clawpump-products
```

## Use

Paste a token contract address into the agent's chat. That's the whole interface.

```
GATEKEEPER VERDICT: DEAD
Token: MoonSafeElonAI (MSEAI) — 4kfP…9qXz
TIER 1: Mint authority ACTIVE — dev can print supply
Verdict basis: Tier-1 check #1 failed. No further checks required.
```

Thirty seconds. Pennies in compute. One rug avoided pays for a thousand runs.

## Threshold notes

Defaults are tuned for small speculative positions ($40–$500). The liquidity-depth floor (check #7) should scale with your position size — a $5,000 position needs a far deeper pool to exit than a $50 one. Edit the SKILL.md table; it's plain Markdown.

## The trading-bot skill suite

This repo also ships ten sibling skills that together form a live trading bot. Unlike Gatekeeper, that suite drives live, real-money order placement on venues the user enables. Gatekeeper's never-trades guarantee applies to the gatekeeper skill only — it does not extend to the rest of the repo. And `npx skills add trelnar/clawpump-products` imports the whole repo, so install selectively if the screener is all you want.

- **short-horizon-research** — finds and scores short-horizon trade candidates
- **execution** — places and manages orders on the venues the user has enabled
- **approval-gate** — the SMS approval flow for trades that need a human yes
- **risk-limits** — position sizing, exposure caps, and the drawdown halt
- **portfolio-state** — tracks holdings, balances, and flow-adjusted portfolio value
- **trade-journal** — records every trade, decision, and outcome
- **backtest-replay** — backtests strategies and governs shadow-mode promotion
- **vps-ops** — keeps the bot's server alive, patched, and monitored
- **alert-format** — formats the SMS alerts and reports the bot sends
- **docs-voice** — the writing style guide for the suite's documentation

---

*Verdicts are a safety screen, not financial advice. A PASS means the token cleared these 12 checks at screening time — it does not mean the token will go up, and half of everything that passes will still go to zero for ordinary market reasons.*
