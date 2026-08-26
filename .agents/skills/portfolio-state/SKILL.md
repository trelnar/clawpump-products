---
name: portfolio-state
description: Use when reading or writing positions, cash, portfolio value, whitelist, halt, or pending-approval state, when reconciling against venue balances, when computing the rolling 24-hour value series, or when rebuilding state after a restart.
---

# Portfolio State

This skill defines the single source of truth for what the bot owns, what it is worth, and what mode it is in. Every other skill reads from here: **risk-limits** reads the value series and halt state, **execution** reads and updates positions and cash, **approval-gate** reads and updates the whitelist and pending approvals, **short-horizon-research** reads positions for sizing and concentration.

Rules:

- State covers dedicated sub-accounts and wallets only. Main balances must never appear in state, valuation, or limit calculations.
- Current state lives here. History lives in **trade-journal**. State must always be reconstructible from **trade-journal** plus venue APIs.
- Every state transition (position open/close, whitelist change, halt change, freeze, reconciliation result) logs to **trade-journal**.
- All capital limits are percentages of total trading portfolio value. Never store or assume fixed dollar capital.

## What it tracks

| State | Contents |
|---|---|
| Cash | Per venue, per currency: available balance, reserved (open-order) balance |
| Positions | Per asset per venue: quantity, average cost basis, per-lot entries with entry timestamp, price, fees |
| PnL | Realized PnL per closed lot; unrealized PnL per open position from current marks |
| Whitelist | Approved assets keyed by canonical identifier (**approval-gate**), approval and revocation timestamps |
| Halt state | Current buying mode: NORMAL, EMERGENCY_HALT (**risk-limits**), USER_STOP (**approval-gate**), RECON_FREEZE, SELL_ONLY (recovery) |
| Pending approvals | Active approval codes, request type, asset, expiry (**approval-gate**) |
| Marks | Latest price per held asset: value, source, timestamp |
| Value series | Rolling flow-adjusted total-value samples for the 20%/24 h halt (**risk-limits**) |

## Persistence

State persists in a local SQLite database on the VPS. It must survive process restarts and host reboots. Backup and disk monitoring live in **vps-ops**.

- Open the database in WAL mode.
- Wrap every fill application (cash change + lot change + journal reference) in a single transaction.
- Never cache state across a restart. On start, read from disk, then run cold-start recovery (below).

### Data model

| Table | Key columns |
|---|---|
| `cash` | venue, currency, available, reserved, updated_at |
| `positions` | asset_id, venue, quantity, avg_cost, updated_at |
| `lots` | lot_id, asset_id, venue, quantity, price, fees, ticket_id, entry_ts |
| `realized_pnl` | asset_id, venue, closed_ts, proceeds, cost, fees, pnl, ticket_id |
| `whitelist` | asset_id, venue_or_chain, approved_ts, revoked_ts |
| `halt_state` | mode, reason, since_ts, cleared_ts, approval_ref |
| `pending_approvals` | code, request_type, asset_id, sent_ts, expires_ts, status |
| `marks` | asset_id, price, source, mark_ts, stale (derived) |
| `value_series` | sample_ts, total_value, flow_adjusted_value |
| `cash_flows` | flow_ts, venue, type (deposit/withdrawal), currency, amount |
| `reconciliations` | recon_ts, venue, result, discrepancy_detail |

`asset_id` is the canonical identifier from **approval-gate**: exchange symbol plus venue for equities; contract address plus chain for tokens.

## Reconciliation

Internal state and venue-reported state must match. State drift is a critical fault: acting on wrong balances breaks the 5% position limit and the 20% halt, so drift blocks buying until resolved.

Poll every automatable venue (Coinbase Advanced Trade, approved equities APIs, and on-chain wallet balances, per the **execution** routing table) and compare against internal cash and positions:

