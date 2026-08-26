---
name: execution
description: Use this skill to turn an approved BUY NOW, ADD, or SELL NOW action from short-horizon-research into filled orders on the correct venue, with pre-trade gates, slippage control, idempotent order handling, and full journaling.
---

# Execution

This skill converts a trading action into orders. It does not decide what to trade. It receives an action ticket from **short-horizon-research** and either fills it, escalates it, or rejects it with a logged reason.

All trading is live and uses real money. Orders execute only from dedicated sub-accounts and wallets. Bot API keys must have withdrawal permission disabled. The bot must never touch main balances.

## Input: the action ticket

An action ticket contains: action (BUY NOW / ADD / SELL NOW), asset, venue, target notional as a percentage of total trading portfolio value, buy zone or exit basis, and the research timestamp. Reject any ticket missing these fields and log the rejection to **trade-journal**.

A ticket may also carry an optional advanced-instrument block: instrument_type, direction, strike, expiry, leverage, venue. This block is present only on approved advanced trades.

## Venue routing

Route every order by asset class. This table is the routing authority. Edit the table; it's plain Markdown.

| Venue | Asset class | Mode | Notes |
|---|---|---|---|
| Coinbase Advanced Trade | Crypto (listed pairs) | **Automated** | Primary automated crypto venue. Official API. |
| On-chain DEX (via wallet) | New tokens, unlisted crypto | **Automated** | Approved by user decision (2026-08-26). Dedicated hot wallet only — hot-wallet caveat in **risk-limits**. See DEX swap rules. |
| E*TRADE | Equities | **Automated where API-approved** | Official API. Automation applies only to accounts with API access approved. |
| Alpaca | Equities | **Recommended addition** | Official API. Preferred automated-equities venue if added. |
| IBKR | Equities, advanced instruments | **Recommended addition** | Official API. Advanced instruments still require approval per **approval-gate**. |
| Robinhood | Equities, crypto | **Alert-only** | Alert-only by user decision. Robinhood offers an official crypto-only trading API; promotion to automated requires explicit user sign-off, like Alpaca/IBKR. |
| Crypto.com | Crypto | **Alert-only** | Alert-only by user decision. Crypto.com offers an official exchange API; promotion to automated requires explicit user sign-off, like Alpaca/IBKR. |

Official APIs only, on every venue, present or future: the bot places orders only through official, documented trading APIs. Unofficial, scraped, or reverse-engineered interfaces are prohibited everywhere. A venue without an official trading API is alert-only.

Rules:

- The bot must never automate Robinhood or Crypto.com through unofficial, scraped, or reverse-engineered APIs. No exceptions.
- For alert-only venues, send a manual-execution SMS formatted per **alert-format** and stop. The user executes by hand.
- If an asset is tradable on multiple automated venues, prefer the venue with the deepest liquidity for that asset.
- If no automated venue can trade the asset, downgrade the ticket to an alert and log it.

## Pre-trade gate sequence

Every order must pass all applicable gates, in this order, immediately before submission. A failed gate blocks the order and logs the block to **trade-journal**. Gates apply per order, including each child order of a split.

| # | Gate | Applies to | Pass condition |
|---|---|---|---|
| 1 | Halt / kill switch | Buys only | **portfolio-state** halt mode is NORMAL — this covers EMERGENCY_HALT, USER_STOP, RECON_FREEZE, and SELL_ONLY (halt conditions per **risk-limits**). SELL NOW executes even during a halt. |
| 2 | Stale data | All orders | Quote and research data no older than the stale-data limit (table below). Otherwise re-quote before proceeding. |
| 3 | Risk limits | Buys and adds | **risk-limits** confirms the order keeps the position at or under 5% of total trading portfolio value and violates no other hard limit. |
| 4 | Exit safety | Token buys and adds | A read-only exit-safety check, fresh within the freshness window (table below): a simulated sell of the intended position notional succeeds, transfer tax is at or under the cap, and the pool or book is deep enough to exit the full position within the liquidity-based cap in **risk-limits**. Cannot-sell, tax over cap, or insufficient exit depth blocks the buy. Nothing else does: age, holder counts, hype, and token quality are never rejection criteria — risk is modeled by **short-horizon-research**, not vetoed here. |
| 5 | Approval gate | First-time buys; advanced-instrument opens | **approval-gate** shows the asset whitelisted, or an explicit SMS approval for this ticket. Approval is required to open an advanced-instrument position (options, leverage, shorts, futures, perps) — every time, never whitelist-able. Closing, reducing, or exiting an existing advanced position is an exit: automatic, never gated on approval. |

