---
name: gatekeeper
description: Use this skill to run any Solana token contract address through a 12-check scam-and-quality gauntlet before any token buy, returning one verdict — PASS, DEAD, or WALK — with raw numbers and zero execution capability.
---

# Gatekeeper

Gatekeeper is the token screen that says no. It runs a Solana token through 12 checks and returns one word: **PASS**, **DEAD**, or **WALK**. Raw numbers included. Opinions not.

Gatekeeper never trades. Ever. It holds no keys, controls no wallets, signs nothing, and submits no orders. It can look. It cannot touch. Every execution capability stays disabled while this skill runs — that's the product.

## Input

A Solana token contract address. That's the whole interface.

Run Gatekeeper when:

- The user pastes a contract address into chat.
- **execution** requests a screen before a token buy or add.
- **short-horizon-research** surfaces a token candidate.

## Non-negotiables

- A **DEAD** verdict cannot be overridden. Not by the model. Not by the user. The only thing that changes a DEAD is the token itself changing on-chain (for example, an authority revoked), confirmed by a fresh run.
- Report measured values only. No opinions, no softening, no "but the community looks strong."
- Fail closed. A Tier-1 check that cannot be measured counts as failed → DEAD. A Tier-2 check that cannot be measured counts as failed → WALK. No data is a failing grade.
- Gatekeeper must never place, modify, or cancel an order, and must never sign a transaction. The sell-simulation check uses read-only simulation only.
- Measure with the agent's enabled market-intelligence and on-chain tools. Never estimate a value you can read.

## Running the gauntlet

1. Resolve the address: name, ticker, decimals, token program (SPL or Token-2022).
2. Run Tier 1 in order (checks 1–5). Stop at the first failure and return **DEAD**. Run no further checks.
3. If Tier 1 clears, run all of Tier 2 (checks 6–12). Report every failure, not just the first.
4. Any Tier-2 failure → **WALK**. All 12 checks clear → **PASS**.
5. Log the verdict, timestamp, and every measured value to **trade-journal**.

## Tier 1 — instant DEAD

Any failure here means the dev can take the money, or already built the token so you can't leave. Short-circuit on the first failure.

| # | Check | Measure | DEAD when (editable default) |
|---|---|---|---|
| 1 | Mint authority | Mint authority on the token | Authority is active (not revoked) — dev can print supply |
| 2 | Freeze authority | Freeze authority on the token | Authority is active (not revoked) — dev can freeze your wallet |
| 3 | LP lock/burn | Share of LP tokens burned or verifiably locked, and remaining lock duration | < 90% of LP burned or locked, or remaining lock < 30 days |
| 4 | Sell simulation (honeypot) | Read-only simulated sell of the intended position notional (minimum $500) | Simulation fails, reverts, or returns materially less than quoted — you can buy but not sell |
| 5 | Transfer tax | Combined buy + sell tax, including Token-2022 transfer-fee extension | Combined tax > 10% |

## Tier 2 — the quality bar

Tier 2 runs only after Tier 1 clears. Any failure → WALK. All seven values appear in the verdict block regardless of outcome.

| # | Check | Measure | WALK when (editable default) |
|---|---|---|---|
| 6 | Top-10 concentration | Combined share held by top 10 wallets, excluding LP, burn, and known CEX wallets | > 30% of supply |
| 7 | Liquidity depth floor | Total pool liquidity in USD across tracked pools | < $25,000 |
| 8 | Volume authenticity | 24h volume ÷ liquidity, and unique 24h trader count | Ratio > 10, or unique traders < 50 — volume is wash, not demand |
| 9 | Contract age | Time since first liquidity was added | < 24 hours |
| 10 | Holder count | Current holder count | < 300 holders |
| 11 | Ticker impersonation | Ticker/name collision against established assets (top-500 crypto, well-known equity brands) | Name or ticker matches an established asset and this address is not the canonical one |
| 12 | Deployer history | Deployer wallet's prior tokens: LP pulls, >90% collapses, deployment rate | Any prior rug, or > 5 tokens deployed in the past 30 days |

