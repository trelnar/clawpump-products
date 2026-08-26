---
name: backtest-replay
description: Use this skill to evaluate any challenger model, parameter change, or new signal against recorded history and shadow trading before it is allowed to affect live decisions, and to auto-demote a promoted model that degrades live.
---

# Backtest Replay

This skill is the gate for model changes. It implements the **short-horizon-research** "Model improvement" section. No challenger model, parameter change, signal addition, signal reweighting, or similarity-characteristic change goes live until it passes this harness end to end: replay, out-of-sample comparison, then shadow mode. There is no fast path. A change the harness has not passed does not touch live decisions.

Scope boundary: this harness tunes the decision pipeline only. The hard limits in **risk-limits** (5% position cap, 20%/24 h emergency halt) are not parameters. No backtest result can justify changing them, and the harness must apply them unchanged in every simulation.

## Recorded-input replay

The live bot records every decision input as it runs. **trade-journal** `forecasts.evidence_state` captures the signals, data points, reference quotes, and model version behind every decision at decision time. Replay re-runs the decision pipeline against those recorded inputs exactly as they were known then.

Requirements:

- Any journaled period must replay bit-for-bit: same inputs, same pinned model version, same versioned config, same random seeds, same decisions. A replay of the production model over its own history that produces different decisions is a harness bug. Fix it before trusting any comparison.
- Discovery-stage inputs are journaled too, not only inputs behind issued forecasts. A challenger claiming better discovery must be testable against the full recorded signal stream, including signals production saw and ignored.
- An input that was not journaled cannot be replayed and must not be reconstructed from present-day sources. If a period's inputs are incomplete, mark the period non-replayable and exclude it from comparisons for both models symmetrically.

### No lookahead

- The replay feeds inputs in recorded timestamp order. At decision time T, the challenger sees only rows with timestamp ≤ T.
- Outcome rows, resolved prices, and anything derived from them are never inputs.
- Historical-analogue lookups inside a replayed decision query only data recorded before T.
- Corrections (`supersedes` rows) are visible only from their own timestamps, not from the timestamps of the rows they correct. The model knows at T what was believed at T, including beliefs later corrected.

## Survivorship

The replay universe is every asset that produced a recorded discovery event in the period. That includes tokens that rugged, tokens **gatekeeper** would kill today, stocks that halted, and assets that were delisted or went to zero. Excluding losers from the universe invalidates the run: a model scored only on survivors is scored on the answer key.

- A rugged or drained token held at rug time scores as a total loss of the position.
- A halted stock held at halt scores at the post-halt reopening price; if it never reopened in the window, score it as a total loss.
- Unresolvable outcomes (per **trade-journal**) score as losses for held positions and never count as identification wins.
- Never fabricate or interpolate prices to soften these cases.

## Execution realism

Simulated fills must be pessimistic. Backtest profits that live execution cannot capture are noise. Model fills from the recorded market state:

- Charge the recorded venue fee on every simulated fill.
- Cross the recorded spread; never assume mid-price or maker fills.
- Scale slippage with simulated order size against recorded depth and volume.
- Produce partial fills when recorded liquidity cannot absorb the order, mirroring the child-order splitting rules in **execution**.
- Apply the same pre-trade gates **execution** applies live, including **risk-limits** checks and the **approval-gate** whitelist state as it existed at the replayed time. Alert-only venues (Robinhood, Crypto.com) fill nothing in simulation, exactly as live.

Exit liquidity is the binding constraint, especially for microcaps and new tokens. A backtest must not assume it can sell more than a fraction of recorded depth. A simulated 5× on a position the recorded pool could never absorb is a 5× on paper and a rug in production.

