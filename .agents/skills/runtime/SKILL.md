---
name: runtime
description: Use when deciding which layer — the deterministic core process or the AI agent — owns a behavior, when scheduling agent work, or when handling model-layer failures.
---

# Runtime

The bot runs as two layers on the VPS. The split is the safety model: the agent decides, the core enforces.

## The two layers

| Layer | What it is | What it owns |
|---|---|---|
| Deterministic core | A code process (no model calls) | **execution** order placement, **risk-limits** enforcement, **portfolio-state**, **approval-gate** parsing and webhook, **position-monitor** fast path, **trade-journal** writes, **market-data** feed handling, **alert-format** message assembly |
| Agent layer | Model-driven research runner | **short-horizon-research** (all research agents), **signal-hygiene** labeling, **position-monitor** model path, **capital-allocation** suggestions, **backtest-replay** challenger analysis |

Rules:

- The agent layer must never hold venue credentials or place orders. It emits action tickets (schema in **execution**); the core validates every ticket against **risk-limits** mechanically. Model output cannot bypass a rejection.
- The core must never require a model call on the trade path. Mechanical exits, gate checks, approval handling, and Telegram command parsing run as code within the **execution** gate time budget.
- The core exposes read-only state (positions, cash, marks, freshness) to the agent layer. The agent reads; it does not write state directly.

## Scheduling

The agent layer runs three queues, in priority order:

1. **Held-position re-evaluations** — queued by **position-monitor**'s model path. Always served first.
2. **COMING UP revalidations** — trigger-met candidates awaiting promotion.
3. **Discovery** — continuous scans at the cadences in **market-data**.

Event-driven wakeups (trigger fired, halt, approval received, reconciliation freeze) preempt queued work.

## Compute budget

| Setting (editable) | Default |
|---|---|
| Model spend budget per day | Set at go-live; alert at 80% |
| Budget priority on exhaustion | Cut discovery breadth first; never cut held-position re-evaluation |
| Max model latency before fallback | 60 seconds |
| Secondary model endpoint | Configured at deploy; same contract |

Degrading discovery is an ops event: alert per **alert-format**, never silent.

## Model-layer failure

If the agent layer is down, errors, or exceeds latency limits:

- The core keeps running. Mechanical exits from **position-monitor** still fire. Sells still execute.
- New buys pause (no research means no valid tickets). Ops alert: model layer down, positions still protected.
- On recovery, drain the re-evaluation queue before resuming discovery.

The reverse failure (core down) is a **vps-ops** incident: the dead-man's switch fires, nothing trades.

## Deployment shape

- Two supervised services per **vps-ops**: `bot-core` and `bot-agent`. The core restarts into **portfolio-state** cold-start recovery; the agent restarts stateless and re-reads queues.
- Changes to agent-layer decision logic go through **backtest-replay** shadow mode. Changes to core enforcement code go through **go-live** phase gates at reduced size when they touch order placement.
