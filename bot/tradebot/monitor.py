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
        time_stop_check(p)
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


def reconcile_positions():
    """Compare the books against what the venues actually hold, both ways.

    Alert only -- never trades on its own. Every orphan bug found in either
    audit failed in the same direction: real assets existing that the database
    does not know about, each becoming a permanent loss rather than a logged
    anomaly. This is the mechanism that would have caught them as anomalies."""
    from .exchanges import evm_dex, solana_dex
    from . import alerts
    problems = []
    booked = {p["asset_id"]: p for p in state.positions()}

    # 1. every book entry still backed by real tokens
    for asset, p in booked.items():
        chain = p.get("chain")
        if chain not in ("solana", "base"):
            continue
        addr = asset.split(":", 1)[1]
        try:
            raw, dec = (solana_dex.token_balance(addr) if chain == "solana"
                        else evm_dex.token_balance(addr))
        except Exception as e:
            journal.log_event("recon_position_fetch_fail", asset, str(e))
            continue
        held = raw / (10 ** dec) if dec else raw
        want = p["qty"] or 0
        if want <= 0:
            continue
        drift = abs(held - want) / want
        if drift > config.POSITION_DRIFT_PCT:
            problems.append(f"{asset}: books {want:.6g}, wallet {held:.6g} "
                            f"({drift:.0%} off)")

    # 2. tokens held that no position row claims
    try:
        for mint, amount in solana_dex.all_token_balances().items():
            if mint == solana_dex.USDC_MINT:
                continue
            if f"solana:{mint}" not in booked:
                problems.append(f"UNTRACKED solana:{mint}: wallet holds {amount:.6g}")
    except Exception as e:
        journal.log_event("recon_orphan_scan_fail", detail=str(e))

    if problems:
        journal.log_event("position_recon_mismatch", detail=problems)
        alerts.ops("POSITION MISMATCH — books disagree with the wallets:\n"
                   + "\n".join(problems[:10])
                   + "\nNo automatic action taken. Verify before trading.")
    return problems


def time_stop_check(p, now=None):
    """A position past its thesis window with no move is capital held against a
    prediction that did not happen. Flags for model reassessment; never sells
    on its own -- the strategy skill forbids a mechanical time exit."""
    now = time.time() if now is None else now
    entry_ts = p.get("entry_ts")
    if entry_ts is None:          # not `or` -- a 0 timestamp is a value, not a miss
        return False
    age = now - entry_ts
    limit = config.TIME_STOP_DEFAULT_SEC * config.TIME_STOP_SLACK
    if age < limit:
        return False
    key = f"timestop:{p['asset_id']}"
    if state.get_kv(key):
        return False
    state.set_kv(key, int(now))
    journal.log_event("time_stop", p["asset_id"], {"age_hours": round(age / 3600, 1)})
    state.set_kv(f"reeval:{p['asset_id']}", str(now))
    return True


_last_recon = [0.0]
_last_pos_recon = [0.0]


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
