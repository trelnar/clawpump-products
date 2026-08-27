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
| Coinbase (sub-account) | 40% | 10% of venue allocation |
| Hot wallet (on-chain) | 30% | 10% of venue allocation |
| Equities venue | 25% | 10% of venue allocation |
| Unallocated reserve | 5% | — |

The reserve buffers adds, fees, and gas top-ups. **risk-limits** per-venue exposure caps apply on top of allocation targets.

## Gas floors

The hot wallet must always hold enough native token (e.g. SOL) to execute an editable number of exits (default: 20) at current fee levels. Running out of gas while holding positions is an incident: ops alert, and **position-monitor** treats affected positions as exit-impaired until the user tops up.

## The buy constraint

A candidate is actionable only at a venue holding sufficient cash for the **risk-limits**-computed size.

- Enough cash at the right venue → normal pipeline.
- Not enough → never silently skip the opportunity. Downgrade to an alert that names the shortfall and the exact suggested transfer: `NOT BOUGHT <asset>: $X short at <venue>. Suggest move $Y from <venue A>.`
- A partial position (smaller than computed size but above venue minimums) can proceed when the ticket allows it; the alert still reports the shortfall.

## Rebalance suggestions

Suggest — by SMS, with exact amounts and direction — when any of these trigger:

| Trigger (editable) | 
|---|
| Venue cash below its floor |
| Allocation drift beyond ±10 points from target |
| 3+ candidates missed at one venue for insufficient cash within 24 h |

Suggestions are throttled per **alert-format** and never repeat within an editable cooldown (default 12 h). Transfers are executed by the user; the bot detects arrival via **portfolio-state** reconciliation (a deposit is an explained flow) and logs the outcome to **trade-journal**.

## What allocation steers

Allocation percentages steer opportunity coverage: a heavy hot-wallet allocation favors new tokens; the equities allocation is bounded by the settlement rules in **equities-constraints**. When discovery consistently finds candidates one venue cannot fund, that is a rebalance signal, not a reason to loosen limits.
