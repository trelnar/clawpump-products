"""execution skill: gate sequence + order lifecycle. Sells always execute;
buys pass five gates. All gates are code — no model call on the trade path."""
import functools
import json
import threading
import time

from . import alerts, approval, config, journal, marketdata, ratchet, risk, state
from .exchanges import coinbase, evm_dex, solana_dex


def _gates_buy(ticket, total_value, marks_fresh):
    """Gates 1-4 (gate 5 = approval handled by caller). Raises risk.Reject."""
    # gate 1: halt mode NORMAL
    if state.get_mode() != "NORMAL":
        raise risk.Reject("halt", state.get_mode())
    # gate 2: stale data / ticket age. A ticket that was deferred is aged
    # from the moment it was first deferred: the wait must not eat the
    # window in which the operator can still answer the approval it leads
    # to. The zone check below still guards the price every pass.
    deferred_at = _deferred_at(ticket)
    age = time.time() - max(ticket["ts"], deferred_at or 0)
    if age > config.TICKET_MAX_AGE_SEC:
        if deferred_at:
            raise risk.Reject("waited_out", "its 5-minute move never settled within the "
                              f"{config.TICKET_MAX_AGE_SEC // 60} minutes the call was good for")
        raise risk.Reject("stale_ticket", f"age {int(age)}s")
    ref = marketdata.price(ticket["asset_id"])
    if ref is None:
        raise risk.Reject("stale_data", "no reference price")
    lo, hi = ticket.get("buy_zone_lo"), ticket.get("buy_zone_hi")
    if lo and hi and not (lo <= ref <= hi):
        raise risk.Reject("out_of_zone", f"price {ref} not in [{lo},{hi}]")
    # gate 2b: entry timing -- from the same read, no second call
    _entry_timing(ticket, marketdata.last_info(ticket["asset_id"]))
    # gate 3: risk-limits (includes fat-finger + cash)
    notional = ticket["notional_usd"]
    risk.check_buy(ticket["asset_id"], ticket["venue"], ticket.get("chain"),
                   notional, ref, ref, total_value, marks_fresh)
    # gate 3b: can we actually pay in this product's quote currency?
    if ticket["venue"] == "coinbase":
        quote = asset_quote_currency(ticket["asset_id"])
        try:
            avail = coinbase.quote_balance(quote)
        except Exception as e:
            raise risk.Reject("quote_balance_unknown", f"{quote}: {e}")
        if avail < notional:
            raise risk.Reject("wrong_quote_currency",
                              f"{quote} available {avail:.2f} < {notional:.2f}")
    # gate 4: exit-safety for tokens
    chain = ticket.get("chain")
    if chain in ("solana", "base"):
        mod = solana_dex if chain == "solana" else evm_dex
        addr = ticket["asset_id"].split(":", 1)[1]
        ok, reason, measured = mod.exit_safety(addr, notional)
        if not ok:
            raise risk.Reject("exit_safety", reason or "failed")
        # A buy quote and an immediate sell quote of what it returns: the
        # cost of being wrong before the market has moved at all. 9% was
        # allowed here; on a +30%/-15% trade that is a third of the win.
        loss = measured.get("roundtrip_loss", 0)
        if loss > config.ROUNDTRIP_LOSS_MAX:
            raise risk.Reject("roundtrip_cost",
                              f"{loss:.1%} > {config.ROUNDTRIP_LOSS_MAX:.0%}")
        _check_gas(chain)
    return ref


def _check_gas(chain):
    """Refuse to enter a chain that cannot pay for the way out. An empty gas
    wallet is otherwise discovered by the exit -- while already holding."""
    try:
        native = solana_dex.sol_balance() if chain == "solana" else evm_dex.eth_balance()
    except Exception as e:
        raise risk.Reject("gas_unknown", f"{chain}: {e}")
    floor = config.GAS_COST_PER_EXIT[chain] * config.GAS_EXITS_FLOOR
    if native < floor:
        raise risk.Reject(
            "gas_floor",
            f"{chain} has {native:.6g} {config.CHAIN_GAS_TOKEN[chain]}, "
            f"needs {floor:.6g} for {config.GAS_EXITS_FLOOR} exits")


def _defer_key(ticket):
    return f"defer:{ticket['ticket_id']}"


def _deferred_at(ticket):
    """When this ticket was first deferred, or None. The marker lives until
    the ticket reaches a terminal state (filled, failed, blocked)."""
    v = state.get_kv(_defer_key(ticket))
    try:
        return float(v) if v else None
    except (TypeError, ValueError):
        return None


