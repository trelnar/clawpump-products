---
name: equities-constraints
description: Use before any equities order or plan — PDT day-trade budgets, settlement, halts, sessions, and shorting mechanics that constrain how equity trades execute.
---

# Equities Constraints

US market-structure rules the bot must obey on equity venues. These are mechanics, not quality judgments — nothing here vetoes an asset. **execution** consults this skill before every equity order.

## Pattern day trader (PDT)

A margin account under $25,000 equity is limited to 3 day trades per 5 rolling business days. A same-day round trip in the same symbol spends one.

- Track the day-trade counter per account in **portfolio-state**; check it before any equity buy that might round-trip same-day.
- When the budget is low, prefer holding overnight — the 1–3 day horizon usually allows it — and alert when the budget forces a plan change.
- An invalidation-level exit always wins over the day-trade budget. Never hold a losing position to dodge a PDT flag; take the day trade, alert, and pause new equity buys if the account would be restricted.
- At or above $25k equity, PDT does not bind; the counter still logs.

## Account types

| Account type | Constraint (mechanical) |
|---|---|
| Margin ≥ $25k | No PDT limit; margin rules apply |
| Margin < $25k | 3 day trades / 5 business days |
| Cash | Settled funds only (T+1); no day-trade limit, but a good-faith violation occurs if unsettled proceeds are spent and the new position is sold before settlement — the bot must not create GFVs |

Buying power for cash accounts = settled cash, tracked in **portfolio-state** (settled vs unsettled).

## Halts

- **LULD volatility pauses** (usually 5–10 min) and **news halts** (open-ended): a held stock that halts cannot be exited. Send an ops alert immediately; on reopen, queue an urgent **short-horizon-research** revalidation before any action — reopen prints gap.
- **SSR** (short-sale restriction, triggers on a 10% intraday decline): restricts short entries for the rest of the day and the next — applies to approved advanced-instrument shorts only.

## Sessions

| Session | Rules |
|---|---|
| Regular hours (9:30–16:00 ET) | Full order policy per **execution** |
| Pre/post market | Limit orders only; thin liquidity — halve the per-order book-depth cap; wider slippage is expected, not an anomaly |
| Overnight | No session. Gap risk is inherent to holding equities on a 1–3 day horizon; it belongs in the research skill's risk modeling, never as a veto |

A SELL NOW that lands outside all sessions alerts immediately and executes at the next session open if the exit condition still holds (**execution** already defines this).

## Shorting (advanced instruments)

Approved shorts require a locate; hard-to-borrow fees must be priced into expected return before the approval request is sent. No locate, no order.

## Integration

- **execution**: consult before every equity order (PDT budget, settlement, session, halt state).
- **risk-limits**: the day-trade budget is enforced like any other pre-order check — a failed check is a mechanical rejection.
- **portfolio-state**: tracks settled vs unsettled cash and the rolling day-trade counter.
- **market-data**: halt and SSR state come from the equities feeds.
