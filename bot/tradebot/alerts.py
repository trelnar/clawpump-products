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


def not_bought(asset, gate, measured):
    return _out("ops", f"NOT BOUGHT {asset}: {gate}, {measured}", asset_id=asset, force=True)