def _post_gate_block(ticket):
    """The checks that sit between the market gates and the order: they are
    about THIS asset's recent history, and they must hold at the moment of
    the buy. process_ticket ran them once; the approved path did not run
    them at all, and a deferral makes 'the moment of the tap' a window of
    minutes in which the asset can stop out, be revoked, or be told NO.
    Returns (rule, detail) or None."""
    asset = ticket["asset_id"]
    age = ratchet.reentry_paused(asset)
    if age is not None:
        return "ratchet_reentry", {"min_ago": int(age / 60)}
    age = state.rejected_recently(asset)
    if age is not None:
        return "rejected_cooldown", {"rejected_min_ago": int(age / 60)}
    age = state.stopped_out_recently(asset)
    if age is not None:
        return "stopout_cooldown", {"stopped_min_ago": int(age / 60)}
    return None


def _entry_timing(ticket, info):
    """Not while it is being sold into, and not on the leg we already missed.

    The first AUTO buys of 2026-09-13 went in as their tokens were dumping:
    one stopped out two minutes after the fill, the other was 20% below its
    zone by the time the gates ran. Both were correct calls on a chart from a
    few minutes earlier. A 5-minute move of -5% or worse means the sellers
    are still there; +30% or more in five minutes is the move the thesis
    wanted, already happened. Either DEFERS: the ticket is left as it is and
    tried again on the next pass, until it ages out at TICKET_MAX_AGE_SEC."""
    if ticket.get("chain") not in ("solana", "base") or not info:
        return
    m5 = info.get("change_m5")
    if m5 is None:
        return
    if m5 <= -config.ENTRY_M5_MIN_PCT:
        raise risk.Reject("falling_knife",
                          f"down {abs(m5):.0f}% in the last 5 min", defer=True)
    if m5 >= config.ENTRY_M5_MAX_PCT:
        raise risk.Reject("spiking",
                          f"up {m5:.0f}% in the last 5 min", defer=True)


DEFERRED = object()   # _run_gates: 'not now'; the ticket is untouched


def _run_gates(ticket, total_value, marks_fresh):
    """Gates 1-4. Returns the reference price; DEFERRED when a gate asked for
    the next pass; or None having logged and alerted the rejection."""
    asset = ticket["asset_id"]
    try:
        ref = _gates_buy(ticket, total_value, marks_fresh)
    except risk.Reject as rj:
        if rj.defer:
            key = _defer_key(ticket)
            if not state.get_kv(key):
                state.set_kv(key, f"{time.time():.0f}")
                journal.log_event("ticket_deferred", asset,
                                  {"rule": rj.rule, "detail": rj.detail})
                alerts.ops(f"Not buying {alerts.symbol(asset)} yet: {rj.detail}. I look "
                           f"again every minute for up to {config.TICKET_MAX_AGE_SEC // 60} "
                           "min and carry on once it settles.")
            return DEFERRED
        risk.log_reject(asset, rj)
        state.set_ticket_status(ticket["ticket_id"], f"blocked:{rj.rule}")
        if rj.rule == "waited_out":
            alerts.ops(f"I didn't buy {alerts.symbol(asset)}: {rj.detail}.")
        else:
            alerts.not_bought(asset, rj.rule, rj.detail)
        state.del_kv(_defer_key(ticket))
        return None
    return ref


def process_ticket(ticket, total_value, marks_fresh):
    """Called by core for each new BUY NOW ticket. Returns disposition."""
    ref = _run_gates(ticket, total_value, marks_fresh)
    if ref is DEFERRED:
        return "deferred"
    if ref is None:
        return "blocked"
    # A ratchet winner stays whitelisted, so its pause must come before the
    # whitelist shortcut or it is unreachable for the only case it exists
    # for. A NO is an answer, not a request to ask again in 15 minutes. A
    # stop that just fired is the market's answer. None of these is a
    # question for the operator.
    blocked = _post_gate_block(ticket)
    if blocked:
        rule, detail = blocked
        state.set_ticket_status(ticket["ticket_id"], f"blocked:{rule}")
        journal.log_event(f"ticket_{rule}", ticket["asset_id"], detail)
        state.del_kv(_defer_key(ticket))
        return "blocked"
    # gate 5: whitelist or approval
    if state.is_whitelisted(ticket["asset_id"]):
        return execute_buy(ticket, ref)
    if state.auto_approve_active():
        # The operator's standing YES, bounded in time. The asset is whitelisted
        # exactly as a tap would, so the same TTL, re-entry cap and loss-revoke
        # govern it afterwards; a stop-out or a NO above already blocked it.
        state.whitelist_add(ticket["asset_id"], ticket.get("chain") or ticket["venue"])
        journal.log_event("auto_approved", ticket["asset_id"],
                          {"until": state.auto_approve_until()})
        alerts.ops(f"AUTO-approved {ticket['asset_id']} ${ticket['notional_usd']:.2f} "
                   f"(auto mode; NO is not possible, REVOKE <asset> withdraws it)")
        return execute_buy(ticket, ref)
    approval.request_buy_approval(ticket, ref, {
        "Size": f"${ticket['notional_usd']:.2f}",
        "Zone": f"{ticket.get('buy_zone_lo')}-{ticket.get('buy_zone_hi')}",
        "Invalidation": ticket.get("invalidation_price")})
    state.del_kv(_defer_key(ticket))   # the approval starts a fresh clock
    return "awaiting_approval"


