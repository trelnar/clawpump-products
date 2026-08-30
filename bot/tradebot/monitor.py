"""position-monitor skill: the mechanical fast path. Level crossings fire
sells immediately — no model call, no approval. Runs inside the core loop."""
import time

from . import config, execution, journal, marketdata, state


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


def portfolio_tick():
    """Sample flow-adjusted value; run the halt check; expire approvals."""
    assets = [p["asset_id"] for p in state.positions()]
    marks, fresh = marketdata.marks(assets)
    value = state.total_value(marks) if fresh or not assets else None
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
