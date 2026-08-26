---
name: trade-journal
description: Use when logging any forecast, order, fill, approval, alert, risk rejection, or state transition, when resolving a forecast's outcome after its 1–3 day window, when computing calibration, or when assembling a REPORT reply.
---

# Trade Journal

This skill defines the append-only record of everything the bot predicted and everything it did. Every other skill writes here: **short-horizon-research** logs forecasts, **execution** logs orders and fills, **approval-gate** logs approvals and inbound SMS, **risk-limits** logs rejections, **portfolio-state** logs state transitions. The journal answers three questions at any time: what did the bot believe, what did it do, and what actually happened.

The journal is history. Current state lives in **portfolio-state** and must always be reconstructible from this journal plus venue APIs.

## Append-only rules

- Rows are never edited and never deleted. No exceptions, including for the bot's own mistakes.
- Corrections are new rows. A correction row sets `supersedes` to the id of the row it corrects. Readers must use the latest non-superseded row.
- Write the intent before the action, the result after: an order's submission row must be committed before the order goes to the venue. **portfolio-state** cold-start recovery depends on this ordering.
- All timestamps are UTC with millisecond precision.
- All position sizes and notionals are recorded as percentages of total trading portfolio value at event time, alongside the absolute quantity and price. Never record a size as a bare dollar assumption about total capital.
- The journal is a dedicated SQLite database on the VPS, separate from the **portfolio-state** database. Open it in WAL mode. Create `BEFORE UPDATE` and `BEFORE DELETE` triggers on every journal table that `RAISE(ABORT)` — append-only is enforced by the database, not by convention.

## What gets logged

| Source | Entries |
|---|---|
| **short-horizon-research** | Every forecast (fields below), every action-state change, missed-opportunity and false-positive findings, the raw discovery-stage signal stream — including signals that never produced a forecast |
| **execution** | Order submissions, fills, partial fills, cancels, venue rejections, gate blocks, re-quotes, escalations, ticket downgrades to alert-only |
| **approval-gate** | Approval requests, every inbound SMS (raw text, sender, parse result), approvals, rejections, expiries, invalid-code and unregistered-sender attempts, whitelist adds and revokes |
| **risk-limits** | Every rejection: limit name, measured value, limit value; halt triggers; STOP and FLATTEN events |
| **portfolio-state** | Halt-mode transitions, reconciliation results and adjustments, freezes, recovery sequences, cash flows |
| **alert-format** | Every outbound alert: type, content, delivery status |
| **execution** (exit safety) | Every exit-safety check result, including checks that were never traded — **backtest-replay** uses them |
| **vps-ops** | Incidents, watchdog pauses and resumes |
| **backtest-replay** | Promotion and demotion summaries; shadow-mode forecasts, flagged as shadow |

### Forecast fields

Each forecast row must record exactly the **short-horizon-research** self-calibration list:

- Asset (canonical identifier per **approval-gate**)
- Timestamp
- Evidence state (structured snapshot of the signals the forecast used, plus reference quotes at decision time)
- Action (BUY NOW / COMING UP / HOLD / ADD / SELL NOW)
- Entry price (current price at forecast time; actual entry comes from fills)
- Buy zone
- 2× target
- Higher-multiple targets
- Predicted timing
- 2× probability
- 3× probability when stated
- 5× probability when stated
- 10× probability when stated
- Confidence
- Position size (percentage of total trading portfolio value)
- Actual outcome — recorded later as a linked `outcomes` row; the forecast row itself never changes

Record the evidence state completely enough that **backtest-replay** can re-run the decision from the row alone, without live data. Also record the model version that produced the forecast, so challenger-vs-production comparisons stay attributable.

## Schema sketch

| Table | Key columns |
|---|---|
| `forecasts` | forecast_id, ts, asset_id, action, evidence_state, entry_price, buy_zone_low, buy_zone_high, target_2x, higher_targets, predicted_timing, p_2x, p_3x, p_5x, p_10x, confidence, size_pct, model_version, shadow (flag), supersedes |
| `orders` | order_id, ts, forecast_id, ticket_id, client_order_id, venue, asset_id, side, order_type, limit_price, quoted_price, notional_pct, child_n, status, supersedes |
| `fills` | fill_id, ts, order_id, venue_fill_id, quantity, price, fees, slippage_vs_quote, partial (flag), supersedes |
| `approvals` | approval_id, ts, code, request_type, asset_id, sent_ts, expires_ts, resolved_ts, resolution (approved / rejected / expired / invalid), inbound_raw, sender, supersedes |
| `alerts` | alert_id, ts, direction (outbound / inbound), kind, asset_id, body, delivery_status, parse_result, supersedes |
| `outcomes` | outcome_id, ts, forecast_id, window_close_ts, resolution_basis (filled / unfilled), max_multiple, hit_2x, hit_3x, hit_5x, hit_10x, time_to_2x, exit_result, realized_multiple, realized_pnl_pct, entry_slippage, exit_slippage, supersedes |
| `events` | event_id, ts, kind (state_transition / risk_rejection / gate_block / recon / security / missed_opportunity / false_positive / ops), ref_id, detail, supersedes |
| `exit_checks` | check_id, ts, asset_id, contract_address, result (PASS / FAIL), fail_reason, measured_values, supersedes |
| `discovery_inputs` | input_id, ts, asset_id, source, payload, supersedes |

