---
name: mayfly
description: Short-hold momentum trading for hyped Solana tokens with mechanical exits. Use when the user pastes a token call or contract address wanting a quick in-and-out trade, asks to scan for hype or momentum plays, or asks to review or exit open MAYFLY positions. Every position gets a stop-loss, profit targets, and a hard time limit written at entry. Not for investing, accumulating, or leverage.
---

# MAYFLY — the hype trade that dies on schedule

Adult mayflies live about a day. Positions opened under this skill live less.

MAYFLY trades the tokens people call "early": Discord calls, paid boosts, trending pools, pump.fun curves about to graduate. It takes multiple small swings per day, holds for minutes to hours, and exits on rules that were written down before the entry filled. It never holds overnight, never averages down, and never argues with its own stop.

GATEKEEPER (this repo's other product) is the screen that says no. MAYFLY is what's allowed to say yes: small, fast, and pre-committed to its own funeral.

**Working assumption, stated once and built into everything below:** by the time a call reaches a public channel, earlier wallets are loaded and your buy is somebody's exit. MAYFLY treats every signal as probable distribution and trades it anyway — with size that expects to be wrong and exits that don't wait to find out.

---

## Operating modes

| Mode | What happens | Default |
|---|---|---|
| `shadow` | Full pipeline runs — signals, screens, entries, exits — with paper fills at quoted price ± 1% modeled slippage. Real ledger, no money. | **Yes. MAYFLY starts here.** |
| `live` | Same pipeline, real execution. | Requires arming, below |

**Arming live mode requires ALL of:**

1. At least **10 completed shadow trades** in the ledger, and the user has been shown the shadow ledger summary.
2. The user types in chat, exactly: `MAYFLY LIVE — I can lose every dollar of this`
3. Execution and market-data capabilities actually enabled on the agent (on ClawPump: Market Intelligence + Token Sniper).
4. The agent can re-check open positions at least every 2 minutes (continuous or scheduled runs). If monitoring between chat messages cannot be guaranteed, MAYFLY stays in shadow — a stop-loss nobody is watching is a story, not a stop.

No shadow ledger, no live trading. You are allowed to lose paper money first.

---

## Entry paths

### CALL mode — the user brings the signal

The user pastes a call: a Discord/Telegram message, a ticker, or a contract address.

1. **Parse.** Extract contract addresses (Solana base58, 32–44 chars) and `$TICKER` strings. If only a ticker: resolve via DEX search, take the highest-liquidity match, and check for ticker impersonation (same symbol as a major token = resolve by address only or decline).
2. **Timestamp the call.** Record token price at the moment the call was received — this is `call_price`, the baseline for the chase guard.
3. **Tag the source.** Which server/channel/caller, as specifically as the user can say. Feeds the caller scorecard.
4. Run the pipeline (below).

Getting calls in: paste them by hand, forward via webhook, or run a **bot account** in a server where you have permission to run one. Automating a personal Discord account (self-botting) violates Discord's ToS and gets accounts banned — MAYFLY does not ask for that and does not need it.

### SCAN mode — MAYFLY hunts

On request ("scan for plays") or on a 15–30 minute cadence when running continuously. Sources, in priority order (endpoints and field mappings in `references/signal-sources.md`):

| Source | What it signals |
|---|---|
| DexScreener token boosts (latest) | Someone **paid** to promote this token, usually the team. Marketing push imminent. Also: dump often scheduled behind it. Candidate, never endorsement. |
| Pump.fun bonding curve 70–95% complete | Pre-graduation window — the "about to go more public" moment people front-run. Graduation adds liquidity and visibility. |
| GeckoTerminal / DexScreener trending + new pools | Volume and buyer spikes crossing thresholds before the token hits mainstream feeds. |
| 5m/1h anomalies on watched pairs | Volume pace ≥ 3× prior hour, net buy pressure, holder growth. |

Score each candidate 0–10 (1 point each, capped): m5 volume ≥ $5k · m5 volume rising · buys>sells · h1 makers ≥ 50 · liquidity ≥ $25k · boost active < 2h old · curve 70–95% · age < 48h · not yet on trending (earlier is worth more) · social mention provided by user. Take at most the **top 2 candidates per scan**, then run each through the full pipeline. A scan that produces zero entries is a successful scan.

---

## The pipeline

Every candidate, both modes, in this order. Any hard failure → output `MAYFLY DECLINED` with the reason and stop.

### 1. Screen — the gate

**If the GATEKEEPER skill is installed, run it first. DEAD means no trade. That verdict cannot be argued with, including by the user. Same rule as over there.**

If GATEKEEPER is not installed, apply this built-in minimum gate — ALL must pass:

| # | Check | Threshold |
|---|---|---|
| G1 | Mint authority | Revoked |
| G2 | Freeze authority | Revoked |
| G3 | LP tokens | ≥ 80% burned or locked |
| G4 | Top-10 holders (excluding LP) | < 30% of supply |
| G5 | Liquidity | ≥ $15,000 AND ≥ 20× `unit_size` |
| G6 | Token age | ≥ 30 minutes (the zero-block snipe race was lost before you saw the signal) |
| G7 | Sells in last 15 min tape | Present — people are demonstrably able to exit |
| G8 | Wash smell | h1 volume ≤ 40× liquidity AND h1 traders ≥ 30 distinct wallets |
| G9 | Ticker | Not impersonating a top-200 token |

### 2. Confirm — momentum, not hope

ALL required at decision time:

| # | Check | Threshold |
|---|---|---|
| C1 | m5 volume | ≥ $5,000 and above the prior 5m |
| C2 | m5 buys : sells (count) | ≥ 1.5 |
| C3 | Structure | Price above its level 15 min ago |
| C4 | Not already vertical | h1 change ≤ +150% |
| C5 | Chase guard (CALL mode) | Price ≤ +35% above `call_price` |
| C6 | Freshness | Signal ≤ 20 minutes old |
| C7 | Impact | A `unit_size` buy moves price < 2% (estimate from pool depth) |

A great token that fails C5 is not a missed trade. It is somebody else's completed trade.

### 3. Size — decided by the table, not the vibe

- Position = `unit_size`. Flat. Every trade. Winners do not compound into the next lottery ticket; losers do not get revenge size.
- Respect `max_concurrent` and `max_entries_per_day`. Full book = decline, even a 10/10 candidate.
- Hard ceiling $500 per position regardless of config edits — above that, exit slippage in these pools eats the strategy (same scaling note as GATEKEEPER's liquidity floor).

### 4. Enter

Single market buy of `unit_size`, max slippage 2% — abort the fill if worse, do not chase the fill. Record the actual fill price. In shadow mode: quoted mid ± 1% modeled slippage.

**Immediately write the exit ticket** — all exits computed from fill price, fixed at entry:

```
MAYFLY ENTRY — $TICKER (CA: …)
mode: live | source: discord/#alpha-calls @somecaller | score 7/10 | gate PASS 9/9
fill: $0.00842 × $75 | slippage 0.6%
EXIT TICKET (fixed at entry):
  stop      $0.00716  (−15%)
  tp1       $0.01053  (+25% → sell 50%, stop → breakeven)
  tp2       $0.01347  (+60% → sell 25%)
  runner    25% rides a 20% trail off the high
  clock     dies 17:42 UTC (4h)
```

### 5. Manage — the loop

While any position is open, re-check every 60s (or the fastest the runtime allows; if that's worse than every 2 min, live mode should not have been armed). Precedence order — first match acts, no discretion:

| Priority | Trigger | Action |
|---|---|---|
| 1 | Liquidity < 75% of entry-time liquidity | **Market-exit everything now.** Rug in progress. Accept the slippage. |
| 2 | Dev/top-10 wallet sells > 2% of supply | Market-exit now |
| 3 | Re-screen turns DEAD (re-run gate hourly on holds) | Market-exit now |
| 4 | Stop hit | Exit full remaining position |
| 5 | TP1 / TP2 hit | Scale out per ticket; after TP1, stop moves to breakeven |
| 6 | Runner: price < (session high − 20%) | Exit runner |
| 7 | Clock expired | Exit full remaining position at market. The clock does not care that it "looks strong." |
| 8 | Dead tape: m5 volume < 15% of entry-time pace for 30 min | Exit — hype that stops moving is just a bag |

If the session/agent is about to go offline and monitoring cannot be handed off: **flatten everything first.** MAYFLY never holds unattended.

### 6. Log — every trade, no exceptions

Append to the ledger (a markdown table the agent maintains in its memory/workspace):

`opened | closed | ticker | CA | mode | source | score | gate | size | fill | exits (each partial: price/size/reason) | PnL % | PnL $ | exit_reason`

After every exit: set the 24h re-entry cooldown on that token and update the caller scorecard.

---

## Kill switches

| Condition | Action |
|---|---|
| Day PnL ≤ −4% of `bankroll` | Halt new entries until next UTC day. Open positions still managed to exit. |
| 3 consecutive stop-outs | Same halt. Three stops in a row means the market regime, the source, or both are wrong today. |
| Week PnL ≤ −10% of `bankroll` | Halt the week. Post the ledger and the caller scorecard for review before re-arming. |
| Same token after exit | 24h cooldown, no re-entry — winners included. It already paid you or already bit you. |

Halts are announced, logged, and not negotiable mid-day. Config changes take effect the next UTC day, so the loss cap can't be "adjusted" while it's the thing currently tripping.

## Caller scorecard

Every CALL-mode source accumulates: calls seen, taken, declined (and why), hit rate, average PnL. After 10 taken calls with negative expectancy, the source is **muted** — its future calls get logged and declined automatically. The user can unmute by saying so, and the scorecard keeps counting.

Loyalty is for dogs. The scorecard decides.

## What MAYFLY will not do

- **Leverage.** No.
- **Average down.** Adding to losers is the exact behavior that got every top-ranked trader vetoed in [Agent Audit #001](../../../agent-audit-001-phoenix-copytrade.md). It does not get smarter with memecoins.
- **Widen a stop or extend the clock.** The ticket written at entry is the ticket.
- **Hold overnight**, or unattended, ever.
- **Trade a DEAD verdict**, buy sub-30-minute launches, or exceed a cap because "this one's different." It isn't. That's why there's a table.

## Config

Plain markdown — edit the values. Changes apply next UTC day.

| Key | Default | Notes |
|---|---|---|
| `mode` | `shadow` | `live` only via the arming procedure |
| `bankroll` | $2,000 | Total capital MAYFLY may ever touch |
| `unit_size` | $75 | Flat per-position; hard ceiling $500 |
| `max_concurrent` | 3 | |
| `max_entries_per_day` | 8 | |
| `stop_loss` | −15% | |
| `tp1` / `tp2` | +25% / +60% | Sell 50% / 25% |
| `runner_trail` | 20% | On the final 25% |
| `time_stop` | 4h | Sane range 30m–6h |
| `max_slippage` | 2% | Abort fills beyond this |
| `scan_cadence` | 20 min | SCAN mode |
| `daily_loss_halt` | −4% of bankroll | Or 3 consecutive stops |
| `weekly_loss_halt` | −10% of bankroll | |
| `reentry_cooldown` | 24h | Per token |

Worst normal day at defaults: 8 entries all stopped ≈ −$90 on a $2,000 bankroll, then the halt trips first (−4% = −$80). The math of the whole strategy lives in that sentence.

---

*The honest part: MAYFLY's edge, if it has one, is not prediction — it is that it leaves on time, every time, while hype-chasing humans do not. Expect hit rates near a coin flip in good weeks, strings of small red, and occasional runners paying for the pile. Zero is a reachable outcome for any individual token and for the strategy. Fund it only with money whose total loss changes nothing about your life. Nothing in this file is financial advice.*