def execute_approved(ticket, total_value, marks_fresh):
    """A tapped YES satisfies gate 5 and nothing else. Gates 1-4 -- halt mode,
    ticket staleness and buy zone, risk limits, exit-safety -- run again here
    against the state at the moment of the tap, which may be minutes and one
    STOP later than the alert that asked for it."""
    ref = _run_gates(ticket, total_value, marks_fresh)
    if ref is DEFERRED:
        return "deferred"
    if ref is None:
        return "blocked"
    asset = ticket["asset_id"]
    # The YES whitelisted the asset. If that grant is gone by the time the
    # order is placed -- a REVOKE while the ticket was deferred -- the tap
    # no longer stands. Same for a stop-out or a NO in the meantime.
    if not state.is_whitelisted(asset):
        state.set_ticket_status(ticket["ticket_id"], "blocked:revoked")
        journal.log_event("ticket_revoked", asset)
        state.del_kv(_defer_key(ticket))
        alerts.ops(f"Not buying {alerts.symbol(asset)}: its approval was withdrawn "
                   "before the order went in.")
        return "blocked"
    blocked = _post_gate_block(ticket)
    if blocked:
        rule, detail = blocked
        state.set_ticket_status(ticket["ticket_id"], f"blocked:{rule}")
        journal.log_event(f"ticket_{rule}", asset, detail)
        state.del_kv(_defer_key(ticket))
        return "blocked"
    return execute_buy(ticket, ref)


def _no_balance(asset_id, pos):
    """The wallet reports nothing where the books expect a holding.

    That is NOT proof the position is gone. It is equally what a lagging RPC
    read looks like, or a wrapped-SOL swap that unwrapped to the native
    balance, or simply the wrong mint. Deleting the row on that evidence is
    precisely how a real holding becomes invisible to every exit the bot has.
    Only genuine dust is closed; anything worth money is kept and escalated."""
    booked = pos.get("cost_basis_usd") or 0
    if booked <= config.DUST_USD:
        state.close_position(asset_id)
        return "dust"
    journal.log_event("sell_no_balance", asset_id,
                      {"booked_qty": pos.get("qty"), "booked_cost": booked})
    alerts.ops(f"{asset_id}: books show {pos.get('qty'):.6g} units (${booked:.2f}) but "
               f"the wallet reports none. The position is NOT closed -- check the "
               f"wallet before trading this asset again.")
    return "no_balance"


def clamp_fraction(frac):
    """Model-supplied fractions reach the sell path. Anything outside (0,1] is
    nonsense -- and a negative one used to round to zero base units, which the
    dust branch reads as 'nothing left' and DELETES a live position."""
    try:
        f = float(frac)
    except (TypeError, ValueError):
        return 1.0
    if f != f or f <= 0:      # NaN or non-positive
        return 1.0
    return min(f, 1.0)


def asset_quote_currency(asset_id):
    """cex:BTC-USDC -> USDC. What the order will actually be charged in."""
    product = asset_id.split(":", 1)[1]
    return product.rsplit("-", 1)[-1] if "-" in product else config.COINBASE_QUOTE


def _entry_liquidity(asset_id, chain):
    """position-monitor's liquidity-drain exit compares live pool depth against
    entry-time depth. Without a baseline recorded here it never fires."""
    if chain not in ("solana", "base"):
        return None
    try:
        info = marketdata.dexscreener_token(chain, asset_id.split(":", 1)[1])
        return info["liquidity_usd"] if info else None
    except Exception as e:
        journal.log_event("entry_liquidity_fail", asset_id, str(e))
        return None


def _sanity_qty(qty, price, spent):
    """C1 guard: a booked position must be worth roughly what we paid for it.
    Catches raw-base-units vs whole-token unit errors before they enter state
    and slacken every percentage limit that reads off portfolio value."""
    if qty is None or qty <= 0:
        raise RuntimeError("zero quantity filled")
    if not price or price <= 0 or not spent or spent <= 0:
        return  # nothing to compare against; caller already confirmed the fill
    implied = qty * price
    f = config.QTY_SANITY_FACTOR
    if implied > spent * f or implied < spent / f:
        raise RuntimeError(f"unit mismatch: {qty:.6g} @ {price:.6g} = ${implied:.2f} "
                           f"vs ${spent:.2f} paid")


