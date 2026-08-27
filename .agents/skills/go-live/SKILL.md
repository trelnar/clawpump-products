---
name: go-live
description: Use when bringing the bot from first credentials to full position sizing, when evaluating a phase gate, or after a critical incident forces a phase drop.
---

# Go-Live

The phased shakedown from first key to full sizing. Real money from day one, minimum size first. Each phase exists to catch one class of failure while the stakes are small — this is a shakedown sequence, not bureaucracy.

## Phase 0 — wiring (no orders)

Verification checklist. All items must pass:

- [ ] Keys scoped to dedicated sub-accounts; withdrawal disabled; IP-locked (**risk-limits** startup verification passes)
- [ ] Hot wallet funded with allocation + gas floor (**capital-allocation**)
- [ ] SMS round trip: bot sends a code, user replies, bot confirms parse (**approval-gate**)
- [ ] STOP and RESUME drill completes
- [ ] FLATTEN wiring check (dry: confirm code flow, no positions to flatten)
- [ ] Dead-man's switch fires when the heartbeat is paused (**vps-ops**)
- [ ] 48 h of clean **portfolio-state** reconciliation, zero unexplained breaks
- [ ] All **market-data** feeds green with fallbacks exercised once

## Phase 1 — minimum size, full pipeline

Live trading at venue-minimum order sizes. The pipeline runs exactly as production: research, approval, gates, execution, journal — nothing stubbed.

Exit criteria (editable): all of —

| Criterion | Default |
|---|---|
| Days in phase | 5 |
| Completed buy→exit cycles | 5, including at least 1 per automated venue and 1 DEX token |
| Fills vs plan | Every fill within its slippage cap; realized slippage logged vs model |
| Journal completeness | Every forecast, order, fill, and outcome row present in **trade-journal**; calibration rows landing |
| Reconciliation | Zero unexplained breaks |
| Mechanical exits | Zero missed by **position-monitor** |

## Phase 2 — ramp

Position sizes step through 25% → 50% → 100% of **risk-limits**-computed sizes. Each step gates on (editable):

| Gate | Default |
|---|---|
| Days at current step | 3 |
| Completed trades at current step | 5 |
| Unexplained reconciliation breaks | 0 |
| Missed mechanical exits | 0 |
| Orphan orders (unknown to **portfolio-state**) | 0 |
| SMS approval round trip | Under 2 min median |

## Advancement and regression

- The bot requests each phase or step advance with an **approval-gate** code; the user confirms by SMS. The bot never self-promotes.
- Any critical incident — unexplained break, missed mechanical exit, orphan order — drops one step automatically, alerts, and restarts that step's clock.
- Later, changed core enforcement code that touches order placement re-enters the ramp at reduced size (**runtime** deployment rules); agent-layer decision changes go through **backtest-replay** shadow mode instead.

Everything logs to **trade-journal**. Phase state lives in **portfolio-state**.
