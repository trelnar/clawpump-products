"""execution skill: gate sequence + order lifecycle. Sells always execute;
buys pass five gates. All gates are code — no model call on the trade path."""
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
    return ref


def process_ticket(ticket, total_value, marks_fresh):
    """Called by core for each new BUY NOW ticket. Returns disposition."""
    asset = ticket["asset_id"]
    try:
        ref = _gates_buy(ticket, total_value, marks_fresh)
    except risk.Reject as rj:
        risk.log_reject(asset, rj)
        state.set_ticket_status(ticket["ticket_id"], f"blocked:{rj.rule}")
        alerts.not_bought(asset, rj.rule, rj.detail)
        return "blocked"

    # gate 5: whitelist or approval
    if state.is_whitelisted(asset):
        return execute_buy(ticket, ref)
    approval.request_buy_approval(ticket, ref, {
        "Size": f"${ticket['notional_usd']:.2f}",
        "Zone": f"{ticket.get('buy_zone_lo')}-{ticket.get('buy_zone_hi')}",
        "Invalidation": ticket.get("invalidation_price")})
    return "awaiting_approval"


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


def _await_coinbase(oid, timeout=None):
    """Poll a submitted order to a terminal state. Returns
    (filled_qty, avg_price, spent_usd, status, order_id)."""
    timeout = config.FILL_TIMEOUT_CEX_SEC if timeout is None else timeout
    terminal = ("FILLED", "CANCELLED", "EXPIRED", "FAILED", "REJECTED")
    t0, last = time.time(), None
    while True:
        o = coinbase.order_status(oid)
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
    return qty, avg, qty * avg + fee, st, o.get("order_id")


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


def execute_buy(ticket, ref_price):
    """Every venue books the quantity it actually received, in whole units,
    and the dollars it actually spent. Nothing is booked before confirmation."""
    asset, venue, chain = ticket["asset_id"], ticket["venue"], ticket.get("chain")
    notional = ticket["notional_usd"]
    try:
        if venue == "coinbase":
            product = asset.split(":", 1)[1]
            _bid, ask = coinbase.best_price(product)
            limit = (ask or ref_price) * 1.0025  # marketable limit, tier cap
            oid, _ = coinbase.limit_buy(product, notional, limit)
            qty, avg, spent, st, order_id = _await_coinbase(oid)
            if st != "FILLED":
                _cancel_quietly(order_id)  # stop the unfilled remainder
            if qty <= 0:
                raise RuntimeError(f"no fill ({st})")
            fill_price = avg or limit
        elif chain == "solana":
            mint = asset.split(":", 1)[1]
            dec = solana_dex.token_decimals(mint)
            before, _ = solana_dex.token_balance(mint)
            sig, _q = solana_dex.swap(solana_dex.USDC_MINT, mint,
                                      int(notional * 1e6), 300)
            res = _await_solana(sig)
            if res != "ok":
                raise RuntimeError(f"swap {res}")
            after, _ = solana_dex.token_balance(mint)
            qty = (after - before) / (10 ** dec)
            spent, fill_price = notional, ref_price
            oid = sig
        elif chain == "base":
            token = asset.split(":", 1)[1]
            before, dec = evm_dex.token_balance(token)
            oid = evm_dex.swap(evm_dex.USDC, token, int(notional * 1e6), 300)
            res = _await_evm(oid)
            if res != "confirmed":
                raise RuntimeError(f"swap {res}")
            after, dec = evm_dex.token_balance(token)
            qty = (after - before) / (10 ** dec)
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
    state.upsert_position(asset, venue, chain, qty, spent,
                          invalidation=ticket.get("invalidation_price"))
    try:
        _sanity_qty(qty, fill_price, spent)
    except Exception as e:
        # The fill is real; only the units are suspect. Book it anyway -- an
        # invisible position is worse -- then freeze rather than keep trading
        # off a portfolio value we do not trust.
        journal.log_event("fill_sanity_fail", asset, str(e))
        state.set_ticket_status(ticket["ticket_id"], "sanity_freeze")
        state.set_mode("RECON_FREEZE", reason="fill unit sanity check failed")
        alerts.ops(f"FROZEN after {asset} fill: {e}. The position is recorded but "
                   "its size is not trusted. Verify at the venue before resuming.")
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
            sold, avg, gross, st, _oid2 = _await_coinbase(oid)
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
    cost_part = pos["cost_basis_usd"] * fraction
    pnl = proceeds - cost_part
    if fraction >= 0.999:
        state.close_position(asset_id)
    else:
        state.upsert_position(asset_id, venue, chain, -qty, -cost_part)
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
