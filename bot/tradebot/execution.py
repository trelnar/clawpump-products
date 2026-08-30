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


def execute_buy(ticket, ref_price):
    asset, venue, chain = ticket["asset_id"], ticket["venue"], ticket.get("chain")
    notional = ticket["notional_usd"]
    try:
        if venue == "coinbase":
            product = asset.split(":", 1)[1]
            bid, ask = coinbase.best_price(product)
            limit = (ask or ref_price) * 1.0025  # marketable limit, tier cap
            oid, _ = coinbase.limit_buy(product, notional, limit)
            qty = notional / limit
        elif chain == "solana":
            mint = asset.split(":", 1)[1]
            sig, q = solana_dex.swap(solana_dex.USDC_MINT, mint,
                                     int(notional * 1e6), 300)
            st = _await_solana(sig)
            if st != "ok":
                raise RuntimeError(f"swap {st}")
            qty = int(q["outAmount"])
            oid = sig
        elif chain == "base":
            token = asset.split(":", 1)[1]
            h = evm_dex.swap(evm_dex.USDC, token, int(notional * 1e6), 300)
            if evm_dex.confirm(h) == "failed":
                raise RuntimeError("swap failed")
            qty = notional / ref_price
            oid = h
        else:
            raise RuntimeError(f"venue {venue}/{chain} not automatable")
    except Exception as e:
        journal.log_event("buy_failed", asset, str(e))
        state.set_ticket_status(ticket["ticket_id"], "failed")
        alerts.not_bought(asset, "execution", str(e)[:120])
        return "failed"

    state.set_cash(venue if venue == "coinbase" else chain,
                   state.cash(venue if venue == "coinbase" else chain) - notional)
    state.upsert_position(asset, venue, chain, qty, notional,
                          invalidation=ticket.get("invalidation_price"))
    state.set_ticket_status(ticket["ticket_id"], "filled")
    journal.log_fill(client_oid=oid, asset_id=asset, side="buy", qty=qty,
                     price=ref_price, fee_usd=None, venue=venue or chain, tx_ref=oid)
    alerts.action_alert("BOUGHT", asset, ref_price, {"Size": f"${notional:.2f}"})
    return "filled"


def _await_solana(sig, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = solana_dex.confirm(sig)
        if st in ("confirmed", "finalized"):
            return "ok"
        if st == "failed":
            return "failed"
        time.sleep(3)
    return "timeout"


def execute_sell(asset_id, reason, fraction=1.0):
    """Sells are never gated. Prefer a worse fill over an unfilled exit."""
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
            proceeds = qty * price
        elif chain == "solana":
            mint = asset_id.split(":", 1)[1]
            raw, _dec = solana_dex.token_balance(mint)
            amt = int(raw * fraction)
            if amt <= 0:
                state.close_position(asset_id)
                return "dust"
            sig, q = solana_dex.swap(mint, solana_dex.USDC_MINT, amt, 600)
            _await_solana(sig)
            proceeds = int(q["outAmount"]) / 1e6
            oid = sig
        elif chain == "base":
            token = asset_id.split(":", 1)[1]
            raw, _dec = evm_dex.token_balance(token)
            amt = int(raw * fraction)
            oid = evm_dex.swap(token, evm_dex.USDC, amt, 600)
            proceeds = qty * price
        else:
            return "manual_only"
    except Exception as e:
        journal.log_event("sell_failed", asset_id, str(e))
        alerts.ops(f"SELL FAILED {asset_id}: {str(e)[:120]}. Manual action may be needed.")
        return "failed"

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
