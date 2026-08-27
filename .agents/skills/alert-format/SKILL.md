---
name: alert-format
description: Use when composing any outbound Telegram message — action alerts, approval requests, ops alerts, STATUS or REPORT replies, WHY replies — or when deciding whether a change is material enough to send at all.
---

# Alert Format

This skill defines the wire format for every Telegram message the bot sends and the throttle rules that decide whether a message goes out. Content comes from other skills: recommendations from **short-horizon-research**, approval mechanics from **approval-gate**, state from **portfolio-state**, calibration from **trade-journal**, incidents from **vps-ops**, **execution**, and **risk-limits**. Writing style follows **docs-voice**. Transport (Telegram bot provisioning, polling or webhook, delivery monitoring) lives in **vps-ops**.

Log every outbound message to **trade-journal**: kind, asset, full body, delivery status.

## Message classes

| Class | Prefix | Throttled |
|---|---|---|
| Action alert | Action word (`BUY NOW`, `COMING UP`, `HOLD`, `ADD`, `SELL NOW`) | Yes, except SELL NOW |
| Approval request | Action word + reply code | No |
| Ops alert | `OPS:` | Never |
| Command reply (`STATUS`, `REPORT`, `WHY`, confirmations, errors) | Named by command | No — always answer a command |

## Telegram constraints

Every message must satisfy all of the following:

- One alert is one message (Telegram allows 4096 characters — length is rarely the constraint; scannability is). Never split one alert across separate messages.
- Line 1 must carry the action, the asset, and the current price. The lock-screen notification preview shows roughly the first 100 characters; the decision must be readable from it.
- Approval requests attach an inline keyboard: an `Approve` and a `Reject` button whose callback data carries the single-use code (**approval-gate**). A button tap is equivalent to the typed `YES <code>` / `NO <code>`; typed commands remain valid.
- Keep the ASCII conventions: write `2x`, not `2×`; `-`, not `—`; `>=`, not `≥`; straight quotes. They read identically on every device and keep templates portable.
- No greetings, sign-offs, emojis, markdown, or filler. Every line must change what the user knows or does.
- Timestamps in UTC, 24-hour format.

Length caps are editable defaults. Edit the table; it's plain Markdown.

| Message class | Max lines |
|---|---|
| Action alert / approval request | 15 |
| Ops alert | 2 |
| STATUS reply | 25 |
| REPORT reply | 30 |
| WHY reply | 30 |

When content exceeds the cap, drop fields from the bottom of the template up. Never drop the first line, probabilities, confidence, or the approval buttons and code.

## Action alerts

Lead with the action, then the asset, then the price — all on line 1. Use the field set from the **short-horizon-research** alert format, adapted for the message template:

```
[ACTION] [ASSET/TICKER] @ [price]
Type: Stock / Crypto
Exch: [exchange or chain]
Buy zone: [range]
2x target: [price]
Higher: [price or range]
5x potential: [assessment]
P(2x): [%]  P(5x): [% when relevant]
Conf: [%]
Window: [estimated timing]
What: [one sentence]
Hype: [one sentence]
Trigger: [only when relevant]
```

Rules:

- The action must be one of: `BUY NOW`, `COMING UP`, `HOLD`, `ADD`, `SELL NOW`.
- Identify the asset by ticker plus venue for equities, ticker plus chain for tokens. On any ticker-impersonation risk, append the short contract address.
- Omit fields that do not apply: `Buy zone` on HOLD and SELL NOW, `Trigger` when none exists, `P(5x)` when not modeled. Never omit price, `P(2x)`, or `Conf`.
- SELL NOW replaces `Buy zone` with `Exit: [reason, <=6 words]` and current position P&L.
- For alert-only venues (Robinhood, Crypto.com — see **execution**), append `MANUAL - no API. Trade by hand.` The bot never places these orders.
- Mark `2x candidate`, `High-upside`, or `5x+ candidate` on line 1 after the price only when **short-horizon-research** assigns the label.
- No reasoning in the alert. Evidence is available via `WHY` (below).

Example:

```
SELL NOW WIF/SOL @ 0.00312 (+86%)
Type: Crypto  Exch: Raydium
Exit: whale distribution, hype rolling over
P(2x): 12%  Conf: 79%
```

## Approval requests

An approval request is an action alert plus the `Approve`/`Reject` inline buttons and the reply block from **approval-gate**. Put the single-use code on line 1 so the typed fallback works from the notification preview alone.

```
BUY NOW BONK/SOL @ 0.0000142 - reply YES K7NR4T
Type: Crypto  Exch: Raydium
Buy zone: 0.0000138-0.0000146
2x target: 0.0000284  Higher: 0.000040+
P(2x): 58%  P(5x): 11%  Conf: 74%
Window: 12-36h
What: dog-hat memecoin, new CEX listing
Hype: 3 independent communities, mentions 9x in 6h
Reply YES K7NR4T to buy and whitelist.
Reply NO K7NR4T to reject.
Expires: 30 min (14:52 UTC)
```