## Threshold notes

Defaults are tuned for small speculative positions ($40–$500). Edit the table; it's plain Markdown.

The liquidity-depth floor (check #7) must scale with position size — a $5,000 position needs a far deeper pool to exit than a $50 one. Apply:

`floor = max(table value, 50 × intended position size in USD)`

Size the sell simulation (check #4) to the intended position, not a dust amount. Honeypots often let dust out.

## Verdicts

**DEAD** — a Tier-1 check failed. Do not touch. A DEAD verdict is never overridable per-verdict — not by the model, not by the user. Editing the Tier-1 thresholds in this file (the “editable default” column) changes future runs only; it never rescues a verdict already issued.

**WALK** — Tier 1 cleared, but the token failed the quality bar. Walk away. Automatic buying is blocked. The user can edit Tier-2 thresholds and re-run; the thresholds are the only lever.

**PASS** — the token cleared all 12 checks at screening time. A PASS means exactly that and nothing more. It does not mean the token will go up. Half of everything that passes will still go to zero for ordinary market reasons.

**UNSUPPORTED** — the address is not a Solana token. Gatekeeper screens Solana only; a non-Solana contract address returns UNSUPPORTED. Fail closed — UNSUPPORTED is never a PASS, and **execution** treats the token as unscreened: alert-only, no automated buy.

## Output format

Return one verdict block. Lead with the verdict. Raw numbers, no commentary.

DEAD (short-circuit — name only the failed check):

```
GATEKEEPER VERDICT: DEAD
Token: MoonSafeElonAI (MSEAI) — 4kfP…9qXz
TIER 1: Mint authority ACTIVE — dev can print supply
Verdict basis: Tier-1 check #1 failed. No further checks required.
```

WALK (list every failed Tier-2 check with the measured value against the threshold):

```
GATEKEEPER VERDICT: WALK
Token: GigaPaw (GPAW) — 8mQr…2vLd
TIER 1: 5/5 clear
TIER 2: Check #7 failed — liquidity $8,200 < $25,000 floor
TIER 2: Check #10 failed — 112 holders < 300 minimum
Verdict basis: Quality bar not met. Walk away.
```

PASS (all measured values, then the caveat):

```
GATEKEEPER VERDICT: PASS
Token: Clawback (CLAW) — 9xTe…7wRb
TIER 1: 5/5 clear
TIER 2: 7/7 clear
Liquidity: $61,400 | Top-10: 22% | Holders: 1,840 | Age: 3d 4h | Vol/Liq: 3.1 | Unique traders (24h): 412 | Ticker: no collision | Deployer: 1 token, 0 rugs
Verdict basis: All 12 checks cleared at 2026-08-26T14:02Z. PASS ≠ will go up.
```

When a verdict goes out by SMS, format it per **alert-format**. Do not add reasoning to the block unless the user asks — `Why?` in chat, or the exact-match SMS command `WHY <asset>` (per **alert-format**).

## Role in the bot

- **execution** requires a PASS fresh within its freshness window (default: 10 minutes, defined in the **execution** gate table) before any token buy or add. A stale PASS requires a re-run. DEAD or WALK blocks the buy.
- A PASS does not bypass anything. Position sizing and the 5% cap belong to **risk-limits**. First-buy SMS approval and whitelisting belong to **approval-gate**. Gatekeeper clears a token; it never authorizes a trade.
- Whitelisted assets still get screened. Approval removes the approval step, not the gauntlet.
- Sells never wait on Gatekeeper. Exits always execute per **execution**.
- Log every verdict to **trade-journal**, including PASSes that were never traded — **backtest-replay** uses them to test whether the thresholds are catching the right tokens.

Thirty seconds. Pennies in compute. One rug avoided pays for a thousand runs.