Blocked-buy notification: when a BUY NOW ticket for an already-alerted asset is blocked by gates 3–5, send a short SMS naming the blocking gate and the measured value (example: `NOT BOUGHT <asset>: exit-safety, sell-sim failed`) so the user can act manually or adjust editable thresholds deliberately.

Gate timing defaults. Edit the table; it's plain Markdown.

| Parameter | Default |
|---|---|
| Exit-safety check freshness window | 10 minutes |
| Max transfer tax (exit-safety) | 10% |
| Pre-trade gate time budget | 5 seconds |
| Max quote age at submission | 5 seconds (crypto), 10 seconds (equities) |
| Max research-timestamp age for BUY NOW | 15 minutes; older tickets return to **short-horizon-research** for revalidation |

Approval reminders (binding, from **approval-gate**): the first buy of a non-whitelisted asset requires SMS approval, and approval whitelists the asset. Adds to whitelisted assets, holds, and all sells execute automatically. Resuming automatic buying after an emergency halt requires explicit SMS approval.

## Order policy

### Default order type

Use marketable limit orders. Set the limit at the current best price plus (buy) or minus (sell) the slippage cap for the asset's liquidity tier. Never submit an uncapped market order except in the sell-escalation path below.

### Slippage caps by liquidity tier

Classify the asset by visible liquidity at order time. Edit the table; it's plain Markdown.

| Tier | Definition (visible book depth or pool liquidity within 1% of mid) | Max slippage from mid |
|---|---|---|
| Deep | Depth > 500× order notional | 0.25% |
| Medium | Depth 50–500× order notional | 0.75% |
| Thin | Depth 5–50× order notional | 1.5% |
| Micro / new token | Depth < 5× order notional | 3.0% |

### Depth caps and order splitting

- A single order's notional must not exceed 10% of visible book depth within the slippage cap price (order-book venues) or 1% of pool liquidity (DEX). Edit these values in the table below.
- If the ticket notional exceeds the cap, split it into child orders. Submit children sequentially. Re-run gates 1–3 and re-quote before each child.
- Stop splitting when cumulative slippage reaches the tier cap or the child count limit. Fill what fits; log the shortfall.

| Parameter | Default |
|---|---|
| Max order notional vs visible book depth | 10% |
| Max swap notional vs DEX pool liquidity | 1% |
| Max child orders per ticket | 5 |
| Min spacing between child orders | 20 seconds |

### DEX swap rules (on-chain tokens)

DEX automation is user-approved (2026-08-26).

- Set swap slippage tolerance to the tier cap, never higher.
- Route swaps through an MEV-protected or private RPC. Never broadcast swaps above the public-mempool ceiling through a public mempool.
- Set a swap deadline; treat an expired swap as a cancel.
- Set priority fees dynamically from recent network conditions; cap them.

Edit the table; it's plain Markdown.

| Parameter | Default |
|---|---|
| Swap slippage tolerance | Tier cap (table above) |
| Public-mempool ceiling | 0% — always use private/MEV-protected RPC |
| Swap deadline | 60 seconds |
| Priority fee | 75th percentile of recent blocks, capped at 0.5% of swap notional |

### Advanced instruments

