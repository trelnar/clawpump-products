"""position-monitor skill: the mechanical fast path. Level crossings fire
sells immediately — no model call, no approval. Runs inside the core loop."""
import time

from . import config, execution, journal, marketdata, state


def entry_price(p):
    """Cost basis per whole unit. Partial exits scale qty and cost together, so
    this stays the original entry across a scale-out."""
    return (p["cost_basis_usd"] / p["qty"]) if p.get("qty") else None


def run_profit_plan(p, price):
    """A standing scale-out fires mechanically -- position-monitor requires the
    fast path to stay armed while the model layer is thinking. Fractions are of
    the REMAINING position, and each leg fires at most once."""
    asset = p["asset_id"]
    entry = entry_price(p)
    if not entry:
        return False
    legs = state.position_plan(p).get("profit_plan") or []
    done = state.plan_legs_done(asset)
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        mult, frac = leg.get("multiple"), leg.get("sell_fraction")
        if not mult or not frac:
            continue
        try:
            key = state.leg_key(leg)
        except (TypeError, ValueError, KeyError):
            continue
        if key in done or price < entry * mult:
            continue
        journal.log_event("profit_plan_leg", asset,
                          {"leg": key, "multiple": mult, "fraction": frac, "price": price})
        result = execution.execute_sell(
            asset, f"standing plan: {frac:.0%} of remainder at {mult}x",
            execution.clamp_fraction(frac))
        if result in ("filled", "dust", "no_position"):
            state.mark_plan_leg_done(asset, leg)   # no-op once the position is gone
        return True  # at most one leg per tick; re-evaluate on the next pass
    return False


def check_positions():
    """One monitoring pass over every held position."""
    for p in state.positions():
        asset = p["asset_id"]
        price = marketdata.price(asset)
        if price is None:
            journal.log_event("monitor_blind", asset, "no price; treating as deteriorating")
            continue
        inv = p.get("invalidation_price")
        if inv and price <= inv:
            journal.log_event("invalidation_cross", asset, {"price": price, "inv": inv})
            execution.execute_sell(asset, f"invalidation {inv} crossed at {price}")
            continue
        if run_profit_plan(p, price):
            continue
        # liquidity deterioration on tokens
        if p.get("chain") in ("solana", "base") and p.get("entry_liquidity_usd"):
            addr = asset.split(":", 1)[1]
            info = marketdata.dexscreener_token(p["chain"], addr)
            if info:
                drop = 1 - info["liquidity_usd"] / p["entry_liquidity_usd"]
                if drop >= config.LIQ_DRAIN_EXIT:
                    journal.log_event("liquidity_drain", asset, {"drop": drop})
                    execution.execute_sell(asset, f"pool liquidity down {drop:.0%}")
                elif drop >= config.LIQ_DRAIN_WARN:
                    journal.log_event("liquidity_warn", asset, {"drop": drop})
                    state.set_kv(f"reeval:{asset}", str(time.time()))


_last_recon = [0.0]


def reconcile_cash():
    """portfolio-state reconciliation (v1): pull venue balances into the cash
    table. Per-venue failures are logged, never fatal."""
    from .exchanges import coinbase, evm_dex, solana_dex
    for venue, fn in (("coinbase", coinbase.usdc_balance),
                      ("solana", solana_dex.usdc_balance),
                      ("base", evm_dex.usdc_balance)):
        try:
            state.set_cash(venue, fn())
        except Exception as e:
            journal.log_event("recon_fetch_fail", detail=f"{venue}: {e}")


def portfolio_value():
    """Read-only portfolio value for gate checks: no sampling, no
    reconciliation, no effect on the halt series."""
    assets = [p["asset_id"] for p in state.positions()]
    marks, fresh = marketdata.marks(assets)
    value = state.total_value(marks) if fresh or not assets else None
    if value is not None and value <= 0:
        value = None
    return value, fresh


def portfolio_tick():
    """Sample flow-adjusted value; run the halt check; expire approvals."""
    if time.time() - _last_recon[0] >= config.RECON_INTERVAL_SEC or _last_recon[0] == 0:
        import threading
        w = threading.Thread(target=reconcile_cash, daemon=True)
        w.start()
        w.join(timeout=45)
        if w.is_alive():
            journal.log_event("recon_timeout", detail="venue call exceeded 45s; loop continues")
        _last_recon[0] = time.time()
    assets = [p["asset_id"] for p in state.positions()]
    marks, fresh = marketdata.marks(assets)
    value = state.total_value(marks) if fresh or not assets else None
    if value is not None and value <= 0:
        value = None  # uninitialized or all-fetch-failed; never sample $0 as real
    if value is not None:
        state.sample_value(value)
        from . import risk
        if risk.check_halt(value):
            from . import alerts
            peak = state.trailing_max(config.HALT_WINDOW_SEC)
            alerts.ops(f"HALT. Portfolio {value:.2f} vs 24h peak {peak:.2f} (-20%). "
                       f"Buying stopped; selling continues. Reply RESUME to restart.")
    state.expire_pendings()
    return value, fresh