Reply syntax, code generation, expiry, and the advanced-instrument and resume variants are defined in **approval-gate**. Quote the reply commands exactly as **approval-gate** specifies — the parser is exact-match, so a typo in the alert produces unparseable replies.

## Ops alerts

Prefix `OPS:`. One line: what happened, what the bot did, what needs the user. Never throttled, never batched, never deduped away. If nothing needs the user, end after the action taken.

```
OPS: <cause>. <action taken>. <what the user must do, if anything>.
```

| Event | Source | Example |
|---|---|---|
| Emergency halt | **risk-limits** | `OPS: HALT. Portfolio -21% in 24h (peak 14210, now 11226). Buying stopped; sells continue. Reply RESUME to restart.` |
| Bot offline / restarted | **vps-ops** | `OPS: Bot restarted after 14 min down. State rebuilt, reconciling. SELL_ONLY until clean.` |
| Reconciliation mismatch | **portfolio-state** | `OPS: Recon mismatch Coinbase BONK: internal 41.2M, venue 39.8M. Buying frozen. Check for manual trades.` |
| Venue down | **vps-ops** | `OPS: Coinbase API down 7 min. Buying paused there; exits retrying. 2 positions on venue.` |
| Order failure | **execution** | `OPS: SELL WIF order rejected 3x (insufficient liquidity). Retrying smaller clips. No action needed yet.` |

Additional ops lines (key permission faults, inbound Telegram path down, backup failures, security events) are defined in **vps-ops** and **approval-gate** and use the same one-line format.

## STATUS reply

Answer the `STATUS` command (parsed in **approval-gate**) from **portfolio-state**:

```
STATUS 14:22 UTC
Value: 12,840 USD (24h -3.1%)
Cash: 6,210 USD (CB 5,100 / ETRADE 1,110)
Positions 3:
BONK +41% (2.9% of book)
WIF +86% (4.1%)
RGTI -8% (3.0%)
Halt: NORMAL  Whitelist: 12  Pending: 1
```

One line per position: asset, unrealized P&L %, percent of portfolio value. `Halt` shows the current mode from **portfolio-state** (NORMAL, EMERGENCY_HALT, USER_STOP, RECON_FREEZE, SELL_ONLY).

## REPORT reply

Answer the `REPORT` family (content assembled by **trade-journal**):

```
REPORT 24h
Forecasts: 9  Entered: 3  Exited: 2
Realized: +11.4%  Hit 2x: 1/2
Open windows: 4
Calibration: stated P(2x) 55% avg, realized 48% (n=31, in range)
```

`REPORT WEEK` adds max-multiple distribution and slippage vs plan. `REPORT CAL` lists one line per probability bucket and target level: `P(2x) 60-70: stated 64, realized 41, n=17 FLAG`. Flag thresholds live in **trade-journal**.

## WHY reply

`WHY <asset>` returns the evidence behind the most recent alert or position for that asset. `WHY`, like the `REPORT` commands, is a row in the **approval-gate** command table (**approval-gate** owns the parser); this skill defines only the reply content and format. `WHY` is read-only and must never trigger a trade or state change.

```
WHY BONK
Bull: CEX listing confirmed (2 sources); mention velocity 9x/6h across 3 communities; smart-money accumulation on-chain.
Bear: top-10 wallets hold 31%; one prior failed breakout.
Invalidation: close below 0.0000131.
Analogue: PEPE listing pattern 2023 - 2x in 31h median.
```

Cap at the WHY line limit. If the user asks again (`WHY <asset>` a second time within 1 h), send the next level of detail per the **docs-voice** detail-on-demand rule. An asset with no alert history in 7 days gets `No recent analysis: <asset>`.

## Throttle and dedupe

Alert on material change only (**short-horizon-research** automation objective). Defaults below are editable — edit the table; it's plain Markdown.

| Rule | Default |
|---|---|
| Material probability change (re-alert same asset, same action) | P(2x) or P(5x) moves >= 10 points |
| Material confidence change | >= 15 points |
| Minimum re-alert interval, same asset, no material change | 4 h |
| COMING UP minor updates | Batch into one digest, max 1 per 60 min |
| HOLD reconfirmations | Suppress; send only on entry to HOLD or material change |
| Digest cap | 5 assets per digest message, ranked by P(2x) |
| Quiet hours | None. Send at any hour |

Always send immediately, never throttled, never batched:

- Any action-state change (COMING UP -> BUY NOW, HOLD -> SELL NOW, HOLD -> ADD).
- Every SELL NOW.
- Every approval request and every reply to an inbound command.
- Price crossing a stated buy zone, 2x target, or invalidation level for a held or alerted asset.
- An asset reaching 2x with evidence supporting a higher target.
- Every `OPS:` alert.

Dedupe: never resend an alert whose action and all material fields match the last sent alert for that asset. A repeated alert must state what changed on line 2 (`Chg: P(2x) 58->71`).

If outbound delivery fails, follow **approval-gate**: retry, then treat the message as never sent — an unconfirmed approval request must never lead to a trade. Log delivery failures to **trade-journal** and escalate per **vps-ops**.