An advanced-instrument order executes only after per-trade approval via **approval-gate**. Use limit orders only — never market orders on options. Size and leverage must match the ticket exactly as approved. Exits of advanced positions are automatic and follow the standard sell path.

## Idempotency

- Attach a deterministic client order ID to every order: `bot-{venue}-{asset}-{ticket-id}-{child-n}`.
- On any timeout, network error, or ambiguous response, query order state by client order ID before doing anything else. The order might be live or filled.
- Never blind-retry. Retry only after the venue confirms the original order is not live and not filled.
- If order state cannot be confirmed, treat the order as possibly live: stop submitting for that asset, reconcile against **portfolio-state**, and send an ops alert per **alert-format**.
- For on-chain swaps, confirm transaction status by hash before any re-send. A dropped transaction can still land later; account for it in **portfolio-state**.

## Partial fills

| Situation | Action |
|---|---|
| Buy partially filled, price still inside slippage cap | Re-quote and keep working the remainder. |
| Buy partially filled, price beyond cap | Cancel the remainder. Keep the partial position. Do not chase. |
| Buy remainder unfilled at give-up timeout | Cancel the remainder. Log fill ratio. |
| Sell partially filled | Keep working. Re-quote at each interval, widening toward the tier cap in steps. |
| Sell still unfilled after max re-quotes | On order-book venues, escalate to a market order for the remainder. On DEX, widen slippage stepwise to the escalation ceiling, then alert. |

Edit the table; it's plain Markdown.

| Parameter | Default |
|---|---|
| Re-quote interval | 15 seconds |
| Buy give-up timeout | 90 seconds |
| Max sell re-quotes before escalation | 4 |
| DEX sell escalation slippage ceiling | 2× tier cap |

Sells are risk-off actions. Prefer a worse fill over an unfilled sell. Buys are opportunistic. Prefer an unfilled buy over a chased fill.

## Market hours (equities)

Crypto executes 24/7. Equities follow these rules:

- Regular trading hours (RTH): full order policy applies.
- Extended hours: limit orders only, slippage cap halved, no sell escalation to market orders.
- Never submit a market order outside RTH.
- Do not queue a BUY NOW ticket overnight. The signal is perishable. Return the ticket to **short-horizon-research** for revalidation at the next session open.
- A SELL NOW ticket outside all trading sessions triggers an immediate SMS alert per **alert-format**, then executes at the next session open if the exit condition still holds.

## Failure handling

| Failure | Response |
|---|---|
| Order rejected by venue | Log reason. Do not resubmit unchanged. If the cause is fixable (precision, min size), fix once and resubmit; otherwise fail the ticket. |
| Rate limit | Back off per venue guidance, then resume. Halt submissions to that venue after 3 consecutive rate-limit failures. |
| Venue API down or unreachable | Fail buys. For sells, retry with backoff for 10 minutes, then send an ops alert with manual-execution instructions. |
| Repeated failures (3 consecutive) on one asset | Stop trading that asset. Send an ops alert. |
| Unconfirmable order state | Follow the idempotency procedure above. |
| Position mismatch vs **portfolio-state** | Stop submitting and trigger **portfolio-state** reconciliation. On a confirmed beyond-tolerance discrepancy, **portfolio-state** sets RECON_FREEZE (all automatic buying stops immediately) and alerts immediately. |

Escalate ops alerts by SMS using **alert-format**. An ops alert must state: what failed, current position state, and what manual action (if any) the user should take. Host-level failures (process down, clock drift, disk) are handled by **vps-ops**.

## Journaling

Log every event to **trade-journal**: order submissions, fills, partial fills, cancels, rejections, gate blocks, re-quotes, escalations, and reconciliations. Each entry records timestamp, ticket ID, client order ID, venue, asset, action, quoted price, limit price, filled quantity, fill price, slippage vs quote at decision time, fees, and gate outcomes. Execution quality data feeds **backtest-replay** and the calibration loop in **short-horizon-research**.
