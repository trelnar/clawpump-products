# CLAUDE.md

Guidance for AI assistants working in this repository.

## What this repository is

`clawpump-products` is a **markdown-only content repository** — there is no application code, no build system, no tests, and no dependencies. It publishes two kinds of product content for the ClawPump trading-agent ecosystem:

1. **Agent skills** in the `SKILL.md` format, distributed via `npx skills add trelnar/clawpump-products` or imported into a ClawPump agent from GitHub.
2. **Agent audit reports** — timestamped, evidence-based reviews of trading agents and automated strategies.

## Repository structure

```
README.md                              # Product page for GATEKEEPER (the flagship skill)
agent-audit-001-phoenix-copytrade.md   # Audit series entry #001 (numbered, dated)
.agents/skills/gatekeeper/SKILL.md     # Gatekeeper skill definition (currently an empty placeholder)
CLAUDE.md                              # This file
```

- **Skills** live at `.agents/skills/<skill-name>/SKILL.md`. This path layout is the distribution format that skill-compatible clients consume — do not move or rename it.
- **Audits** live at the repo root, named `agent-audit-NNN-<slug>.md` with a zero-padded sequence number.

### Known state

`.agents/skills/gatekeeper/SKILL.md` is currently empty, even though the README describes it as containing the 12-check gauntlet and an editable threshold table. If asked to work on the Gatekeeper skill, the check logic described in the README (tiered checks, PASS/DEAD/WALK verdicts, position-size-scaled liquidity thresholds) still needs to be authored there.

## The Gatekeeper product

Gatekeeper screens Solana tokens through a 12-check scam-and-quality gauntlet and returns exactly one verdict: **PASS**, **DEAD**, or **WALK**. Core product invariants that any edit must preserve:

- **Read-only, always.** Gatekeeper never trades. All execution capabilities are disabled by design; it requires only Market Intelligence, Token Sniper, and Bitget Intel capabilities. Never add trading, swapping, or transaction-signing behavior to the skill.
- **Verdicts are non-negotiable.** A DEAD verdict cannot be argued with or overridden — including by the user. Do not add override mechanisms.
- **Tiered checks short-circuit.** A Tier-1 failure (e.g. active mint/freeze authority) ends the run immediately; output states which check failed and stops.
- **Output format:** a `GATEKEEPER VERDICT:` line, token identification, the failing/passing tier details, and raw numbers. Facts, not opinions.
- **Thresholds are user-editable plain Markdown** (a table in SKILL.md), tuned by default for small speculative positions ($40–$500).

## Audit report conventions

New audits must follow the pattern established by `agent-audit-001-phoenix-copytrade.md`:

- Filename `agent-audit-NNN-<slug>.md`; increment the sequence number.
- Header records **when the audit was conducted and when it was published**, plus a one-line result summary (e.g. "11 traders audited · 0 qualified · $0 allocated").
- **Criteria are pre-committed** — written and stated before any subject is examined. Verdicts are mechanical PASS/VETO against those criteria, with the specific failed criterion named. No discretion, no price predictions, no "vibes."
- Findings are presented with receipts: claimed figures vs. on-chain reality, in a table.
- Every audit ends with a **disclaimer** (point-in-time observations, not financial advice, not accusations of wrongdoing) and the audit-series footer line.

## Writing style

- Voice is terse, declarative, and safety-first ("Raw numbers included. Opinions not."). Match it.
- Never remove or weaken disclaimers. Anything touching trading content must keep the not-financial-advice framing.
- Plain GitHub-flavored Markdown only; no HTML, no build tooling.

## Development workflow

- There is nothing to build, lint, or test — changes are edits to Markdown files.
- Verify links and file paths referenced in prose actually exist (the README ↔ SKILL.md relationship is the main coupling).
- Commit messages are short and descriptive (e.g. "Create agent-audit-001-phoenix-copytrade.md"); the default branch is `main`.
