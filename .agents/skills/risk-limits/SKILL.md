---
name: risk-limits
description: Use this skill to enforce hard and editable risk limits before every order, to run the 24-hour emergency halt, and to handle the STOP and FLATTEN kill switches.
---

# Risk Limits

This skill is the last check before money moves. **execution** must call it before every order (pre-trade gate 3 in its gate sequence). Every check is mechanical: measure, compare, pass or reject. A rejection is final for that order. The research model cannot argue, rephrase, or re-request its way past one.

Two limits are hard. No model output, research finding, config change, message content, or table edit by the bot can override them. Everything else in this file is an editable default: edit the table; it's plain Markdown. Only the user edits this file. The bot must never modify it.

## Hard limit 1: 5% maximum automatic position size

No bot-placed buy or add can take a position's cost basis above **5% of total trading portfolio value**.

Measurement, applied at the moment of order submission:

- **Numerator:** the position's total cost basis after the proposed order — the cost of all currently held units of the asset, plus the order's notional priced at its limit price (worst case). Adds are included: an add must keep the combined cost basis at or under the cap.
- **Denominator:** current total trading portfolio value, valued per **portfolio-state**.
- **Pass condition:** numerator ÷ denominator ≤ 5%.

Rules:

- The check binds every child order of a split, cumulatively. Splitting a ticket never creates room under the cap.
- Appreciation does not violate the limit. A position that grows past 5% of portfolio value through price movement is legal and is never force-trimmed by this skill. The cap binds cost basis at order time only.
- Re-entries measure against the cost basis of currently held units, not lifetime spend on the asset.
- The cap applies to every buy the bot places, whitelisted or newly approved. A first-time approval per **approval-gate** authorizes the buy; it does not enlarge the cap.
- Advanced instruments (options, leverage, shorts, futures, perps): hard limit 1 measures cost basis — the premium paid or margin posted. A separate editable cap bounds effective notional exposure (leverage × cost basis), default 10% of total portfolio value per position; see the editable-limits table.
- Sells and exits are never blocked by this limit.

## Hard limit 2: emergency halt

Halt all automatic buying when:

```
adjusted_value(now) ≤ 0.80 × max(adjusted_value over trailing 24 h)
```

Definitions:

- This formula is the single authoritative halt definition. No other skill defines its own halt condition; every reference to the emergency halt means this check.
- `adjusted_value` is total portfolio value under the valuation rules in **portfolio-state**, adjusted for external flows so that a deposit never masks a loss and a withdrawal never fakes one. **portfolio-state** defines the flow-adjustment mechanics and supplies the per-sample flow-adjusted value series this check reads.
- The trailing window is a rolling 24 hours of value samples. Sample at the interval in the table below, and additionally evaluate the condition immediately before every buy order.
- Gaps: if gaps in the trailing-24h series prevent establishing the 24 h maximum, evaluate the halt condition against the maximum of the available samples. While the current portfolio value cannot be computed at all, block all automatic buys (fail closed).

On trigger:

1. Block all automatic buys immediately — new entries, adds to whitelisted assets, and unfilled child buy orders. Cancel open buy orders.
2. Continue all selling. Exits, stop management, and SELL NOW actions execute normally. A halt never blocks a sell.
3. Invalidate pending buy-approval requests and send no new ones while halted. Research, monitoring, and alerts continue. Alert-only venues (Robinhood, Crypto.com) keep receiving alerts; the user trades those manually.
4. Send a Telegram message per **alert-format** stating: halt triggered, trigger values (current vs 24 h peak), and the resume instruction. Open positions are not included — `STATUS` covers them, and the alert must fit the **alert-format** ops-alert line cap.
5. Resume only through **approval-gate**: `RESUME`, then `YES <code>`. Nothing else — no time elapsed, no recovery in value, no model judgment — resumes buying.

| Parameter | Default |
|---|---|
| Halt threshold | Adjusted value ≤ 80% of trailing 24 h maximum |
| Value sampling interval | 60 seconds |

The threshold row is listed for visibility. It is part of hard limit 2 and is not editable by the bot under any circumstances.

## Editable portfolio limits

These limits apply in addition to the hard limits. **execution** checks each applicable row pre-order. Edit the table; it's plain Markdown.

