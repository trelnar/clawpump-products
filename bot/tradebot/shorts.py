"""The short leg: perpetual-futures shorts on Hyperliquid, 1x.

Self-contained on purpose. The long book, its monitor and its exits assume
qty > 0 and price-goes-up-is-good in a hundred places; a short bolted into
them would be a bug farm. This module keeps its own book (the `shorts`
table), its own monitor pass, and its own mirrored exits, and shares only
what is safe to share: discovery and research, the approval flow (kind
"short"), AUTO mode, cooldowns, the PNL and HOLDING reports, and the alert
channel.

Thesis (mirror of the long leg): p30 = P(price falls HL_TARGET within
P30_WINDOW_SEC) on a token that just pumped and is fading. The core opens at
p30 >= BUY_P30_MIN, stops HL_STOP_PCT above the fill, and a mirrored ratchet
covers 75% once the move has held and then bounces. Everything is 1x: a
squeeze costs the stop, never the account.
"""
import json
import math
import threading
import time

from . import alerts, config, journal, state
from .exchanges import hyperliquid as hl


def enabled():
    return bool(config.SHORTS_ENABLED)


_cover_lock = threading.RLock()     # FLATTEN (poller thread) vs monitor (core loop)


def asset_id(coin):
    return f"perp:{coin}"


def coin_of(asset):
    return asset.split(":", 1)[1]


# --- the book ---------------------------------------------------------------------
def open_positions():
    return journal.query("SELECT * FROM shorts ORDER BY entry_ts")


def get(asset):
    r = journal.query("SELECT * FROM shorts WHERE asset_id=?", (asset,))
    return r[0] if r else None


def _write(row):
    cols = ",".join(row)
    with journal._lock:
        c = journal.conn()
        c.execute(f"INSERT OR REPLACE INTO shorts ({cols}) VALUES ({','.join('?' * len(row))})",
                  list(row.values()))
        c.commit()


def _delete(asset):
    with journal._lock:
        journal.conn().execute("DELETE FROM shorts WHERE asset_id=?", (asset,))
        journal.conn().commit()


def marks():
    """{asset_id: mid} for every open short, one API call."""
    rows = open_positions()
    if not rows:
        return {}
    try:
        m = hl.mids()
    except Exception as e:
        journal.log_event("hl_mids_fail", detail=str(e)[:120])
        return {}
    return {r["asset_id"]: m.get(r["coin"]) for r in rows if m.get(r["coin"])}


def unrealised(marks_):
    total = 0.0
    for r in open_positions():
        px = marks_.get(r["asset_id"])
        if px:
            total += (r["entry_price"] - px) * r["qty"]
    return total


# --- discovery --------------------------------------------------------------------
def candidates(limit=None):
    """Tokens that pumped over 24h and are fading now: the short thesis's
    natural pool. From one meta call plus a few candle snapshots."""
    if not enabled():
        return []
    limit = limit or config.HL_CANDIDATES
    try:
        ctx = hl.contexts()
    except Exception as e:
        journal.log_event("hl_contexts_fail", detail=str(e)[:120])
        return []
    pool = []
    for coin, c in ctx.items():
        if c["prev_day"] <= 0 or c["volume_24h"] < config.HL_MIN_VOLUME_USD:
            continue
        chg24 = c["mark"] / c["prev_day"] - 1
        if chg24 >= config.HL_PUMP_MIN:
            pool.append((chg24, coin, c))
    pool.sort(reverse=True)
    out = []
    for chg24, coin, c in pool[:limit * 2]:
        try:
            cs = hl.candles(coin, "1h", 8)
        except Exception as e:
            journal.log_event("hl_candles_fail", coin, str(e)[:80])
            continue
        if len(cs) < 3:
            continue
        chg1h = cs[-1]["c"] / cs[-2]["c"] - 1
        chg6h = cs[-1]["c"] / cs[max(0, len(cs) - 7)]["c"] - 1
        hi24 = max(x["h"] for x in cs)
        out.append({
            "asset_id": asset_id(coin), "coin": coin, "side": "short", "venue": "hyperliquid",
            "price": c["mark"], "chg24": round(chg24, 4), "chg1h": round(chg1h, 4),
            "chg6h": round(chg6h, 4), "off_high_8h": round(c["mark"] / hi24 - 1, 4) if hi24 else None,
            "funding_hourly": c["funding"], "volume_24h": round(c["volume_24h"]),
            "open_interest": round(c["open_interest"]),
            "candles_1h": [[x["h"], x["l"], x["c"], x["v"]] for x in cs[-8:]],
            "what": f"{coin} perp: +{chg24:.0%} 24h, {chg1h:+.1%} last hour",
        })
        journal.log_discovery(asset_id(coin), "hyperliquid_pumpers", {"chg24": chg24})
        if len(out) >= limit:
            break
    return out