def _await_coinbase(order_id, timeout=None):
    """Poll one order -- by the exchange's order_id, never a list read -- to a
    terminal state. Returns (filled_qty, avg_price, gross_usd, fee_usd, status).
    gross is the venue's quote-currency total before fees: a buy costs
    gross + fee, a sell yields gross - fee. Returning the sum for both sides
    overstated every Coinbase sell's proceeds by twice the fee."""
    timeout = config.FILL_TIMEOUT_CEX_SEC if timeout is None else timeout
    terminal = ("FILLED", "CANCELLED", "EXPIRED", "FAILED", "REJECTED")
    t0, last = time.time(), None
    while True:
        o = coinbase.order_status(order_id)
        if o:
            last = o
            if (o.get("status") or "").upper() in terminal:
                break
        if time.time() - t0 >= timeout:
            break
        time.sleep(2)
    o = last or {}
    qty = float(o.get("filled_size") or 0)
    avg = float(o.get("average_filled_price") or 0)
    fee = float(o.get("total_fees") or 0)
    st = (o.get("status") or "UNKNOWN").upper()
    # filled_value is the venue's own quote-currency total; prefer it to our
    # arithmetic, which loses the per-fill price mix on a multi-fill order.
    gross = float(o.get("filled_value") or 0) or qty * avg
    return qty, avg, gross, fee, st


def _cancel_quietly(order_id):
    if not order_id:
        return
    try:
        coinbase.cancel(order_id)
    except Exception as e:
        journal.log_event("cancel_failed", detail=f"{order_id}: {e}")


def _await_solana(sig, timeout=None):
    timeout = config.FILL_TIMEOUT_SOL_SEC if timeout is None else timeout
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = solana_dex.confirm(sig)
        if st in ("confirmed", "finalized"):
            return "ok"
        if st == "failed":
            return "failed"
        time.sleep(3)
    return "timeout"


def _await_evm(tx_hash, timeout=None):
    """confirm() reports 'unknown' while a tx is still pending -- treating that
    as success is how an unlanded swap became a phantom position."""
    timeout = config.FILL_TIMEOUT_EVM_SEC if timeout is None else timeout
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = evm_dex.confirm(tx_hash)
        if st in ("confirmed", "failed"):
            return st
        time.sleep(5)
    return "timeout"


def _landed_by_balance(read_raw, before, tx_ref):
    """Receipt polling timed out. Ask the wallet instead.

    The third Base round trip landed on-chain -- 0.002 WETH in the wallet, USDC
    down five dollars -- while the receipt endpoint answered 'unknown' for the
    full 180 seconds, so the buy was booked as a timeout and the tokens were
    orphaned with no position row. The receipt is a convenience; the balance
    is the fact. A raised balance means the swap landed, and we proceed as
    confirmed. An unchanged one after the retries means it genuinely did not."""
    for attempt in range(config.SETTLE_READ_TRIES):
        try:
            if read_raw() > before:
                journal.log_event("confirmed_by_balance", detail=str(tx_ref))
                return "ok"
        except Exception as e:
            journal.log_event("balance_read_fail", detail=str(e)[:120])
        if attempt + 1 < config.SETTLE_READ_TRIES:
            time.sleep(config.SETTLE_READ_SLEEP_SEC)
    raise RuntimeError(f"swap timeout and no tokens arrived ({tx_ref})")


def _settled_qty(read_raw, before, decimals, fallback_raw=0):
    """Quantity received by a swap that has ALREADY confirmed.

    Past this point the money is spent and the tokens are ours, so a failed or
    not-yet-visible balance read must never be reported as a fill of zero --
    that books nothing, tells the owner NOT BOUGHT, and orphans real tokens
    that no reconciliation path can ever find again. Retry, then fall back to
    the quote's own output and tell the caller the number is unmeasured."""
    last_err = None
    for attempt in range(config.SETTLE_READ_TRIES):
        try:
            after = read_raw()
            if after > before:
                return (after - before) / (10 ** decimals), True
        except Exception as e:
            last_err = e
        if attempt + 1 < config.SETTLE_READ_TRIES:
            time.sleep(config.SETTLE_READ_SLEEP_SEC)
    journal.log_event("settle_read_unresolved",
                      detail=f"{last_err}" if last_err else "balance unchanged")
    if fallback_raw > 0:
        return fallback_raw / (10 ** decimals), False
    raise RuntimeError(f"swap confirmed but quantity unreadable: {last_err}")