| Trigger | Timing (editable — edit the table; it's plain Markdown) |
|---|---|
| Interval poll | Every 5 minutes |
| After every fill, partial fill, or cancel | Within 30 seconds of the event |
| After any unconfirmable order state (**execution** idempotency path) | Immediately |
| On cold start | Before any trading resumes |
| Unexplained-freeze re-alert | Every 30 minutes |

Discrepancy tolerances (editable). A difference within tolerance auto-adjusts internal state to the venue value and logs the adjustment. A difference beyond tolerance is a discrepancy.

| Quantity type | Tolerance |
|---|---|
| Equity share count | 0 — exact match required |
| Crypto position quantity | 0.05% of position, or below venue minimum order size |
| Cash per currency | 0.1% of that venue's cash balance |

On any discrepancy beyond tolerance:

1. Set halt mode `RECON_FREEZE`. All automatic buying stops immediately. Selling and exit management continue.
2. Send an SMS ops alert per **alert-format**: venue, asset or currency, internal value, venue value, and last matching journal entry.
3. Attempt to explain the difference from **trade-journal** and venue history: an unrecorded fill, a landed on-chain transaction, a fee, a deposit or withdrawal. If fully explained, adopt the venue value, record the explanation in `reconciliations`, log to **trade-journal**, and clear the freeze.
4. If unexplained, stay frozen. Re-alert at the interval in the trigger table above. Clearing an unexplained freeze requires explicit user approval via **approval-gate**.

The venue is authoritative for balances. **trade-journal** is authoritative for why balances changed. Never edit internal state to match the venue without recording the cause.

`RECON_FREEZE` is independent of the emergency halt and `STOP`. Clearing one mode never clears another.

## Valuation

Mark-to-market every held asset:

- Crypto and tokens: current best bid on the position's venue. For DEX tokens, the executable sell price for the position size at current pool depth, not the spot mid.
- Equities during regular hours: current best bid.
- Equities outside trading sessions: last session close.

Use the bid side, not mid or ask. Portfolio value feeds risk checks; optimistic marks weaken them.

### Staleness

A mark older than its staleness threshold is stale. Thresholds are editable — edit the table; it's plain Markdown.

| Asset class | Staleness threshold | Stale valuation |
|---|---|---|
| Crypto, exchange-listed | 2 minutes | Last known mark minus 10% haircut |
| DEX / new tokens | 1 minute | Last known mark minus 25% haircut |
| Equities (regular hours) | 5 minutes | Last known mark minus 5% haircut |
| Equities (outside sessions) | Last close accepted | No haircut |

| Parameter | Default (editable) |
|---|---|
| Stale-positions share of total value that blocks the denominator | 10% |
| Consecutive mark-refresh failures before an ops alert | 3 |

Stale-mark rules:

- Value stale positions conservatively with the haircut above. Never carry a stale mark at face value into total portfolio value.
- Block any buy whose sizing, 5% limit check, or buy-zone check depends on a stale mark, including the total-portfolio-value denominator when stale positions exceed the share in the table above. Refresh the mark, then re-run the **execution** gate sequence.
- Never block a sell on staleness. Sells re-quote at submission per **execution**.
- If a price source fails repeatedly (the consecutive-failure count in the table above), send an ops alert per **alert-format**.

## Rolling 24-hour value series

**risk-limits** reads this series to enforce the emergency halt: total portfolio value down 20% within a rolling 24-hour window stops all automatic buying.

| Parameter | Default (editable) |
|---|---|
| Sampling interval | 1 minute |
| Full-resolution retention | 7 days |
| Downsampled retention (hourly, for **backtest-replay**) | 180 days |

Each sample records total portfolio value: all cash plus all positions at current conservative marks, converted to the base currency.

### Deposit and withdrawal adjustment

The halt must measure trading losses, not cash movements. Record every external deposit and withdrawal in `cash_flows` when reconciliation detects it. Each sample in the rolling value series carries a flow adjustment (`flow_adjusted_value`): from the point a deposit occurs, its amount is subtracted from every later sample; from the point a withdrawal occurs, its amount is added back. Every sample in the window is therefore directly comparable. **risk-limits** evaluates its halt as the flow-adjusted current value against the maximum flow-adjusted sample in the trailing 24 hours.

A withdrawal must never look like a crash. A deposit must never mask one. The halt formula in **risk-limits** is the single authoritative halt definition; this skill owns only the corrected series.

If a sample cannot be computed (venue outage, all marks stale), record the gap. Never interpolate a missing sample as a real value; **risk-limits** treats gaps per its own rules.

## Cold start and crash recovery

On every process start — clean or after a crash — the bot must not trade until state is rebuilt and verified:

1. Enter `SELL_ONLY` mode: no automatic buys; sells and exit management for known positions allowed once step 3 passes for the relevant venue.
2. Rebuild: load the SQLite state, then replay any **trade-journal** entries newer than the last applied entry (fills, cancels, on-chain transactions that landed during downtime).
3. Reconcile every venue against the rebuilt state, including open orders: adopt or cancel per the **execution** idempotency rules, and check for on-chain transactions that landed while the bot was down.
4. Recompute all marks and one fresh value series sample. Verify the series has no unexplained gap larger than the sampling interval; log any gap to **trade-journal**.
5. Restore the prior halt state exactly. A crash must never clear `EMERGENCY_HALT`, `USER_STOP`, or `RECON_FREEZE`. Expire any pending approval codes whose expiry passed during downtime.
6. Exit `SELL_ONLY` only when every venue reconciles clean. If any venue is unreachable, that venue stays `SELL_ONLY` (alert-only for buys) and an ops alert goes out per **alert-format**; other venues can resume independently.
7. Log the full recovery sequence and mode transitions to **trade-journal**.

If steps 2–3 disagree beyond tolerance, this is a discrepancy: follow the reconciliation procedure. Downtime detection and process supervision live in **vps-ops**.

## Read interface for other skills

- `STATUS` replies (**approval-gate**) come from this skill: total value, 24 h change (flow-adjusted), open positions with unrealized PnL, whitelist count, pending approvals, current halt mode.
- **execution** gate 3 and **risk-limits** must read position size and total value from the same snapshot — never mix values from different samples in one limit check.
- **short-horizon-research** reads positions, cost basis, and entry timestamps for position management, concentration, and re-entry tracking.
- All writes go through this skill. No other skill updates positions, cash, whitelist, halt state, or the value series directly.
