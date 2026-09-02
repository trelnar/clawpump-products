"""execution skill: gate sequence + order lifecycle. Sells always execute;
buys pass five gates. All gates are code — no model call on the trade path."""
import json
import time

from . import alerts, approval, config, journal, marketdata, risk, state
from .exchanges import coinbase, evm_dex, solana_dex


def _gates_buy(ticket, total_value, marks_fresh):
    """Gates 1-4 (gate 5 = approval handled by caller). Raises risk.Reject."""
    # gate 1: halt mode NORMAL
    if state.get_mode() != "NORMAL":
        raise risk.Reject("halt", state.get_mode())
    # gate 2: stale data / ticket age
    if time.time() - ticket["ts"] > config.TICKET_MAX_AGE_SEC:
        raise risk.Reject("stale_ticket", f"age {int(time.time()-ticket['ts'])}s")
    ref = marketdata.price(ticket["asset_id"])
    if ref is None:
        raise risk.Reject("stale_data", "no reference price")
    lo, hi = ticket.get("buy_zone_lo"), ticket.get("buy_zone_hi")
    if lo and hi and not (lo <= ref <= hi):
        raise risk.Reject("out_of_zone", f"price {ref} not in [{lo},{hi}]")
    # gate 3: risk-limits (includes fat-finger + cash)
    notional = ticket["notional_usd"]
    risk.check_buy(ticket["asset_id"], ticket["venue"], ticket.get("chain"),
                   notional, ref, ref, total_value, marks_fresh)
    # gate 4: exit-safety for tokens
    chain = ticket.get("chain")
    if chain in ("solana", "base"):
        mod = solana_dex if chain == "solana" else evm_dex
        addr = ticket["asset_id"].split(":", 1)[1]
        ok, reason, measured = mod.exit_safety(addr, notional)
        if not ok:
            raise risk.Reject("exit_safety", reason or "failed")
        # gas-aware minimum: round-trip cost within cap of notional
        loss = measured.get("roundtrip_loss", 0)
        if loss > config.ROUNDTRIP_COST_MAX * 3:  # tolerance: quotes bundle slippage
            raise risk.Reject("roundtrip_cost", f"{loss:.1%}")
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


def _run_gates(ticket, total_value, marks_fresh):
    """Gates 1-4. Returns the reference price, or None having logged and
    alerted the rejection."""
    asset = ticket["asset_id"]
    try:
        return _gates_buy(ticket, total_value, marks_fresh)
    except risk.Reject as rj:
        risk.log_reject(asset, rj)
        state.set_ticket_status(ticket["ticket_id"], f"blocked:{rj.rule}")
        alerts.not_bought(asset, rj.rule, rj.detail)
        return None


def process_ticket(ticket, total_value, marks_fresh):
    """Called by core for each new BUY NOW ticket. Returns disposition."""
    ref = _run_gates(ticket, total_value, marks_fresh)
    if ref is None:
        return "blocked"
    # gate 5: whitelist or approval
    if state.is_whitelisted(ticket["asset_id"]):
        return execute_buy(ticket, ref)
    approval.request_buy_approval(ticket, ref, {
        "Size": f"${ticket['notional_usd']:.2f}",
        "Zone": f"{ticket.get('buy_zone_lo')}-{ticket.get('buy_zone_hi')}",
        "Invalidation": ticket.get("invalidation_price")})
    return "awaiting_approval"


