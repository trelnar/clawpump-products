"""alert-format skill: message templates + throttle/dedupe. ASCII conventions
(2x not 2×). Line 1 carries action + asset + price."""
import time

from . import config, journal

_last_sent = {}   # (kind, asset) -> ts
THROTTLE_SEC = 900
_send_fn = None   # set by telegram module


def bind_sender(fn):
    global _send_fn
    _send_fn = fn


def _out(kind, body, asset_id=None, buttons=None, force=False):
    key = (kind, asset_id)
    if not force and kind not in ("ops", "approval", "sell"):
        if time.time() - _last_sent.get(key, 0) < THROTTLE_SEC:
            return False
    _last_sent[key] = time.time()
    delivered = 0
    if _send_fn:
        delivered = 1 if _send_fn(body, buttons=buttons) else 0
    journal.log_alert(kind, body, asset_id, delivered)
    return bool(delivered)


def ops(text):
    return _out("ops", f"OPS: {text}", force=True)


def action_alert(action, asset, price, fields):
    lines = [f"{action} {asset} @ {price}"]
    for k, v in fields.items():
        if v not in (None, ""):
            lines.append(f"{k}: {v}")
    return _out("action", "\n".join(lines), asset_id=asset)


def approval_request(code, action, asset, price, fields, expiry_min):
    lines = [f"{action} {asset} @ {price} - code {code}"]
    for k, v in fields.items():
        if v not in (None, ""):
            lines.append(f"{k}: {v}")
    lines.append(f"Reply YES {code} / NO {code} (expires {expiry_min} min)")
    buttons = [[("Approve", f"YES {code}"), ("Reject", f"NO {code}")]]
    return _out("approval", "\n".join(lines), asset_id=asset, buttons=buttons, force=True)


def sell_alert(asset, price, reason, pnl_pct=None):
    p = f" ({pnl_pct:+.0%})" if pnl_pct is not None else ""
    return _out("sell", f"SELL NOW {asset} @ {price}{p}\nReason: {reason}", asset_id=asset, force=True)


# --- plain-English trade messages (operator request 2026-09-13) --------------
_symbol_cache = {}


def symbol(asset_id):
    """A name a person recognises: the token's ticker, or the Coinbase base
    currency. Cached in kv so a closed position still reads by name."""
    if asset_id in _symbol_cache:
        return _symbol_cache[asset_id]
    from . import state
    kind, _, ident = asset_id.partition(":")
    sym = state.get_kv(f"symbol:{asset_id}")
    if not sym:
        if kind in ("cex", "perp"):
            sym = ident.split("-")[0]
        else:
            try:
                from . import marketdata
                info = marketdata.dexscreener_token(kind, ident)
                sym = (info or {}).get("base_symbol") or ""
            except Exception:
                sym = ""
        sym = (sym or ident[:6]).strip()
        state.set_kv(f"symbol:{asset_id}", sym)
    _symbol_cache[asset_id] = sym
    return sym


def plain_reason(reason):
    """Why a sell happened, in the operator's words."""
    r = (reason or "").lower()
    if "invalidation" in r:
        return "it hit the stop"
    if "ratchet take" in r:
        return "it spiked and turned, so I took the profit"
    if "ratchet floor" in r:
        return "the run faded, so I banked it"
    if "ratchet stall" in r:
        return "it went cold, so I banked it"
    if "standing plan" in r:
        return "it reached the planned scale-out level"
    if "agent sell" in r:
        return "the research layer called the exit"
    if "flatten" in r:
        return "you asked me to flatten"
    if "liquidity" in r:
        return "the pool was draining"
    return reason or "exit"


def bought(asset, spent, price, stop, exits):
    sym = symbol(asset)
    stop_pct = f" ({(stop / price - 1):+.0%})" if (stop and price) else ""
    body = (f"I bought {sym} for ${spent:.2f} at {price:.4g}.\n"
            f"Exit plan: stop at {stop:.4g}{stop_pct}" if stop else
            f"I bought {sym} for ${spent:.2f} at {price:.4g}.\nExit plan: no stop set")
    if exits:
        body += "; " + exits
    return _out("action", body, asset_id=asset)


def shorted(asset, notional, price, stop):
    sym = symbol(asset)
    body = (f"I shorted {sym} for ${notional:.2f} at {price:.4g} (1x).\n"
            f"Exit plan: stop at {stop:.4g} ({stop / price - 1:+.0%}); I cover 75% once "
            f"-{config.HL_ARM_PCT:.0%} holds and it bounces; out at {config.HL_MAX_HOLD_SEC // 3600}h regardless.")
    return _out("action", body, asset_id=asset)


def covered(asset, pnl_usd, pnl_pct, reason, fraction, remaining_usd):
    sym = symbol(asset)
    verb = "a gain" if pnl_usd >= 0 else "a loss"
    part = "" if fraction >= 0.999 else f" {fraction:.0%} of"
    r = (reason or "").lower()
    why = ("it hit the stop" if "stop" in r else
           "it bounced off the low, so I banked it" if "ratchet floor" in r else
           "it capitulated and turned, so I took the profit" if "ratchet take" in r else
           "the thesis window ran out" if "window" in r else
           "you asked me to flatten" if "flatten" in r else reason)
    body = (f"I covered{part} my {sym} short for {verb} of ${abs(pnl_usd):.2f}"
            + (f" ({pnl_pct:+.0%})" if pnl_pct is not None else "") + f" because {why}.")
    if fraction < 0.999 and remaining_usd:
        body += f" Still short about ${remaining_usd:.2f} of it."
    return _out("sell", body, asset_id=asset, force=True)


def sold(asset, pnl_usd, pnl_pct, reason, fraction_sold, remaining_usd):
    sym = symbol(asset)
    verb = "a gain" if pnl_usd >= 0 else "a loss"
    part = "" if fraction_sold >= 0.999 else f" {fraction_sold:.0%} of"
    body = (f"I sold{part} {sym} for {verb} of ${abs(pnl_usd):.2f}"
            + (f" ({pnl_pct:+.0%})" if pnl_pct is not None else "")
            + f" because {plain_reason(reason)}.")
    if fraction_sold < 0.999 and remaining_usd:
        body += f" Still holding about ${remaining_usd:.2f} of it."
    return _out("sell", body, asset_id=asset, force=True)


def not_bought(asset, gate, measured):
    return _out("ops", f"NOT BOUGHT {asset}: {gate}, {measured}", asset_id=asset, force=True)