def _measure_proceeds(read_delta, read_balance, before, estimate):
    """Proceeds of a sell that has ALREADY confirmed, in order of trust.

    1. The transaction's own token-balance change (exact; immune to a lagging
       node and to any other flow on the wallet).
    2. The wallet balance, re-read until it rises above `before`.
    3. The quote's estimate, flagged unmeasured -- never a silent zero.

    A single balance read stood here. When it lagged the confirmation it
    booked $0 proceeds: a -100% exit that was not one, an approval revoked
    over it, and a realised PNL nobody could trust."""
    try:
        d = read_delta()
        if d is not None and d > 0:
            return float(d), True
    except Exception as e:
        journal.log_event("proceeds_tx_read_fail", detail=str(e)[:120])
    last_err = None
    for attempt in range(config.SETTLE_READ_TRIES):
        try:
            after = read_balance()
            if after > before:
                return after - before, True
        except Exception as e:
            last_err = e
        if attempt + 1 < config.SETTLE_READ_TRIES:
            time.sleep(config.SETTLE_READ_SLEEP_SEC)
    journal.log_event("proceeds_unresolved",
                      detail=f"{last_err}" if last_err else "balance unchanged")
    return float(estimate or 0), False


def repair_zero_proceeds(days=30):
    """Exits booked with $0 proceeds are re-read from their transaction.

    Before _measure_proceeds, a sell whose balance read lagged was booked as
    a total loss. The chain still has the truth: the sell's own transaction.
    Rewrites the exit_pnl row in place (keeping the old number), logs each
    correction, tells the operator once. Safe to run at every start."""
    since = time.time() - days * 86400
    rows = journal.query("SELECT event_id, ts, asset_id, detail FROM events "
                         "WHERE kind='exit_pnl' AND ts>? ORDER BY ts", (since,))
    parsed = []
    for r in rows:
        try:
            parsed.append((r, json.loads(r["detail"] or "{}")))
        except (TypeError, ValueError):
            continue
    # A transaction belongs to one exit. Never book the same one twice.
    used = {str(d.get("tx")) for _r, d in parsed if d.get("tx")}
    fixed = []
    for r, d in parsed:
        if d.get("repaired"):
            continue
        if (d.get("proceeds") or 0) > 0 and not d.get("unmeasured"):
            continue
        asset = r["asset_id"] or ""
        chain = asset.split(":", 1)[0]
        if chain not in ("solana", "base"):
            continue
        tx = d.get("tx")
        if not tx:
            # Rows from before the tx was journaled: the sell fill is written
            # right AFTER its exit row, so take the first one at or after it.
            f = journal.query("SELECT tx_ref FROM fills WHERE asset_id=? AND side='sell' "
                              "AND ts BETWEEN ? AND ? ORDER BY ts ASC LIMIT 1",
                              (asset, r["ts"] - 5, r["ts"] + 600))
            tx = f[0]["tx_ref"] if f else None
            if not tx or tx in used:
                journal.log_event("exit_repair_skipped", asset,
                                  "no sell fill to read" if not tx else f"tx already booked: {tx}")
                continue
            used.add(tx)
        try:
            if chain == "solana":
                delta = solana_dex.tx_token_delta(tx, solana_dex.USDC_MINT)
            else:
                delta = evm_dex.tx_token_delta(tx, evm_dex.USDC)
        except Exception as e:
            journal.log_event("exit_repair_fail", asset, str(e)[:120])
            continue
        if not delta or delta <= 0:
            # The chain agrees: nothing came back. That is a real total loss.
            journal.log_event("exit_repair_skipped", asset,
                              f"chain reports {delta} USDC from {tx}")
            continue
        cost = float(d.get("cost") or 0)
        old = float(d.get("pnl") or 0)
        new = delta - cost
        d.update({"proceeds": round(delta, 4), "pnl": round(new, 4),
                  "repaired": True, "pnl_before_repair": old, "tx": tx})
        d.pop("unmeasured", None)
        with journal._lock:
            journal.conn().execute("UPDATE events SET detail=? WHERE event_id=?",
                                   (json.dumps(d), r["event_id"]))
            journal.conn().commit()
        journal.log_event("exit_pnl_repaired", asset, {"was": old, "now": round(new, 4), "tx": tx})
        # The phantom loss put the asset in the stop-out cooldown; a real
        # non-loss lifts it. The approval it withdrew stays withdrawn.
        if float(d.get("share") or 1) >= 0.999 and new >= -config.LOSS_THRESHOLD_USD:
            state.del_kv(f"stopout:{asset}")
        fixed.append((asset, old, new))
    if fixed:
        money = lambda v: f"{'-' if v < 0 else '+'}${abs(v):.2f}"  # noqa: E731
        parts = ", ".join(f"{alerts.symbol(a)} was {money(o)}, actually {money(n)}"
                          for a, o, n in fixed)
        alerts.ops(f"I re-checked {len(fixed)} past sale(s) against the blockchain and "
                   f"fixed the numbers: {parts}. PNL is updated. Any approval I withdrew "
                   "over the old number stays withdrawn; the token will ask you again "
                   "when it is next proposed.")
    return fixed