| Parameter | Default (editable — edit the table; it's plain Markdown) |
|---|---|
| Max exit participation, order book venues | 20% of recorded resting depth within 2% of the touch, per fill interval |
| Max exit participation, DEX pools | 10% of recorded pool liquidity per fill interval |
| Max participation of recorded interval volume | 15% |
| Slippage model | Walk the recorded book / pool curve for the simulated size; minimum 1 tick or 10 bps beyond recorded spread, whichever is worse |
| Unfillable remainder | Held to next interval; still unfilled at window close scores at the worse of last recorded bid or zero for drained pools |
| Fees | Recorded venue fee schedule at the replayed time, taker rate |

## Evaluation metrics

The primary metric is **correct identification of assets that achieved at least 2× within 1–3 days**:

- **Recall:** of the universe assets that achieved ≥2× in the window, the share the model flagged BUY NOW early enough that a buy-zone entry reached 2×.
- **Precision:** of the model's BUY NOW forecasts, the share that reached 2× from entry.

A challenger must improve identification without buying its recall with junk signals. Also compute, per run:

- 3×, 5×, and 10× identification (recall and precision per level)
- False-positive rate
- Missed-opportunity rate
- Calibration per target level (stated vs realized, per **trade-journal** bucketing) — 2× and 5× calibrate separately
- Expected return vs realized return, after simulated fees and slippage
- Maximum drawdown and time under water
- Upside capture: realized multiple vs max recorded multiple on winners

Do not promote on general returns at the expense of the primary metric. A challenger with higher simulated PnL and lower 2× identification fails.

## Splits and sample sizes

- Hold out an out-of-sample period the challenger never touched during development. Tuning on the holdout burns it: pick a new one.
- Use walk-forward evaluation: fit or tune on window N, score on window N+1, roll forward. Report only test-window results, aggregated.
- A comparison below minimum sample size is not evidence. Report it as "insufficient sample" and keep collecting; never round it up to a pass.

| Parameter | Default (editable — edit the table; it's plain Markdown) |
|---|---|
| Out-of-sample holdout | Most recent 25% of replayable history, minimum 14 days |
| Walk-forward window | 14 days train / 7 days test, rolled weekly |
| Min resolved forecasts, out-of-sample, per model | 100 |
| Min universe assets that achieved ≥2× in the test period | 30 |
| Min BUY NOW forecasts per model in the test period | 25 |
| Min resolved 5×+ forecasts before scoring 5× metrics | 15 |

## Promotion pipeline

A challenger advances through three stages in order. Failing any stage returns it to development.

**Stage 1 — Replay comparison.** Run challenger and production over the same replayable periods with identical fill modeling. The challenger must beat production on the primary metric out-of-sample, at or above minimum sample sizes.

**Stage 2 — Shadow mode.** The challenger runs live on the VPS beside production (resources per **vps-ops**): same live inputs, real-time decisions, paper fills through the pessimistic fill model above. Shadow forecasts are logged to **trade-journal** with their model version and a shadow flag. Shadow mode places no orders, consumes no capital, and sends no user alerts. Shadow must run for the minimum period and reach minimum samples, and the challenger must hold its edge on live data.

**Stage 3 — Switch.** Promote only when Stages 1 and 2 both pass. Log the promotion to **trade-journal**, retain the outgoing production model as the instant-rollback target, and notify the user via **alert-format** (informational; no approval required — the promoted model still faces every **approval-gate** and **risk-limits** check live). No promotion while the **risk-limits** emergency halt is active; shadow mode continues through a halt.

| Parameter | Default (editable — edit the table; it's plain Markdown) |
|---|---|
| Promotion margin, primary recall | ≥ +3 percentage points absolute vs production, out-of-sample |
| Precision floor | No more than 2 percentage points below production |
| False-positive rate | Not worse than production by more than 2 percentage points |
| Calibration gate | No material-miscalibration flag (per **trade-journal**) at 2× in the test period |
| Minimum shadow period | 14 days |
| Min shadow BUY NOW forecasts, resolved | 25 |
| Shadow edge requirement | Primary recall ≥ production's over the same shadow window; precision floor applies |
| Max concurrent shadow models | 2 |

## Auto-demotion

Promoted models are on probation forever. Monitor live production performance from **trade-journal** outcomes on a rolling window and demote automatically when the model degrades:

- Primary metric falls below its own shadow-period performance by more than the demotion margin.
- **trade-journal** flags material miscalibration at 2× on live forecasts.
- Live realized slippage or fill quality diverges from simulation enough to erase the promotion margin.

On demotion: revert to the retained prior model immediately, log the demotion and trigger to **trade-journal**, and alert the user via **alert-format**. The demoted model returns to the challenger pool; it must repass the full pipeline to be promoted again. Demotion is automatic and requires no approval. It never loosens any limit and never interrupts open-position management: exits keep running under the reverted model.

| Parameter | Default (editable — edit the table; it's plain Markdown) |
|---|---|
| Live monitoring window | Rolling 14 days, recomputed on every outcome resolution |
| Demotion margin, primary recall | > 5 percentage points below the model's shadow-period recall |
| Demotion margin, precision | > 5 percentage points below the model's shadow-period precision |
| Min resolved live forecasts before demotion can trigger | 20 |
| Consecutive-loss circuit breaker | 8 consecutive failed BUY NOW forecasts triggers immediate review alert regardless of sample minimum |

## Reporting

After every replay comparison, shadow-period close, promotion, and demotion, write a summary to **trade-journal** `events` and send a short SMS via **alert-format**: model versions, primary metric for both models, sample sizes, and the action taken. Keep it scannable. Detail on demand.