# --- opening ------------------------------------------------------------------------
def _gate(ticket, ignore_age=False):
    """Reasons not to open. Returns None when clear."""
    asset = ticket["asset_id"]
    if state.get_mode() != "NORMAL":
        return f"halt: {state.get_mode()}"
    if get(asset):
        return "already_short"
    if not ignore_age and time.time() - ticket["ts"] > config.TICKET_MAX_AGE_SEC:
        return "stale_ticket"
    if len(open_positions()) >= config.HL_MAX_OPEN:
        return "max_shorts"
    for fn, name in ((state.rejected_recently, "rejected_cooldown"),
                     (state.stopped_out_recently, "stopout_cooldown")):
        if fn(asset) is not None:
            return name
    try:
        av = hl.account_value()
    except Exception as e:
        return f"account_unreadable: {str(e)[:60]}"
    committed = sum(r["notional_usd"] for r in open_positions())
    need = ticket["notional_usd"] * config.HL_MARGIN_BUFFER
    if av - committed < need:
        return f"margin: ${av - committed:.2f} free on Hyperliquid, need ${need:.2f}"
    return None


def process_ticket(ticket):
    """Gates, then approval (kind 'short') or, under AUTO / whitelist, open."""
    asset = ticket["asset_id"]
    why = _gate(ticket)
    if why:
        state.set_ticket_status(ticket["ticket_id"], f"blocked:{why.split(':')[0]}")
        journal.log_event("short_blocked", asset, why)
        if why.startswith("margin") or why.startswith("account"):
            alerts.ops(f"Not shorting {alerts.symbol(asset)}: {why}.")
        return "blocked"
    if state.is_whitelisted(asset) or state.auto_approve_active():
        if not state.is_whitelisted(asset):
            state.whitelist_add(asset, "hyperliquid")
            journal.log_event("auto_approved", asset, {"short": True})
        return execute(ticket)
    if journal.query("SELECT 1 FROM pending_approvals WHERE status='pending' AND asset_id=? "
                     "AND expires > ?", (asset, time.time())):
        state.set_ticket_status(ticket["ticket_id"], "blocked:already_asked")
        return "blocked"       # one open question per asset; two taps would open two shorts
    from . import approval
    code = approval.new_code()
    state.add_pending(code, "short", asset, ticket["ticket_id"], config.APPROVAL_EXPIRY_SEC)
    state.set_ticket_status(ticket["ticket_id"], "awaiting_approval")
    alerts.approval_request(code, "SHORT", asset, ticket.get("detail") or "", {
        "Size": f"${ticket['notional_usd']:.2f} at 1x",
        "Stop": f"+{config.HL_STOP_PCT:.0%} above entry"}, config.APPROVAL_EXPIRY_SEC // 60)
    return "awaiting_approval"


def execute(ticket, approved=False):
    """Open. On the approved path the tap is the freshness signal, so the
    ticket-age gate is skipped; everything else still applies."""
    asset, coin = ticket["asset_id"], coin_of(ticket["asset_id"])
    why = _gate(ticket, ignore_age=approved)
    if why:
        state.set_ticket_status(ticket["ticket_id"], f"blocked:{why.split(':')[0]}")
        if approved:
            alerts.ops(f"{alerts.symbol(asset)} short approved but not opened: {why}.")
        return "blocked"
    try:
        sz, px, oid = hl.open_short(coin, ticket["notional_usd"])
    except Exception as e:
        journal.log_event("short_failed", asset, str(e)[:200])
        # The order may have landed before the error: the exchange is the fact.
        if _adopt_orphan(coin, "open error"):
            state.set_ticket_status(ticket["ticket_id"], "filled")
            return "filled"
        state.set_ticket_status(ticket["ticket_id"], "failed")
        alerts.ops(f"Short of {coin} failed: {str(e)[:120]}")
        return "failed"
    now = time.time()
    stop = px * (1 + config.HL_STOP_PCT)
    _write({"asset_id": asset, "coin": coin, "qty": sz, "entry_price": px,
            "notional_usd": sz * px, "entry_ts": now, "stop_price": stop,
            "ratchet": json.dumps(_new_ratchet()), "lwm": px, "last_alert_ts": now})
    state.set_ticket_status(ticket["ticket_id"], "filled")
    journal.log_fill(client_oid=str(oid), asset_id=asset, side="short", qty=sz, price=px,
                     fee_usd=round(sz * px * config.HL_FEE_RATE, 4), venue="hyperliquid",
                     tx_ref=str(oid))
    alerts.shorted(asset, sz * px, px, stop)
    return "filled"


# --- covering -----------------------------------------------------------------------
def cover(asset, fraction, reason):
    with _cover_lock:
        return _cover(asset, fraction, reason)


def _cover(asset, fraction, reason):
    r = get(asset)
    if not r:
        return "no_position"
    coin = r["coin"]
    # The exchange is the fact. A row with no position behind it (closed in
    # the UI, dust, a crash between fill and write) must not become a
    # COVER FAILED alert every 10 seconds.
    try:
        pos = hl.positions()
    except Exception as e:
        journal.log_event("hl_positions_fail", asset, str(e)[:120])
        pos = None
    live = pos.get(coin) if pos is not None else None
    if pos is not None and (live is None or live["size"] >= 0):
        journal.log_event("short_gone", asset, {"book_qty": r["qty"],
                                                "exchange": live["size"] if live else None})
        _delete(asset)
        alerts.ops(f"My {coin} short is no longer on Hyperliquid; I've dropped it from the book.")
        return "gone"
    full = fraction >= 0.999
    sz = None
    if not full:
        sz = hl.round_size(coin, r["qty"] * fraction)
        if sz <= 0 or (r["qty"] - sz) * (live["entry"] if live else r["entry_price"]) < 1.0:
            full, sz = True, None            # a dust remainder is not worth an orphan
    try:
        filled, px, oid = hl.close_short(coin, sz)
    except Exception as e:
        journal.log_event("cover_failed", asset, str(e)[:200])
        alerts.ops(f"COVER FAILED {coin}: {str(e)[:120]}. Short still open; check Hyperliquid.")
        return "failed"
    fee = (filled * px + filled * r["entry_price"]) * config.HL_FEE_RATE
    pnl = (r["entry_price"] - px) * filled - fee
    cost = r["entry_price"] * filled
    share = min(filled / r["qty"], 1.0) if r["qty"] else 1.0
    prior = _life_pnl(asset, r["entry_ts"])       # BEFORE this exit's own row
    journal.log_event("exit_pnl", asset, {"pnl": round(pnl, 4), "proceeds": round(cost + pnl, 4),
                                          "cost": round(cost, 4), "share": round(share, 4),
                                          "reason": reason[:60], "short": True})
    journal.log_fill(client_oid=str(oid), asset_id=asset, side="cover", qty=filled, price=px,
                     fee_usd=round(fee, 4), venue="hyperliquid", tx_ref=str(oid))
    remaining = r["qty"] - filled
    if full or share >= 0.999 or remaining <= 0:
        _delete(asset)
        total = prior + pnl
        if config.WHITELIST_REAPPROVE_AFTER_LOSS and total < -config.LOSS_THRESHOLD_USD:
            state.whitelist_revoke(asset)
            state.note_stopout(asset)
        remaining_usd = 0.0
    else:
        rs = json.loads(r["ratchet"] or "{}")
        _write({**r, "qty": remaining, "notional_usd": remaining * r["entry_price"],
                "ratchet": json.dumps(rs)})
        remaining_usd = remaining * px
    alerts.covered(asset, pnl, pnl / cost if cost else None, reason, share, remaining_usd)
    return "filled"


def _life_pnl(asset, entry_ts):
    rows = journal.query("SELECT detail FROM events WHERE kind='exit_pnl' AND asset_id=? AND ts>?",
                         (asset, entry_ts))
    total = 0.0
    for x in rows:
        try:
            total += float(json.loads(x["detail"]).get("pnl") or 0)
        except (TypeError, ValueError):
            continue
    return total


def flatten():
    out = {}
    for r in open_positions():
        out[r["asset_id"]] = cover(r["asset_id"], 1.0, "FLATTEN")
    return out


# --- the mirrored ratchet + monitor ---------------------------------------------------
def _new_ratchet():
    return {"armed": False, "run": [], "last_minute": None, "cur": None, "ceiling": None,
            "breach": 0, "done": False, "budget": 0.0, "lwm_ts": None}


def _tick_ratchet(r, rs, price, now):
    """Mirror of ratchet.on_tick on one short. Returns 'take'|'floor'|None and
    mutates rs and r['lwm']/r['stop_price'] in place."""
    entry = r["entry_price"]
    minute = int(now // 60)
    if rs["last_minute"] is None:
        rs["last_minute"], rs["cur"] = minute, price
    elif minute > rs["last_minute"]:
        close = rs["cur"]
        rs["run"] = ([] if minute - rs["last_minute"] > 1 else rs["run"] + [close])[-config.RATCHET_ARM_DWELL_BARS:]
        if close < (r["lwm"] or entry):
            r["lwm"], rs["lwm_ts"] = close, rs["last_minute"] * 60 + 60
        rs["last_minute"], rs["cur"] = minute, price
    else:
        rs["cur"] = price
    if rs["done"]:
        return None
    if not rs["armed"] and len(rs["run"]) >= config.RATCHET_ARM_DWELL_BARS and \
            all(c <= entry * (1 - config.HL_ARM_PCT) for c in rs["run"]):
        rs["armed"], rs["budget"] = True, config.RATCHET_FRACTION * r["qty"]
        r["stop_price"] = min(r["stop_price"], entry * (1 - config.HL_BE_PCT))   # breakeven-ish
        rs["ceiling"] = r["stop_price"]
        journal.log_event("short_ratchet_armed", r["asset_id"], {"entry": entry, "closes": rs["run"]})
    if not rs["armed"]:
        return None
    lwm = r["lwm"] or entry
    gain = 1 - lwm / entry                                   # how far down it went
    stale_h = ((now - (rs["lwm_ts"] or now)) / 3600)
    gb = config.HL_GIVEBACK * (0.6 if stale_h > 1 else 1.0)  # tighter once it stalls
    ceiling = min(rs["ceiling"] or 9e18, entry * (1 - config.HL_BE_PCT),
                  lwm * (1 + gb), entry * (1 - config.RATCHET_KEEP * gain))
    rs["ceiling"] = ceiling
    # TAKE: a capitulation candle that is already bouncing
    if rs.get("cur") and rs["run"]:
        v15 = math.log(price / rs["run"][0]) if rs["run"][0] > 0 else 0
        if v15 <= -config.HL_TAKE_PCT and price > lwm * (1 + gb / 2):
            return "take"
    rs["breach"] = rs["breach"] + 1 if price > ceiling else 0
    if rs["breach"] >= config.RATCHET_FLOOR_BREACHES:
        return "floor"
    return None


def monitor(now=None):
    """One pass: stop, mirrored ratchet, max-hold. Called from the core loop."""
    if not enabled():
        return
    rows = open_positions()
    if not rows:
        return
    now = time.time() if now is None else now
    try:
        m = hl.mids()
    except Exception as e:
        journal.log_event("hl_mids_fail", detail=str(e)[:120])
        return
    for r in rows:
        r = dict(r)
        price = m.get(r["coin"])
        if not price:
            journal.log_event("monitor_blind", r["asset_id"], "no mid")
            continue
        if price >= r["stop_price"]:
            journal.log_event("short_stop", r["asset_id"], {"price": price, "stop": r["stop_price"]})
            cover(r["asset_id"], 1.0, f"stop {r['stop_price']:.4g} hit at {price:.4g}")
            continue
        rs = json.loads(r["ratchet"] or "{}") or _new_ratchet()
        fired = _tick_ratchet(r, rs, price, now)
        if fired and rs["budget"] > 0:
            frac = min(rs["budget"] / r["qty"], 1.0) if r["qty"] else 0
            r["ratchet"] = json.dumps(rs)
            _write(r)
            before = r["qty"]
            res = cover(r["asset_id"], frac, f"ratchet {fired} at {price:.4g}")
            after = get(r["asset_id"])
            if res in ("filled", "gone", "no_position"):
                if after:                     # partial fill keeps the rest of the budget
                    rs2 = json.loads(after["ratchet"] or "{}")
                    sold = before - after["qty"]
                    rs2["budget"] = max(0.0, rs["budget"] - sold)
                    rs2["done"] = rs2["budget"] <= 0
                    after = dict(after)
                    after["ratchet"] = json.dumps(rs2)
                    _write(after)
            continue
        r["ratchet"] = json.dumps(rs)
        _write(r)
        if now - r["entry_ts"] >= config.HL_MAX_HOLD_SEC:
            journal.log_event("short_max_hold", r["asset_id"], {"hours": round((now - r["entry_ts"]) / 3600, 1)})
            cover(r["asset_id"], 1.0, "held past the thesis window")


def _adopt_orphan(coin, why):
    """A short on the exchange with no row: give it a stop and a row."""
    try:
        live = hl.positions().get(coin)
    except Exception:
        return False
    if not live or live["size"] >= 0 or get(asset_id(coin)):
        return False
    qty, px = abs(live["size"]), live["entry"] or (hl.mid(coin) or 0)
    if not px:
        return False
    now = time.time()
    _write({"asset_id": asset_id(coin), "coin": coin, "qty": qty, "entry_price": px,
            "notional_usd": qty * px, "entry_ts": now, "stop_price": px * (1 + config.HL_STOP_PCT),
            "ratchet": json.dumps(_new_ratchet()), "lwm": px, "last_alert_ts": now})
    journal.log_event("short_adopted", asset_id(coin), {"qty": qty, "entry": px, "why": why})
    alerts.ops(f"Found a {coin} short on Hyperliquid that wasn't in my book ({why}); adopted it "
               f"with a stop at {px * (1 + config.HL_STOP_PCT):.4g}.")
    return True


_last_recon = [0.0]


def reconcile(now=None):
    """Both directions, every RECON_POSITIONS_SEC: exchange shorts with no
    row get adopted; rows with no exchange position get dropped."""
    if not enabled():
        return
    now = now or time.time()
    if now - _last_recon[0] < config.RECON_POSITIONS_SEC:
        return
    _last_recon[0] = now
    try:
        live = hl.positions()
    except Exception as e:
        journal.log_event("hl_positions_fail", detail=str(e)[:120])
        return
    for coin, p in live.items():
        if p["size"] < 0 and not get(asset_id(coin)):
            _adopt_orphan(coin, "reconcile")
    for r in open_positions():
        p = live.get(r["coin"])
        if p is None or p["size"] >= 0:
            journal.log_event("short_gone", r["asset_id"], {"book_qty": r["qty"]})
            _delete(r["asset_id"])
            alerts.ops(f"My {r['coin']} short is no longer on Hyperliquid; dropped it from the book.")


def summary_lines(marks_):
    out = []
    for r in open_positions():
        px = marks_.get(r["asset_id"])
        entry = r["entry_price"]
        sym = r["coin"]
        if px:
            pnl = (entry - px) * r["qty"]
            now_txt = f"now {pnl:+.2f} ({1 - px / entry:+.0%} in our favour)"
        else:
            now_txt = "no price right now"
        held = (time.time() - r["entry_ts"]) / 3600
        rs = json.loads(r["ratchet"] or "{}")
        exits = [f"stop at {r['stop_price']:.4g} ({r['stop_price'] / entry - 1:+.0%})"]
        if rs.get("armed") and not rs.get("done"):
            exits.append(f"ratchet armed, low {r['lwm'] / entry:.3f}x, covers 75% above "
                         f"{(rs.get('ceiling') or 0) / entry:.3f}x")
        elif rs.get("done"):
            exits.append("ratchet already banked 75%")
        else:
            exits.append(f"ratchet arms after -{config.HL_ARM_PCT:.0%} holds 3 min")
        exits.append(f"covered at {config.HL_MAX_HOLD_SEC // 3600}h regardless")
        out.append(f"- SHORT {sym}: ${r['notional_usd']:.2f} in, {now_txt}, held {held:.1f}h\n"
                   f"  Exits: " + "; ".join(exits))
    return out