# Orders come from two threads: the core loop (buys, monitor sells) and the
# Telegram poller (FLATTEN, and approvals before they moved to the core). With
# no mutex, FLATTEN could sell a position the monitor was mid-way through
# selling -- two exits for one holding, the second of which fails or, worse,
# partially fills against a stale quantity. One lock, held for the whole
# place-confirm-book sequence, means the second caller sees the books as the
# first left them. Cost: a FLATTEN waits for an in-flight order to settle.
_order_lock = threading.RLock()


def _serialised(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        with _order_lock:
            return fn(*a, **k)
    return wrapper


@_serialised
def execute_buy(ticket, ref_price):
    """Every venue books the quantity it actually received, in whole units,
    and the dollars it actually spent. Nothing is booked before confirmation."""
    asset, venue, chain = ticket["asset_id"], ticket["venue"], ticket.get("chain")
    notional = ticket["notional_usd"]
    entry_liq = _entry_liquidity(asset, chain)  # baseline before we move the pool
    measured = True
    fee = None                                   # venue fee when the venue reports one
    try:
        if venue == "coinbase":
            product = asset.split(":", 1)[1]
            _bid, ask = coinbase.best_price(product)
            limit = (ask or ref_price) * 1.0025  # marketable limit, tier cap
            oid, _ = coinbase.limit_buy(product, notional, limit)
            qty, avg, gross, fee, st = _await_coinbase(oid)
            spent = gross + fee
            if st != "FILLED":
                _cancel_quietly(oid)  # stop the unfilled remainder
            if qty <= 0:
                raise RuntimeError(f"no fill ({st})")
            fill_price = avg or limit
        elif chain == "solana":
            mint = asset.split(":", 1)[1]
            dec = solana_dex.token_decimals(mint)
            before, _ = solana_dex.token_balance(mint)
            sig, q = solana_dex.swap(solana_dex.USDC_MINT, mint,
                                     int(notional * 1e6), 300)
            res = _await_solana(sig)
            if res == "failed":
                raise RuntimeError("swap failed on-chain")
            if res != "ok":
                # The signature poll timed out. The wallet decides, not the
                # RPC: if the tokens arrived the swap landed, whatever the
                # status endpoint managed to say about it.
                res = _landed_by_balance(lambda: solana_dex.token_balance(mint)[0],
                                         before, sig)
            qty, measured = _settled_qty(
                lambda: solana_dex.token_balance(mint)[0], before, dec,
                fallback_raw=int((q or {}).get("outAmount") or 0))
            spent, fill_price = notional, ref_price
            oid = sig
        elif chain == "base":
            token = asset.split(":", 1)[1]
            before, dec = evm_dex.token_balance(token)
            oid = evm_dex.swap(evm_dex.USDC, token, int(notional * 1e6), 300)
            res = _await_evm(oid)
            if res == "failed":
                raise RuntimeError("swap reverted on-chain")
            if res != "confirmed":
                res = _landed_by_balance(lambda: evm_dex.token_balance(token)[0],
                                         before, oid)
            qty, measured = _settled_qty(
                lambda: evm_dex.token_balance(token)[0], before, dec)
            spent, fill_price = notional, ref_price
        else:
            raise RuntimeError(f"venue {venue}/{chain} not automatable")
        if qty is None or qty <= 0:
            raise RuntimeError("zero quantity filled")
    except Exception as e:
        journal.log_event("buy_failed", asset, str(e))
        state.set_ticket_status(ticket["ticket_id"], "failed")
        state.del_kv(_defer_key(ticket))
        alerts.not_bought(asset, "execution", str(e)[:120])
        return "failed"
    state.del_kv(_defer_key(ticket))

    cash_venue = venue if venue == "coinbase" else chain
    state.set_cash(cash_venue, state.cash(cash_venue) - spent)
    plan = ticket.get("plan")
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except (TypeError, ValueError):
            plan = None
    # The stop is set from the FILL, not from the research: STOP_LOSS_PCT below
    # it, or the model's invalidation when that is tighter. Under the 2x thesis
    # stops sat 30-40% down; the +30%/6h thesis needs 2:1.
    stop = fill_price * (1 - config.STOP_LOSS_PCT) if fill_price else None
    inv = max(ticket.get("invalidation_price") or 0, stop or 0) or None
    state.upsert_position(asset, venue, chain, qty, spent, entry_liq=entry_liq,
                          invalidation=inv, plan=plan)
    if plan and state.position_plan(asset) != plan:
        state.set_position_plan(asset, plan)  # an ADD carries a revised plan
    doubt = None
    if not measured:
        doubt = "quantity came from the quote, not a confirmed balance read"
    else:
        try:
            _sanity_qty(qty, fill_price, spent)
        except Exception as e:
            doubt = str(e)
    if doubt:
        # The fill is real; only our number for it is suspect. Book it anyway --
        # an invisible position is worse -- then freeze rather than keep trading
        # off a portfolio value we do not trust.
        journal.log_event("fill_not_trusted", asset, doubt)
        state.set_ticket_status(ticket["ticket_id"], "sanity_freeze")
        state.set_mode("RECON_FREEZE", reason=f"unverified fill: {doubt[:80]}")
        alerts.ops(f"FROZEN after {asset} fill: {doubt}. The position IS recorded and "
                   "its exits are armed, but its size is not trusted. Verify at the "
                   "venue, then RESUME.")
        return "sanity_freeze"
    state.set_ticket_status(ticket["ticket_id"], "filled")
    # What we actually paid per unit against the print we decided on. The
    # stop is set from the print (the monitor compares against prints); the
    # gap between the two is the entry half of the friction PNL reports.
    eff = (spent / qty) if (qty and spent) else None
    if eff and fill_price and venue != "coinbase":
        journal.log_event("buy_friction", asset, {
            "ref": fill_price, "eff": eff, "pct": round(eff / fill_price - 1, 4)})
    journal.log_fill(client_oid=oid, asset_id=asset, side="buy", qty=qty,
                     price=eff or fill_price, fee_usd=fee, venue=venue or chain, tx_ref=oid)
    alerts.bought(asset, spent, fill_price, inv,
                  "I bank 75% once +20% holds; the rest rides. Reviewed every 30 min.")
    return "filled"


@_serialised
def execute_sell(asset_id, reason, fraction=1.0):
    """Sells are never gated. Prefer a worse fill over an unfilled exit -- but
    an unconfirmed exit is not an exit: the position stays on the books."""
    pos = state.get_position(asset_id)
    if not pos:
        return "no_position"
    venue, chain = pos["venue"], pos["chain"]
    qty = pos["qty"] * fraction
    price = marketdata.price(asset_id) or 0
    fee = None
    measured = True
    try:
        if venue == "coinbase":
            product = asset_id.split(":", 1)[1]
            oid, _ = coinbase.market_sell(product, qty)
            sold, avg, gross, fee, st = _await_coinbase(oid)
            if sold <= 0:
                raise RuntimeError(f"no fill ({st})")
            qty, price = sold, (avg or price)
            proceeds = gross - fee
        elif chain == "solana":
            mint = asset_id.split(":", 1)[1]
            raw, dec = solana_dex.token_balance(mint)
            amt = int(raw * fraction)
            if amt <= 0:
                return _no_balance(asset_id, pos)
            qty = amt / (10 ** dec) if dec else qty   # what actually leaves
            before = solana_dex.usdc_balance()
            sig, q = solana_dex.swap(mint, solana_dex.USDC_MINT, amt, 600)
            res = _await_solana(sig)
            if res == "failed":
                raise RuntimeError("swap failed on-chain")
            if res != "ok":
                _landed_by_balance(lambda: solana_dex.usdc_balance(), before, sig)
            proceeds, measured = _measure_proceeds(
                lambda: solana_dex.tx_token_delta(sig, solana_dex.USDC_MINT),
                solana_dex.usdc_balance, before,
                int((q or {}).get("outAmount") or 0) / 1e6 or price * qty)
            oid = sig
        elif chain == "base":
            token = asset_id.split(":", 1)[1]
            raw, dec = evm_dex.token_balance(token)
            amt = int(raw * fraction)
            if amt <= 0:
                return _no_balance(asset_id, pos)
            qty = amt / (10 ** dec) if dec else qty
            before = evm_dex.usdc_balance()
            oid = evm_dex.swap(token, evm_dex.USDC, amt, 600)
            res = _await_evm(oid)
            if res == "failed":
                raise RuntimeError("swap reverted on-chain")
            if res != "confirmed":   # the USDC arriving is the fact, not the receipt
                _landed_by_balance(lambda: evm_dex.usdc_balance(), before, oid)
            proceeds, measured = _measure_proceeds(
                lambda: evm_dex.tx_token_delta(oid, evm_dex.USDC),
                evm_dex.usdc_balance, before, price * qty)
        else:
            return "manual_only"
    except Exception as e:
        journal.log_event("sell_failed", asset_id, str(e))
        alerts.ops(f"SELL FAILED {asset_id}: {str(e)[:120]}. Position still held; "
                   "manual action may be needed.")
        return "failed"

    if proceeds < 0:
        proceeds = 0.0  # a negative delta means someone else moved the cash
    if price and qty and venue != "coinbase" and measured:
        # the exit half of the friction: realised per unit vs the print sold on
        journal.log_event("sell_friction", asset_id, {
            "mid": price, "eff": proceeds / qty, "pct": round(proceeds / qty / price - 1, 4)})
    if not measured:
        alerts.ops(f"I sold {alerts.symbol(asset_id)} but could not confirm how much USDC "
                   f"came back. I've booked an estimate of ${proceeds:.2f} for now (PNL "
                   "will show it as an estimate); please check the wallet balance.")
    cash_venue = venue if venue == "coinbase" else chain
    state.set_cash(cash_venue, state.cash(cash_venue) + proceeds)
    # Book what actually left, not what we asked to leave. A partial fill on a
    # full exit must reduce the position, never delete it -- deleting orphans
    # the unsold tokens, and nothing reconciles positions back from the venue.
    held = pos["qty"] or 0.0
    if venue == "coinbase":
        sold_share = min(qty / held, 1.0) if held else 1.0
    else:
        # A swap is atomic: the requested share of the wallet balance left,
        # all of it. Booking by wallet units against booked units would turn
        # a rounding drift into a phantom residual position -- and a swap
        # that took every unit the books know of closes the position,
        # whatever fraction was asked for: what would remain is a negative
        # row that no monitor guards and the next buy lands on top of.
        sold_share = min(fraction, 1.0)
        if held and qty >= held * 0.999:
            sold_share = 1.0
    cost_part = pos["cost_basis_usd"] * sold_share
    pnl = proceeds - cost_part
    # Prior partial exits on this life, read BEFORE this exit's own row is
    # journaled: reading after it double-counted the closing slice, turning a
    # +$0.01 life into a "loss" that revoked the approval.
    prior_pnl = _life_pnl(asset_id, pos.get("entry_ts"))
    # One row per exit with the realised number: the PNL tally reads these.
    journal.log_event("exit_pnl", asset_id, {
        "pnl": round(pnl, 4), "proceeds": round(proceeds, 4), "cost": round(cost_part, 4),
        "share": round(sold_share, 4), "reason": reason[:60], "tx": str(oid),
        "entry_ts": pos.get("entry_ts"),
        **({} if measured else {"unmeasured": True})})
    if sold_share >= 0.999:
        # Judge the whole position life, not the closing slice: a ratchet that
        # banked +22% on 75% and a breakeven stop on the rest is a win.
        pnl_total = prior_pnl + pnl
        # -$0.000001 from fee rounding is not a loss. The first Solana round
        # trip withdrew an approval over "exited at a loss ($-0.00)".
        if config.WHITELIST_REAPPROVE_AFTER_LOSS and pnl_total < -config.LOSS_THRESHOLD_USD:
            state.whitelist_revoke(asset_id)
            state.note_stopout(asset_id)
            journal.log_event("whitelist_ended_on_loss", asset_id, {"pnl": round(pnl, 2)})
            alerts.ops(f"{asset_id} exited at a loss (${pnl:.2f}). Its approval is "
                       "withdrawn; a new buy will ask you again.")
        else:
            state.note_reentry(asset_id)
        ratchet.on_close(asset_id, pnl_total)
        state.close_position(asset_id)
    else:
        state.upsert_position(asset_id, venue, chain, -min(qty, held), -cost_part)
        journal.log_event("partial_exit", asset_id,
                          {"requested": fraction, "sold_share": round(sold_share, 4)})
    journal.log_fill(client_oid=oid, asset_id=asset_id, side="sell", qty=qty,
                     price=price, fee_usd=fee, venue=venue or chain, tx_ref=oid)
    left = state.get_position(asset_id)
    remaining_usd = (left["qty"] * price) if (left and price) else 0.0
    alerts.sold(asset_id, pnl, (pnl / cost_part) if cost_part else None, reason,
                sold_share, remaining_usd)
    return "filled"


def _life_pnl(asset_id, entry_ts):
    """Realised P&L already booked on this position life (partial exits)."""
    if entry_ts is None:
        return 0.0
    rows = journal.query("SELECT detail FROM events WHERE kind='exit_pnl' AND asset_id=? AND ts>?",
                         (asset_id, entry_ts))
    total = 0.0
    for r in rows:
        try:
            total += float(json.loads(r["detail"]).get("pnl") or 0)
        except (TypeError, ValueError):
            continue
    return total


def flatten_all():
    state.set_mode("USER_STOP", reason="FLATTEN")
    results = {}
    for p in state.positions():
        results[p["asset_id"]] = execute_sell(p["asset_id"], "FLATTEN", 1.0)
    alerts.ops(f"FLATTEN complete: {results}")
    return results
