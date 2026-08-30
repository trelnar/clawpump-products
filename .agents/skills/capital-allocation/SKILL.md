---
name: capital-allocation
description: Use when deciding whether cash exists at a venue for a trade, when suggesting cross-venue transfers, or when checking gas and reserve floors.
---

# Capital Allocation

Where the cash lives and how it moves. The structural fact: exchange keys cannot withdraw, so every cross-venue transfer is manual and user-executed. The bot only ever suggests.

## Venue allocation

Percentages of total trading capital — the amount itself is set by the user and can change. Edit the table; it's plain Markdown.

| Venue | Target allocation (editable) | Cash floor (editable) |
|---|---|---|
| Hot wallets (on-chain, all chains) | 45% | 10% of venue allocation |
| Coinbase (sub-account) | 25% | 10% of venue allocation |
| Equities venue | 25% | 10% of venue allocation |
| Unallocated reserve | 5% | — |

On-chain capital splits across the enabled chains in the **execution** chain registry. Hold trading capital as the chain's stablecoin (USDC where available) rather than the native token, so an idle balance does not carry native-token price risk.

| Chain | Share of on-chain allocation (editable) |
|---|---|
| Solana | 45% |
| Base | 25% |
| BNB Chain | 15% |
| Arbitrum | 10% |
| Ethereum mainnet | 5% |

The reserve buffers adds, fees, and gas top-ups. **risk-limits** per-venue exposure caps apply on top of allocation targets.

## Gas floors

Every enabled chain must hold enough of its native gas token to execute an editable number of exits (default: 20) at current fee levels — SOL on Solana, ETH on Base, Arbitrum, and Ethereum, BNB on BNB Chain. The floor is measured per chain, never pooled: gas on Base cannot fund an exit on Solana.

Running out of gas while holding positions is an incident: ops alert, and **position-monitor** treats affected positions on that chain as exit-impaired until the user tops up. A chain below its gas floor accepts no new buys; exits on it continue while gas remains.

Because a gas float is idle capital, an enabled chain with no position and no gas float is dormant rather than broken — the first buy on it is blocked with a top-up suggestion, not silently skipped.

## The buy constraint

A candidate is actionable only at a venue holding sufficient cash for the **risk-limits**-computed size.

- Enough cash at the right venue → normal pipeline.
- Not enough → never silently skip the opportunity. Downgrade to an alert that names the shortfall and the exact suggested transfer: `NOT BOUGHT <asset>: $X short at <venue>. Suggest move $Y from <venue A>.`
- A partial position (smaller than computed size but above venue minimums) can proceed when the ticket allows it; the alert still reports the shortfall.

## Rebalance suggestions

Suggest — by Telegram message, with exact amounts and direction — when any of these trigger:

| Trigger (editable) | 
|---|
| Venue cash below its floor |
| Allocation drift beyond ±10 points from target |
| 3+ candidates missed at one venue for insufficient cash within 24 h |

Suggestions are throttled per **alert-format** and never repeat within an editable cooldown (default 12 h). Transfers are executed by the user; the bot detects arrival via **portfolio-state** reconciliation (a deposit is an explained flow) and logs the outcome to **trade-journal**.

## What allocation steers

Allocation percentages steer opportunity coverage: a heavy hot-wallet allocation favors new tokens; the equities allocation is bounded by the settlement rules in **equities-constraints**. When discovery consistently finds candidates one venue cannot fund, that is a rebalance signal, not a reason to loosen limits.
