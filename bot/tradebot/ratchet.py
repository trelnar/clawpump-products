"""The ratchet: a context-aware profit exit that runs in the deterministic core.

Today a position only leaves on a stop, a standing plan leg, or the model's
SELL_NOW. A token that goes +20% and fades gives it all back. The ratchet
arms once a gain has HELD (three consecutive 1-minute closes at or above
entry x 1.20), raises the stop to breakeven, hands RATCHET_FRACTION of the
position to a floor that only rises -- sized to the coin's own volatility,
tighter the faster the run and the longer it stalls -- and sells that share
on a blow-off (TAKE), a floor breach (FLOOR), or a cold stall (STALL). The
rest rides under the breakeven stop, the standing plan and the model.

Modes: off | shadow | live. Shadow computes everything and journals what it
WOULD have sold, plus a 72h counterfactual per would-sell, so the RATCHET
report can say whether the rule beats what actually happened before it is
allowed to trade. Design record: RATCHET.md.
"""
import json
import math
import time

from . import config, journal, state

_KEY = "ratchet:{}"
TRIGGERS = ("take", "floor", "stall")
MODES = ("off", "shadow", "live")


def mode():
    """RATCHET LIVE / SHADOW / OFF in Telegram overrides the config default,
    so switching needs no VPS session."""
    v = state.get_kv("ratchet_mode")
    return v if v in MODES else config.RATCHET_MODE


def set_mode(m):
    state.set_kv("ratchet_mode", m)
    journal.log_event("ratchet_mode", detail=m)


# --- persistence ------------------------------------------------------------
def load(asset_id):
    v = state.get_kv(_KEY.format(asset_id))
    try:
        return json.loads(v) if v else None
    except ValueError:
        return None


def _save(asset_id, st):
    state.set_kv(_KEY.format(asset_id), json.dumps(st))


def clear(asset_id):
    state.set_kv(_KEY.format(asset_id), "")


def _new(p):
    is_cex = (p.get("venue") == "coinbase")
    return {"entry_ts": p.get("entry_ts"), "armed": False, "armed_ts": None,
            "hwm": None, "hwm_ts": None, "floor": None, "budget_qty": 0.0,
            "breach": 0, "sigma1": (config.RATCHET_SIGMA_SEED_CEX if is_cex
                                   else config.RATCHET_SIGMA_SEED_TOKEN),
            "last_minute": None, "cur_close": None, "prev_close": None, "run": [],
            "done": False, "sold": [], "cex": is_cex}


def entry_price(p):
    return (p["cost_basis_usd"] / p["qty"]) if p.get("qty") else None