def execute_approved(ticket, total_value, marks_fresh):
    """A tapped YES satisfies gate 5 and nothing else. Gates 1-4 -- halt mode,
    ticket staleness and buy zone, risk limits, exit-safety -- run again here
    against the state at the moment of the tap, which may be minutes and one
    STOP later than the alert that asked for it."""
    ref = _run_gates(ticket, total_value, marks_fresh)
    if ref is None:
        return "blocked"
    return execute_buy(ticket, ref)


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
    terminal state. Returns (filled_qty, avg_price, spent_usd, status)."""
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
    return qty, avg, gross + fee, st


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


def execute_buy(ticket, ref_price):
    """Every venue books the quantity it actually received, in whole units,
    and the dollars it actually spent. Nothing is booked before confirmation."""
    asset, venue, chain = ticket["asset_id"], ticket["venue"], ticket.get("chain")
    notional = ticket["notional_usd"]
    entry_liq = _entry_liquidity(asset, chain)  # baseline before we move the pool
    measured = True
    try:
        if venue == "coinbase":
            product = asset.split(":", 1)[1]
            _bid, ask = coinbase.best_price(product)
            limit = (ask or ref_price) * 1.0025  # marketable limit, tier cap
            oid, _ = coinbase.limit_buy(product, notional, limit)
            qty, avg, spent, st = _await_coinbase(oid)
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
            if res != "ok":
                raise RuntimeError(f"swap {res}")
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
            if res != "confirmed":
                raise RuntimeError(f"swap {res}")
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
        alerts.not_bought(asset, "execution", str(e)[:120])
        return "failed"

    cash_venue = venue if venue == "coinbase" else chain
    state.set_cash(cash_venue, state.cash(cash_venue) - spent)
    plan = ticket.get("plan")
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except (TypeError, ValueError):
            plan = None
    state.upsert_position(asset, venue, chain, qty, spent, entry_liq=entry_liq,
                          invalidation=ticket.get("invalidation_price"), plan=plan)
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
    journal.log_fill(client_oid=oid, asset_id=asset, side="buy", qty=qty,
                     price=fill_price, fee_usd=None, venue=venue or chain, tx_ref=oid)
    alerts.action_alert("BOUGHT", asset, fill_price, {"Size": f"${spent:.2f}"})
    return "filled"


def execute_sell(asset_id, reason, fraction=1.0):
    """Sells are never gated. Prefer a worse fill over an unfilled exit -- but
    an unconfirmed exit is not an exit: the position stays on the books."""
    pos = state.get_position(asset_id)
    if not pos:
        return "no_position"
    venue, chain = pos["venue"], pos["chain"]
    qty = pos["qty"] * fraction
    price = marketdata.price(asset_id) or 0
    try:
        if venue == "coinbase":
            product = asset_id.split(":", 1)[1]
            oid, _ = coinbase.market_sell(product, qty)
            sold, avg, gross, st = _await_coinbase(oid)
            if sold <= 0:
                raise RuntimeError(f"no fill ({st})")
            qty, price = sold, (avg or price)
            proceeds = gross
        elif chain == "solana":
            mint = asset_id.split(":", 1)[1]
            raw, _dec = solana_dex.token_balance(mint)
            amt = int(raw * fraction)
            if amt <= 0:
                state.close_position(asset_id)
                return "dust"
            before = solana_dex.usdc_balance()
            sig, _q = solana_dex.swap(mint, solana_dex.USDC_MINT, amt, 600)
            res = _await_solana(sig)
            if res != "ok":
                raise RuntimeError(f"swap {res}")
            proceeds = solana_dex.usdc_balance() - before
            oid = sig
        elif chain == "base":
            token = asset_id.split(":", 1)[1]
            raw, _dec = evm_dex.token_balance(token)
            amt = int(raw * fraction)
            if amt <= 0:
                state.close_position(asset_id)
                return "dust"
            before = evm_dex.usdc_balance()
            oid = evm_dex.swap(token, evm_dex.USDC, amt, 600)
            res = _await_evm(oid)
            if res != "confirmed":
                raise RuntimeError(f"swap {res}")
            proceeds = evm_dex.usdc_balance() - before
        else:
            return "manual_only"
    except Exception as e:
        journal.log_event("sell_failed", asset_id, str(e))
        alerts.ops(f"SELL FAILED {asset_id}: {str(e)[:120]}. Position still held; "
                   "manual action may be needed.")
        return "failed"

    if proceeds < 0:
        proceeds = 0.0  # a negative delta means someone else moved the cash
    cash_venue = venue if venue == "coinbase" else chain
    state.set_cash(cash_venue, state.cash(cash_venue) + proceeds)
    # Book what actually left, not what we asked to leave. A partial fill on a
    # full exit must reduce the position, never delete it -- deleting orphans
    # the unsold tokens, and nothing reconciles positions back from the venue.
    held = pos["qty"] or 0.0
    sold_share = min(qty / held, 1.0) if held else 1.0
    cost_part = pos["cost_basis_usd"] * sold_share
    pnl = proceeds - cost_part
    if sold_share >= 0.999:
        state.close_position(asset_id)
    else:
        state.upsert_position(asset_id, venue, chain, -qty, -cost_part)
        journal.log_event("partial_exit", asset_id,
                          {"requested": fraction, "sold_share": round(sold_share, 4)})
    journal.log_fill(client_oid=oid, asset_id=asset_id, side="sell", qty=qty,
                     price=price, fee_usd=None, venue=venue or chain, tx_ref=oid)
    alerts.sell_alert(asset_id, price, reason,
                      pnl_pct=(pnl / cost_part) if cost_part else None)
    return "filled"


def flatten_all():
    state.set_mode("USER_STOP", reason="FLATTEN")
    results = {}
    for p in state.positions():
        results[p["asset_id"]] = execute_sell(p["asset_id"], "FLATTEN", 1.0)
    alerts.ops(f"FLATTEN complete: {results}")
    return results
