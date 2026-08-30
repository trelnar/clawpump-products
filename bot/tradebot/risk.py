"""risk-limits skill: mechanical pre-order enforcement. Fail closed. The model
cannot argue past a rejection. Hard limits are module constants in config and
are not read from any runtime-editable source."""
import time

from . import config, journal, state


class Reject(Exception):
    def __init__(self, rule, detail=""):
        self.rule = rule
        self.detail = detail
        super().__init__(f"{rule}: {detail}")


def _deployed(positions):
    return sum(p["cost_basis_usd"] for p in positions)


def check_buy(asset_id, venue, chain, notional_usd, ref_price, limit_price,
              total_value, marks_fresh=True, group=None):
    """Every rule that can block a buy, evaluated in order. Raises Reject."""
    if state.get_mode() != "NORMAL":
        raise Reject("halt", f"mode={state.get_mode()}")
    if not marks_fresh:
        raise Reject("stale_data", "portfolio marks stale; sizing denominator unsafe")
    if total_value is None or total_value <= 0:
        raise Reject("stale_data", "portfolio value unavailable (fail closed)")

    positions = state.positions()
    pos = state.get_position(asset_id)
    existing_cost = pos["cost_basis_usd"] if pos else 0.0

    # hard limit 1: 5% cost-basis cap, adds included, worst-case at limit price
    if existing_cost + notional_usd > config.MAX_POSITION_PCT * total_value + 1e-9:
        raise Reject("hard_position_cap",
                     f"{existing_cost + notional_usd:.2f} > 5% of {total_value:.2f}")

    if not pos and len(positions) >= config.MAX_CONCURRENT_POSITIONS:
        raise Reject("max_concurrent", str(len(positions)))
    if _deployed(positions) + notional_usd > config.MAX_AGGREGATE_DEPLOYED_PCT * total_value:
        raise Reject("aggregate_deployed", "")
    venue_cost = sum(p["cost_basis_usd"] for p in positions if p["venue"] == venue)
    if venue_cost + notional_usd > config.MAX_PER_VENUE_PCT * total_value:
        raise Reject("per_venue_cap", venue)
    if chain:
        chain_cost = sum(p["cost_basis_usd"] for p in positions if p["chain"] == chain)
        if chain_cost + notional_usd > config.MAX_PER_CHAIN_PCT * total_value:
            raise Reject("per_chain_cap", chain)
    if group:
        g_cost = sum(p["cost_basis_usd"] for p in positions
                     if (p.get("correlation_group") or "") == group)
        if g_cost + notional_usd > config.MAX_CORRELATION_GROUP_PCT * total_value:
            raise Reject("correlation_cap", group)

    # fat-finger checks
    if notional_usd > config.MAX_SINGLE_ORDER_PCT * total_value:
        raise Reject("fat_finger_notional",
                     f"{notional_usd:.2f} > {config.MAX_SINGLE_ORDER_PCT:.1%} of portfolio")
    if ref_price and limit_price:
        dev = abs(limit_price - ref_price) / ref_price
        if dev > config.MAX_PRICE_DEVIATION:
            raise Reject("fat_finger_price", f"deviation {dev:.1%}")

    # venue cash
    if state.cash(venue) < notional_usd:
        raise Reject("insufficient_cash", f"{venue} has {state.cash(venue):.2f}")


def compute_size(total_value):
    """Position size: 5% cap scaled by go-live phase factor."""
    ph = state.phase()
    if ph <= 1:
        return config.PHASE1_ORDER_USD if ph == 1 else 0.0
    return config.MAX_POSITION_PCT * total_value * state.size_factor()


def check_halt(current_value):
    """Hard limit 2: flow-adjusted current value <= 80% of trailing-24h max."""
    peak = state.trailing_max(config.HALT_WINDOW_SEC)
    if peak is None or current_value is None:
        return False
    if current_value <= (1 - config.HALT_DRAWDOWN_PCT) * peak:
        if state.get_mode() == "NORMAL":
            state.set_mode("EMERGENCY_HALT",
                           reason=f"value {current_value:.2f} <= 80% of 24h peak {peak:.2f}")
            journal.log_event("emergency_halt", detail={"value": current_value, "peak": peak})
            return True
    return False


def log_reject(asset_id, reject: Reject):
    journal.log_event("risk_reject", asset_id, {"rule": reject.rule, "detail": reject.detail})
