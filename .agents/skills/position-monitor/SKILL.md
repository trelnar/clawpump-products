---
name: position-monitor
description: Use when watching held positions, COMING UP candidates, and confirmation triggers between research cycles, and when firing mechanical exits.
---

# Position Monitor

The watcher between research cycles. **short-horizon-research** decides; this skill makes sure nothing happens to a position or a maturing candidate while nobody is looking. It runs in the deterministic core (**runtime**) — the fast path never waits on a model.

## What it watches

- Every held position, continuously.
- Every COMING UP candidate and its confirmation trigger.
- Liquidity on every held token.
- Feed freshness (from **market-data**) for everything above.

## Frequency ladder

Edit the table; it's plain Markdown.

| Target | Check interval (editable) |
|---|---|
| Held on-chain tokens | 5 s |
| Held listed crypto | 10 s |
| Held equities (market sessions) | 10 s |
| Held equities (off hours) | 5 min |
| COMING UP — near trigger or high signal velocity | 30 s |
| COMING UP — far from trigger | 5 min |

Per the research skill's coming-up monitoring rules, intervals tighten as a candidate approaches its predicted trigger, and with volatility and signal velocity.

## Fast path (mechanical, no model)

Every position carries research-supplied exit conditions: an invalidation price, scenario-invalidating levels, and any standing profit plan. The fast path evaluates them on every tick:

- A crossed invalidation level fires a SELL NOW ticket to **execution** immediately. No model call, no approval — sells are automatic.
- A standing profit plan (e.g. scale out half at 2x) executes the same way when its level hits.
- The fast path stays armed at all times, including while the model path is thinking.

## Model path (queued re-evaluation)

Nuanced state changes queue a re-evaluation by **short-horizon-research** through the **runtime** priority queue (held positions first):

- 2x reached with no standing plan — the hold / take-profit / scale-out reassessment the research skill requires.
- Momentum exhaustion, narrative deterioration, hype-velocity collapse.
- A COMING UP trigger condition met — revalidate before promotion to BUY NOW; never promote on the trigger alone.
- Anything the fast path cannot express as a level.

If the model layer is down, the fast path keeps protecting positions (**runtime** failure rules).

## Liquidity deterioration

Losing the exit is the worst case. On a held token:

| Condition (editable) | Action |
|---|---|
| Pool liquidity down 30% from entry-time depth | Queue urgent re-evaluation; ops alert |
| Pool liquidity down 50%, or exit slippage estimate exceeds 2x tier cap | Fire exit evaluation immediately — default to exit unless research overrides within 60 s |
| Price feed lost on a held asset past staleness | Ops alert; treat as deteriorating until coverage returns |

## Logging and alerts

Every trigger fire, promotion, exit, and state change logs to **trade-journal**. Alerts follow **alert-format** throttle rules: state changes send, ticks do not. Monitoring workloads run on the **market-data** monitoring reserve and never block on discovery.
