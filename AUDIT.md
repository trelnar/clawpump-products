# Audit Inventory — 2026-09-01

106 gaps found across six lenses (spec-drift, correctness, ops, security, strategy-data, money-path),
each independently verified by a second agent instructed to refute it. Ordered by severity.
See HANDOFF.md for the prioritized narrative; this file is the complete record.

## 1. [CRITICAL] Solana position quantity is stored in raw token units, corrupting every percentage limit
*Lens: spec-drift*

**What:** execute_buy books the Solana fill as qty = int(q['outAmount']) - Jupiter's raw integer output (10^decimals per whole token, typically 1e6 or 1e9). Marks come from DexScreener priceUsd, which is USD per whole token. state.total_value multiplies the two, so one $5 Solana fill reports a position worth $5,000,000 (6-decimal token) and inflates total portfolio value by the same amount. Everything keyed off total_value then breaks at once: the 5% cap, aggregate-deployed, per-chain, and fat-finger checks all go slack; the 24h trailing peak is set to a fabricated number; STATUS shows nonsense PnL. When that position is later closed, value collapses ~100% against the fake peak and fires an immediate EMERGENCY_HALT. The Base path stores whole-token units (qty = notional / ref_price), so the two chains are also inconsistent with each other.

**Where:** bot/tradebot/execution.py:81 (qty = int(q["outAmount"])) vs bot/tradebot/execution.py:87 (qty = notional / ref_price); bot/tradebot/state.py:157-164 (total_value multiplies qty by the USD mark)