# --- bars ---------------------------------------------------------------------
def write_bar(asset_id, q, now):
    minute = int(now // 60)
    with journal._lock:
        journal.conn().execute(
            "INSERT INTO price_bars (asset_id, minute_ts, close, buys5, sells5, liquidity_usd) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(asset_id, minute_ts) DO UPDATE SET "
            "close=excluded.close, buys5=excluded.buys5, sells5=excluded.sells5, "
            "liquidity_usd=excluded.liquidity_usd",
            (asset_id, minute, q["price"], q.get("buys_m5"), q.get("sells_m5"),
             q.get("liquidity_usd")))
        journal.conn().commit()


def close_at(asset_id, minute):
    """Close of the latest completed bar at or before `minute`, or None."""
    r = journal.query("SELECT close FROM price_bars WHERE asset_id=? AND minute_ts<=? "
                      "ORDER BY minute_ts DESC LIMIT 1", (asset_id, minute))
    return r[0]["close"] if r else None


def prune_bars(now=None):
    now = now or time.time()
    with journal._lock:
        journal.conn().execute("DELETE FROM price_bars WHERE minute_ts < ?",
                               (int((now - config.PRICE_BARS_RETENTION_DAYS * 86400) // 60),))
        journal.conn().commit()


# --- the math ---------------------------------------------------------------------
def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _sigma_update(st, close, prev):
    # A bar with no trade (unchanged print on a thin pool) says nothing about
    # volatility; feeding zeros collapsed sigma and pulled the floor into the
    # noise on exactly the illiquid tokens where that is most dangerous.
    if prev and close and prev > 0 and close > 0 and close != prev:
        r = abs(math.log(close / prev))
        a = config.RATCHET_SIGMA_ALPHA
        st["sigma1"] = _clamp((1 - a) * st["sigma1"] + a * r,
                              config.RATCHET_SIGMA_MIN, config.RATCHET_SIGMA_MAX)


def sigma15(st):
    return st["sigma1"] * math.sqrt(15)


def compute_floor(st, entry, now):
    """The floor that only rises. Pure: no I/O."""
    hwm, hwm_ts = st["hwm"], st["hwm_ts"]
    G = hwm / entry - 1
    h_run = max((hwm_ts - st["entry_ts"]) / 3600, 0.25)
    speed = min(1 + 2 * G / h_run, 3)
    t_stale = _clamp(0.5 * h_run, 0.5, 6)
    stale = min(1 + max(0.0, (now - hwm_ts) / 3600) / t_stale, 2)   # never 0: k divides by it
    k = _clamp(config.RATCHET_K0 / (speed * stale), config.RATCHET_K_MIN, config.RATCHET_K_MAX)
    s15 = sigma15(st)
    gb_min = config.RATCHET_GB_MIN_CEX if st["cex"] else config.RATCHET_GB_MIN_TOKEN
    gb = _clamp(k * s15, gb_min, config.RATCHET_GB_MAX)
    trail = hwm * (1 - gb)
    lock = min(entry * (1 + config.RATCHET_KEEP * G), hwm * (1 - config.RATCHET_LOCK_NOISE * s15))
    return max(st["floor"] or 0, entry * config.RATCHET_BE_STOP, trail, lock)


def _cold(q, st):
    if st["cex"]:
        return False
    b, s = q.get("buys_m5") or 0, q.get("sells_m5") or 0
    return (b + s) >= config.RATCHET_COLD_MIN_TXNS and s > b


def _hold_fresh(asset_id, now):
    """A HOLD from the last research cycle with real conviction on rising
    signal suppresses STALL: a wrong HOLD costs one cycle of the share."""
    r = journal.query("SELECT action, p2x, ts FROM forecasts WHERE asset_id=? "
                      "ORDER BY ts DESC LIMIT 1", (asset_id,))
    if not r or now - (r[0]["ts"] or 0) > config.RATCHET_HOLD_FRESH_SEC:
        return False
    if r[0]["action"] not in ("HOLD", "ADD") or (r[0]["p2x"] or 0) < config.RATCHET_STALL_HOLD_P2X:
        return False
    try:
        from . import signals
        return (signals.features(asset_id).get("accel") or 0) >= 0
    except Exception:
        return True


def _blowoff(asset_id, st, price, minute, now):
    age_min = (now - st["entry_ts"]) / 60
    entry_anchor = st["entry_anchor"]
    c15 = close_at(asset_id, minute - 15) if age_min >= 15 else entry_anchor
    c5 = close_at(asset_id, minute - 5) if age_min >= 5 else entry_anchor
    if not c15 or not c5 or c15 <= 0 or c5 <= 0:
        return False
    v15, v5 = math.log(price / c15), math.log(price / c5)
    return v15 >= config.RATCHET_TAKE_V15 and (v5 - v15 / 3) <= config.RATCHET_TAKE_DECEL


# --- the tick ---------------------------------------------------------------------
def on_tick(p, q, now=None):
    """Called once per monitor tick for a held position with a fresh quote.
    Returns the trigger name when a sell fired (live) or would have (shadow)."""
    m = mode()
    if m == "off" or not q or not q.get("price"):
        return None
    asset, price = p["asset_id"], q["price"]
    entry = entry_price(p)
    if not entry or p.get("entry_ts") is None:
        return None
    now = time.time() if now is None else now
    st = load(asset)
    if not st or st.get("entry_ts") != p["entry_ts"]:
        st = _new(p)
        st["hwm"], st["hwm_ts"], st["entry_anchor"] = entry, p["entry_ts"], entry
    else:
        st = {**_new(p), "hwm": entry, "hwm_ts": p["entry_ts"], "entry_anchor": entry, **st}
    write_bar(asset, q, now)             # bars continue after the share is sold
    if st["done"]:
        return None
    minute = int(now // 60)

    # minute rollover: the previous bar is complete
    if st["last_minute"] is None:
        st["last_minute"], st["cur_close"] = minute, price
    elif minute > st["last_minute"]:
        close = st["cur_close"]
        if minute - st["last_minute"] > 1:
            # A feed gap: the pre-gap close neither continues the dwell nor
            # measures volatility (its return would span the gap).
            st["run"], st["prev_close"] = [], close
        else:
            _sigma_update(st, close, st["prev_close"])
            st["prev_close"] = close
            st["run"] = (st["run"] + [close])[-config.RATCHET_ARM_DWELL_BARS:]
        if close > st["hwm"]:
            st["hwm"], st["hwm_ts"] = close, st["last_minute"] * 60 + 60
        st["last_minute"], st["cur_close"] = minute, price
    else:
        st["cur_close"] = price

    fired = None
    # arming
    if not st["armed"] and len(st["run"]) >= config.RATCHET_ARM_DWELL_BARS and \
            all(c >= entry * (1 + config.RATCHET_G_ARM) for c in st["run"]):
        st["armed"], st["armed_ts"] = True, now
        st["budget_qty"] = config.RATCHET_FRACTION * (p["qty"] or 0)
        st["floor"] = entry * config.RATCHET_BE_STOP
        journal.log_event("ratchet_armed", asset, {
            "mode": m, "entry": entry, "closes": st["run"], "hwm": st["hwm"],
            "budget_qty": st["budget_qty"], "sigma15": round(sigma15(st), 4)})
        if m == "live":
            inv = max(p.get("invalidation_price") or 0, entry * config.RATCHET_BE_STOP)
            state.upsert_position(asset, p["venue"], p.get("chain"), 0, 0, invalidation=inv)
        age_h = (now - st["entry_ts"]) / 3600
        if age_h <= config.RATCHET_TAKE_COLD_AGE_H and _cold(q, st):
            fired = "take"

    if st["armed"] and not fired:
        st["floor"] = compute_floor(st, entry, now)
        if _blowoff(asset, st, price, minute, now):
            fired = "take"
        else:
            if q.get("fresh"):
                st["breach"] = st["breach"] + 1 if price < st["floor"] else 0
            if st["breach"] >= config.RATCHET_FLOOR_BREACHES:
                fired = "floor"
            else:
                age_h = (now - st["entry_ts"]) / 3600
                allow = _clamp(10 * age_h, config.RATCHET_STALL_MIN_MIN, config.RATCHET_STALL_MAX_MIN)
                if (now - st["hwm_ts"]) / 60 >= allow and _cold(q, st) and not _hold_fresh(asset, now):
                    fired = "stall"

    if fired:
        _fire(p, st, q, fired, entry, now)
    _save(asset, st)
    return fired


def _fire(p, st, q, trigger, entry, now):
    asset, price = p["asset_id"], q["price"]
    qty = p.get("qty") or 0
    fraction = min(st["budget_qty"] / qty, 1.0) if qty > 0 else 0
    detail = {"trigger": trigger, "price": price, "entry": entry, "hwm": st["hwm"],
              "floor": st["floor"], "fraction": round(fraction, 4),
              "multiple": round(price / entry, 4), "sigma15": round(sigma15(st), 4),
              "minutes_held": round((now - st["entry_ts"]) / 60, 1), "mode": mode()}
    if mode() == "live":
        from . import execution
        result = execution.execute_sell(asset, f"ratchet {trigger} at {price:.4g}",
                                        execution.clamp_fraction(fraction))
        detail["result"] = result
        journal.log_event("ratchet_exit", asset, detail)
        if result in ("filled", "dust", "no_position"):
            st["budget_qty"], st["done"] = 0.0, True
        else:
            return          # not sold: no counterfactual to score
    else:
        journal.log_event("ratchet_would_sell", asset, detail)
    with journal._lock:
        journal.conn().execute(
            "INSERT INTO ratchet_track (asset_id, entry_ts, entry_price, armed_ts, trigger, "
            "shadow_price, shadow_ts, fraction, hwm, floor, sigma15, max_after, last_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (asset, st["entry_ts"], entry, st["armed_ts"], trigger, price, now, fraction,
             st["hwm"], st["floor"], sigma15(st), price, now))
        journal.conn().commit()
    st["sold"].append({"trigger": trigger, "price": price, "ts": now})
    st["budget_qty"], st["done"] = 0.0, True


# --- lifecycle hooks ----------------------------------------------------------------
def on_close(asset_id, pnl_total):
    """Full close of a position life. A ratchet winner gets a short re-buy
    pause instead of nothing; losers keep the existing 6h stop-out path."""
    st = load(asset_id)
    if st and st.get("armed") and mode() == "live" and pnl_total >= 0:
        state.set_kv(f"ratchet_exit:{asset_id}", str(time.time()))
    clear(asset_id)


def reentry_paused(asset_id):
    v = state.get_kv(f"ratchet_exit:{asset_id}")
    if not v:
        return None
    age = time.time() - float(v)
    return age if age < config.RATCHET_REENTRY_SEC else None


def is_armed(asset_id):
    st = load(asset_id)
    return bool(st and st.get("armed") and not st.get("done"))


def summary(asset_id):
    """What the model is told about a held position's ratchet state."""
    st = load(asset_id)
    if not st or not st.get("armed"):
        return None
    return {"armed": True, "hwm": st["hwm"], "floor": st["floor"],
            "share_remaining": st["budget_qty"] > 0, "mode": mode(),
            "minutes_since_peak": round((time.time() - st["hwm_ts"]) / 60) if st["hwm_ts"] else None}


# --- counterfactual tracking + the report ---------------------------------------------
def track(marks, now=None):
    """Sample the 72h counterfactual after each shadow sell; called from the
    calibration pass with the marks it already fetched."""
    now = now or time.time()
    rows = journal.query("SELECT id, asset_id, shadow_ts, max_after FROM ratchet_track "
                         "WHERE resolved=0")
    for r in rows:
        px = marks.get(r["asset_id"])
        with journal._lock:
            if px and px > 0:
                journal.conn().execute("UPDATE ratchet_track SET max_after=MAX(max_after,?), "
                                       "last_ts=? WHERE id=?", (px, now, r["id"]))
            if now - r["shadow_ts"] >= config.RATCHET_TRACK_SEC:
                journal.conn().execute("UPDATE ratchet_track SET resolved=1 WHERE id=?", (r["id"],))
            journal.conn().commit()
    return [r["asset_id"] for r in rows]


def _actual_multiple(asset_id, entry_ts, entry_price):
    rows = journal.query("SELECT detail FROM events WHERE kind='exit_pnl' AND asset_id=? AND ts>?",
                         (asset_id, entry_ts))
    proceeds = cost = 0.0
    for r in rows:
        try:
            d = json.loads(r["detail"])
            proceeds += float(d.get("proceeds") or 0)
            cost += float(d.get("cost") or 0)
        except (TypeError, ValueError):
            continue
    if cost > 0:
        return proceeds / cost
    pos = state.get_position(asset_id)
    if pos and pos.get("entry_ts") == entry_ts:
        from . import marketdata
        px = marketdata.cached_price(asset_id) or marketdata.price(asset_id)
        return (px / entry_price) if px and entry_price else None
    return None


def report_text(days=30):
    since = time.time() - days * 86400
    armed = journal.query("SELECT COUNT(*) n FROM events WHERE kind='ratchet_armed' AND ts>?",
                          (since,))[0]["n"]
    rows = journal.query("SELECT * FROM ratchet_track WHERE shadow_ts>? ORDER BY shadow_ts",
                         (since,))
    lines = [f"RATCHET {days}d  mode={mode()}  share={config.RATCHET_FRACTION:.0%}",
             f"armed {armed}, would-sell {len(rows)}"
             + ("  (" + ", ".join(f"{t} {sum(1 for r in rows if r['trigger'] == t)}"
                                  for t in TRIGGERS) + ")" if rows else "")]
    resolved = [r for r in rows if r["resolved"]]
    if resolved:
        f = config.RATCHET_FRACTION
        blended, actual, tail = [], [], 0
        for r in resolved:
            sm = r["shadow_price"] / r["entry_price"]
            cf = (r["max_after"] or r["shadow_price"]) / r["entry_price"]
            am = _actual_multiple(r["asset_id"], r["entry_ts"], r["entry_price"])
            if am is None:
                continue
            actual.append(am)
            blended.append(f * sm + (1 - f) * am)
            if sm < 1.15 and cf >= 2.0:
                tail += 1
        if actual:
            mb, ma = sum(blended) / len(blended), sum(actual) / len(actual)
            g1 = mb >= ma + 0.05
            g2 = tail / len(actual) <= 0.10
            lines.append(f"resolved {len(actual)}: shadow-blend {mb:.2f}x vs actual {ma:.2f}x "
                         f"-> gate1 {'PASS' if g1 else 'fail'}")
            lines.append(f"sold early then 2x within 72h: {tail}/{len(actual)} "
                         f"-> gate2 {'PASS' if g2 else 'fail'}")
            lines.append("verdict: " + ("ready to go live" if g1 and g2 else
                                        "keep shadowing" if len(actual) < 30 else "do not enable"))
    else:
        lines.append("no resolved counterfactuals yet (72h after each would-sell)")
    for p in state.positions():
        s = summary(p["asset_id"])
        if s:
            e = entry_price(p) or 0
            lines.append(f"  {p['asset_id'][:24]} armed: peak {s['hwm']/e:.2f}x floor "
                         f"{s['floor']/e:.2f}x {'share pending' if s['share_remaining'] else 'share sold'}")
    return "\n".join(lines)