Every foreign reference (`forecast_id`, `order_id`, `ticket_id`) must be recorded at write time. An orphan row that cannot be joined back to its forecast is a journaling bug; log it as an `events` row and fix the writer.

## Outcome resolution

Resolve every forecast when its window closes. The window is the forecast's predicted timing, capped at the maximum below. Resolve forecasts that never became positions too — unfilled BUY NOW, expired approvals, COMING UP that never triggered, and alert-only tickets all get outcome rows. These rows are the raw material for missed-opportunity and false-positive analysis.

Record in the `outcomes` row:

- **Max multiple reached:** highest price in the window divided by the entry basis. For filled positions, the basis is the actual average entry fill. For unfilled forecasts, the basis is the buy-zone midpoint.
- **Target hits:** whether price reached each stated level — 2×, 3×, 5×, 10× — and the time to 2× when hit.
- **Exit result:** realized multiple and realized PnL as a percentage of the position, or `no position` for unfilled forecasts.
- **Slippage vs plan:** actual entry fills vs buy zone, and actual exit fills vs the exit basis on the sell ticket, from `fills`.

| Parameter | Default (editable — edit the table; it's plain Markdown) |
|---|---|
| Maximum resolution window | 72 h from forecast timestamp |
| Price source for max multiple | Venue trade prints; DEX pool price for on-chain tokens |
| Resolution check interval | Hourly sweep for closable windows |

If price data for the window is unavailable (delisting, pool drained, venue outage), record the outcome as unresolvable with the reason. Never fabricate or interpolate a max multiple.

## Calibration

Calibration measures whether stated probabilities match reality. Compute it from `forecasts` joined to `outcomes`.

- Bucket forecasts by stated probability, per target level. Calibrate each level separately: 2×, 3×, 5×, and 10× each get their own buckets. A model can be well calibrated at 2× and poorly calibrated at 5×.
- For each bucket, compare the stated probability against the realized hit frequency.
- A stated 70% that hits materially under 70% is miscalibration. So is a stated 70% that hits materially over — it means the model is leaving qualified opportunities undersized or unreported.
- On a miscalibration flag, write an `events` row and surface the bucket to **short-horizon-research**, which owns the forecasting adjustment. This skill measures; it never adjusts forecasts itself.

| Parameter | Default (editable — edit the table; it's plain Markdown) |
|---|---|
| Bucket width | 10 percentage points |
| Minimum forecasts per bucket before flagging | 20 |
| Material miscalibration | Realized frequency outside the stated bucket by more than 15 percentage points |
| Recomputation | After every outcome resolution; full sweep daily |

Also compute calibration conditioned on confidence: high-confidence miscalibration is a stronger signal than low-confidence miscalibration and is reported first.

## Feeds to other skills

- **short-horizon-research** false-positive analysis reads high-probability forecasts whose outcomes failed, split by level: failed to reach 2×, reached 2× but not higher, and misclassified 5×+ candidates.
- **short-horizon-research** missed-opportunity analysis writes its findings back as `events` rows (`missed_opportunity`), including which signals existed and which were missed, so discovery improvements are auditable.
- **backtest-replay** replays recorded inputs: `forecasts.evidence_state` plus the quotes captured at decision time let a challenger model re-decide historical situations exactly as the production model saw them. Journal completeness is what makes replay honest — a decision input that was not journaled cannot be replayed.
- **portfolio-state** replays journal entries newer than its last applied entry during cold-start recovery.

## On-demand reports

The user can request reports by SMS. Command parsing and sender verification live in **approval-gate**; the `REPORT` family extends its command set. Reply formats live in **alert-format**. This skill assembles the content.

| Command | Content |
|---|---|
| `STATUS` | Answered from **portfolio-state**; the journal contributes nothing beyond pending-approval context |
| `REPORT` | Last 24 h: forecasts made, positions entered and exited, realized PnL %, hit rate at 2×, open outcome windows, one-line calibration summary (average stated vs realized P(2×)) |
| `REPORT WEEK` | Trailing 7 days: same fields plus max multiple distribution and slippage vs plan summary |
| `REPORT CAL` | Current calibration table per target level: bucket, stated vs realized, sample count, flags |

Reports are read-only. A report request must never trigger a trade, a resolution, or a schema change.

## Backup and retention

- The journal database is backed up off-VPS daily. Backup mechanics, encryption, and restore testing live in **vps-ops**.
- Retention is indefinite. Never prune journal rows; the calibration and replay loops need full history.
- A restored backup is a copy of history, not a new history. After a restore, **portfolio-state** must reconcile against venues before trading resumes.