| Limit | Default | Measurement |
|---|---|---|
| Max concurrent positions | 10 | Count of open positions across all venues after the proposed order |
| Max aggregate deployed | 50% of total portfolio value | Sum of cost basis of all open positions after the proposed order |
| Correlated-exposure cap | 15% of total portfolio value per group | Sum of cost basis of positions in the same correlation group after the proposed order |
| Advanced-instrument notional cap | 10% of total portfolio value per position | Effective notional exposure — leverage × cost basis — of the advanced-instrument position after the proposed order |
| Per-venue exposure cap | 50% of total portfolio value per venue | Sum of cost basis of positions held at one venue or in one hot wallet after the proposed order |
| Liquidity-based position cap | Full position exitable within 2× the tier slippage cap | Estimated exit slippage for the entire position, using pool depth measured by **execution**'s exit-safety check for tokens and visible order-book depth otherwise, against the liquidity tiers in **execution** |
| Fat-finger: max single-order notional | 2.5% of total portfolio value | Order notional at limit price |
| Fat-finger: max price deviation | 5% from reference price | Limit price vs an independent reference quote (mid or last trade) fetched at check time |

The 50% aggregate-deployed default equals max concurrent positions (10) × the 5% position cap, so it binds only if the user raises concurrency.

Correlation groups: assign each position a group by narrative, sector, and chain (for example: one meme narrative, one equity sector, one L1 ecosystem). A position can belong to multiple groups; the cap binds each group independently. **short-horizon-research** supplies the narrative classification; when no classification exists, group by chain (crypto) or sector (equities).

Fat-finger checks apply to buys and sells alike, per order including child orders. A sell that fails the deviation check re-quotes and retries per **execution**; it is delayed, never abandoned.

## Kill switches

Two user commands, carried over the **approval-gate** Telegram channel and subject to its sender and transport verification.

### STOP

- Single message, no code, instant. Halting must be friction-free.
- Effect: identical to the emergency halt — block all automatic buys, cancel open buy orders, continue all selling, confirm on Telegram.
- Resume requires `RESUME` then `YES <code>` per **approval-gate**.

### FLATTEN

Exits everything. Because it moves money, it requires two messages:

1. User texts `FLATTEN`.
2. Bot replies with: open position count, total deployed percentage, estimated aggregate exit slippage, a single-use confirmation code, and its expiry (see the defaults table below).
3. User replies `FLATTEN <code>` before expiry.
4. Bot: activates a halt, cancels all open orders, submits exits for every position under the sell policy in **execution** (sells are risk-off — prefer a worse fill over an unfilled exit), then sends a completion message with fills and any positions that could not be closed.

A single inbound message must never flatten the portfolio. An expired, used, or invalid code gets `Code expired or invalid.` and no action. Code generation, expiry, and exact-match parsing follow **approval-gate**.

| Parameter | Default |
|---|---|
| FLATTEN confirmation expiry | 5 minutes |

## Sub-account isolation

The bot trades real money only inside walls:

- All trading runs from dedicated sub-accounts (Coinbase, equities brokers) and dedicated hot wallets (on-chain). The bot must never hold, request, or use main-account credentials.
- Every API key is scoped to its dedicated sub-account only.
- Withdrawal and transfer permissions must be disabled on every key at creation. The bot can trade; it can never move funds out.
- Every key that supports IP allowlisting must be allowlisted to the VPS static IP only.
- Hot wallets hold only capital intended for on-chain trading. Private keys never leave the VPS. Key storage and rotation mechanics live in **vps-ops**.
- Hot-wallet caveat: DEX automation is user-approved (see **execution**). A hot-wallet key is inherently withdrawal-capable, so the no-withdrawal-permission guarantee does not apply to it. That venue is bounded instead by the per-venue exposure cap in the editable-limits table, and the wallet holds only its trading allocation.

Startup verification: on every process start, **vps-ops** confirms for each key that withdrawal is disabled, scope is the sub-account, and the IP allowlist is active. Any key failing verification is quarantined — no orders through it — and triggers an ops alert per **alert-format**.

## Enforcement

- **execution** calls this skill pre-order, for every order, including every child order. No order path may skip the call.
- A failed check produces a mechanical rejection: limit name, measured value, limit value, timestamp. Log every rejection to **trade-journal**.
- A rejection is not a negotiation. The research model cannot override, appeal, or re-argue it. Re-submitting a restructured order to evade a position-level or portfolio-level limit is itself a violation: those limits bind cumulative totals, not order shapes.
- If required inputs are unavailable — **portfolio-state** stale, reference quote unavailable, depth data missing — the check fails closed: reject the order and log the reason. Never pass a check on missing data.
- Limit changes take effect only when the user edits this file. Changes never apply retroactively to logged rejections, and no change can weaken hard limit 1 or hard limit 2.
