# ClawPump Products

Agent skills and receipts for trading Solana with rules instead of feelings. One philosophy across the shelf: criteria pre-committed, exits mechanical, verdicts you cannot argue with.

| Product | One-liner |
|---|---|
| [GATEKEEPER](#gatekeeper--the-token-screen-that-says-no) | The token screen that says no. |
| [MAYFLY](#mayfly--the-hype-trade-that-dies-on-schedule) | The hype trade that dies on schedule. |

## GATEKEEPER — the token screen that says no

A ClawPump agent skill that runs any Solana token through a 12-check scam-and-quality gauntlet and returns one word you can act on: **PASS**, **DEAD**, or **WALK**. Raw numbers included. Opinions not.

### What it catches

- Devs who can still print supply or freeze your wallet (mint/freeze authority)
- Ten wallets holding the whole float
- Unlocked liquidity — the classic rug
- Wash-traded volume dressed up as demand
- Day-old contracts, ghost-town holder counts, ticker impersonators

### What it will not do

Trade. Ever. Gatekeeper ships with every execution capability disabled — it can look, it cannot touch. A DEAD verdict cannot be argued with, including by you. That's the product.

### Use

Paste a token contract address into the agent's chat. That's the whole interface.

```
GATEKEEPER VERDICT: DEAD
Token: MoonSafeElonAI (MSEAI) — 4kfP…9qXz
TIER 1: Mint authority ACTIVE — dev can print supply
Verdict basis: Tier-1 check #1 failed. No further checks required.
```

Thirty seconds. Pennies in compute. One rug avoided pays for a thousand runs.

### Threshold notes

Defaults are tuned for small speculative positions ($40–$500). The liquidity-depth floor (check #5) should scale with your position size — a $5,000 position needs a far deeper pool to exit than a $50 one. Edit the SKILL.md table; it's plain Markdown.

### Agent capabilities

Enable Market Intelligence, Token Sniper, and Bitget Intel on the agent. Disable everything else.

## MAYFLY — the hype trade that dies on schedule

The other side of the gate. MAYFLY takes the risky, hype-driven tokens — Discord calls, paid boosts, trending pools, pump.fun curves about to graduate — and trades them the only way they can be traded honestly: small, fast, and pre-committed to the exit. Multiple entries a day, holds measured in minutes to hours, never overnight.

### What it does

- **Two ways in.** CALL mode: paste a call from your Discord and MAYFLY parses, screens, and trades it. SCAN mode: it hunts the boost feeds, trending pools, and late bonding curves on a 20-minute cadence.
- **The gate comes first.** Every candidate is screened before sizing — if GATEKEEPER is installed, a DEAD verdict kills the trade with no appeal; without it, MAYFLY runs its own built-in 9-check minimum gate.
- **The exit is written at entry.** Every fill immediately produces an exit ticket: −15% stop, scale-outs at +25% and +60%, a 20% trailing runner, and a hard time stop (default 4 hours). Liquidity pulls, dev dumps, or a mid-hold DEAD re-screen trigger immediate market exit.
- **Discipline is enforced, not suggested.** Flat position sizing, daily and weekly loss halts, 24-hour re-entry cooldowns, and a caller scorecard that auto-mutes sources with negative expectancy after 10 calls. Loyalty is for dogs; the scorecard decides.
- **Ships in shadow mode.** It trades on paper until you've watched it lose politely. Going live requires 10 logged shadow trades and typing a sentence you can't claim you didn't read.

### What it will not do

Leverage, average down, widen a stop, extend the clock, hold overnight, or argue with the gate. Averaging down is the exact behavior that got every top-ranked trader vetoed in [Agent Audit #001](agent-audit-001-phoenix-copytrade.md) — it does not get smarter with memecoins.

### Agent capabilities

Shadow mode needs Market Intelligence only. Live mode needs Market Intelligence + Token Sniper, and an agent that can re-check open positions at least every 2 minutes — if it can't watch the stop, it doesn't get to place one.

## Install

**Into a ClawPump agent:** Skills → import from GitHub → point at this repo. Enable the capabilities listed per product above.

**Any SKILL.md-compatible client:**

```bash
npx skills add trelnar/clawpump-products
```

## Receipts

- [Agent Audit #001 — ClawPump Copy-Trade Leaderboard (Phoenix Perps)](agent-audit-001-phoenix-copytrade.md) · 11 traders audited, 0 qualified, $0 allocated. Criteria pre-committed, verdicts mechanical.

---

*Verdicts are a safety screen, not financial advice. A PASS means the token cleared these checks at screening time — it does not mean the token will go up, and half of everything that passes will still go to zero for ordinary market reasons. MAYFLY positions are speculative to the point of parody; fund them only with money whose total loss changes nothing about your life.*