**Fix:** Divide by decimals on the Solana path: read decimals from solana_dex.token_balance(mint) (or the Jupiter quote's token metadata) and store whole-token qty. Add a unit assertion in upsert_position and a startup sanity check that qty*mark is within an order of magnitude of cost_basis_usd.

## 2. [CRITICAL] The Telegram listener is an unsupervised daemon thread with unguarded dict access — the STOP/FLATTEN kill switch can die silently
*Lens: ops*

**What:** `core.main()` calls `poller.start()` once and never checks `poller.is_alive()`. Inside `Poller.run`, only the `_call('getUpdates')` is wrapped in try/except; the `for u in updates` loop and `self._dispatch(u)` are not. And `_dispatch` reads `cq['from']['id']` and `u['message']['from']['id']` outside any try — an update shape lacking `from` (a channel post, a service message, a migrated-chat notice) raises KeyError straight out of `run()` and kills the thread permanently. The core process stays alive and keeps trading with the user's only kill switch dead and no indication anywhere. vps-ops requires the opposite: restart the listener, and if it is still down after 5 minutes pause all automatic buying because 'the user's STOP path is broken.' Secondarily, `self.offset` is in-memory and resets to 0 on restart, so updates Telegram has not had confirmed get replayed and re-executed after a crash.

**Where:** bot/tradebot/core.py:64-65 (start, never checked); bot/tradebot/telegram.py:56-69 (dispatch outside try, `cq['from']['id']` / `u['message']['from']['id']` unguarded); .agents/skills/vps-ops/SKILL.md incident runbook row 'Inbound Telegram path down'

**Fix:** Wrap `_dispatch` body in try/except, use `.get()` chains for sender extraction, persist `offset` to the kv table, and add a per-loop `if not poller.is_alive(): restart it; if still dead 5 min → set_mode('SELL_ONLY') + ops alert`. Test by having the bot added to a channel or by sending it a sticker.

## 3. [CRITICAL] Buys are booked as filled without any fill or confirmation check on all three venues
*Lens: money-path*

**What:** No venue path verifies that the order actually happened. Coinbase: `limit_buy` submits a GTC limit at ask*1.0025 and returns immediately; execute_buy then decrements cash, writes a position at `qty = notional/limit`, logs a fill and alerts BOUGHT. If the limit never fills, the DB shows a position that does not exist and a resting order that is never queried or cancelled — `coinbase.order_status` and `coinbase.cancel` are defined and called from nowhere. Base: `evm_dex.confirm(h)` is called on the line after `send_raw_transaction`, when no receipt can exist yet; confirm returns "unknown" on TransactionNotFound and the guard only tests `== "failed"`, so essentially every Base buy is recorded as filled unconfirmed, with qty derived from an unverified reference price rather than the route's actual amountOut. Solana is the only path that waits, and it fails the other way: `_await_solana` returns "timeout" after 90s and raises, marking the ticket failed — but a Solana transaction that is unknown at 90s can still land, leaving tokens in the wallet with no position row and nothing that will ever sell them. This is the half-completed-swap case, and no code detects it.

**Where:** bot/tradebot/execution.py:69-72, :84-88, :108-117; bot/tradebot/exchanges/evm_dex.py:117-131; bot/tradebot/exchanges/coinbase.py:41-49, :74-86

**Fix:** For Coinbase, poll order_status by client_oid until filled/cancelled with a give-up timeout, cancel the remainder, and record qty and price from the venue's filled_size/average_filled_price. For Base, poll `wait_for_transaction_receipt` and parse the actual token delta (or call token_balance before/after) instead of `notional/ref_price`. For Solana, on timeout mark the ticket ambiguous and re-check the signature on later loops rather than treating it as a clean failure.

## 4. [HIGH] Approved buys skip four of the five execution gates
*Lens: spec-drift*

**What:** execution.py defines gates 1-4 (halt mode, stale data, risk.check_buy, exit-safety) in _gates_buy, and process_ticket runs them before requesting approval. But the YES path does not re-run them: core.on_approved_buy re-checks only price, ticket age, and buy zone, then calls execution.execute_buy(ticket, price) directly, and execute_buy contains no gate logic at all. Up to 30 minutes (APPROVAL_EXPIRY_SEC) can pass between the gate run and the fill. In that window the portfolio can enter EMERGENCY_HALT, cash can be spent by another ticket, the 5% cost-basis cap can be breached by a prior fill, or the token's exit-safety round-trip can fail (a rug drains the pool). All of it executes anyway. risk-limits states the 5% check is 'applied at the moment of order submission' and that 'no order path may skip the call'; execution lists approval as gate 5, not a replacement for 1-4. Since the whitelist is empty, every trade the bot has yet to make takes this path.

**Where:** bot/tradebot/core.py:42-56 (on_approved_buy -> execution.execute_buy), bot/tradebot/execution.py:63-105 (execute_buy has no gate calls), bot/tradebot/execution.py:9-39 (_gates_buy only reachable from process_ticket)

**Fix:** Refactor execute_buy to call _gates_buy(ticket, value, fresh) itself, and have process_ticket and on_approved_buy both go through it. on_approved_buy must fetch a fresh (value, fresh) from monitor.portfolio_tick() rather than passing nothing.

## 5. [HIGH] Positions and cash are booked from assumptions, never from venue confirmation
*Lens: spec-drift*

**What:** Three of the four order paths write state before the venue has confirmed anything. (1) Coinbase: limit_buy submits a GTC limit order and returns; the SDK response is discarded and never checked for success (the coinbase SDK returns {success: false, error_response: ...} rather than raising), then execute_buy immediately decrements cash, inserts a position at qty = notional/limit, and writes a fills row. A rejected order (min-size, base-increment precision on a $5 order) or an order that simply never fills produces a phantom position the bot will later try to sell. (2) Base buy: evm_dex.confirm(h) is called in the same breath as send_raw_transaction, so the receipt does not exist yet and it returns 'unknown', which is not 'failed', so the buy is booked as filled. (3) Solana sell: the _await_solana(sig) return value is discarded, and proceeds are taken from the pre-trade quote. A failed or timed-out sell still closes the position and credits phantom cash. Because reconcile_cash only pulls cash and never compares positions, this divergence is permanent and silent.

**Where:** bot/tradebot/execution.py:71-72 and bot/tradebot/exchanges/coinbase.py:47-49 (response unchecked); bot/tradebot/execution.py:85-88 (confirm immediately after send); bot/tradebot/execution.py:141-142 (_await_solana result discarded, proceeds from quote)

**Fix:** Poll coinbase.order_status(oid) (already written, never called) until terminal before writing state, and book actual filled_size/average_filled_price. Wait on the Base receipt with w3.eth.wait_for_transaction_receipt before booking. Check the _await_solana return on the sell path and read the actual post-swap USDC delta. Verify web3/hexbytes version behavior: on hexbytes>=1.0, .hex() drops the 0x prefix and get_transaction_receipt will not find the tx.

## 6. [HIGH] Sell proceeds are fabricated when the price feed is unavailable
*Lens: spec-drift*

**What:** execute_sell computes price = marketdata.price(asset_id) or 0. A DexScreener 429 or a Coinbase blip makes price 0, and for the Coinbase and Base paths proceeds = qty * price = 0. The position is deleted, $0 is credited to cash, and the fills row records a sell at price 0. Total portfolio value drops by the full position value on the next tick, which against the trailing-24h peak is a bookkeeping-only drawdown that can trip the 20% EMERGENCY_HALT. The alert sent to the user reads 'SELL NOW <asset> @ 0'. This is most likely exactly when it will happen: the invalidation-cross sell that follows a price crash is the same moment the feed is under load.

**Where:** bot/tradebot/execution.py:127 (price = marketdata.price(asset_id) or 0), bot/tradebot/execution.py:133/149 (proceeds = qty * price), bot/tradebot/execution.py:157-158 (cash credited)

**Fix:** Never book proceeds from a mark. Read the actual fill (Coinbase order_status average_filled_price x filled_size; on-chain, the realized USDC balance delta after confirmation). If proceeds cannot be determined, write an events row, leave cash untouched, and raise a RECON_FREEZE rather than crediting zero.

## 7. [HIGH] There is no take-profit path and no way for research to say SELL
*Lens: spec-drift*

**What:** The forecast JSON schema's action enum is BUY_NOW / COMING_UP / PASS only. SELL_NOW, HOLD, and ADD - all four of which short-horizon-research defines as action states - cannot be produced, so core.py's SELL_NOW ticket branch is unreachable dead code. position-monitor's 'standing profit plan (e.g. scale out half at 2x)' has no implementation either: positions.plan is always written as json.dumps({}) and is never read anywhere. That leaves exactly three exit paths in the whole system: invalidation-price cross, pool liquidity down >=50%, and a manual FLATTEN. A position that goes to 3x is held until price falls all the way back through its invalidation level. The strategy's entire premise is capturing a 2x within 1-3 days, and the code has no mechanism to capture one.

**Where:** bot/tradebot/agent/prompts.py:64-65 (enum), bot/tradebot/core.py:86-88 (unreachable SELL_NOW branch), bot/tradebot/state.py:104 (plan written as {}), bot/tradebot/monitor.py:8-32 (only invalidation and liquidity exits)

**Fix:** Decide the intended design and implement one: either add SELL_NOW/HOLD/ADD to the schema and feed the agent per-position context (cost basis, entry ts, current mark, unrealized PnL - today it only receives asset_id strings), or implement a mechanical take-profit ladder in monitor.check_positions reading a populated plan column. Also add a time-based exit for the 1-3 day window; nothing currently closes a stale position.

## 8. [HIGH] STOP and the emergency halt do not cancel open buy orders, and the confirmation says they do
*Lens: spec-drift*

**What:** risk-limits requires both STOP and the emergency halt to 'block all automatic buys immediately ... Cancel open buy orders.' coinbase.cancel() exists but is never called from anywhere in the codebase, and there is no order-tracking table to cancel from. Since buys are placed as GTC limit orders that can rest indefinitely, a resting buy can still fill after the user has stopped the bot. The STOP reply text asserts the opposite: 'STOP acknowledged. Buying halted, open buy orders cancelled, selling continues.' The user is told a safety action happened that did not.

**Where:** bot/tradebot/approval.py:57-60 (message text), bot/tradebot/exchanges/coinbase.py:85-86 (cancel defined, zero callers), bot/tradebot/exchanges/coinbase.py:47 (limit_order_gtc_buy)

**Fix:** Track live order ids in a table, and on STOP / EMERGENCY_HALT / FLATTEN iterate them calling coinbase.cancel plus order_status confirmation. Until that exists, correct the STOP message so it does not claim cancellation.

## 9. [HIGH] Cold-start recovery is absent, and SELL_ONLY is an unrecoverable dead end
*Lens: spec-drift*

**What:** portfolio-state and vps-ops both require every process start - clean, crash, or watchdog kill - to enter SELL_ONLY, rebuild from the journal, reconcile every venue including open orders, and only then resume. state.init() sets SELL_ONLY only when mode is NULL, i.e. once, on a virgin database. A restart while mode is NORMAL resumes buying instantly with no reconciliation, no journal replay, and no detection of an on-chain swap that landed during downtime. Worse, if the bot ever is in SELL_ONLY there is no way out: RESUME only accepts USER_STOP and EMERGENCY_HALT, so it replies 'Nothing to resume (mode SELL_ONLY)', and the agent loop skips research entirely in SELL_ONLY, so no tickets are generated either. Recovery requires hand-editing the kv table in SQLite. RECON_FREEZE has the identical trap and is additionally never set by any code path.

**Where:** bot/tradebot/state.py:36-41 (init), bot/tradebot/approval.py:61-65 (RESUME mode whitelist), bot/tradebot/agent/runner.py:127 (agent skips SELL_ONLY and RECON_FREEZE), grep: state.set_mode("RECON_FREEZE") has zero call sites

**Fix:** Make init() always enter SELL_ONLY, add a reconcile-then-promote step that exits SELL_ONLY only on a clean venue comparison, and add SELL_ONLY and RECON_FREEZE to the RESUME-eligible modes as a manual override. Confirm the live DB's current mode value before touching anything.

## 10. [HIGH] Reconciliation compares cash only; position drift is undetectable and RECON_FREEZE is dead
*Lens: spec-drift*

**What:** monitor.reconcile_cash overwrites the cash table from venue balances every 5 minutes with no comparison, no tolerance check, and no discrepancy handling - a mismatch is silently adopted. Positions are never compared against venue or on-chain balances at all. portfolio-state specifies tolerances (0.05% crypto qty, 0.1% cash), a RECON_FREEZE on any beyond-tolerance break, an explanation attempt from journal history, and 30-minute re-alerts; none of that exists, and RECON_FREEZE is never set anywhere in the codebase. The tables the spec names - lots, realized_pnl, marks, cash_flows, reconciliations, halt_state - do not exist in the schema. Consequence: given the fill-booking bugs above, internal state will diverge from reality on the very first trade and nothing will ever notice or stop trading.

**Where:** bot/tradebot/monitor.py:38-48 (blind overwrite), bot/tradebot/state.py:11-33 (schema has no lots/marks/cash_flows/reconciliations/realized_pnl)

**Fix:** Add a reconcile_positions() that fetches on-chain token balances and Coinbase holdings, compares against the positions table at spec tolerance, and sets RECON_FREEZE plus an ops alert on a break. Log every adjustment to a reconciliations table. Do this before the first live fill, not after.

## 11. [HIGH] The Telegram bot token is written into the database on any transport failure
*Lens: spec-drift*

**What:** telegram._call builds the URL as https://api.telegram.org/bot<TOKEN>/<method>. Both the send path and the poll path catch the exception and journal str(e). requests exceptions (ConnectionError, ReadTimeout, HTTPError, SSLError) embed the full request URL in their message, so a single network blip persists the live bot token as plaintext in the events table - which is also the thing vps-ops mandates be backed up off-VPS. vps-ops: 'Never write secrets, API keys, or approval codes to logs.' Anyone with the DB file, or a future backup, gets control of the approval channel: the ability to send fake approval requests and read every portfolio message.

**Where:** bot/tradebot/telegram.py:11 (API template), bot/tradebot/telegram.py:33-34 (journal.log_event("telegram_send_fail", detail=str(e))), bot/tradebot/telegram.py:52-53 (same on the poll loop)

**Fix:** Scrub before logging: detail=str(e).replace(config.TELEGRAM_TOKEN, "<redacted>"), or log only type(e).__name__ plus a status code. Then grep the live DB (SELECT detail FROM events WHERE kind LIKE 'telegram%') and rotate the token via BotFather if it appears.

## 12. [HIGH] Two threads place orders with no mutex, and blocking venue calls freeze the STOP path and the fast exit path
*Lens: spec-drift*

**What:** The Telegram poller is a separate thread whose handler executes real orders: an approved buy (on_approved_buy -> execute_buy) and FLATTEN (on_flatten -> a sell of every position) both run on the poller thread, while monitor.check_positions fires invalidation sells on the main loop thread. Nothing serializes them, so the same position can be read and mutated concurrently - a FLATTEN racing an invalidation sell submits two exits for one balance and applies the cost-basis delta twice. Second, the calls block for a long time: _await_solana polls for up to 90 s and the EVM path waits on an approve receipt (up to 90 s) plus the swap. While an approved buy is running on the poller thread, the user's STOP is not even read - approval-gate requires STOP to be instant and friction-free. While a buy runs on the main thread, position-monitor's fast path is stalled, contradicting 'the fast path stays armed at all times'.

**Where:** bot/tradebot/core.py:62-66 (poller wired to execute callbacks), bot/tradebot/approval.py:93/99/129 (order execution on the poller thread), bot/tradebot/execution.py:108-117 (_await_solana 90 s), bot/tradebot/exchanges/evm_dex.py:107-108 (wait_for_transaction_receipt timeout=90), bot/tradebot/monitor.py:19 (sells on the main thread)

**Fix:** Add a single threading.RLock around all of execute_buy / execute_sell / flatten_all. Move order execution off the poller thread onto a work queue the main loop drains, so command parsing (especially STOP) never blocks. Handle STOP and FLATTEN-confirm inline before any queued work.

## 13. [HIGH] Gas floors are never checked at runtime, so an exit can fail for want of $0.50 of gas
*Lens: spec-drift*

**What:** capital-allocation requires each chain to hold gas for a configurable number of exits (default 20), blocks new buys on a chain below its floor, and treats running out of gas while holding positions as an incident with an ops alert and exit-impaired handling. config.GAS_EXITS_FLOOR and config.CHAIN_GAS_TOKEN are defined and never referenced. solana_dex.sol_balance and evm_dex.eth_balance are only ever called by the one-shot phase0.py script. Nothing in the core loop reads native balances. With roughly $15 of gas total across two chains, and the Base path spending gas on a redundant approve transaction plus the swap for every single buy, a depleted gas float will surface as a failed SELL producing only an ops alert - the position is then stuck.

**Where:** grep: GAS_EXITS_FLOOR and CHAIN_GAS_TOKEN have zero uses outside bot/tradebot/config.py:61,78; bot/tradebot/exchanges/evm_dex.py:101-109 (approve sent on every swap); bot/tradebot/execution.py:152-155 (sell failure is alert-only)

**Fix:** Add a per-chain gas check to _gates_buy (reject with a top-up suggestion below the floor) and to monitor.check_positions (ops alert when a chain holding positions drops under N exits' worth). Cache the ERC-20 allowance so the Base approve is only sent when the existing allowance is insufficient.

## 14. [HIGH] Position monitoring bypasses the price cache and will rate-limit itself out of monitoring
*Lens: spec-drift*

**What:** monitor.check_positions runs every 5 s and, per token position, makes two uncached DexScreener calls: marketdata.price() (which always performs a live fetch - the cache is only read via cached_price, used by marks()) and marketdata.dexscreener_token() for the liquidity check. That is 24 requests/minute per token position, plus discovery's ~15-30 calls per cycle. Five positions is ~120/min, ten is ~240/min, against DexScreener's ~300/min. On a 429 the call returns None and the code logs 'monitor_blind' and does continue - no ops alert, no exit evaluation, position silently unwatched. market-data and position-monitor both require a held asset losing price coverage to raise an ops alert and be treated as deteriorating. Concurrently, marks() goes not-fresh, so risk.check_buy rejects every buy with stale_data. The failure mode is: the more positions you hold, the less they are monitored.

**Where:** bot/tradebot/monitor.py:12 and :24 (two live fetches per position per pass), bot/tradebot/marketdata.py:46-67 (price() never consults _price_cache; cached_price is only used by marks), bot/tradebot/monitor.py:14 (monitor_blind logged, no alert), bot/tradebot/config.py:63 (MONITOR_INTERVAL_TOKEN_SEC = 5)

**Fix:** Have check_positions call a cached accessor and reuse the single dexscreener_token payload for both price and liquidity (it already carries both). Add a consecutive-failure counter that raises an ops alert and queues an exit evaluation after 3 misses, per the market-data degradation ladder.

## 15. [HIGH] The approval message the user must act on contains no evidence
*Lens: spec-drift*

**What:** alert-format specifies the approval template (Type, Exch, Buy zone, 2x target, Higher, P(2x), P(5x), Conf, Window, What, Hype) and states that price, P(2x), and Conf may never be omitted. The code passes only Size, Zone, and Invalidation into alerts.approval_request, so the message is action + asset + price + those three fields. The probabilities, confidence, target, window, what-it-is and hype-driver - the entire basis for a human deciding yes or no - are computed by the model, stored in forecasts, and then dropped before the message is built. The approval gate is the one human checkpoint in this system, and it currently asks for a yes/no with no information attached. Related reply drift: WHY dumps 3500 characters of raw model JSON instead of the Bull/Bear/Invalidation format (and blows the 30-line cap); report_text accepts an arg and ignores it, so REPORT WEEK and REPORT CAL silently return the plain 24h report; and STATUS/REPORT/WHY are all emitted through alerts.ops so they arrive prefixed 'OPS:' as ops alerts rather than command replies.

**Where:** bot/tradebot/execution.py:56-59 (fields dict), bot/tradebot/alerts.py:42-49 (approval_request), bot/tradebot/core.py:35-39 (why_text raw JSON), bot/tradebot/core.py:26-32 (report_text ignores arg), bot/tradebot/approval.py:71-76 (replies routed through alerts.ops)

**Fix:** Pass the forecast row (or the full candidate dict already stored in evidence_state) through the ticket into approval_request and render the alert-format template. Add a plain command-reply sender that does not prefix 'OPS:', and implement REPORT WEEK / REPORT CAL or reject them explicitly.

## 16. [HIGH] Outcome resolution and calibration have no writer - the data needed for them is not being recorded either
*Lens: spec-drift*

**What:** This is not only that no forecast has resolved yet. The outcomes table is created and journal.log_outcome is defined, but nothing calls it: there is no window-close sweep, no max-multiple tracker, no hourly resolution job, no probability bucketing, no miscalibration events row, no REPORT CAL. Beyond the missing loop, the inputs it would need are not being captured: fills.fee_usd is always written as None so realized PnL ignores Coinbase fees and gas entirely (material at $5 orders); fills record the reference price rather than the actual fill price so slippage-vs-plan is unmeasurable; orders rows carry no forecast_id or ticket_id so they cannot be joined back to a forecast; forecasts store no model_version and size_usd is always None. trade-journal also requires a separate database from portfolio-state, append-only enforcement via BEFORE UPDATE/DELETE triggers, and supersedes columns - all three are absent, and state.py actively UPDATEs and DELETEs rows in the same file. Even after trades start, this history will not support calibration or replay.

**Where:** bot/tradebot/journal.py:42-47 and :132-134 (outcomes table + log_outcome, zero callers), bot/tradebot/execution.py:102-103 and :165-166 (fee_usd=None, price=ref_price), bot/tradebot/agent/runner.py:94-101 (size_usd None, no model_version), bot/tradebot/journal.py:14-63 (no triggers, no supersedes, shared DB)

**Fix:** Add the missing columns and triggers now, before more rows accumulate, then write the hourly resolution sweep (72h cap, buy-zone midpoint basis for unfilled forecasts) and the bucketed calibration query behind REPORT CAL. Capture real fees and fill prices as part of the fill-confirmation fix.

## 17. [HIGH] No go-live phase can ever advance - the request path does not exist
*Lens: spec-drift*

**What:** approval.Commands._yes handles a pending of kind 'phase' (increment kv.phase, confirm), but nothing anywhere creates a 'phase' pending approval - there is no request_phase_approval, and the only pending creators are request_buy_approval, request_resume_approval, and the FLATTEN confirm. None of the go-live phase-1 exit criteria (5 days, 5 completed buy-to-exit cycles including one per venue, fills within slippage cap, journal completeness, zero recon breaks, zero missed mechanical exits) is tracked or evaluated by any code. The bot is therefore pinned at $5 orders indefinitely, and the only way past it is hand-editing the kv table - which also bypasses every gate the phase ramp exists to enforce. The same is true in reverse: go-live requires an automatic one-step drop on any critical incident, which is also unimplemented.

**Where:** bot/tradebot/approval.py:100-103 (orphaned 'phase' branch), grep: no caller creates a pending with kind='phase'; bot/tradebot/state.py:74-79 (phase read from kv)

**Fix:** Implement request_phase_approval plus a criteria evaluator that counts completed round trips, recon breaks and missed exits from the journal, and have the core loop request the advance when they clear. Add the automatic phase drop on a critical incident. Document that the current phase value was set manually.

## 18. [HIGH] Approval codes use a hex alphabet the spec excludes, and REVOKE can never match a token
*Lens: spec-drift*

**What:** approval-gate: 'Format: 6 characters, uppercase letters and digits, excluding 0 O 1 I L.' new_code() returns secrets.token_hex(3).upper() - six hex characters, whose alphabet is 0-9A-F, i.e. it includes the exact 0 and 1 the spec excludes for typo safety on a typed fallback. Separately, REVOKE is structurally broken: the parser uppercases the entire message before splitting, then whitelist_revoke does an exact-match UPDATE ... WHERE asset_id=?. Asset ids are 'solana:<base58 mint>' and 'base:0x<hex>', both case-sensitive, so an uppercased argument can never match a row. The command then replies 'Revoked <asset>.' unconditionally, so the user is told a revocation succeeded when nothing changed - and the spec's 'No whitelist entry: <asset>' response does not exist.

**Where:** bot/tradebot/approval.py:12-13 (new_code), bot/tradebot/approval.py:48 (text.upper()), bot/tradebot/approval.py:68-70 (unconditional confirmation), bot/tradebot/state.py:148-153 (exact-match UPDATE)

**Fix:** Generate codes from the spec alphabet (ABCDEFGHJKMNPQRSTUVWXYZ23456789). Preserve the original-case argument for REVOKE and WHY (uppercase only the command verb), check rowcount, and reply 'No whitelist entry: <asset>' on a miss.

## 19. [HIGH] Rejected assets are re-proposed within 15 minutes; the pending-request cap is unenforced
*Lens: spec-drift*

**What:** approval-gate defines a 6-hour post-rejection suppression window and a cap of 5 concurrent pending requests (dropping the lowest-ranked candidate rather than exceeding it). Neither is implemented. _no resolves the pending and sets the ticket rejected, but records nothing that discovery consults, so the next 15-minute cycle can rediscover the same token and send a fresh approval request. The agent submits up to MAX_CANDIDATES_PER_CYCLE = 6 tickets per cycle, already one above the cap. Compounding this: the agent keeps researching and submitting during USER_STOP and EMERGENCY_HALT, and each ticket is then blocked at gate 1 and emits a forced, unthrottled 'NOT BOUGHT' ops alert - so a halted bot spams the user up to 6 times every 15 minutes, while risk-limits requires that no new approval requests go out while halted.

**Where:** bot/tradebot/approval.py:105-115 (_no records no suppression), bot/tradebot/agent/runner.py:12 (MAX_CANDIDATES_PER_CYCLE = 6), bot/tradebot/agent/runner.py:127 (agent runs during halts), bot/tradebot/execution.py:50 -> bot/tradebot/alerts.py:57-58 (force=True, unthrottled)

**Fix:** Add a rejected-asset suppression check (query approvals for a rejection inside 6h) in runner.submit, cap pending approvals at 5 before calling request_buy_approval, and make the agent skip ticket submission entirely when mode != NORMAL.

## 20. [HIGH] vps-ops guarantees that do not exist: watchdog, honest heartbeat, backups, key verification, inbound-down handling
*Lens: spec-drift*

**What:** Five specified host-level protections have no implementation. (1) WatchdogSec=60 with a per-cycle notify is mandated so a hung process is killed, not just a dead one; neither unit file has WatchdogSec or NotifyAccess, so a wedged loop is never restarted. (2) The heartbeat must ping 'only at the end of a completed, healthy main-loop cycle'; it fires at the top of the loop on a timer, before any work, inside a blanket try/except that swallows every error - so healthchecks reads green while every cycle throws. (3) Daily encrypted off-VPS backups of the journal are required; there is no backup script, no timer, no VACUUM INTO anywhere in the repo, and the single SQLite file on one VPS disk is the only record of everything. (4) The startup key-permission check (withdrawal disabled, scope, IP allowlist, quarantine on failure) does not exist. (5) The runbook rule 'inbound Telegram down 5 min -> pause all automatic buying, the user's STOP path is broken' does not exist - the poller retries silently forever with no alert, so the bot keeps buying with no kill switch reachable.

**Where:** systemd/tradebot-core.service and systemd/tradebot-agent.service (no WatchdogSec), bot/tradebot/core.py:72-75 and :89-90 (ping first, blanket except), grep: no 'backup'/'watchdog'/'VACUUM INTO' anywhere in scripts/ systemd/ bot/, bot/tradebot/telegram.py:47-54 (silent retry loop)

**Fix:** Move heartbeat.ping() to the end of the try block so it only fires on a clean cycle. Add WatchdogSec=60 plus sd_notify. Write a backup timer using VACUUM INTO + age encryption to off-box storage. Add a poller-failure counter that raises an ops alert and sets USER_STOP after 5 minutes of getUpdates failures.

## 21. [HIGH] Discovery universe is paid promotion plus an alphabetical slice, with no contract-address verification
*Lens: spec-drift*

**What:** Distinct from the known 'thin data' item: the shape of the universe is itself a problem. On-chain candidates come only from DexScreener token-boosts (paid placement - a token appears because someone bought promotion) and token-profiles/latest. signal-hygiene names unverifiable contract addresses as one of the suite's few hard stops and requires verification against canonical sources before trading any address harvested from promotional content; no verification step exists in the code, so an impersonation token with a real project's name is indistinguishable to this pipeline. On the CEX side, coinbase_movers fetches the product list and then iterates usd[:80] - the first 80 entries of an unsorted list of several hundred, effectively a fixed alphabetical slice, not the top movers - so the same handful of products are examined every cycle and everything after them is never seen. It also costs 80 sequential HTTP calls per 15-minute cycle.

**Where:** bot/tradebot/marketdata.py:163 and :171 (boosts + profiles), bot/tradebot/marketdata.py:190 (for pid in usd[:80]), grep: no address-verification code path anywhere

**Fix:** Rank the Coinbase product list (by 24h volume) before slicing, or page through it across cycles. Add at least a minimal address check (pair age, deployer history, symbol collision against known tokens) before a token can reach a BUY_NOW ticket, and record the result in exit_checks.

## 22. [HIGH] backtest-replay is unimplemented, so every decision-logic change goes straight to live money
*Lens: spec-drift*

**What:** No part of this skill exists: no replay harness, no shadow-mode runner, no challenger comparison, no promotion pipeline, no auto-demotion, no consecutive-loss circuit breaker. The forecasts.shadow column is created and never set to 1. vps-ops states that any change to discovery, scoring, sizing, or entry/exit rules must run in shadow mode before controlling real orders, and go-live says agent-layer decision changes go through shadow mode instead of the phase ramp. With no harness, both rules are unenforceable - editing the prompt, the JSON schema, or a threshold in config.py changes live trading behavior immediately with no evaluation gate. This also means there is currently no defined process for the handoff recipient to safely change the strategy.

**Where:** bot/tradebot/journal.py:21 (shadow column, never written), grep: no shadow/replay/challenger code anywhere in bot/

**Fix:** At minimum, add a shadow flag to the agent runner (an env var that logs forecasts with shadow=1 and submits no tickets) so a changed prompt can run beside production before promotion. The full replay harness depends on the journal completeness fixes above and should not be attempted before them.

## 23. [HIGH] deploy.sh contradicts the deploy procedure and loosens the secrets file
*Lens: spec-drift*

**What:** vps-ops requires deploys from tagged commits into versioned release directories with a current symlink, a deploy hold that pauses buying first, and one-command rollback. deploy.sh instead does git checkout -B onto a branch HEAD in place, in the live /opt/tradebot, while both services are running - replacing code and pip-installing under a running process - and then never restarts the services, so an update silently does not take effect until someone remembers to restart. There is no hold, no release history, and no rollback. It also weakens security: harden.sh correctly creates /etc/tradebot/secrets.env as 0600 root:root, and deploy.sh then chowns it root:bot and chmods 0640, making every secret readable by the service user. That is unnecessary - systemd reads EnvironmentFile as root before dropping privileges - so a compromise of the bot process now yields the Coinbase key, the Anthropic key, and the Telegram token directly.

**Where:** scripts/deploy.sh:11-15 (in-place branch checkout), scripts/deploy.sh:26-29 (chown root:bot, chmod 0640) vs scripts/harden.sh (chmod 0600 root:root), scripts/deploy.sh (no systemctl restart)

**Fix:** Restore secrets.env to 0600 root:root and verify the services still start. Change deploy.sh to stop services, install to a timestamped release dir, repoint a current symlink, and restart - or at minimum add the restart and a printed rollback command.

## 24. [HIGH] Solana buys book position qty in RAW token base units; every other venue books human units
*Lens: correctness*

**What:** execute_buy stores `qty = int(q["outAmount"])` for Solana — Jupiter's outAmount is an integer in the token's smallest unit (10^decimals). Coinbase books `qty = notional / limit` and Base books `qty = notional / ref_price`, both human units. state.total_value() then does `p["qty"] * marks[asset]` where the mark is a USD-per-whole-token price from DexScreener. A $5 buy of a 6-decimal token at $0.0001 stores qty=5e10 and values the position at $5,000,000. Consequences chain outward: (a) total_value explodes, so every percentage-based limit in risk.check_buy (5% position cap, 2.5% fat-finger, aggregate/venue/chain caps) becomes non-binding — the next order could be sized at $125k if phase ever advances past 1; (b) risk.check_halt samples the inflated value into value_series, so a routine 20% wiggle in that one token trips EMERGENCY_HALT; (c) core.status_text reports garbage to Telegram; (d) partial-sell residuals drift because pos['qty'] and the on-chain balance are in different units. This fires on the FIRST Solana buy, which is the most likely first trade the bot ever makes given research is DEX-fed.

**Where:** /home/user/clawpump-products/bot/tradebot/execution.py:80 (`qty = int(q["outAmount"])`) vs :72 and :87; consumed at /home/user/clawpump-products/bot/tradebot/state.py:163

**Fix:** Divide by token decimals at book time: fetch decimals via solana_dex.token_balance(mint) (it already returns dec) or from the Jupiter quote, and store `int(q['outAmount']) / 10**dec`. Add a unit assertion in upsert_position rejecting any position whose qty*mark exceeds ~10x its cost basis at open. Re-check the numbers by hand on a $5 paper buy before unpausing.

## 25. [HIGH] There is no working exit path — every mechanical sell trigger is dead or unreachable
*Lens: correctness*

**What:** Four supposed exits, all non-functional. (1) Liquidity-drain exit reads p['entry_liquidity_usd'], but execute_buy calls upsert_position without entry_liq, so the column is always NULL and the branch never runs. (2) Standing profit plans: the `plan` column is written as json.dumps({}) and never read; execute_sell's `fraction` argument is never called with anything but 1.0, so 'scale out half at 2x' does not exist. (3) Agent-driven exits: core.py:86 handles action == 'SELL_NOW', but the JSON schema enum is ["BUY_NOW","COMING_UP","PASS"] and submit() does `if c['action'] != 'BUY_NOW': continue` — the agent structurally cannot emit a sell. (4) The re-evaluation queue: monitor writes kv key `reeval:<asset>` and nothing anywhere reads it; the research payload gives the model only a bare list of held asset_ids — no entry price, cost basis, P&L, or invalidation — so it could not reason about an exit even if it had a verb for it. That leaves the invalidation-price cross as the sole exit, and it is itself conditional (see the invalidation=0 finding). Net: a position opened that does not cross a valid invalidation level is held forever with no take-profit, no trailing stop, and no time stop.

**Where:** /home/user/clawpump-products/bot/tradebot/execution.py:99 (no entry_liq); /home/user/clawpump-products/bot/tradebot/monitor.py:22,32; /home/user/clawpump-products/bot/tradebot/agent/prompts.py:65; /home/user/clawpump-products/bot/tradebot/agent/runner.py:102; spec at .agents/skills/position-monitor/SKILL.md 'Fast path'

**Fix:** Minimum viable fix before any trade: pass entry_liquidity_usd into upsert_position from the DexScreener info already fetched during gates; add a hard time-stop and a take-profit level in monitor.check_positions driven off cost_basis_usd/qty. Longer term, add SELL_NOW to the schema enum, stop filtering it out in submit(), and put position economics (entry, cost, current P&L, invalidation, age) into the research payload.

## 26. [HIGH] Coinbase order responses are discarded — a rejected or unfilled limit order is booked as a fill
*Lens: correctness*

**What:** limit_buy returns `(oid, r)` and execute_buy binds `oid, _ = ...`, throwing away the response. The coinbase-advanced-py SDK returns a 200 with `success: false` and an `error_response` body for rejected orders (bad base_size precision, bad price precision, insufficient funds, min-size) rather than raising, so no exception reaches the try/except. Even for an accepted order this is a GTC *limit* order at ask*1.0025 that may simply rest unfilled. Either way the code proceeds to debit cash, insert a position, mark the ticket 'filled', write a fills row, and send a 'BOUGHT' Telegram alert. Precision failure is close to guaranteed: base_size is formatted `:.8f` and limit_price `:.8g` with no read of the product's base_increment/quote_increment/base_min_size. The most likely outcome of the very first Coinbase trade is a phantom position plus, sometimes, a real resting order the bot has no record of and can never cancel — coinbase.cancel and coinbase.order_status are both defined and called from nowhere.

**Where:** /home/user/clawpump-products/bot/tradebot/execution.py:71; /home/user/clawpump-products/bot/tradebot/exchanges/coinbase.py:43,47-49,74,85

**Fix:** Check `r['success']` and raise on false with the error_response body. Fetch get_product() once per product and quantize base_size/limit_price to base_increment/quote_increment, rejecting below base_min_size. Then poll order_status(oid) until filled/cancelled and book the ACTUAL filled_size and average_filled_price rather than the assumed quantity; cancel the remainder at the 90s give-up timeout the execution skill specifies.

## 27. [HIGH] A DexScreener response without priceUsd yields price 0.0, which reads as an invalidation cross and liquidates the position
*Lens: correctness*

**What:** dexscreener_token coerces `float(best.get('priceUsd') or 0)`, so a pair record missing priceUsd returns 0.0, not None. price() guards the cache write with `if p:` but still `return p` — so price() returns the float 0.0. monitor.check_positions only skips on `price is None`; 0.0 falls through to `if inv and price <= inv`, true for every positive invalidation level, and immediately fires a full execute_sell. One malformed upstream response dumps the whole position at market. The same 0.0 propagates into execute_sell as `price = marketdata.price(...) or 0` → proceeds computed as 0 for Coinbase and Base, so cash is not credited and the Telegram alert reports a -100% loss on a trade that may have been profitable. It also reaches _gates_buy, which only checks `ref is None`, and produces a ZeroDivisionError on the Base buy path (qty = notional / ref_price).

**Where:** /home/user/clawpump-products/bot/tradebot/marketdata.py:35,55-57; /home/user/clawpump-products/bot/tradebot/monitor.py:12-19; /home/user/clawpump-products/bot/tradebot/execution.py:127,87

**Fix:** Make price() return None for any non-positive value (`return p if p and p > 0 else None`), and change monitor's guard to `if not price:`. Add an explicit `price <= 0` reject in _gates_buy. Require two consecutive ticks below the invalidation level before firing the sell, so a single bad response cannot liquidate.

## 28. [HIGH] A restart between sending the swap and marking the ticket re-executes the same buy
*Lens: correctness*

**What:** Ticket status stays 'new' from the moment the core loop picks it up until execute_buy's final set_ticket_status('filled') — after the on-chain tx is already broadcast and irreversible. The Solana path holds that window open for up to 90 seconds inside _await_solana; the Base path holds it through an approve receipt wait (90s) plus a swap. systemd is Restart=always/RestartSec=10, and the core loop re-reads state.tickets('new') every 60s. A crash, OOM, or `systemctl restart` in that window leaves a still-'new' ticket that is bought a second time on the next pass, well inside the 900s TICKET_MAX_AGE_SEC window. There is no offsetting reconciliation: reconcile_cash pulls USDC balances only, never token balances, so an unrecorded on-chain buy is invisible forever. The execution skill's idempotency contract is also unmet — client order IDs are random (`tb-{uuid4}`) rather than the specified deterministic `bot-{venue}-{asset}-{ticket-id}-{child-n}`, so the venue cannot dedupe either.

**Where:** /home/user/clawpump-products/bot/tradebot/execution.py:63-101 (status set only at :101); /home/user/clawpump-products/bot/tradebot/exchanges/coinbase.py:42; /home/user/clawpump-products/bot/tradebot/monitor.py:41-48; .agents/skills/execution/SKILL.md 'Idempotency'

**Fix:** Write status 'submitting' plus the client_oid/tx signature to the ticket row BEFORE the venue call, and on startup sweep any 'submitting' tickets: query coinbase.order_status / solana_dex.confirm / evm_dex.confirm by that id and adopt or fail rather than re-submitting. Switch client_oid to the deterministic form. Add token-balance reconciliation so an orphan on-chain position surfaces.

## 29. [HIGH] Base buys are booked before confirmation; confirm() always returns 'unknown' immediately after send
*Lens: correctness*

**What:** evm_dex.confirm() calls w3.eth.get_transaction_receipt(tx_hash) with no wait. Called microseconds after send_raw_transaction, web3 raises TransactionNotFound, the bare except swallows it, and it returns 'unknown'. execute_buy only aborts on the exact string 'failed', so 'unknown' is treated as success and the position is booked. A reverted or dropped Base swap therefore produces a phantom position with real cash decremented. The Solana path does this correctly via _await_solana's polling loop — the two chains are inconsistent. The sell path is worse: execute_sell's Base branch calls evm_dex.swap() and never calls confirm() at all, then credits `proceeds = qty * price` and deletes the position row; a failed sell tx leaves the bot believing it is flat while still holding the token. On the Solana sell branch _await_solana(sig) is called but its return value is discarded, so 'failed' or 'timeout' also books the sale.

**Where:** /home/user/clawpump-products/bot/tradebot/exchanges/evm_dex.py:125-131; /home/user/clawpump-products/bot/tradebot/execution.py:85-86, :141, :148-149

**Fix:** Give evm_dex a wait_for_confirm(h, timeout) built on w3.eth.wait_for_transaction_receipt, and treat anything other than explicit success as failure on the buy path. On both sell paths, check the confirmation result before touching cash or deleting the position; on a failed/timeout sell keep the position, raise an ops alert, and do not retry blind.

## 30. [HIGH] An approved buy bypasses the halt check, all risk limits, and exit-safety
*Lens: correctness*

**What:** process_ticket runs gates 1-4 then, for a non-whitelisted asset, hands off to approval. When YES arrives, core.on_approved_buy re-checks only three things — price is not None, ticket age, and the buy zone — then calls execution.execute_buy directly. It never re-checks state.get_mode(), never calls risk.check_buy (position cap, concurrent count, aggregate/venue/chain caps, fat-finger notional, venue cash), and never re-runs exit_safety. The approval window is up to 30 minutes (APPROVAL_EXPIRY_SEC=1800). So a YES answered after an EMERGENCY_HALT fired, after the cash was spent elsewhere, or after the token's exit liquidity collapsed executes anyway — contradicting the execution skill's 'gates apply immediately before submission'. Separately, risk.check_buy's fat-finger price check is a no-op on the primary path: _gates_buy passes `ref` as both ref_price and limit_price, so deviation is always exactly 0, while the limit actually submitted (ask*1.0025) is never checked against anything.

**Where:** /home/user/clawpump-products/bot/tradebot/core.py:42-56; /home/user/clawpump-products/bot/tradebot/execution.py:25-26,53-60; .agents/skills/execution/SKILL.md 'Pre-trade gate sequence'

**Fix:** Have on_approved_buy call _gates_buy(ticket, total_value, marks_fresh) rather than duplicating a subset of checks, and pass the real intended limit price into risk.check_buy so the fat-finger deviation test has something to compare.

## 31. [HIGH] Approved buys execute on the Telegram poller thread, blocking STOP and FLATTEN for 90+ seconds
*Lens: correctness*

**What:** telegram.Poller is a thread whose run loop calls self.handler(text) synchronously; the chain is Commands.handle → _yes → on_approved_buy → execute_buy. execute_buy blocks for the full swap lifecycle — up to 90s in _await_solana, or an approve-receipt wait plus a swap on Base. While that runs, no further getUpdates poll happens, so STOP, FLATTEN, and NO are unreachable during exactly the window when the user is most likely to want them. It also means buys mutate positions/cash concurrently with the core loop's monitor.check_positions and execute_sell, with no mutex between the two threads — close_position and upsert_position can interleave on the same asset.

**Where:** /home/user/clawpump-products/bot/tradebot/telegram.py:47-58,79-82; /home/user/clawpump-products/bot/tradebot/approval.py:91-93; /home/user/clawpump-products/bot/tradebot/core.py:56

**Fix:** Make _yes on a buy enqueue the approval (set the ticket status to 'approved') and let the core loop execute it on its own thread. Add a single execution mutex so a buy and a sell for the same asset cannot interleave.

## 32. [HIGH] check_positions makes 2 uncached HTTP calls per position per 5s tick, inside the core loop
*Lens: correctness*

**What:** monitor.check_positions calls marketdata.price(asset) — which goes straight to the network, deliberately bypassing cached_price() — and then, for tokens, calls marketdata.dexscreener_token(chain, addr) again for the same token, duplicating the request it just made. MONITOR_INTERVAL_TOKEN_SEC is 5 and MAX_CONCURRENT_POSITIONS is 10, so a full book generates ~240 DexScreener requests/minute on top of the agent's discovery sweep, which will hit rate limits (and rate-limited responses become price=None → monitor blind). Worse, these are synchronous with a 10s timeout inside the core loop: ten slow or hanging positions stall the loop for ~200 seconds, freezing portfolio_tick, halt evaluation, ticket pickup, and the heartbeat. This is the same failure class as the already-fixed 'core loop freeze from an untimed venue call' — that fix wrapped reconcile_cash in a thread with a 45s join but left monitor's market-data calls unguarded.

**Where:** /home/user/clawpump-products/bot/tradebot/monitor.py:12,24 vs the guarded pattern at :53-60; /home/user/clawpump-products/bot/tradebot/marketdata.py:63-67 (cached_price, no callers in monitor)

**Fix:** Reuse the single dexscreener_token result for both the price and the liquidity check. Route monitor reads through a short-TTL cache. Wrap the whole check_positions pass in the same daemon-thread + join(timeout) pattern already used for reconcile_cash, and add per-host request pacing like the one already added for GeckoTerminal.

## 33. [HIGH] Swaps broadcast through public RPCs, with no deadline and an uncapped priority fee
*Lens: correctness*

**What:** The execution skill sets a 'Public-mempool ceiling: 0% — always use private/MEV-protected RPC', a 60-second swap deadline, and a priority fee capped at 0.5% of swap notional. The code defaults SOLANA_RPC to api.mainnet-beta.solana.com and BASE_RPC to mainnet.base.org — both public. The Jupiter swap body sets `prioritizationFeeLamports: 'auto'` with no ceiling and no Jito bundle; the Kyber build call passes no deadline. On a $5 notional an 'auto' priority fee during congestion is a material fraction of the trade, and a public-mempool Base swap at 300bps tolerance on a thin token is a sandwich target. Slippage is also hardcoded 300bps buys / 600bps sells rather than derived from SLIPPAGE_TIERS, which is defined in config and read by nothing. Related: the 'gas-aware minimum position' gate compares ROUNDTRIP_COST_MAX against exit_safety's roundtrip_loss, which is computed purely from aggregator quotes and contains no gas or priority-fee component at all — so the rule that is supposed to enforce a gas-viable minimum size is measuring a gas-free number.

**Where:** /home/user/clawpump-products/bot/tradebot/config.py:33-34,52,62; /home/user/clawpump-products/bot/tradebot/exchanges/solana_dex.py:98-101,81; /home/user/clawpump-products/bot/tradebot/exchanges/evm_dex.py:93-95; /home/user/clawpump-products/bot/tradebot/execution.py:36-38

**Fix:** Point SOLANA_RPC at a Jito-enabled or private endpoint and BASE_RPC at a private/MEV-protected relay in secrets.env. Cap prioritizationFeeLamports at an absolute lamport value derived from notional. Pass a deadline to the Kyber build. Wire SLIPPAGE_TIERS to measured pool depth, and add estimated entry+exit gas into the roundtrip_loss figure before comparing it to ROUNDTRIP_COST_MAX.

## 34. [HIGH] A position can be opened with no invalidation price, and nothing detects it
*Lens: correctness*

**What:** The ticket's invalidation is `c.get('wave_invalidation') or c.get('invalidation_price')`. Both are plain numbers in the schema and wave_invalidation is documented as '0 when the count yields none'. If the model returns 0 for both — or invalidation_price is absent — the position row gets invalidation_price = 0 or NULL. monitor then evaluates `if inv and price <= inv`, and 0/NULL is falsy, so the position has no stop at all. Given the invalidation cross is the only surviving exit trigger, such a position is unmanaged for its entire life. Nothing validates the ticket before execution: neither submit(), process_ticket(), nor _gates_buy checks that invalidation_price is present and below the entry price, despite the execution skill's 'Reject any ticket missing these fields'. Note also that wave_invalidation takes PRECEDENCE over invalidation_price, inverting the wave-structure skill's own rule that structure is 'context and invalidation, never a signal on its own'.

**Where:** /home/user/clawpump-products/bot/tradebot/agent/runner.py:115-116; /home/user/clawpump-products/bot/tradebot/monitor.py:16-17; /home/user/clawpump-products/bot/tradebot/agent/prompts.py:85-86

**Fix:** Add a ticket-validation gate in process_ticket rejecting any ticket where invalidation_price is missing, <= 0, or >= the reference price. Reverse the precedence so invalidation_price is the base and wave_invalidation can only tighten it.

## 35. [HIGH] Positions are never reconciled, RECON_FREEZE is never set, and a rebuilt DB cannot be resumed
*Lens: correctness*

**What:** reconcile_cash pulls USDC balances for three venues and nothing else. Token balances, Coinbase base-currency holdings, and open orders are never compared against the positions table. RECON_FREEZE appears only in the MODES tuple — no code path sets it — and the `reconciliations` table the portfolio-state skill specifies does not exist in journal.SCHEMA. The restart procedure (rebuild from journal, reconcile every venue including open orders, exit SELL_ONLY only when clean) is not implemented; state.init() only sets SELL_ONLY when mode is NULL. Every phantom-position and orphan-position failure above is therefore permanent and silent. Related and separately checkable: the bot cold-starts in SELL_ONLY, but approval's RESUME branch only accepts modes ('USER_STOP','EMERGENCY_HALT') — SELL_ONLY is not in that tuple, so if the DB is ever lost or rebuilt (new VPS, restore, wiped /var/lib/tradebot) the bot comes up in SELL_ONLY with no in-band way to reach NORMAL; it needs a manual sqlite UPDATE.

**Where:** /home/user/clawpump-products/bot/tradebot/monitor.py:38-48; /home/user/clawpump-products/bot/tradebot/state.py:9,36-41; /home/user/clawpump-products/bot/tradebot/approval.py:62; .agents/skills/portfolio-state/SKILL.md:78-83,148-151

**Fix:** Add a reconcile_positions pass reading on-chain token balances and Coinbase accounts, comparing to the positions table, setting RECON_FREEZE plus an ops alert on any break beyond tolerance. Add 'SELL_ONLY' to the RESUME-eligible modes.

## 36. [HIGH] Coinbase calls are not scoped to the HypeBot portfolio, and USD and USDC balances are summed as if fungible
*Lens: correctness*

**What:** config.COINBASE_PORTFOLIO defaults to 'HypeBot' and is read by nothing. get_accounts(limit=250) passes no retail_portfolio_id, and limit_order_gtc_buy / market_order_sell pass no portfolio, so both the balance read and the orders land in whatever portfolio the CDP key resolves to by default rather than provably in HypeBot — the isolation the setup relies on is asserted in a doc, not enforced in code. Separately, usdc_balance() sums every account whose currency is 'USDC' OR 'USD' into one number risk.check_buy treats as spendable. Coinbase does not auto-convert between them, and the agent discovers products filtered on quote_currency in ('USD','USDC'), so a '-USD' product gets ticketed and passes the cash gate against a USDC-only balance, then is rejected by the venue. Note also that prices come from the Exchange/Pro API (api.exchange.coinbase.com) while orders go to Advanced Trade; the product universes are not identical, so a discovered product may not be tradeable on the order venue at all.

**Where:** /home/user/clawpump-products/bot/tradebot/config.py:20 (zero readers); /home/user/clawpump-products/bot/tradebot/exchanges/coinbase.py:25-30,47; /home/user/clawpump-products/bot/tradebot/marketdata.py:23,186-187

**Fix:** Resolve the HypeBot portfolio uuid once at startup and pass retail_portfolio_id on get_accounts and every order. Track USD and USDC as separate cash keys and match the ticket's quote currency. Validate each discovered product against the Advanced Trade product list before ticketing it.

## 37. [HIGH] Unbounded journal growth, deploys that never restart, unpinned dependencies, and an alert flood during halts
*Lens: correctness*

**What:** Four operational items a handoff needs. (1) journal.log_discovery writes up to 20KB per candidate per 15-minute cycle for ~40 DexScreener items plus up to 80 Coinbase products; value_series is the only table with a retention delete (8 days), and there is no VACUUM or WAL checkpoint policy, so the SQLite file on /var/lib/tradebot grows without bound. (2) scripts/deploy.sh claims in its header to halt buying during deploy — it contains no set_mode call — and never restarts either service, so a deploy leaves the previous code running until someone runs systemctl by hand. (3) bot/requirements.txt pins nothing (anthropic>=1.0, web3>=7.6, coinbase-advanced-py>=1.8) and deploy.sh reinstalls on every run, so an upstream breaking release changes behavior on the next deploy with no signal — live concern given the runner depends on the newer output_config/effort request shape. (4) alerts.not_bought is force=True so it bypasses the 900s throttle, and the agent keeps generating tickets while in EMERGENCY_HALT, producing up to 6 'NOT BOUGHT: halt' Telegram messages every 15 minutes for as long as the halt lasts. Minor: approval's command parser uppercases the whole message before extracting the argument, so `REVOKE solana:<mint>` calls whitelist_revoke on an uppercased, case-sensitive-mismatched asset_id — it matches nothing, changes nothing, and still replies 'Revoked'. There is also no code path that creates a 'phase' pending approval, so the phase-advance branch in _yes is unreachable and advancing past phase 1 requires a manual kv edit.

**Where:** /home/user/clawpump-products/bot/tradebot/journal.py:127-129 and state.py:172 (only retention rule); /home/user/clawpump-products/scripts/deploy.sh:5,38-44; /home/user/clawpump-products/bot/requirements.txt; /home/user/clawpump-products/bot/tradebot/alerts.py:58 with agent/runner.py:127; /home/user/clawpump-products/bot/tradebot/approval.py:48,69,100

**Fix:** Add a nightly prune of discovery_inputs and events older than ~30 days plus a disk check in the heartbeat path. Append `systemctl restart tradebot-core tradebot-agent` to deploy.sh and either implement or delete the halt-during-deploy claim. Generate a pip freeze lockfile and install from it. Gate the agent loop on mode == 'NORMAL'. Preserve original case for command arguments and add a phase-advance approval request.

## 38. [HIGH] No backup exists at all — and the wallet private keys are single-copy, on that one box
*Lens: ops*

**What:** vps-ops specifies a `tradebot-backup.timer` doing daily `VACUUM INTO` → age/GPG encrypt → off-VPS upload, with 90-day retention and quarterly restore tests. None of it exists: `scripts/` contains only harden/deploy/spend/phase0/gen_wallets, `deploy.sh` installs exactly two units (`tradebot-core.service`, `tradebot-agent.service`), and grep for backup/VACUUM/integrity_check across bot/, scripts/, systemd/ returns zero hits. Worse, `gen_wallets.py` mints raw keys (not BIP39 mnemonics) and prints only the public addresses — so there is no seed phrase the user could ever have written down. The only copies of the Solana and EVM private keys are /etc/tradebot/solana_wallet.json and /etc/tradebot/evm_wallet.key on this VPS. Vultr instance loss, a botched `ufw`/sshd change, or disk failure permanently destroys ~$605 of USDC plus gas, plus the entire journal (every forecast, order, discovery row — i.e. the whole calibration dataset).

**Where:** scripts/gen_wallets.py:28-42 (prints addresses only); scripts/deploy.sh:47-49 (installs 2 units, no backup timer); .agents/skills/vps-ops/SKILL.md 'Backups' section

**Fix:** Today, before anything else: `sudo cat /etc/tradebot/solana_wallet.json` and `/etc/tradebot/evm_wallet.key`, store both in the user's password manager, and verify by importing the EVM key into a watch-only wallet and matching the address. Then write scripts/backup.sh (sqlite3 `VACUUM INTO /tmp/snap.db`, `age -r <pubkey>`, upload to B2/S3, alert on 2 consecutive failures) plus tradebot-backup.timer, and add it to deploy.sh.

## 39. [HIGH] Cold-start recovery is not implemented — every restart resumes in NORMAL and buys within 60s
*Lens: ops*

**What:** portfolio-state specifies a 7-step cold start (SELL_ONLY → replay journal → reconcile every venue including open orders → recompute marks → restore halt state → exit SELL_ONLY only when all venues reconcile clean), and vps-ops says 'No flag, environment variable, or code path can skip it.' The actual implementation is `state.init()`: it sets SELL_ONLY only when `get_mode() is None`, i.e. on the first boot of an empty database. On every subsequent start — reboot, OOM kill, crash, `systemctl restart`, deploy — the persisted mode is read back as-is, so a bot that was NORMAL comes back NORMAL and starts processing tickets on the first 60s value tick. There is no position reconciliation anywhere: `monitor.reconcile_cash()` pulls only USDC balances into the `cash` table. Nothing in the codebase ever sets RECON_FREEZE (grep: the string appears once, in the MODES tuple), and the `reconciliations`, `lots`, `cash_flows` and `marks` tables the skill specifies do not exist in STATE_SCHEMA.

**Where:** bot/tradebot/state.py:36-41 (`init()`); bot/tradebot/monitor.py:38-48 (`reconcile_cash` — cash only); .agents/skills/portfolio-state/SKILL.md:142-154

**Fix:** Make `init()` unconditionally `set_mode('SELL_ONLY')` on every start, and add a `cold_start()` that fetches venue positions (Coinbase accounts, Solana `getTokenAccountsByOwner`, Base ERC20 balances), diffs against the `positions` table, sets RECON_FREEZE + ops alert on any break beyond tolerance, and only then sets NORMAL. Until that lands, treat every restart as requiring a manual STOP → eyeball STATUS vs the venues → RESUME.

## 40. [HIGH] State writes after a filled order sit outside the try — a fault there leaves a real position with no record and no detection
*Lens: ops*

**What:** In `execute_buy`, the try/except covers only the venue calls (lines 71-95). The four writes that make the fill real to the bot — `set_cash`, `upsert_position`, `set_ticket_status`, `log_fill` — run unprotected at lines 97-104. Any failure there (disk full, SQLite locked past the 5s default timeout by the agent process writing its 20 KB discovery payloads to the same file, a corrupt DB, SIGKILL from a reboot) means the order executed at the venue and the bot has no idea. Because there is no position reconciliation (see previous finding), that orphan is invisible forever: `monitor.check_positions()` iterates `state.positions()`, so it gets no invalidation exit and no liquidity-drain exit, and the asset is now whitelisted so the bot can buy it again. `execute_sell` has the identical shape (lines 143-165). Compounding this: `orders` rows are written with status 'submitted'/'sent' and no code ever updates them to a terminal state, so there is no set of unresolved orders a recovery pass could work from — `coinbase.order_status()` exists but is never called.

**Where:** bot/tradebot/execution.py:71-104 (try ends at :95, state writes at :97-104); grep shows `order_status` defined at exchanges/coinbase.py:74 with zero callers

**Fix:** Write an intent row (client_oid, status='pending') before the venue call and update it to a terminal status after; wrap the post-fill state writes in their own try that, on failure, sets RECON_FREEZE and fires an ops alert naming the client_oid/tx signature. Add a startup sweep over non-terminal `orders` rows that queries the venue before doing anything else.

## 41. [HIGH] The dead-man's switch pings before the work, so it stays green through a total core failure
*Lens: ops*

**What:** vps-ops is explicit: 'Ping only at the end of a completed, healthy main-loop cycle. A ping must prove the loop works, not that a timer fires.' In `core.main()` the heartbeat is the *first* statement inside the loop's try, before `monitor.check_positions()` and `monitor.portfolio_tick()`, and the loop-level `except` at line 89 swallows everything into a `core_loop_error` journal row. So if the SQLite file goes read-only or corrupt, or marketdata fails wholesale, or any position check raises, the process throws at line 77+ every single second while `heartbeat.ping()` at line 74 keeps firing — healthchecks.io stays green, no notification is sent, and positions go entirely unmanaged. This is the exact failure mode the switch exists to catch. Note also that a corrupt DB makes `journal.log_event` itself throw, so even the error record disappears.

**Where:** bot/tradebot/core.py:72-90 (ping at :74, work at :76-88, blanket except at :89)

**Fix:** Move `heartbeat.ping()` to the end of the loop body and gate it on a success flag set only after check_positions and portfolio_tick both return; add a consecutive-error counter that stops pinging after N failed cycles so the monitor actually fires.

## 42. [HIGH] The agent layer has no alert path and no liveness coverage — the Anthropic key expiring in 30 days will be completely silent
*Lens: ops*

**What:** `agent/runner.py` imports config, journal, marketdata, risk, state, prompts — it does not import `alerts` or `telegram`, and grep for either across bot/tradebot/agent/ returns nothing. Its only error handling is `except Exception: journal.log_event('agent_loop_error', ...)` followed by a 900s sleep, forever. Nothing in the agent pings healthchecks either — only the core does. So when the Anthropic key expires, every cycle 401s, a row lands in the SQLite events table, and the user is told nothing: Telegram is quiet, healthchecks stays green, `systemctl status tradebot-agent` says active. Given the observed steady state is already ~0 tickets per cycle, a dead research layer is behaviourally indistinguishable from a working one — the user could go weeks believing the bot is hunting. The same silence covers quota exhaustion, a model deprecation, an SDK schema change breaking `output_config`, or a persistent `stop_reason == 'refusal'`.

**Where:** bot/tradebot/agent/runner.py:9 (import line, no alerts); runner.py:135-137 (silent except); heartbeat is only referenced from bot/tradebot/core.py:74

**Fix:** Import alerts into the runner and send an ops alert on N consecutive cycle failures; add a second healthchecks.io check pinged at the end of each successful agent cycle with a ~40 min grace. Separately, put a calendar reminder for the Anthropic key expiry date and note it in the handoff — it is not recorded anywhere in the repo.

## 43. [HIGH] Deploy and rollback do not match spec: no tags, no release directories, no deploy hold, and completely unpinned dependencies
*Lens: ops*

**What:** vps-ops requires deploying from tagged commits into versioned release directories behind a `current` symlink, with a deploy hold (buying paused) before and a reconciliation after, and 'rollback must be one command.' `deploy.sh` instead does `git checkout -B claude/trading-bot-skills-sfqmfo origin/...` inside a single /opt/tradebot working copy; `git tag` in the repo is empty. There is no deploy hold, no post-deploy reconciliation, and rollback means hand-running git commands on the box against a branch head that moves. Separately, bot/requirements.txt is entirely unpinned (`anthropic>=1.0`, `web3>=7.6`, `coinbase-advanced-py>=1.8`, `solders>=0.21`, …) and deploy.sh re-runs `pip install -r` on every deploy — so a routine redeploy or a rebuild-from-scratch can silently pull a breaking major (anthropic 2.x would take out the `output_config`/effort call in runner.py; web3 8.x would take out evm_dex). There is no lockfile, so the environment that currently works cannot be reproduced.

**Where:** scripts/deploy.sh:11-24; `git tag` → empty; bot/requirements.txt (all `>=`)

**Fix:** Run `pip freeze > bot/requirements.lock` on the VPS *now* and commit it — that snapshot is the only record of the working environment. Then tag the current commit, switch deploy.sh to `git checkout <tag>` into /opt/tradebot/releases/<tag> with a `current` symlink, install from the lockfile, and add a STOP-equivalent hold around the restart.

## 44. [HIGH] Blocking venue calls on the trade path still freeze the single-threaded core loop for minutes
*Lens: ops*

**What:** The known-fixed freeze was `reconcile_cash` (now a thread with a 45s join at monitor.py:53-59). The trade path was not fixed: `core.main()` calls `execution.process_ticket` / `execute_sell` inline in the same loop that runs the heartbeat and every position's invalidation check. `_await_solana` polls for up to 90s. `evm_dex.swap` does a blocking `wait_for_transaction_receipt(..., timeout=90)` for the ERC20 approve, then `estimate_gas` + `send_raw_transaction` for the swap, then `execute_buy` calls `confirm()`. A single Base buy can stall the entire loop for two to three minutes, during which no other position's invalidation price is checked and no mechanical exit can fire — and since the heartbeat lives in the same loop with a 60s interval against a 3-minute monitor grace, it also sits right at the edge of a false BOT OFFLINE page. Related correctness bug in the same path: `execute_buy` treats `evm_dex.confirm(h) != 'failed'` as success, but immediately after `send_raw_transaction` there is no receipt yet, so `confirm` returns 'unknown' and the buy is recorded as filled with `qty = notional / ref_price` — a fabricated quantity, not the actual amountOut.

**Where:** bot/tradebot/core.py:83-88 (inline execution); bot/tradebot/execution.py:106-114 (`_await_solana` 90s); bot/tradebot/exchanges/evm_dex.py:104-107 (`wait_for_transaction_receipt` timeout=90); bot/tradebot/execution.py:88-91 (confirm=='failed' check)

**Fix:** Move order execution onto a worker thread (or a short-poll state machine) so the monitor/heartbeat loop never blocks on a venue, and change the Base path to poll `confirm()` until confirmed/failed with a timeout, deriving qty from the receipt's actual token delta rather than notional/price.

## 45. [HIGH] There is zero stdout logging — journalctl is empty and every diagnostic is trapped in SQLite with no reader
*Lens: ops*

**What:** grep for `print(`, `logging.` or `logger` across bot/tradebot/ returns nothing. Every error, mode change, rejection and failure goes to the `events` table via `journal.log_event`. `journalctl -u tradebot-core` will show only 'Started Trading bot deterministic core' plus a Python traceback if the process dies hard — which is precisely where a person picking this up will look first, and they will conclude the bot is healthy. The only tool that reads the DB is scripts/spend.sh, which covers `agent_usage` rows and nothing else; there is no equivalent for core_loop_error, buy_failed, sell_failed, price_fetch_fail, recon_fetch_fail, telegram_poll_fail, monitor_blind or risk_reject. Note also that spend.sh runs `sqlite3` as root against the live WAL database, which can leave root-owned -wal/-shm files in a directory owned by bot:bot.

**Where:** grep -rn 'print(\|logging\.\|logger' bot/tradebot/ → no matches; scripts/spend.sh (only queries kind='agent_usage')

**Fix:** Add a `scripts/tail.sh` (or `python -m tradebot.tail`) that prints the last N rows of `events` grouped by kind, and mirror log_event to stderr at WARNING+ so journalctl becomes useful. Put the exact command in the handoff — otherwise the next operator has no way to see what the bot is doing.

## 46. [HIGH] Most of the vps-ops watchdog table is unimplemented, and several policy constants are dead code
*Lens: ops*

**What:** Beyond disk: clock drift vs NTP is never checked (chrony is installed by harden.sh but nothing reads its offset), despite vps-ops calling clock accuracy 'load-bearing' for Coinbase CDP request signing and approval-code expiry — if chrony fails, Coinbase JWTs are rejected and it surfaces only as a generic `buy_failed` string. Per-venue feed staleness never pauses buying on that venue. The startup key-permission check ('query each venue for the key's permission set; if withdrawal is enabled, run alert-only') does not exist. And a set of config constants that would enforce ops policy have zero uses outside config.py: GAS_EXITS_FLOOR (=20, so nothing verifies the gas float can fund 20 exits, and nothing warns before gas exhaustion makes sells impossible), GATE_TIME_BUDGET_SEC, EXIT_SAFETY_FRESH_SEC, MAX_BOOK_DEPTH_SHARE, MAX_POOL_SHARE, SLIPPAGE_TIERS, MONITOR_INTERVAL_CEX_SEC, and COINBASE_PORTFOLIO. That last one matters: `coinbase.usdc_balance()` sums USD/USDC across every account `get_accounts(limit=250)` returns rather than filtering to the HypeBot portfolio, so if the key's scope is ever broader than intended, main-account cash silently enters the sizing denominator — which portfolio-state forbids outright.

**Where:** bot/tradebot/config.py:52-64 (constants); grep confirms 0 uses outside config.py for GAS_EXITS_FLOOR, GATE_TIME_BUDGET_SEC, EXIT_SAFETY_FRESH_SEC, MAX_BOOK_DEPTH_SHARE, MAX_POOL_SHARE, SLIPPAGE_TIERS, COINBASE_PORTFOLIO; bot/tradebot/exchanges/coinbase.py:22-29

**Fix:** Add a gas-floor pre-check to `execute_buy` (refuse a buy that would drop the native balance below GAS_EXITS_FLOOR × estimated exit cost) and an ops alert when gas runs low; filter `usdc_balance()` to the HypeBot portfolio by retail_portfolio_id; add a clock-offset check (`chronyc tracking`) to the core loop. Delete or implement the remaining dead constants so the config stops implying enforcement that isn't there.

## 47. [HIGH] Rebuild-from-scratch depends on a mutable working branch of a public repo that also publishes the VPS IP and Telegram user ID
*Lens: ops*

**What:** SETUP.md's one-line rebuild is `curl -fsSL https://raw.githubusercontent.com/trelnar/clawpump-products/claude/trading-bot-skills-sfqmfo/scripts/deploy.sh | bash`, and deploy.sh hardcodes the same branch. That is a Claude-generated working branch name, the repo has no tags, and `main` does not contain the bot at all — if the branch is renamed, squash-merged, or the repo is made private, the documented recovery path 404s and there is no other record of how the box was built. The repo also appears to be a published product (README gives `npx skills add trelnar/clawpump-products` install instructions), there is no .gitignore, and SETUP.md commits the VPS static IP (107.191.39.195) and the operator's Telegram user ID (6674587758) in plaintext — telling anyone reading it exactly which host to attack and which account to try to impersonate.

**Where:** SETUP.md 'Deploy the bot' section; scripts/deploy.sh:7-9 (REPO/BRANCH constants); `git tag` empty; `main` branch has no bot/ directory; no .gitignore in repo root

**Fix:** Confirm the repo's visibility. If public, move SETUP.md's IP/user-ID into an untracked local note and add a .gitignore. Either way, tag the deployed commit, pin deploy.sh to the tag, and keep an offline copy of the tarball alongside the wallet-key backup so a rebuild does not depend on GitHub at all.

## 48. [HIGH] Phase advancement, phase regression, and RECON_FREEZE have no code path that can ever trigger them
*Lens: ops*

**What:** go-live says 'The bot requests each phase or step advance with an approval-gate code; the user confirms on Telegram. The bot never self-promotes.' `approval.Commands._yes` handles `p['kind'] == 'phase'` and increments the phase — but grep shows `state.add_pending` is only ever called with kind 'buy', 'resume' and 'flatten'. Nothing constructs a phase-advance request, so that branch is unreachable and the only way off the current $5-per-order phase 1 is to hand-edit the `kv` row with sqlite3 against a live WAL database. Likewise go-live's automatic one-step regression on a critical incident (unexplained break, missed mechanical exit, orphan order) is not implemented, and no code path anywhere sets RECON_FREEZE. This matters for the handoff because the phase-1 exit criteria (5 completed buy→exit cycles, etc.) imply a mechanism that does not exist.

**Where:** bot/tradebot/approval.py:100-103 (`kind == 'phase'` handler); grep `add_pending` → only approval.py:19 'buy', :30 'resume', :121 'flatten'; grep RECON_FREEZE → only state.py:9

**Fix:** Add `request_phase_approval()` gated on the go-live exit criteria measured from the journal, and wire RECON_FREEZE into the reconciliation work from the cold-start finding. Meanwhile, document the manual command (`sqlite3 /var/lib/tradebot/tradebot.db "UPDATE kv SET v='2' WHERE k='phase'"` + restart) so the next operator isn't stuck.

## 49. [HIGH] The kill switch runs in an unsupervised daemon thread that dies silently while the bot keeps trading and the heartbeat keeps reporting healthy
*Lens: security*

**What:** STOP, FLATTEN, NO and RESUME all arrive only through telegram.Poller, a daemon thread started once in core.main and never checked again. Poller.run wraps only the _call in try/except; the 'for u in updates: self._dispatch(u)' loop is outside any try, and _dispatch's journal.log_approval on line 74 is outside its try as well. Any exception there — sqlite locked/disk full, a malformed update, KeyError on update_id — propagates out of run() and the thread ends. The process stays alive, systemd sees a healthy unit, and heartbeat.ping fires at the top of every loop iteration unconditionally, so healthchecks.io reports green. The operator then has no way to stop the bot except SSH. vps-ops has this exact incident row ('Inbound Telegram path down ... If still down 5 min: pause all automatic buying — the user's STOP path is broken'), and it is not implemented anywhere. It also violates the vps-ops rule that a ping must prove the loop worked, not that a timer fired.

**Where:** bot/tradebot/telegram.py:47-58 (run: try covers only _call; dispatch loop unprotected) and :74 (log_approval outside try); bot/tradebot/core.py:65-66 (poller.start, never re-checked) and :73-75 (unconditional heartbeat at loop top); .agents/skills/vps-ops/SKILL.md incident runbook, 'Inbound Telegram down' row

**Fix:** Wrap Poller.run's entire body in try/except inside the while loop so no exception can end the thread; have the poller stamp a liveness key (state.set_kv('tg_poll_ts', now)) on every successful getUpdates; in core.main gate heartbeat.ping on both a completed loop pass and tg_poll_ts being under ~120s old, and set mode SELL_ONLY plus an ops alert when the poller has been silent 5 minutes. Then test by killing the thread (raise inside _dispatch) and confirming healthchecks goes red.

## 50. [HIGH] Both hot wallets blind-sign transactions built by third-party aggregator APIs — no router allowlist, no simulation, no output verification
*Lens: security*

**What:** solana_dex.swap POSTs to quote-api.jup.ag and signs whatever base64 VersionedTransaction comes back: VersionedTransaction(tx.message, [kp]) signs the returned message wholesale, with no check that the instructions touch only the intended mints or that outputs land in the bot's own account. evm_dex.swap takes routerAddress and calldata verbatim from aggregator-api.kyberswap.com, sends an ERC20 approve to that attacker-supplied address, then signs and broadcasts a transaction to it. There is no allowlist of router addresses, no eth_call/simulateTransaction preflight against expected balance deltas, and no cap on what the calldata may do. A compromise, DNS/BGP hijack, or takeover of either hostname converts directly into full drain of the Solana and EVM wallets (~$605 today, everything at phase 4). Note also that the pinned Jupiter host is the legacy v6 quote-api endpoint; a hostname that gets decommissioned and later re-registered is the cheapest version of this attack.

**Where:** bot/tradebot/exchanges/solana_dex.py:93-113 (swap: r.json()['swapTransaction'] signed unverified); bot/tradebot/exchanges/evm_dex.py:93-117 (routerAddress + data from rb.json(), approve then send)

**Fix:** Add a hardcoded router/program allowlist (Jupiter program IDs, KyberSwap router addresses for Base) and reject any built transaction whose target is not on it. Before signing, simulate: solana simulateTransaction and w3.eth.call on the built tx, asserting the wallet's balance deltas match the quote within tolerance. Cap the ERC20 approve to exactly amount_raw (already done) and add a post-swap approval reset. Pin certificate/hostname expectations and treat any aggregator hostname change as a code review, not a config edit.

## 51. [HIGH] Wallet private keys and secrets.env are group-readable by the bot user, and the bot user has interactive SSH with root's key
*Lens: security*

**What:** harden.sh creates /etc/tradebot at 0700 root:root with secrets.env at 0600, and gen_wallets.py writes both keyfiles at 0600 — but deploy.sh then loosens all of it: the directory to 0750 root:bot, secrets.env to 0640 root:bot, and both wallet keyfiles to 0640 root:bot. SETUP.md and gen_wallets.py both still claim 0600, so anyone auditing from the docs or from harden.sh alone gets the wrong picture. The core service does need to read the keyfiles, but secrets.env does not need group read at all — systemd parses EnvironmentFile as root before dropping privileges; the 0640 exists only to make the documented 'sudo -u bot bash -c . /etc/tradebot/secrets.env' phase0 invocation work. Separately, harden.sh copies /root/.ssh/authorized_keys to /home/bot/.ssh/authorized_keys, so the same SSH key logs in as both root and bot, and sshd is set to PermitRootLogin prohibit-password where vps-ops requires PermitRootLogin no. Net effect: one compromised laptop key equals a shell as bot equals 'cat /etc/tradebot/evm_wallet.key' equals both wallets, with no second factor anywhere in the chain.

**Where:** scripts/deploy.sh:25-29 and :46-47 (chown root:bot, chmod 0640) vs scripts/harden.sh:26-28 (0700/0600) and scripts/gen_wallets.py:3,25,36; scripts/harden.sh:20-23 (authorized_keys copy) and :34 (PermitRootLogin prohibit-password); SETUP.md section 5 ('Both keyfiles at mode 0600')

**Fix:** Verify live state: stat -c '%a %U:%G' /etc/tradebot /etc/tradebot/*. Restore secrets.env to 0600 root:root and change deploy.sh to stop chmod'ing it; run phase0 as root or via systemd-run --uid=bot with the EnvironmentFile rather than sourcing it as bot. Keep the keyfiles at 0640 root:bot only if core must read them as bot, and remove /home/bot/.ssh/authorized_keys entirely (admin as root; use systemctl/journalctl, never a bot shell). Set PermitRootLogin without-password or move admin to a named sudo user. Fix the SETUP.md/gen_wallets.py claims so the docs match reality.

## 52. [HIGH] The research service is handed every credential in the system, contradicting its own documented isolation
*Lens: security*

**What:** bot/README.md and the runner docstring both state the agent layer 'holds no venue credentials'. In fact tradebot-agent.service loads the identical EnvironmentFile as core — so the agent process has TELEGRAM_TOKEN, COINBASE_API_KEY, COINBASE_API_SECRET and HEALTHCHECK_URL in its environment — and it runs as User=bot with ProtectSystem=strict, which leaves /etc read-only but fully readable, so it can also read both hot-wallet keyfiles. The agent is the component with the largest untrusted-input surface (it feeds attacker-influenceable market data into a model and parses the response), and it is the one with no need for any of these secrets: it needs only ANTHROPIC_API_KEY and the DB. Any RCE or dependency compromise in the agent path currently yields the wallets and the exchange key, not just bogus tickets.

**Where:** systemd/tradebot-agent.service:10 (EnvironmentFile=/etc/tradebot/secrets.env) and :7-8 (User=bot); bot/README.md:11 and bot/tradebot/agent/runner.py:2-3 ('Holds no venue credentials')

**Fix:** Split the secrets: /etc/tradebot/agent.env with only ANTHROPIC_API_KEY, /etc/tradebot/secrets.env staying 0600 root:root for core. Point tradebot-agent.service at agent.env, add InaccessiblePaths=/etc/tradebot to that unit (it needs no keyfile), and ideally give the agent its own unprivileged user with write access only to the DB. Update bot/README.md once the claim is true.

## 53. [HIGH] REVOKE can never match any asset, and the bot falsely confirms the revocation
*Lens: security*

**What:** Commands.handle uppercases the entire message before parsing, so the argument to REVOKE is always uppercase. state.whitelist_revoke does an exact, case-sensitive SQL equality against whitelist.asset_id, and every asset_id in the system is lowercase-prefixed and case-sensitive in its body ('solana:<base58 mint>', 'base:0x...', 'cex:SOL-USDC'). No possible input can match, so the UPDATE affects zero rows — yet the handler unconditionally replies 'Revoked <asset>. It will require approval again.' approval-gate specifies the opposite ('An unmatched asset gets `No whitelist entry: <asset>` and no change'). Because a single YES permanently whitelists an asset for automatic buys and adds, and revocation is the only user-facing way to undo that, the whitelist is currently append-only from the operator's perspective and the operator will believe otherwise. Nothing has been whitelisted yet (zero trades), so this is cheap to fix now and expensive to discover later.

**Where:** bot/tradebot/approval.py:48 (text.upper()), :68-70 (REVOKE branch, unconditional success message), :92 (whitelist_add on approval); bot/tradebot/state.py:148-153 (case-sensitive UPDATE); .agents/skills/approval-gate/SKILL.md 'STATUS, STOP, REVOKE details'

**Fix:** Parse the command verb case-insensitively but keep the argument's original case (split the raw text; upper() only parts[0] and code arguments); make whitelist_revoke return the rowcount and reply 'No whitelist entry: <asset>' when it is zero. Add a unit test that whitelists 'solana:<mint>' and revokes it through Commands.handle.

## 54. [HIGH] Gate 4 'exit safety' is two aggregator price quotes, not a simulated sell — it cannot detect a honeypot
*Lens: security*

**What:** The execution skill defines gate 4 as 'a simulated sell of the intended position notional succeeds, transfer tax is at or under the cap, and the pool or book is deep enough to exit', with cannot-sell as a blocking condition. Both implementations instead request a buy quote and a sell quote from the aggregator and compute round-trip loss. A quote is routing math over pool reserves; it never executes the token's transfer path, so a token that blocks or taxes sells conditionally (blacklist, transfer hook, sell-only-for-owner, delayed enablement) returns a perfectly healthy round-trip and passes the gate. Transfer tax is never read from the contract; exit depth is never checked at all — MAX_POOL_SHARE, MAX_BOOK_DEPTH_SHARE and SLIPPAGE_TIERS are defined in config and referenced nowhere in the codebase. The failure mode is a bought position that cannot be sold, which also defeats every downstream 'sells always execute' guarantee including FLATTEN. Bounded to $5 at phase 1; 5% of portfolio at phase 4.

**Where:** bot/tradebot/exchanges/solana_dex.py:67-90 and bot/tradebot/exchanges/evm_dex.py:66-85 (quote-based); bot/tradebot/config.py:52-54 (MAX_POOL_SHARE, SLIPPAGE_TIERS, MAX_BOOK_DEPTH_SHARE — zero uses outside config.py); .agents/skills/execution/SKILL.md gate 4 and line 105 ('simulated sell, transfer-tax read, exit-depth read')

**Fix:** Replace the sell quote with an actual simulation from the bot's own address — solana simulateTransaction of the Jupiter sell tx, and w3.eth.call of the Kyber sell calldata — and fail the gate when the simulation reverts. Enforce notional <= MAX_POOL_SHARE * liquidity_usd using the pool liquidity already returned by dexscreener_token. Until that lands, describe the gate in the handoff as 'a priced exit exists', not 'the exit works'.

## 55. [HIGH] Discovery is an attacker-payable feed with no contract-address verification, and the model's chosen asset_id is never cross-checked against it
*Lens: security*

**What:** The only on-chain discovery sources are DexScreener token-boosts and token-profiles — feeds where a token appears because someone paid for placement. signal-hygiene calls trading an unverified harvested address one of the suite's few hard stops ('never trade an address harvested from social content until it is verified against canonical sources'; unverifiable downgrades to alert-only). No verification exists anywhere in the code. The payload key-whitelist in gather() is the only sanitization boundary, and it does let through one attacker-controlled free-text field, base_symbol (a token symbol is arbitrary text chosen by the deployer), straight into the model prompt with no length cap or scrubbing. Worse, submit() takes c['asset_id'] verbatim from the model response and turns it into a ticket that execution splits and hands to the swap router — there is no assertion that the returned asset_id was among the candidates that were sent. A symbol-field injection that persuades the model to emit BUY_NOW for an address never in the payload is caught nowhere in the pipeline; the remaining backstops are the $5 phase-1 size and the first-buy Telegram approval.

**Where:** bot/tradebot/marketdata.py:159-178 (boosts/profiles as sole on-chain discovery); bot/tradebot/agent/runner.py:29-31 (base_symbol passed through unbounded), :52-57 (payload), :89-117 (submit trusts c['asset_id']); .agents/skills/signal-hygiene/SKILL.md 'Contract-address safety'

**Fix:** In submit(), reject any candidate whose asset_id is not in the exact set of chain:address / cex:product ids sent in this cycle's payload, and journal the rejection as a manipulation signal. Truncate and character-scrub base_symbol (e.g. 32 chars, strip newlines and control characters) before it enters the payload. Add the signal-hygiene verification step — at minimum an on-chain deploy-age and pair-creation cross-check — or record explicitly in the handoff that this hard stop is unimplemented and the approval tap is the only address defense.

## 56. [HIGH] The journal — the only record of the bot's behavior — has no backup, encrypted or otherwise
*Lens: security*

**What:** vps-ops specifies a tradebot-backup.timer with daily VACUUM INTO snapshots, public-key encryption whose private half never exists on the VPS, and off-VPS upload with ops alerts after two failed days. Only two unit files exist and neither is a timer; there is no backup script in scripts/. Everything the system knows — fills, forecasts, approval history, the value series the 20% halt depends on, and the calibration data the whole design rests on — lives in one WAL SQLite file on one Vultr instance. Loss of that instance is loss of the entire trade history with no way to reconstruct what the bot did or holds beyond re-reading the chains. It also means there is currently no clean way to purge a credential that has already leaked into old journal rows (see the token-leak finding) without destroying history.

**Where:** systemd/ contains only tradebot-core.service and tradebot-agent.service; no backup script in scripts/; .agents/skills/vps-ops/SKILL.md Backups section

**Fix:** Add scripts/backup.sh doing sqlite3 VACUUM INTO a temp file, age -R <public key> encryption, upload to object storage, and a journal.log_event on success/failure; ship tradebot-backup.timer + .service running as root daily; keep the age private key off the VPS. Do one restore test (PRAGMA integrity_check plus row counts) before calling it done.

## 57. [HIGH] Deploy path is curl-pipe-bash from a mutable branch, in place, with no hold and no rollback — and its own comment claims otherwise
*Lens: security*

**What:** deploy.sh is fetched over the network and piped into root's shell, and it does git checkout -B onto a moving branch (claude/trading-bot-skills-sfqmfo) inside the live /opt/tradebot while both services are running against those files. vps-ops requires tagged commits only, a deploy hold that pauses buying and drains in-flight orders, versioned release directories behind a 'current' symlink, and one-command rollback — none of which exists. The script's header comment says 'Halts buying during deploy (vps-ops)' and the script never touches the mode or the services, so the operator is told a safety property holds that does not. Anyone who can push to that branch (or to the raw.githubusercontent path it curls) executes code as root on the box at the next deploy and as the bot user thereafter. SETUP.md additionally publishes the VPS static IP and the Telegram user ID in-repo, and README.md advertises the repo for public installation.

**Where:** scripts/deploy.sh:3 (curl | bash), :4 (comment claiming a buying halt), :7-16 (branch checkout in place), :50-52 (unit copy, no restart/hold); SETUP.md:5 ('Server IP ... 107.191.39.195'); .agents/skills/vps-ops/SKILL.md 'Deploys and upgrades'

**Fix:** Confirm the repo's visibility with gh repo view --json visibility; if public, move SETUP.md's IP and user ID to an untracked local file and treat the IP as known-exposed. Change deploy.sh to clone a tag into /opt/tradebot/releases/<tag>, flip a 'current' symlink, and bracket the swap with a SELL_ONLY hold plus systemctl stop/start; download-and-inspect rather than curl|bash. Add branch protection on the deploy branch.

## 58. [HIGH] DexScreener fields already fetched are thrown away before the prompt — the market agent is blind on data that costs nothing
*Lens: strategy-data*

**What:** `dexscreener_token()` receives the full pair object and keeps only price, liquidity.usd, volume.h24, pairAddress, dexId, baseToken.symbol, pairCreatedAt. The same response already contains `priceChange.{m5,h1,h6,h24}`, `txns.{m5,h1,h6,h24}.{buys,sells}`, `volume.{m5,h1,h6}`, `fdv`, `marketCap`, `info.socials`/`info.websites`, and `boosts.active`. It stashes the rest under `raw` — and then `gather()` rebuilds the candidate dict from an explicit six-key whitelist that excludes `raw`. So the model never sees a single rate-of-change number. Every candidate arrives as a still photograph: one price, one 24h volume, one liquidity figure, a symbol. The skill's market agent is asked for momentum, relative volume, volatility and order flow; the buy/sell txn counts that are the only available order-flow proxy are fetched and discarded. Same for the boosts feed: `it['raw']` carries the token `description` and `links` (the only free-text narrative anywhere in the pipeline) and is journaled but never forwarded to the model. Zero additional API calls are needed to fix any of this.

**Where:** bot/tradebot/marketdata.py:34-43 (projection); bot/tradebot/agent/runner.py:124-126 (six-key whitelist rebuild drops `raw`); bot/tradebot/agent/runner.py:121 (boost raw journaled, not forwarded)

**Fix:** Widen the projection in dexscreener_token() to include priceChange (all windows), txns buys/sells (m5/h1/h6), volume m5/h1/h6, fdv, marketCap, info.socials and boosts.active; forward the boost description/links from `it['raw']` into the candidate. Then re-run one cycle and diff the payload — this is the single highest information-per-dollar change available.

## 59. [HIGH] entry_liquidity_usd is never written, so the liquidity-drain exit can never fire
*Lens: strategy-data*

**What:** monitor.check_positions() guards the pool-drain exit with `if p.get('chain') in ('solana','base') and p.get('entry_liquidity_usd')`. The only writer of positions is execute_buy(), which calls `state.upsert_position(asset, venue, chain, qty, notional, invalidation=...)` — the `entry_liq` parameter defaults to None, and the ticket schema has no liquidity column to carry it anyway even though gather() measured liquidity_usd for that exact token minutes earlier. Every position will therefore be inserted with entry_liquidity_usd NULL and both the LIQ_DRAIN_EXIT (50%) and LIQ_DRAIN_WARN (30%) branches are unreachable code. For an on-chain microcap, losing the pool is the dominant loss mode and this is the only feed-driven control against it; the position-monitor skill calls it out as the worst case. It has never been noticed because zero positions have ever existed. The first real token buy will run with this protection silently absent.

**Where:** bot/tradebot/monitor.py:22 (guard) vs bot/tradebot/execution.py:99-100 (upsert_position called without entry_liq); bot/tradebot/state.py:92 (entry_liq=None default)

**Fix:** Add a liquidity_usd column to the tickets table, populate it in runner.submit() from the candidate's measured liquidity, pass it through execute_buy() as entry_liq, and add an assertion or startup check that no open on-chain position has NULL entry_liquidity_usd. Verify with: sqlite3 /var/lib/tradebot/tradebot.db 'select asset_id, entry_liquidity_usd from positions'.

## 60. [HIGH] The forecast schema has no SELL/HOLD/ADD action — the research layer structurally cannot exit or add, and no take-profit exists anywhere
*Lens: strategy-data*

**What:** FORECAST_SCHEMA's action enum is exactly [BUY_NOW, COMING_UP, PASS]. submit() only creates a ticket when action == 'BUY_NOW'. core.py has an `elif t['action'] == 'SELL_NOW'` branch that is unreachable dead code — nothing in the codebase can produce a SELL_NOW ticket. Consequences: the skill's entire 'Position management' section (immediate exit, re-entry, reversal, scale-out, 'SELL NOW does not require the target to have been reached') is unimplementable; the ADD action that the go-live phase plan assumes ('adds/sells automatic') cannot be generated; and position-monitor's specified 'standing profit plan (e.g. scale out half at 2x)' has no writer and no reader (positions.plan is always the literal '{}'). The only exits that exist are invalidation-price cross and the drain check that is itself dead. A position that reaches 2x is never harvested. Worse, the agent-supplied invalidation_price becomes a hard full-exit stop evaluated every 5 seconds on a DexScreener price — on a microcap expected to move 2x in 1-3 days, ordinary intraday noise will stop the position out long before the thesis window elapses, so the calibration data this eventually produces will measure the stop policy, not the forecast.

**Where:** bot/tradebot/agent/prompts.py:64-65 (enum); bot/tradebot/agent/runner.py:197-198 (non-BUY_NOW discarded); bot/tradebot/core.py:86-88 (unreachable SELL_NOW branch); bot/tradebot/monitor.py:8-32 (no target/time exit)

**Fix:** Add SELL_NOW/ADD/HOLD to the schema enum and to submit()'s ticket path; feed held positions into the payload with entry price, cost basis, unrealized P&L, age and current invalidation (today they arrive as bare asset_id strings, runner.py:146). Separately, add a mechanical take-profit and a max-hold timer to monitor.py so a thesis has a defined resolution even if the model layer stays silent.

## 61. [HIGH] The discovery universe is two paid-promotion endpoints — it selects for late, advertised tokens
*Lens: strategy-data*

**What:** dexscreener_trending() reads only /token-boosts/top/v1 and /token-profiles/latest/v1. Both are advertising products: a token appears because its team paid DexScreener for placement or submitted a profile. That is not a market-structure signal, and boosts are typically bought during or after a move — directly contrary to the skill's objective #5, 'early identification before price fully reflects the signal.' There is no organic feed at all: no new-pool stream, no volume-ranked or price-change-ranked list, no launchpad events, despite the market-data skill's registry naming 'New-token launches — DEX factory and launchpad event streams' as a required feed class. Combined with the $5,000 liquidity floor (runner.py:123) this filters out precisely the sub-$5k-liquidity fresh pools where 2x-in-days is most common — and that floor is ~10x stricter than execution actually needs, since at the $5 phase-1 order size MAX_POOL_SHARE=1% is satisfied by a $500 pool. Answer to the universe question: boosted Solana microcaps genuinely do 2x routinely, so the universe is not empty — but this pipeline samples the promoted, high-liquidity, already-moving tail of it, which is the subpopulation with the worst 2x rate and the highest rug rate.

**Where:** bot/tradebot/marketdata.py:159-178 (both endpoints); bot/tradebot/agent/runner.py:123 (liquidity_usd > 5000); .agents/skills/market-data/SKILL.md feed registry row 'New-token launches'

**Fix:** Add GeckoTerminal's keyless endpoints as the organic backbone: /networks/{solana,base}/new_pools and /networks/{solana,base}/trending_pools (~30 req/min free, same host and header the code already uses for OHLCV at marketdata.py:83). Add DexScreener /token-boosts/latest/v1 alongside /top for boost-purchase *acceleration* rather than boost level. Drop the liquidity floor to ~$1,500 and let exit_safety be the real gate. Keep boosts as one labeled source among several, not the universe.

## 62. [HIGH] No on-chain agent input exists, though the two cheapest sources are already configured or free
*Lens: strategy-data*

**What:** The skill's on-chain agent requires wallet accumulation, large-holder activity, token concentration, holder growth, and contract risk. None of these reach the model — the payload has no holder count, no top-holder share, no mint/freeze authority, no LP-lock status, no transfer-tax figure. The only contract-safety check in the system is execution's gate 4, which is a Jupiter round-trip quote (solana_dex.py:67-90). That catches an un-sellable token at the moment of purchase but produces no signal the research layer can weigh, and it runs after the model has already committed a thesis. Two closures cost nothing: (a) the bot already holds a Solana RPC endpoint (config.SOLANA_RPC) — `getTokenLargestAccounts` plus `getTokenSupply` gives top-20 holder concentration in two keyless calls per candidate; (b) GoPlus Security (api.gopluslabs.io/api/v1/solana/token_security and /token_security/8453 for Base) is keyless and free and returns honeypot status, buy/sell tax, mint and freeze authority, LP-lock percentage and top-10 holder share in one call. RugCheck (api.rugcheck.xyz/v1/tokens/{mint}/report) is a keyless Solana-only second opinion. Per signal-hygiene these are labels, not vetoes — they should be scored inputs to the selloff-risk analysis, not new hard stops.

**Where:** bot/tradebot/agent/runner.py:124-126 (candidate fields); bot/tradebot/config.py:33 (SOLANA_RPC already present); .agents/skills/short-horizon-research/SKILL.md 'On-chain agent'

**Fix:** Add a marketdata.token_safety(chain, address) that calls GoPlus (both chains) and, for Solana, getTokenLargestAccounts/getTokenSupply on the existing RPC; attach the result to each candidate as a labeled dict. Budget it to the shortlist first if rate limits bite. Do not wire it into a gate — feed it to the model as scored risk input per signal-hygiene.

## 63. [HIGH] Jupiter quote-api v6 host should be verified before the first token trade — it sits on the critical path for every on-chain buy
*Lens: strategy-data*

**What:** solana_dex.py pins JUP = 'https://quote-api.jup.ag/v6'. Jupiter has been migrating callers off the v6 quote-api host to lite-api.jup.ag / api.jup.ag swap endpoints. This host is used by quote(), swap(), and exit_safety(). Because exit_safety is execution gate 4, a dead or redirecting host does not degrade gracefully — it raises, is caught at solana_dex.py:87-88, and returns `quote_failed`, which rejects every Solana token buy with a risk.Reject('exit_safety') and an ops alert. The failure looks like a risk decision, not an outage. No trade has reached gate 4 yet, so this is untested end to end. It is a one-command check and it is the single most likely thing to silently block the first real trade.

**Where:** bot/tradebot/exchanges/solana_dex.py:11 (JUP constant), :67-90 (exit_safety), bot/tradebot/execution.py:32-34 (gate 4 reject path)

**Fix:** On the VPS run: curl -s -o /dev/null -w '%{http_code}\n' 'https://quote-api.jup.ag/v6/quote?inputMint=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v&outputMint=So11111111111111111111111111111111111111112&amount=5000000&slippageBps=300'. If it is not 200, move JUP to the current Jupiter swap API base and re-run. Add a startup self-test in the core that calls exit_safety on a known-good mint and ops-alerts on quote_failed, so venue outages are distinguishable from risk rejections.

## 64. [HIGH] Every cycle is stateless — no prior forecasts, no discovery history, no outcomes, and no code that could ever write one
*Lens: strategy-data*

**What:** The research payload is {now_utc, held_positions (ids only), go_live_phase, untrusted_market_data}. Nothing from any previous cycle is included: no prior forecasts on the same token, no COMING_UP candidates awaiting a trigger, no record of what was seen 15 minutes ago. The historical-analogue agent therefore has no corpus and cannot function even in principle. Beyond the known 'calibration unexercised' item, the sharper fact is that no resolver exists: journal.log_outcome() is defined and called from nowhere, the `outcomes` table has no writer anywhere in the repo, and there is no cron, no systemd timer, and no scripts/ entry that would resolve a forecast or run the missed-opportunity rescan the market-data skill specifies as a daily job. The loop will not close by itself even after trades start. Compounding this, backtest-replay requires the full recorded signal stream including signals production ignored — but gather() breaks out of the discovery loop at the AGENT_MAX_CANDIDATES cap *before* calling log_discovery, so items past the 15th are never journaled at all.

**Where:** bot/tradebot/agent/runner.py:147-152 (payload); bot/tradebot/journal.py:132-134 (log_outcome, no callers); bot/tradebot/agent/runner.py:119-121 (break precedes log_discovery); no cron/timer in systemd/ or scripts/

**Fix:** Write a resolver job (systemd timer, daily) that for every discovery_inputs row older than 72h fetches max close from GeckoTerminal OHLCV, computes max_multiple, and writes an outcomes row — this simultaneously builds the calibration dataset, the missed-opportunity dataset and the historical-analogue corpus from data already being logged. Move log_discovery above the cap check so the full stream is recorded. Then feed the last N resolved outcomes into the cached system prefix.

## 65. [HIGH] COMING_UP is a no-op and the runtime skill's three priority queues do not exist
*Lens: strategy-data*

**What:** The skill devotes a section to coming-up monitoring: dynamic monitoring frequency, tightening intervals as a candidate nears its trigger, revalidation before promotion to BUY NOW. In code, a COMING_UP candidate is logged to forecasts and then `continue`d past — no ticket, no watchlist, no trigger stored, no re-check. The next cycle starts from scratch and will only rediscover it if DexScreener still lists it. Likewise runtime specifies three prioritized agent queues (held-position re-evals first, then COMING_UP revalidations, then discovery); runner.main() is a bare `while True: gather(); research(); submit(); sleep(900)` with no queue of any kind. And monitor.py writes `kv['reeval:<asset>']` on a 30% liquidity drop, which nothing ever reads — a write-only key standing in for the model-path re-evaluation the position-monitor skill requires. Net effect for the lens: the pipeline can only ever act on a candidate in the same 15-minute cycle it was discovered, which is the opposite of the skill's staged discover-watch-confirm design.

**Where:** bot/tradebot/agent/runner.py:197-198 (non-BUY_NOW dropped); bot/tradebot/agent/runner.py:220-232 (single unqueued loop); bot/tradebot/monitor.py:32 (reeval key, no reader)

**Fix:** Persist COMING_UP candidates to a watchlist table with their trigger text and invalidation, include the active watchlist in the next cycle's payload, and have the core evaluate numeric triggers mechanically between cycles. Either implement a reader for the reeval: keys or delete the write so the dead path stops implying a protection that is not there.

## 66. [HIGH] Solana buys record position quantity in raw base units, corrupting portfolio value and every risk limit derived from it
*Lens: money-path*

**What:** execute_buy stores `qty = int(q["outAmount"])` for Solana. Jupiter's `outAmount` is in the output mint's smallest unit (raw), but Coinbase (`qty = notional/limit`) and Base (`qty = notional/ref_price`) store whole tokens, and `marketdata.price()` returns USD per whole token. `state.total_value` computes `qty * mark`, so a Solana position is overvalued by 10^decimals — typically 1e6 to 1e9. A $5 buy of a 6-decimal token books as ~$5,000,000 of portfolio value. Consequences cascade through every denominator: the 5% position cap becomes ~$250k, the 2.5% fat-finger cap ~$125k, and the 50% aggregate-deployed cap becomes unreachable, so every subsequent buy that cycle passes all risk checks unchecked. The value_series peak is set to the inflated figure; when the position is sold or the row corrected, value returns to ~$1,000, which is below 80% of the bogus peak, so risk.check_halt fires EMERGENCY_HALT and locks the bot. Sells are unaffected (execute_sell reads the real on-chain raw balance), so the defect is confined to the ledger and valuation — which is exactly what all risk enforcement reads. Solana holds $390, the largest single venue allocation, so this triggers on the most likely first trade.

**Where:** bot/tradebot/execution.py:80 `qty = int(q["outAmount"])` vs :72 and :87; bot/tradebot/state.py:157-164 total_value; bot/tradebot/risk.py:35-67

**Fix:** Divide by decimals at record time: use `solana_dex.token_balance(mint)` (it already returns `(amt, dec)`) or the quote's output-mint decimals, and store `qty = outAmount / 10**dec`. Add a unit assertion in upsert_position rejecting any position whose `qty * mark` exceeds total cash by more than 10x. Backfill/inspect any existing positions rows before the next restart.

## 67. [HIGH] Every automatic exit path is dead code; only a model-supplied invalidation price can close a position
*Lens: money-path*

**What:** The bot has three specified mechanical exits and none of them can fire. (1) Liquidity-drain exit: monitor.py guards on `p.get("entry_liquidity_usd")`, but execute_buy calls `upsert_position(...)` without the `entry_liq` argument, so the column is always NULL and the LIQ_DRAIN_WARN/LIQ_DRAIN_EXIT branch never executes — the ticket does not even carry liquidity, though gather() has it. (2) Standing profit plan ("scale out half at 2x" per position-monitor): `plan` is never passed either, and nothing anywhere reads it — there is no take-profit code at all. (3) Agent SELL_NOW: core.py:86 handles a SELL_NOW ticket, but FORECAST_SCHEMA's action enum is only [BUY_NOW, COMING_UP, PASS], and runner.submit only creates tickets when action == BUY_NOW, so a SELL_NOW ticket can never exist. That leaves the invalidation cross (`if inv and price <= inv`) as the sole automatic exit — and `invalidation_price` comes from the model, where a legitimate 0 (the schema documents 0 as the no-structural-level value for wave_invalidation) is falsy and silently disables the stop. There is no time stop, no max-loss stop, and no trailing stop. Capital can enter positions automatically and, in the ordinary case, has no automatic way out short of the user typing FLATTEN.

**Where:** bot/tradebot/execution.py:99-100 (no entry_liq/plan); bot/tradebot/monitor.py:22 and :17; bot/tradebot/agent/prompts.py:65 enum; bot/tradebot/agent/runner.py:102; bot/tradebot/core.py:86-88

**Fix:** Pass `entry_liq=` from the ticket (carry dexscreener liquidity_usd through add_ticket) and `plan=` into upsert_position. Add SELL_NOW to the schema enum and let submit() create sell tickets. Reject any BUY ticket whose invalidation_price is None or <= 0 rather than opening an unstopped position, and add an absolute max-loss stop as a floor independent of the model.

## 68. [HIGH] Sells credit cash and delete the position without confirming the swap landed
*Lens: money-path*

**What:** execute_sell discards the confirmation result on both chains. Solana: `_await_solana(sig)` is called with its return value thrown away (contrast execute_buy:78, which checks it), so a failed or timed-out swap still proceeds to credit `proceeds`, close the position and log a sell fill. Base: `evm_dex.swap(...)` is called with no confirm at all. In both cases the tokens remain in the wallet while the ledger says they are gone and the cash is there — the position row is DELETEd by close_position, so nothing monitors or re-sells that token afterward, and there is no position reconciliation to notice. Proceeds are also never measured: Solana uses the pre-trade quote's `outAmount` rather than the realized amount, Base and Coinbase use `qty * price` where price is a public ticker read, and `price = marketdata.price(asset_id) or 0` means a failed price fetch books $0 proceeds and a full-cost-basis loss while crediting nothing to cash. This is the most direct route to permanently stranded capital in the codebase.

**Where:** bot/tradebot/execution.py:127, :141-142, :148-149, :157-166

**Fix:** Check `_await_solana`'s return and confirm the Base tx before mutating state; on a non-success, leave the position open and raise an ops alert. Derive proceeds from the realized balance delta (USDC balance before/after) rather than a quote or a ticker price, and never mutate cash when price is None.

## 69. [HIGH] An approved buy bypasses every risk gate, including the emergency halt
*Lens: money-path*

**What:** core.on_approved_buy re-validates only three things — price availability, ticket age, and the buy zone — and then calls `execution.execute_buy(ticket, price)` directly. execute_buy contains no gates whatsoever; the gate sequence lives in `_gates_buy`, which is only reached via process_ticket. So an approval that arrives up to 30 minutes after the request skips: gate 1 (halt mode), gate 3 (risk.check_buy — the 5% cap, concurrency, aggregate deployed, per-venue, per-chain, fat-finger, and the venue cash check), and gate 4 (exit safety). risk-limits explicitly requires that a halt "invalidate pending buy-approval requests" and that execution "must call risk-limits before every order... no order path may skip the call". As written, if EMERGENCY_HALT or STOP fires while an approval is pending, tapping Approve still places the order. The same path also spends cash that may no longer exist, since the venue cash check is in check_buy.

**Where:** bot/tradebot/core.py:42-56 calling execution.py:63 execute_buy, bypassing execution.py:9-39 _gates_buy; risk-limits/SKILL.md hard limit 2 item 3

**Fix:** Route on_approved_buy through process_ticket (or call _gates_buy before execute_buy), and have expire_pendings/set_mode invalidate all pending buy approvals whenever mode leaves NORMAL.

## 70. [HIGH] No position reconciliation exists; RECON_FREEZE is never set and cash drift is silently absorbed
*Lens: money-path*

**What:** portfolio-state specifies a 5-minute poll comparing internal cash AND positions against venue-reported state, with a 0.05% position / 0.1% cash tolerance, RECON_FREEZE plus an ops alert on any break, and a `reconciliations` table. What exists is `reconcile_cash`, which does three things: fetch each venue's USDC balance and overwrite the cash row. It never reads positions, never compares against expected values, never computes a discrepancy, never alerts, and never sets RECON_FREEZE — that mode string is declared in state.MODES and assigned nowhere in the codebase. The practical effect is that any unexplained loss of funds — a stranded half-swap, a dropped transaction that landed later, a manual wallet action, or a hot-wallet key compromise — is silently written into the ledger as the new truth. The only remaining tripwire is the 20%/24h halt. There is also no cold-start recovery: state.init() sets SELL_ONLY only when mode is NULL (first run ever), so a crash mid-swap is followed by a restart straight back into NORMAL with stale state and no replay or reconcile-before-trading step.

**Where:** bot/tradebot/monitor.py:38-48; bot/tradebot/state.py:9, :36-41; grep shows RECON_FREEZE assigned nowhere; portfolio-state/SKILL.md Reconciliation and Cold start sections

**Fix:** Add on-chain and Coinbase position reconciliation against the positions table with the spec'd tolerances, set RECON_FREEZE plus an ops alert on a break, and record explained differences. At minimum, before overwriting each cash row, compare against the prior value and alert on any change not attributable to a logged fill.

## 71. [HIGH] The 24-hour halt series has no flow adjustment, so any manual transfer or top-up moves the risk denominator
*Lens: money-path*

**What:** risk-limits and portfolio-state both require the halt to run on flow-adjusted value so "a deposit never masks a loss and a withdrawal never fakes one", with a `cash_flows` table and per-sample adjustment. In the implementation, `sample_value(value, flows=0.0)` is only ever called as `state.sample_value(value)` — flows is always 0, there is no cash_flows table, and nothing detects deposits or withdrawals. Since reconcile_cash adopts live venue balances, every manual capital movement flows straight into the series. This matters concretely at $1,000: all cross-venue transfers are manual by design (exchange keys cannot withdraw), so moving $100 from Coinbase to Solana leaves a window where the funds have left one venue and not arrived at the other — a 10% instantaneous drop in measured portfolio value. Two such events, or one $200 move, trips the 20% EMERGENCY_HALT with no trading loss having occurred. Conversely a gas top-up or deposit raises the trailing peak for 24 hours, so a subsequent real drawdown is measured against an inflated high-water mark.

**Where:** bot/tradebot/state.py:167-179; bot/tradebot/monitor.py:66-73; bot/tradebot/risk.py:78-89; portfolio-state/SKILL.md "Deposit and withdrawal adjustment"

**Fix:** Add a cash_flows table, detect balance changes at reconciliation that no fill explains, classify them as flows, and subtract/add them cumulatively in the series that trailing_max and check_halt read. Until then, tell the operator to STOP the bot before any manual transfer.

## 72. [HIGH] Fees are recorded nowhere and are absent from cost basis, proceeds, and PnL
*Lens: money-path*

**What:** Both log_fill calls pass `fee_usd=None`; the orders table has no fee column; realized PnL is never written (journal.log_outcome is defined and called from nowhere, so outcomes.realized_pnl_usd is always empty). Cost basis is set to the raw notional and proceeds to an estimate, so every fee is invisible to the ledger, to the STATUS PnL line, and to the calibration loop that is supposed to grade forecasts. At phase-1 sizing this is not a rounding error: Coinbase's lowest-volume tier is roughly 1.2% taker each way, so a $5 round trip loses about $0.12 (2.4%) before any price movement; on-chain, Jupiter/Kyber route fees plus the hardcoded slippage tolerances plus Solana priority fees are all similarly unaccounted. The portfolio-state spec calls for per-lot `fees` and a `realized_pnl` table with a fees column; neither table exists. A person evaluating whether the strategy clears costs cannot do so from this journal, because the journal never recorded the costs.

**Where:** bot/tradebot/execution.py:103 and :166 `fee_usd=None`; bot/tradebot/journal.py:23-32, :132-134; portfolio-state/SKILL.md data model (`lots`, `realized_pnl`)

**Fix:** Populate fee_usd from the venue fill payload (Coinbase order fills carry commission) and from the actual gas spent on-chain, add fees into cost_basis_usd on entry and subtract from proceeds on exit, and write an outcomes/realized_pnl row on every close so cost-vs-edge is measurable.

## 73. [HIGH] REVOKE cannot un-whitelist any asset, and reports success anyway
*Lens: money-path*

**What:** Commands.handle uppercases the entire inbound message (`t = " ".join(text.upper().split())`) before splitting, so the argument to REVOKE is uppercased. Asset IDs are case-sensitive — Solana mints are base58 and Base addresses are EIP-55 checksummed — and whitelist.asset_id is a plain TEXT primary key with no COLLATE NOCASE, so `UPDATE whitelist SET revoked_ts=? WHERE asset_id=?` matches zero rows for every on-chain token. The command then unconditionally replies "Revoked <asset>. It will require approval again." The user believes they have disarmed an asset that is in fact still whitelisted and still buying automatically up to the 5% cap. Since approval whitelists an asset permanently with no expiry, REVOKE is the only per-asset kill switch, and it does not work; the remaining options are the portfolio-wide STOP and FLATTEN. The same uppercasing makes WHY's `LIKE '%arg%'` lookup lossy, though LIKE is case-insensitive so it degrades rather than fails.

**Where:** bot/tradebot/approval.py:48 and :68-70; bot/tradebot/state.py:19 (asset_id TEXT PRIMARY KEY), :148-153

**Fix:** Parse the command verb case-insensitively but preserve the original case of the argument (split the raw text, uppercase only parts[0] and approval codes). Make whitelist_revoke report how many rows it changed and alert honestly when it matched nothing.

## 74. [HIGH] Coinbase order precision, minimum sizes, and portfolio scoping are never queried
*Lens: money-path*

**What:** Nothing in the codebase fetches product metadata. `limit_buy` formats base_size as `f"{notional/limit_price:.8f}"` and limit_price as `f"{limit_price:.8g}"`, but Coinbase requires base_size to be a multiple of the product's base_increment and price a multiple of quote_increment; a mismatched value is rejected outright, so the first Coinbase order is likely to fail on precision rather than fill. base_min_size and quote_min_size are also never read, so there is no verification that the phase-1 $5 order clears the venue minimum for the chosen product — despite go-live and the config comment both describing phase 1 as "venue-minimum sizing", the $5 is a hardcoded constant unrelated to any venue's actual minimum. Separately, `COINBASE_PORTFOLIO = "HypeBot"` is defined in config and referenced nowhere: no retail_portfolio_id is passed to get_accounts or to any order call, so both balance reads and orders go to whatever the key's default portfolio is. `usdc_balance()` compounds this by summing every account with currency USD or USDC across `get_accounts(limit=250)` — if the key's scope is broader than the HypeBot portfolio, unrelated balances inflate total_value and therefore the 5% cap.

**Where:** bot/tradebot/exchanges/coinbase.py:24-30, :41-49, :63-71; bot/tradebot/config.py:20, :75; grep: COINBASE_PORTFOLIO used nowhere

**Fix:** Call get_product once per product, cache base_increment/quote_increment/base_min_size/quote_min_size, quantize size and price before submitting, and reject a ticket whose notional is under the venue minimum. Pass the HypeBot retail_portfolio_id explicitly on get_accounts and every order, and filter usdc_balance to that portfolio.

## 75. [HIGH] The gas-aware round-trip cost gate runs 3x looser than spec and excludes gas entirely
*Lens: money-path*

**What:** execution/SKILL.md sets max round-trip cost at 3% of position notional and defines it as "entry gas + exit gas + both swap fees + estimated slippage both ways". The implementation rejects only when `loss > config.ROUNDTRIP_COST_MAX * 3` — a 9% threshold, with an inline comment as the only record of the widening — and `loss` comes from exit_safety, which is a pure quote round trip that contains no gas at all. exit_safety's own ceiling is looser still at `2 * 0.03 + max_tax` = 16%. Concretely, a $5 Solana position can pass the gate while giving up 9% ($0.45) to spread and impact before fees, and it needs a 9%+ move just to break even; the hardcoded slippage tolerances make this likely rather than theoretical, since buys use 300 bps and sells 600 bps regardless of the asset's liquidity tier. exit_safety is also only ever measured at the increment notional, never the resulting total position, so a position built by three $5 adds has only ever had a $5 exit simulated — a 10x understatement at the $50 cap, exactly the case the liquidity-based position cap in risk-limits is meant to catch.

**Where:** bot/tradebot/execution.py:35-38; bot/tradebot/exchanges/solana_dex.py:84 and evm_dex.py:79; execution.py:75-76 (300 bps) and :140/:148 (600 bps); execution/SKILL.md gas-aware minimum table

**Fix:** Compute round-trip cost as quote loss plus estimated entry and exit gas, enforce it at the spec'd 3% (or record the user's deliberate change in the skill table, not a code comment), and run exit_safety against existing cost basis plus the new increment rather than the increment alone.

## 76. [MEDIUM] The halt series is never flow-adjusted, so a manual transfer reads as a crash
*Lens: spec-drift*

**What:** risk-limits hard limit 2 is defined on flow-adjusted value so 'a deposit never masks a loss and a withdrawal never fakes one', and portfolio-state owns the cash_flows table and the adjustment. Neither exists: state.sample_value has a flows parameter that every caller leaves at the 0.0 default, and there is no cash_flows table. Because reconcile_cash pulls live venue balances, any manual capital movement moves the series directly. Moving $200 off Coinbase, or a bridge/transfer that leaves funds in flight between the Solana and Base wallets for a few minutes, is a >20% move on a ~$880 measured book and fires EMERGENCY_HALT. Topping up capital silently raises the trailing peak and arms a false halt for the next 24 hours. This operator hand-moves USDC between chains, so it is a matter of when.

**Where:** bot/tradebot/state.py:167-173 (flows defaults to 0.0), bot/tradebot/monitor.py:69 (state.sample_value(value) - no flows argument), no cash_flows table in bot/tradebot/state.py:11-33

**Fix:** Add a cash_flows table plus a Telegram command (or a config file the operator edits) to declare a deposit/withdrawal, and subtract/add it in trailing_max and check_halt. Interim mitigation: before any manual capital move, STOP the bot and note that the 24h peak needs resetting afterwards.

## 77. [MEDIUM] capital-allocation is entirely prose: no targets, no drift detection, no transfer suggestions
*Lens: spec-drift*

**What:** The skill defines venue targets (60% hot wallets / 30% Coinbase / 10% reserve), per-chain splits (Solana 65 / Base 35), cash floors at 10% of venue allocation, and three rebalance triggers with a 12h cooldown. None of it exists in code: there are no target constants, no drift computation, no floor checks, and no rebalance message. The specified shortfall alert - 'NOT BOUGHT <asset>: $X short at <venue>. Suggest move $Y from <venue A>.' - is not what gets sent; alerts.not_bought emits only the gate name and detail, so an insufficient-cash block tells the user 'insufficient_cash, solana has 12.40' with no suggested transfer and no shortfall amount. The 'partial position above venue minimums can proceed' rule is also unimplemented. Since the bot cannot move funds itself, these suggestions are the only mechanism by which capital ever gets rebalanced - so opportunities will be missed silently at one venue while cash sits idle at another.

**Where:** bot/tradebot/alerts.py:57-58 (not_bought format), bot/tradebot/risk.py:66-67 (insufficient_cash reject text), grep: no allocation-target constants in bot/tradebot/config.py

**Fix:** Add the allocation table to config, compute drift in portfolio_tick, and emit the spec's suggested-transfer message on a cash-floor breach or a shortfall block, throttled to the 12h cooldown.

## 78. [MEDIUM] runtime's priority queues, budget controls, and failure fallbacks are absent
*Lens: spec-drift*

**What:** The runtime skill specifies three prioritized agent queues (held-position re-evaluations first, then COMING UP revalidations, then discovery), event-driven preemption on trigger/halt/approval/freeze, a daily model spend budget with an 80% alert, discovery-breadth degradation on budget exhaustion, a 60-second max model latency before fallback, and a configured secondary model endpoint. The agent is a single unconditional discovery loop on a 900-second sleep with none of that. Practical consequences: a held position never gets re-evaluated by the model (there is no queue to put it on), COMING_UP candidates are dropped by submit() with no alert and no monitoring so position-monitor's entire COMING UP half is dead, and API spend is uncontrolled at runtime - spend.sh is a manual sqlite query a human must remember to run, with no threshold and no alert.

**Where:** bot/tradebot/agent/runner.py:122-137 (single loop), bot/tradebot/agent/runner.py:102 (COMING_UP dropped), scripts/spend.sh (manual, no alerting), grep: no latency/fallback/budget code

**Fix:** Add a queue table the core writes re-evaluation requests into (2x reached, momentum exhaustion, COMING UP trigger met) and have the agent drain it before discovery. Add a daily spend accumulator from the existing agent_usage events with an 80% ops alert.

## 79. [MEDIUM] equities-constraints has no implementation and SETUP.md sends the operator after credentials nothing can use
*Lens: spec-drift*

**What:** The skill specifies a PDT day-trade counter in portfolio-state, settled-vs-unsettled cash tracking, GFV avoidance, LULD/news halt handling, SSR, and session-dependent order rules. None of it exists, there is no equities venue adapter, and no equities identifiers are representable (asset_id parses as cex: / solana: / base: only). This is arguably correct for now - capital-allocation keeps equities at 0% below $25,000 - but SETUP.md step 4 tells the operator to start the slow E*TRADE production-key process and open and fund an Alpaca account, which nothing in the codebase reads. Anyone picking this up will waste time on it, or worse, fund an account the bot cannot manage or exit.

**Where:** .agents/skills/equities-constraints/SKILL.md (no corresponding code), SETUP.md section 4, bot/tradebot/marketdata.py:46-49 (asset_id kinds)

**Fix:** Mark SETUP.md step 4 as deferred until the account clears $25k, and note in bot/README.md that equities are spec-only. Do not fund an equities venue while there is no adapter.

## 80. [MEDIUM] Gas floors are never checked, so a buy can spend the gas needed to exit it
*Lens: correctness*

**What:** capital-allocation requires each chain to hold native gas for 20 exits, blocks new buys on a chain below its floor, and calls running out of gas while holding positions an incident. config.GAS_EXITS_FLOOR = 20 is defined and read by nothing; solana_dex.sol_balance() and evm_dex.eth_balance() are called only from scripts/phase0.py. Nothing in _gates_buy, risk.check_buy, or the monitor looks at native balance. With ~$15 of gas split across two chains, a run of Base buys (each sending an approve tx plus a swap tx) can drain ETH to the point where the exit swap cannot be submitted — the position becomes unsellable, precisely the failure the exit-safety gate exists to prevent. There is also no low-gas ops alert.

**Where:** /home/user/clawpump-products/bot/tradebot/config.py:61 (GAS_EXITS_FLOOR, zero readers); /home/user/clawpump-products/bot/tradebot/execution.py:9-39; .agents/skills/capital-allocation/SKILL.md 'Gas floors'

**Fix:** Add a gate in _gates_buy that reads sol_balance()/eth_balance() (cached ~60s) and rejects the buy when native balance is below GAS_EXITS_FLOOR × a measured per-exit cost. Emit a throttled ops alert when any chain drops below 2x its floor.

## 81. [MEDIUM] No flow adjustment: any deposit or withdrawal moves the value series and can trip the 20% halt
*Lens: correctness*

**What:** portfolio-state specifies a flow_adjusted_value — deposits subtracted from later samples, withdrawals added back — so the halt measures trading losses rather than cash movement. state.sample_value takes a `flows` argument that every caller leaves at the 0.0 default; value_series.flows is always 0; there is no cash_flows table and no deposit/withdrawal detection. Because reconcile_cash overwrites the cash table from live venue balances every 5 minutes, any manual transfer flows straight into total_value. Concretely: moving the ~$5 stranded on Ethereum mainnet, topping up a gas float, or rebalancing between Solana and Base changes portfolio value with no trade behind it. A withdrawal of more than 20% of the account trips EMERGENCY_HALT instantly; a deposit inflates the trailing-24h peak so the real drawdown threshold sits in the wrong place for the next day.

**Where:** /home/user/clawpump-products/bot/tradebot/state.py:167-173 (flows always 0), :22 schema; /home/user/clawpump-products/bot/tradebot/monitor.py:69; .agents/skills/portfolio-state/SKILL.md:136

**Fix:** Have reconcile_cash diff each venue's balance against the last known balance minus recorded fills, record the unexplained delta as a flow, and pass it into sample_value. Until that exists, document the operational rule: put the bot in USER_STOP before moving any money between venues.

## 82. [MEDIUM] No systemd watchdog and no crash-loop stop — a hung core is undetectable and a crashing one restarts forever
*Lens: ops*

**What:** vps-ops specifies `WatchdogSec=60` with the bot notifying each main-loop cycle 'so a hung process is killed and restarted, not just a dead one', and a crash-loop threshold of 5 restarts in 10 minutes after which the service must stay down. Neither unit file has WatchdogSec, Type=notify, StartLimitIntervalSec or StartLimitBurst; both are `Type=simple` with `Restart=always`. Because RestartSec=10 exceeds systemd's default 10s start-limit interval, the default burst of 5 can never be reached — these units will restart indefinitely, which vps-ops explicitly forbids ('Do not fight a crash loop with more restarts'). And a hung-but-alive core (e.g. blocked in an RPC call, see the blocking-call finding) is invisible to systemd entirely. In practice the user's only crash-loop signal is repeated 'OPS: bot-core started' Telegram messages emitted at core.py:66.

**Where:** systemd/tradebot-core.service and tradebot-agent.service (no WatchdogSec/StartLimit*); bot/tradebot/core.py:66

**Fix:** Add `StartLimitIntervalSec=600` / `StartLimitBurst=5` to both units so a crash loop parks the service in `failed`, and either add `WatchdogSec=120` + `Type=notify` with an `sd_notify(WATCHDOG=1)` at the end of each healthy loop, or accept the gap and note it explicitly in the handoff.

## 83. [MEDIUM] Deposits and withdrawals will trip a false 20% emergency halt — the value series is not actually flow-adjusted
*Lens: ops*

**What:** The `value_series` table has a `flows` column and `sample_value(value, flows=0.0)` accepts one, but the single call site passes only the value, there is no `cash_flows` table (portfolio-state specifies one), and there is no Telegram command or any other path to record a deposit or withdrawal. `risk.check_halt` compares the raw current value against `trailing_max` over 24h. So on a $1,000 book: sweeping the ~$5 stranded Ethereum USDC in, topping up gas, moving $200 out, or rebalancing between Coinbase and the chains all register as portfolio drawdown. A $200 withdrawal is exactly -20% and fires EMERGENCY_HALT, which then needs a Telegram RESUME approval to clear. The reverse is just as bad: a deposit raises the trailing peak, making a later genuine halt fire early.

**Where:** bot/tradebot/state.py:187-193 (`sample_value`, flows defaults 0.0) and its only caller bot/tradebot/monitor.py:69 (`state.sample_value(value)`); bot/tradebot/risk.py:80-92

**Fix:** Before moving any capital in or out, send STOP first. Longer term: add a `FLOW +/-<usd>` Telegram command that writes a cash_flows row, and subtract cumulative flows inside the window from the halt comparison.

## 84. [MEDIUM] No log rotation config, no disk watchdog, and the journal database grows without bound
*Lens: ops*

**What:** vps-ops requires daily rotation of application logs and journald with 14-day retention, and a disk watchdog at 85% warn / 95% critical (critical pauses buying because 'state writes are at risk'). harden.sh installs no journald.conf drop-in and no logrotate config, so retention is whatever Ubuntu defaults to. grep for disk_usage/statvfs across bot/ and scripts/ returns nothing — the disk watchdog does not exist in any form. Meanwhile the DB only ever grows: `log_discovery` writes rows capped at 20 KB of payload, ~15 per 15-minute cycle (~1,400/day), plus events, exit_checks, orders and alerts, and trade-journal mandates indefinite retention so pruning is not an option. Only `value_series` is trimmed (8 days, state.py:190). A full disk is the trigger for the orphan-position failure described above, and nothing warns before it happens.

**Where:** grep for disk_usage/statvfs/logrotate across bot/, scripts/, systemd/ → no matches; bot/tradebot/journal.py:130-133 (`log_discovery`, 20000-char payload); bot/tradebot/state.py:190 (only value_series pruned)

**Fix:** Check current usage (`df -h /`, `du -sh /var/lib/tradebot`) and project growth; add a `shutil.disk_usage` check to the core loop that ops-alerts at 85% and sets SELL_ONLY at 95%; add /etc/systemd/journald.conf.d with SystemMaxUse and MaxRetentionSec=14d.

## 85. [MEDIUM] harden.sh and deploy.sh disagree on secret permissions; the bot user can read every credential including both wallet keys
*Lens: ops*

**What:** harden.sh creates /etc/tradebot as 0700 root:root with secrets.env 0600. deploy.sh then widens it to 0750 root:bot with secrets.env 0640 root:bot, and chmods both wallet keyfiles to 0640 root:bot even though gen_wallets.py deliberately wrote them 0600. vps-ops specifies 0600 root:root loaded via EnvironmentFile, and SETUP.md item 5 says 'Both keyfiles at mode 0600' — so the documented state and the deployed state differ, which a handoff reader will trust wrongly. secrets.env does not actually need the widening (systemd reads EnvironmentFile as root before dropping privileges); the wallet keys do, because the bot process opens them itself. Net effect: any code execution as `bot` — and this process parses untrusted JSON from DexScreener, Jupiter, KyberSwap and GeckoTerminal — yields the Coinbase key, the Telegram token, the Anthropic key and both wallet private keys.

**Where:** scripts/harden.sh:23-25 vs scripts/deploy.sh:31-35 and :44-45; scripts/gen_wallets.py:31,40 (chmod 0600); SETUP.md section 5

**Fix:** Revert secrets.env to 0600 root:root (systemd still loads it) and correct SETUP.md/vps-ops to state that the wallet keyfiles must be bot-readable. If tightening further is wanted, move secrets to systemd `LoadCredential=` so they are not in /proc/<pid>/environ.

## 86. [MEDIUM] SQLite durability and integrity are unverified: no busy_timeout/synchronous setting, no integrity check, and the obvious manual backup silently loses data
*Lens: ops*

**What:** `journal.conn()` opens WAL mode and nothing else — no explicit `busy_timeout` (relying on Python's 5s default, with two processes writing the same file), no explicit `synchronous`, so WAL's default NORMAL applies and a hard host reset (a Vultr reboot, a power event) can lose recently committed transactions — including fills. There is no `PRAGMA integrity_check` anywhere and no restore test, both of which vps-ops requires quarterly. And because the DB is in WAL mode, the intuitive recovery move — `cp /var/lib/tradebot/tradebot.db somewhere` — silently drops everything sitting in the -wal file, which is exactly the most recent activity. A person picking this up will almost certainly do that copy at some point.

**Where:** bot/tradebot/journal.py:70-77 (`conn()`: WAL only, no busy_timeout/synchronous); grep integrity_check/VACUUM across repo → no matches; .agents/skills/vps-ops/SKILL.md Backups steps 1 and 'Restore test'

**Fix:** Set `PRAGMA busy_timeout=30000` and `PRAGMA synchronous=FULL` in `conn()`, run `PRAGMA integrity_check` at startup and ops-alert on failure, and write into the handoff in bold: never `cp` the database — always `sqlite3 tradebot.db "VACUUM INTO '/path/snap.db'"`.

## 87. [MEDIUM] Telegram bot token and healthcheck UUID are written into the SQLite journal by exception logging
*Lens: security*

**What:** telegram._call builds every request as https://api.telegram.org/bot<TOKEN>/<method>. Both callers catch broad exceptions and journal str(e). requests' HTTPError and ConnectionError messages embed the full request URL ('401 Client Error: Unauthorized for url: https://api.telegram.org/bot8123...:AAF.../getUpdates'), so any 4xx, timeout, DNS blip, or proxy error writes the live bot token in plaintext into events.detail. heartbeat.ping does the same for HEALTHCHECK_URL, whose path segment IS the healthchecks.io secret — and the already-fixed 'heartbeat pinging a dead URL' incident is exactly the 404 path that produces that string, so that row is very likely already in the DB. vps-ops says secrets go 'never in trade-journal, never in logs, never in outbound messages'. Consequences: the DB is group-readable by the bot user, has no backups (so no rotation trail), and anyone holding the token can consume the owner's getUpdates stream — swallowing STOP/FLATTEN before the bot sees them — and send forged 'FLATTEN complete' messages to the owner.

**Where:** bot/tradebot/telegram.py:15 (URL with token), :17 raise_for_status, :34 and :53 journal.log_event(detail=str(e)); bot/tradebot/heartbeat.py:12-15; .agents/skills/vps-ops/SKILL.md Secrets section

**Fix:** On the VPS run: sqlite3 /var/lib/tradebot/tradebot.db "SELECT ts,kind,detail FROM events WHERE kind IN ('telegram_send_fail','telegram_poll_fail','heartbeat_fail')" and grep for 'bot' followed by digits+colon and for the hc-ping path. If any hit, rotate the token with @BotFather and regenerate the healthchecks check before anything else. Then wrap telegram._call and heartbeat.ping so the raised/logged message is scrubbed (catch, re-raise RuntimeError(f"telegram {method} {status}") with the URL stripped), and add a redaction filter over journal.log_event that replaces any occurrence of TELEGRAM_TOKEN/HEALTHCHECK_URL substrings.

## 88. [MEDIUM] Any Telegram user can write unbounded rows into the trading journal before the authorization check
*Lens: security*

**What:** Poller._dispatch calls journal.log_approval with raw_text=text[:500] and sender for every inbound update, and only afterwards compares sender to TELEGRAM_USER_ID. Telegram bots are reachable by anyone who knows the @username — no allowlist exists on the Telegram side. So an unauthenticated third party can append 500-byte rows to the approvals table at Telegram's rate limit, on the same SQLite file the core loop reads for positions, mode, tickets and pending approvals, contending on the single global journal._lock shared with the monitor thread that fires invalidation sells. There is no disk-usage watchdog (vps-ops specifies 85% warn / 95% pause-buying; nothing implements it) and no backup, so filling /var/lib/tradebot degrades or corrupts the only record of what the bot has done. The 'unregistered_sender' event is logged too, doubling the write amplification.

**Where:** bot/tradebot/telegram.py:74-78 (log_approval then the sender check); bot/tradebot/journal.py:11 (_lock shared with core/monitor); .agents/skills/vps-ops/SKILL.md stale-data watchdog table (disk 85/95%) — no implementation

**Fix:** Move the journal.log_approval call below the sender check; for unauthorized senders keep at most a rate-limited counter event (one row per sender per hour). Add the vps-ops disk watchdog to the core loop (statvfs on /var/lib/tradebot; warn at 85%, force SELL_ONLY at 95%). Check current damage with: sqlite3 tradebot.db "SELECT sender, COUNT(*) FROM approvals WHERE kind='inbound' GROUP BY sender".

## 89. [MEDIUM] No runtime verification that the Coinbase key still lacks withdrawal permission
*Lens: security*

**What:** vps-ops requires a per-venue key-permission query on every process start, with the venue forced to alert-only and an ops alert raised if withdrawal is enabled — that check is the stated reason the design tolerates an exchange key on a hot host at all ('withdrawal-disabled keys cap the damage'). Nothing in the code queries key scopes; phase0.py only proves the key can read a balance, which a transfer-enabled key also does. The key was created correctly per SETUP.md, but the control that keeps it correct across rotation or re-issue does not exist, and rotation is on a 90-day schedule per the same skill. Blast radius today with the key as documented (View+Trade, IP-allowlisted, HypeBot portfolio only): an attacker on the VPS can churn the ~$275 into losses via bad trades but cannot withdraw. With a mis-scoped rotation, it silently becomes withdrawal of the Coinbase portfolio.

**Where:** scripts/phase0.py:43 (balance check only); no reference to key permissions anywhere in bot/tradebot; .agents/skills/vps-ops/SKILL.md Secrets — 'On startup, query each venue for the key's permission set'

**Fix:** Add a startup check in core.main calling the CDP key-permissions endpoint (coinbase.rest get_api_key_permissions) and refuse to route Coinbase orders — set that venue alert-only plus an ops alert — when can_transfer/can_withdraw is true or the portfolio scope is wider than HypeBot. Add the same as a phase0 check so it is exercised before every deploy.

## 90. [MEDIUM] Approval-code hygiene: codes stored in the journal, no concurrent-pending cap, no post-rejection suppression, one-tap approve
*Lens: security*

**What:** vps-ops says never write approval codes to logs; approval codes are written to the journal at request time (log_approval(code=code)), again as the inbound raw_text of the user's 'YES A1B2C3' reply, and a third time in the alerts table which stores the full approval message body. Anything with read access to the DB — the bot user, the agent process, an unencrypted backup — sees live unexpired codes. That alone is not sufficient to trade (the code must still arrive from the registered Telegram account), but it removes a layer. Separately, approval-gate specifies a concurrent-pending cap of 5 and a post-rejection suppression window; neither is implemented, and submit() has no per-asset dedupe, so the same candidate can generate a fresh approval request every 15-minute cycle indefinitely. With a one-tap inline Approve button, that is a direct path to approval fatigue — which is the only control standing between an attacker-boosted token and a real buy. Codes are also 6 hex characters (24 bits, alphabet includes the 0/1 the skill excludes) rather than the specified 6-char alphanumeric excluding ambiguous glyphs.

**Where:** bot/tradebot/approval.py:13 (token_hex(3).upper()), :21-22 (log_approval with code), :81-90 (raw text logged on YES); bot/tradebot/alerts.py:42-49 with journal.log_alert at :26; .agents/skills/approval-gate/SKILL.md Codes/Expiry tables ('Concurrent pending-request cap | 5')

**Fix:** Stop persisting the code value: store a hash or the pending_approvals row id, and redact 'YES <code>'/'NO <code>' in raw_text and in alert bodies. Enforce the cap of 5 pending requests in request_buy_approval (reject and journal beyond it), add a per-asset suppression window after NO and after a filled buy, and switch new_code to a 6-char draw from the specified alphabet.

## 91. [MEDIUM] No gas-float monitoring anywhere in the running system — the exit path can become impossible without warning
*Lens: security*

**What:** GAS_EXITS_FLOOR (native-token float sized for 20 exits) and CHAIN_GAS_TOKEN are defined in config and used nowhere. sol_balance() and eth_balance() are called only from scripts/phase0.py — never by core, monitor, risk, or execution. Nothing checks native balance before an on-chain buy and nothing alerts when it runs down. The ~$15 gas allocation is split across chains, and every exit — including monitor's mechanical invalidation sells and FLATTEN — needs gas. If SOL or Base ETH is exhausted (failed swaps, the approve-then-swap pair on every Base trade, priority-fee spikes), sells start failing with only a journal row and an 'OPS: SELL FAILED' message, and the operator gets no earlier signal. This is the availability half of the security picture: the ability to exit is a safety control and it currently has no monitoring.

**Where:** bot/tradebot/config.py:61 (GAS_EXITS_FLOOR) and :78 (CHAIN_GAS_TOKEN) — zero uses outside config; sol_balance/eth_balance referenced only at scripts/phase0.py:44,46; bot/tradebot/execution.py:152-155 (sell failure path is alert-only)

**Fix:** Sample sol_balance() and eth_balance() inside monitor.reconcile_cash alongside the USDC balances, persist them, and add two thresholds: an ops alert below GAS_EXITS_FLOOR x estimated per-exit cost, and a buy gate rejecting on-chain entries when the native float would not cover the exit. Check the current floats on the box before any trade is allowed to execute.

## 92. [MEDIUM] The Anthropic key is the one credential with no scope, no IP allowlist, and no spend cap
*Lens: security*

**What:** Coinbase is trade-only and IP-allowlisted, and the wallets are bounded by holding only their allocation — but ANTHROPIC_API_KEY has none of those bounds. It sits in secrets.env (group-readable by bot), is loaded into both services' environments, and if stolen is usable from anywhere in the world against the owner's Anthropic account with no ceiling. config.py treats ANTHROPIC_WORKSPACE_ID as optional and it is absent from the deploy.sh secrets template, which suggests the key in use is not workspace-scoped. Against a $1,000 book with ~$2/day of legitimate API spend, this is plausibly the largest uncapped financial exposure on the host.

**Where:** bot/tradebot/config.py:21-24 (ANTHROPIC_WORKSPACE_ID optional); scripts/deploy.sh:37 (ANTHROPIC_API_KEY in template, no workspace id); systemd/tradebot-agent.service:10

**Fix:** Create a dedicated Anthropic workspace with a monthly spend limit sized to a few multiples of $2/day, issue a workspace-scoped key, set ANTHROPIC_WORKSPACE_ID in secrets.env, and revoke the current key. Track the key's spend in the weekly review alongside scripts/spend.sh.

## 93. [MEDIUM] Execution exception strings are broadcast to Telegram and journaled verbatim — a live landmine once a keyed RPC endpoint is configured
*Lens: security*

**What:** execute_sell sends 'SELL FAILED <asset>: <str(e)[:120]>' to Telegram and journals the untruncated string; execute_buy and monitor.reconcile_cash do the same on the journal side. SOLANA_RPC and BASE_RPC are operator-configurable through secrets.env, and the standard fix for the public-endpoint rate limits this bot has already hit is a keyed provider (Helius, Alchemy, QuickNode), whose credential lives in the URL path. web3 and requests exceptions embed that URL, so the first RPC failure after such an upgrade puts the provider key into both the journal and an outbound Telegram message. Today's defaults are keyless public endpoints, so nothing is leaking yet — which is exactly why this should be fixed before the endpoint changes rather than after.

**Where:** bot/tradebot/execution.py:92,94 and :153-154; bot/tradebot/monitor.py:48; bot/tradebot/config.py:33-34 (SOLANA_RPC / BASE_RPC from env)

**Fix:** Add one redaction helper in journal.py that strips any configured secret substring and any credential-bearing URL path segment, and route every str(e)/repr(e) in execution, monitor, marketdata and telegram through it before journaling or alerting. Note in SETUP.md section 5 that RPC upgrades must go through that helper.

## 94. [MEDIUM] Only 2 of ~15 candidates get any price history, and the shortlist is sorted by the wrong variable
*Lens: strategy-data*

**What:** AGENT_CANDLE_SHORTLIST defaults to 2. gather() sorts enriched candidates by liquidity_usd descending and fetches candles for the top 2. So roughly 13 of 15 candidates reach the model with no time series whatsoever — no momentum, no volatility, no support/resistance, no wave count (the wave-structure block in the system prompt self-disables for them: 'Apply only to candidates whose payload includes a candles series'). Beyond the count, the sort key is anti-correlated with the objective: highest liquidity means largest, most established, most efficiently priced — the candidates least likely to double in 72 hours. The two tokens that get the deepest analysis are systematically the two least likely to qualify. This also means the wave-structure feature (known uncertainty #5) is being evaluated on a sample chosen to make it useless.

**Where:** bot/tradebot/agent/runner.py:129-134 (sort by liquidity_usd desc, slice to AGENT_CANDLE_SHORTLIST); bot/tradebot/config.py:30 (default 2); bot/tradebot/agent/prompts.py:41-42 (wave block conditional on candles)

**Fix:** Change the shortlist sort key to a momentum/attention composite once priceChange and txns are in the payload (e.g. h1 price change x h1 txn count, or volume/liquidity turnover ratio). Raise AGENT_CANDLE_SHORTLIST to 5-6 and lower GECKO_MIN_GAP accordingly, or switch to GeckoTerminal's multi-pool endpoints to amortize the rate limit. Measure the added cycle latency against DISCOVERY_INTERVAL_SEC=900.

## 95. [MEDIUM] Coinbase discovery scans an arbitrary fixed 80 of ~700 products, and cex candidates reach the model as a single number
*Lens: strategy-data*

**What:** coinbase_movers() fetches the full product list then takes `usd[:80]` — the first 80 in whatever order the API returns, with no sort by volume, no rotation, no offset across cycles. The same ~11% slice is scanned forever and the rest of the Coinbase universe is permanently invisible. Then the candidate handed to the model is `{'chain': None, 'product': pid, 'chg24': 0.23}` — the stats response already in hand (last, open, high, low, volume, volume_30day) is discarded, so a Coinbase candidate arrives with no price at all. The schema still requires entry_price, buy_zone_lo and buy_zone_hi, so the model must invent them; execution gate 2 then checks `lo <= ref <= hi` against the real price and will reject essentially every such ticket as out_of_zone. Separately, the highest-value keyless catalyst feed for this bot is being computed and thrown away: /products is called every cycle, and a product_id that was not in yesterday's list is a new Coinbase listing — historically one of the few reliable 2x-in-days events on a CEX.

**Where:** bot/tradebot/marketdata.py:190 (usd[:80]); bot/tradebot/marketdata.py:193-196 (stats captured in raw); bot/tradebot/agent/runner.py:136-140 (candidate reduced to chg24); bot/tradebot/execution.py:20-22 (out_of_zone gate)

**Fix:** Include last/open/high/low/volume in the cex candidate dict and emit asset_id as 'cex:<PRODUCT-ID>' from code rather than relying on the model to construct it. Replace the [:80] slice with a rotating offset persisted in kv, or rank by 24h volume first. Add a listings diff: persist the product-id set each cycle and surface additions as a catalyst-labeled discovery input.

## 96. [MEDIUM] Only 3 of 18 skills reach the model, and it is never told its order size or tradable venues
*Lens: strategy-data*

**What:** prompts.system_prompt() loads short-horizon-research, signal-hygiene and wave-structure. The other fifteen skills — including execution, risk-limits, capital-allocation, go-live and market-data — never reach the model. The payload passes `go_live_phase: 1` as a bare integer with no accompanying definition, so the model has no way to know that phase 1 means every order is $5, that the hard position cap is 5%, that only Coinbase/Solana/Base are automatable, or that DexScreener/GeckoTerminal are its only feeds. This matters for the lens: position sizing, liquidity-adjusted execution potential and 'liquidity-adjusted execution potential' are explicit ranking criteria in the skill, and the model is reasoning about them blind. A model that believed it was sizing $50 would evaluate pool depth very differently than one sizing $5. Note also that .agents/skills/gatekeeper/SKILL.md is a 1-byte empty file, so the '18 skills' count is really 17 plus a stub.

**Where:** bot/tradebot/agent/prompts.py:35,38,44 (three _skill() calls); bot/tradebot/agent/runner.py:147-152 (go_live_phase as int); .agents/skills/gatekeeper/SKILL.md is 1 byte

**Fix:** Add an explicit constraints block to the cached system prefix: current order size in USD from risk.compute_size(), the venue/chain registry from config.VENUES, the hard caps, and the feed list. Keep it inside the cached prefix so it costs nothing per cycle. Either write gatekeeper/SKILL.md or delete the directory.

## 97. [MEDIUM] Correlation cap and fat-finger price check are both unreachable as wired
*Lens: strategy-data*

**What:** risk.check_buy accepts a `group` argument and enforces MAX_CORRELATION_GROUP_PCT against positions.correlation_group. execution.py calls check_buy with eight positional arguments and never passes group, and nothing anywhere writes positions.correlation_group (upsert_position's `group` param is never supplied). The correlation limit can therefore never bind. Separately, check_buy is called as `risk.check_buy(..., ref, ref, total_value, marks_fresh)` — ref_price and limit_price are the same value, so `dev = abs(limit - ref)/ref` is always 0 and the fat_finger_price check is a structural no-op. The actual limit price for Coinbase is computed later in execute_buy (ask * 1.0025) and never passes through the check. This matters to the research lens because a research layer that starts producing many correlated meme-coin candidates in one narrative — exactly what a boosts-driven universe produces — has no mechanism stopping the portfolio from concentrating into one trade expressed five ways.

**Where:** bot/tradebot/risk.py:50-54 (group branch) vs bot/tradebot/execution.py:25-26 (no group passed, ref twice); bot/tradebot/state.py:92-104 (group never written)

**Fix:** Have the model emit a correlation_group label (narrative/chain/launchpad) in the forecast schema, carry it on the ticket, store it on the position, and pass it into check_buy. Pass the real intended limit price into check_buy instead of ref twice, or delete the check so it stops reading as a control that exists.

## 98. [MEDIUM] Silent JSON truncation and a swallowed blind-price alert
*Lens: strategy-data*

**What:** Two small ones a maintainer should know. (1) research() sends `json.dumps(payload)[:60000]` — a hard character slice on serialized JSON. Current payloads land near 10KB so it does not bite today, but raising AGENT_CANDLE_SHORTLIST or widening the candidate fields (both recommended above) moves toward the limit, and the failure mode is silent: the model receives syntactically invalid JSON mid-object with no error raised anywhere. (2) monitor.check_positions() logs `monitor_blind` and `continue`s when a held asset has no price, with the comment 'treating as deteriorating' — but it sends no ops alert and takes no action, whereas position-monitor specifies 'Price feed lost on a held asset past staleness: ops alert; treat as deteriorating until coverage returns.' A position whose feed dies goes quiet rather than escalating.

**Where:** bot/tradebot/agent/runner.py:163 (payload truncation); bot/tradebot/monitor.py:13-15 (log without alert or action)

**Fix:** Replace the character slice with candidate-count trimming plus a journaled warning when trimming occurs. Add an alerts.ops() call on monitor_blind, throttled, and decide whether repeated blindness past STALE_PRICE_SEC should force an exit evaluation.

## 99. [MEDIUM] Gas floors are unenforced, gas is excluded from portfolio value, and the $5 on Ethereum mainnet is unreachable by any code path
*Lens: money-path*

**What:** capital-allocation requires each chain to hold gas for 20 exits, measured per chain, with a chain below its floor accepting no new buys and its positions treated as exit-impaired. `GAS_EXITS_FLOOR = 20` is in config and read by nothing; `solana_dex.sol_balance()` and `evm_dex.eth_balance()` are called only by scripts/phase0.py, never at runtime. No code checks that gas exists before initiating a swap, so the bot can spend its last SOL or ETH entering a position and then be unable to exit — and on Base each swap is two transactions (an unconditional `approve` plus the swap), so an exit costs double and can strand halfway with the approval burned. Native balances are also excluded from `total_value`, which only sums the USDC-denominated cash rows, so roughly $15 of gas is invisible to sizing and to the drawdown series. Ethereum mainnet is absent entirely: it is not in VENUES or CHAIN_GAS_TOKEN, has no RPC in config, and no venue key in the cash table, so the ~$5 of stranded USDC there is invisible to the ledger and unreachable by any automated path. It is recoverable manually — the EVM key at /etc/tradebot/evm_wallet.key yields the same address on mainnet — but the gas to move it likely exceeds its value.

**Where:** bot/tradebot/config.py:61, :77-78, :33-34; grep: GAS_EXITS_FLOOR, CHAIN_GAS_TOKEN, VENUES all unreferenced; bot/tradebot/exchanges/evm_dex.py:101-109; bot/tradebot/state.py:157-164

**Fix:** Add a pre-swap gas check that reads the native balance and blocks buys (never sells) on a chain below an estimated 20-exit floor, with an ops alert. Include native balances in total_value. Decide explicitly whether to write off the $5 on mainnet or sweep it by hand, and document that choice in the handoff so it is not mistaken for a leak later.

## 100. [MEDIUM] Swaps go through public RPCs with no MEV protection, no deadline, and an uncapped priority fee
*Lens: money-path*

**What:** execution/SKILL.md's DEX swap rules set the public-mempool ceiling at 0% ("always use private/MEV-protected RPC"), a 60-second swap deadline, and a priority fee capped at 0.5% of swap notional. None of the three is implemented. SOLANA_RPC defaults to api.mainnet-beta.solana.com and BASE_RPC to mainnet.base.org — both public endpoints — and the Base swap is broadcast with `send_raw_transaction` straight into the public mempool. No deadline parameter is passed to either Jupiter's swap build or Kyber's route/build, so a stale transaction can land arbitrarily late at a price nobody validated. Jupiter is called with `"prioritizationFeeLamports": "auto"`, which is uncapped; on a $5 order a congestion spike can consume a meaningful fraction of the notional. Base gas is likewise unbounded: `gasPrice = w3.eth.gas_price` with `estimate_gas * 1.2` and no ceiling. At $5 the absolute dollar exposure is small, but these are the settings that carry into phases 2-4.

**Where:** bot/tradebot/config.py:33-34; bot/tradebot/exchanges/solana_dex.py:98-108; bot/tradebot/exchanges/evm_dex.py:93-117; execution/SKILL.md DEX swap rules table

**Fix:** Point SOLANA_RPC and BASE_RPC at MEV-protected endpoints (Helius/Jito for Solana, a private Base relay), pass a deadline to both aggregators, and cap the priority fee and gasPrice as a share of swap notional before signing.

## 101. [MEDIUM] The fat-finger price check is a no-op and no slippage tiering, depth cap, or order splitting exists
*Lens: money-path*

**What:** _gates_buy calls `risk.check_buy(..., ref, ref, ...)`, passing the reference price as both ref_price and limit_price, so `dev = abs(limit - ref)/ref` is always exactly 0 and MAX_PRICE_DEVIATION can never trigger. The actual limit price used on Coinbase — `(ask or ref_price) * 1.0025`, derived from a different source (the Advanced Trade book) than ref (the public Exchange ticker) — is never validated against the reference at all, and silently falls back to ref*1.0025 when `best_price` returns no ask. The +0.25% markup is the "deep" tier constant applied unconditionally: SLIPPAGE_TIERS is defined in config and read by nothing, no liquidity-tier classification is performed anywhere, and for a thin altcoin a +0.25% limit is likely to rest unfilled — which, combined with the missing fill check, produces a phantom position. MAX_BOOK_DEPTH_SHARE (10% of book) and MAX_POOL_SHARE (1% of pool) are also unreferenced, and the entire child-order splitting section of the execution skill is unimplemented. At $5 the depth caps do not bind; at $50 on a thin pool they would have.

**Where:** bot/tradebot/execution.py:25-26, :69-70; bot/tradebot/risk.py:60-63; grep: SLIPPAGE_TIERS, MAX_BOOK_DEPTH_SHARE, MAX_POOL_SHARE unreferenced

**Fix:** Pass the real limit price into check_buy so the deviation check has something to compare, classify the liquidity tier from measured depth and set the slippage cap from SLIPPAGE_TIERS, and enforce the depth/pool share caps before the ramp raises order size.

## 102. [MEDIUM] SELL_ONLY and RECON_FREEZE are dead-end modes with no code path back to NORMAL
*Lens: money-path*

**What:** state.init sets SELL_ONLY on a cold start with an empty kv table. The only transition to NORMAL is in Commands._yes under `kind == "resume"`, and the only way to create a resume pending is request_resume_approval, which is reachable only from the RESUME command — which is guarded by `if state.get_mode() in ("USER_STOP", "EMERGENCY_HALT")`. So from SELL_ONLY (or RECON_FREEZE, once something sets it), RESUME replies "Nothing to resume" and there is no supported way to start trading short of hand-editing the kv row in SQLite. This compounds in the agent: runner.main only runs a cycle when mode is in (NORMAL, USER_STOP, EMERGENCY_HALT), so in SELL_ONLY the research layer stops entirely — no discovery, no forecasts, and a silent $0/day cost drop that looks like a healthy idle bot rather than a stuck one. Whoever inherits this should know the recovery procedure before the next unclean restart or DB rebuild.

**Where:** bot/tradebot/state.py:36-41; bot/tradebot/approval.py:61-65, :94-96; bot/tradebot/agent/runner.py:127

**Fix:** Allow RESUME from SELL_ONLY and RECON_FREEZE (gated on a clean reconciliation for the latter), and include both in the agent's run set so research continues while buying is blocked. Document the manual kv override in SETUP.md as the break-glass path.

## 103. [MEDIUM] BOUGHT alerts are throttled for 15 minutes per asset, so automatic adds can deploy capital silently
*Lens: money-path*

**What:** alerts._out force-sends only kinds in ("ops", "approval", "sell"). action_alert uses kind "action", so a second BOUGHT message for the same asset inside THROTTLE_SEC (900s) is dropped without delivery. Because approval whitelists an asset permanently and adds to whitelisted assets execute automatically with no approval, the bot can place several $5 adds in one 15-minute window and the user sees only the first. The user's mental model — set by go-live phase 1 as "first buy needs approval, adds are automatic" — depends on the adds at least being visible after the fact. Note the asymmetry: sells and blocked buys are force-sent, so the silent case is specifically capital going out.

**Where:** bot/tradebot/alerts.py:17-27, :34-39; bot/tradebot/execution.py:104

**Fix:** Force-send the "action" kind, or throttle on (kind, asset, notional) with a much shorter window, so every executed buy produces a message.

## 104. [MEDIUM] Approval-gate concurrency cap and post-rejection suppression are unimplemented, so a rejected asset can re-prompt every 15 minutes
*Lens: money-path*

**What:** approval-gate specifies a concurrent pending-request cap of 5 and a 6-hour post-rejection suppression window. Neither exists: request_buy_approval adds a pending row unconditionally, and _no marks the ticket rejected without recording any suppression, so the next research cycle can re-ticket the same asset and re-prompt immediately. Once tickets start flowing, the user can face an unbounded queue of live approval codes and repeated prompts for an asset they already declined — the classic path to reflexively tapping Approve. Related and smaller: codes are `secrets.token_hex(3).upper()`, which is 6 characters but drawn from 0-9A-F, including the `0` and `1` the skill deliberately excludes as confusable. That one fails safe (a mistyped code is simply not live).

**Where:** bot/tradebot/approval.py:12-25, :105-115; approval-gate/SKILL.md expiry table

**Fix:** Refuse to create a sixth pending approval, and record a rejected_until timestamp per asset that submit() or process_ticket honors for 6 hours.

## 105. [LOW] The gatekeeper skill ships empty and the repo README advertises it as installable
*Lens: spec-drift*

**What:** .agents/skills/gatekeeper/SKILL.md is a single newline - 1 byte. The repository's top-level README.md is not about the trading bot at all: it markets GATEKEEPER as a 12-check Solana token screen with an install path (npx skills add trelnar/clawpump-products) and a sample verdict output. Anyone installing this repo as a skill pack gets an empty skill that does nothing, under a README promising a scam screen. bot/README.md also says '17 skills' while .agents/skills holds 19 directories (18 trading skills plus the empty gatekeeper). For a handoff this is actively misleading about what the repo contains and what is safe to install.

**Where:** .agents/skills/gatekeeper/SKILL.md (1 byte, contains only a newline), README.md (GATEKEEPER product copy with install instructions), bot/README.md:1-2 ('17 skills')

**Fix:** Either restore the gatekeeper skill content or delete the directory and the README that advertises it, and correct the skill count in bot/README.md. Decide whether this repo is the trading bot or the gatekeeper product; it currently claims to be both.

## 106. [LOW] Single SSH key with no documented break-glass, and the wallet keys sit behind it
*Lens: ops*

**What:** harden.sh copies /root/.ssh/authorized_keys to /home/bot at first boot and sets `PermitRootLogin prohibit-password` with `MaxAuthTries 3` and fail2ban. SETUP.md's 'Add your Mac SSH key' item is still unchecked, which suggests exactly one key on one machine. Nothing in the repo documents a second key, a backup admin key, or the Vultr web-console path as break-glass. Because the wallet private keys exist only on this host (see the backup finding), losing that Mac key means losing the only route to ~$605 until the Vultr console is used — which then makes the Vultr account credentials the true single point of failure for the money, and that dependency is written down nowhere. fail2ban with maxretry=3/bantime=1h can also lock the operator out of their own box during a stressful incident if they fumble keys from a new machine.

**Where:** scripts/harden.sh:16-21 (key copy) and :26-35 (sshd drop-in, MaxAuthTries 3); SETUP.md section 2, unchecked SSH-key item

**Fix:** Add a second SSH public key (from a phone or a stored YubiKey) to both /root and /home/bot authorized_keys today, and write the Vultr-console break-glass steps plus who holds the Vultr account recovery into the handoff. Consider whitelisting a known-good IP in fail2ban.
