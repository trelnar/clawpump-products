"""Forecast resolution. The strategy skill's self-calibration loop needs to
know whether a forecast came true; nothing was writing that, so nothing could.

Tracks EVERY forecast, not only the ones that became trades -- the PASSes and
COMING_UPs are where most of the signal is, and they cost nothing to observe.
Prices are sampled forward rather than fetched historically, so this works for
any asset the bot can already mark.
"""
import threading
import time

from . import config, journal, marketdata


def open_tracking(forecast_id, asset_id, action, price):
    if not forecast_id or price is None or price <= 0:
        return
    now = time.time()      # one instant: last_ts == start_ts means 'never sampled'
    with journal._lock:
        journal.conn().execute(
            "INSERT OR IGNORE INTO forecast_tracking "
            "(forecast_id, asset_id, action, start_ts, start_price, max_price, last_ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (forecast_id, asset_id, action, now, price, price, now))
        journal.conn().commit()


def _rows():
    return journal.query("SELECT * FROM forecast_tracking WHERE resolved=0 ORDER BY start_ts DESC")


def tick():
    """One sampling pass.

    Marks are fetched per distinct ASSET (many forecasts share one), youngest
    forecasts first, so the 6h window is always covered. The first version
    sampled the 60 OLDEST rows: at ~340 forecasts a day nothing was sampled
    until it was 72h old, every outcome resolved at its start price (peak
    1.00x, 0% everything), and that fed the model a calibration of 'none of
    your passes ever moved' for two days."""
    rows = _rows()
    from . import ratchet
    pending = journal.query("SELECT DISTINCT asset_id FROM ratchet_track WHERE resolved=0")
    assets, seen = [], set()
    for r in rows:                              # youngest first, distinct
        if r["asset_id"] not in seen:
            seen.add(r["asset_id"])
            assets.append(r["asset_id"])
    assets = assets[:config.TRACK_BATCH]
    seen = set(assets)                          # what will actually be marked
    for r in pending:
        if r["asset_id"] not in seen:
            assets.append(r["asset_id"])
    if not assets and not rows:
        return 0
    marks, _ = marketdata.marks(assets) if assets else ({}, True)
    now = time.time()
    ratchet.track(marks, now)
    updates, touched = [], set()
    for r in rows:
        px = marks.get(r["asset_id"])
        if not px or px <= 0:
            continue
        u = _advance(r, px, now)
        if u is None:
            continue
        updates.append(u)
        touched.add(r["asset_id"])
    with journal._lock:
        c = journal.conn()
        for u in updates:
            c.execute(
                "UPDATE forecast_tracking SET max_price=?, min_price=?, max_6h=?, min_6h=?, "
                "end_6h=?, end_ts=?, stop_ts=?, target_ts=?, last_ts=?, "
                "samples=COALESCE(samples,0)+1 WHERE forecast_id=? AND resolved=0", u)
        c.commit()
    for r in rows:
        if now - r["start_ts"] >= config.TRACK_WINDOW_SEC:
            _resolve(r["forecast_id"])
    return len(touched)


_tick_thread = [None]


def tick_async():
    """tick() off the caller's thread. A pass marks up to TRACK_BATCH assets
    at one HTTP read each -- a minute or more -- and it used to run inline
    in the core loop, during which the 5-second position monitor did not.
    A stop could fire late by exactly that long. Skips a pass while the
    previous one is still running."""
    t = _tick_thread[0]
    if t is not None and t.is_alive():
        return False

    def _run():
        try:
            tick()
        except Exception as e:
            journal.log_event("track_error", detail=repr(e)[:200])
    t = threading.Thread(target=_run, name="tracker", daemon=True)
    _tick_thread[0] = t
    t.start()
    return True


def _thresholds(asset_id):
    """(target, stop, is_short) the thesis implies for this asset."""
    if (asset_id or "").startswith("perp:"):
        return config.HL_TARGET, config.HL_STOP_PCT, True
    return config.P30_TARGET, config.STOP_LOSS_PCT, False


def _friction(asset_id):
    """Round-trip cost the sim charges: a DEX swap pair for tokens, two
    taker fees for a perp. One flat 4% charged to shorts made every good
    short strategy look like a loser."""
    if (asset_id or "").startswith("perp:"):
        return 2 * config.HL_FEE_RATE
    return config.SIM_FRICTION


def _advance(r, px, now):
    """One sample against one open forecast: extremes over the whole horizon,
    extremes and the last print inside the thesis window, and the FIRST time
    the thesis's stop and target each printed. hit_30 says whether +30% was
    ever there; stop_ts vs target_ts says whether the trade would have
    reached it or been stopped out on the way. Those are different numbers,
    and only the second one is money."""
    start = r["start_price"] or 0
    age = now - (r["start_ts"] or 0)
    if age > config.TRACK_WINDOW_SEC:
        return None            # past its horizon: a late print is not part of the record
    in_win = age <= config.P30_WINDOW_SEC
    max_p = max(r["max_price"] or start, px)
    min_p = min(r.get("min_price") or start, px)
    max6, min6, end6 = r.get("max_6h"), r.get("min_6h"), r.get("end_6h")
    end_ts = r.get("end_ts")
    stop_ts, target_ts = r.get("stop_ts"), r.get("target_ts")
    if in_win:
        max6 = max(max6 or 0, px)
        min6 = min(min6 or start, px)
        end6, end_ts = px, now
        target, stop, short = _thresholds(r["asset_id"])
        if start > 0:
            hit_stop = px >= start * (1 + stop) if short else px <= start * (1 - stop)
            hit_target = px <= start * (1 - target) if short else px >= start * (1 + target)
            if hit_stop and stop_ts is None:
                stop_ts = now
            if hit_target and target_ts is None:
                target_ts = now
    return (max_p, min_p, max6, min6, end6, end_ts, stop_ts, target_ts, now, r["forecast_id"])


def _sim(r):
    """The trade the thesis implies, played on the samples: the target banked
    if it printed before the stop, the stop taken if it printed first, else
    the move at the last print inside the window; net of the round trip.
    Returns (result, return), or (None, None) for a row the new tracker
    never observed inside its window (the rows in flight when these columns
    arrived): scoring those as 'flat' would have fed the model several
    hundred phantom losers."""
    start = r["start_price"] or 0
    target, stop, short = _thresholds(r["asset_id"])
    st, tt, end = r.get("stop_ts"), r.get("target_ts"), r.get("end_6h")
    if st is None and tt is None and end is None:
        return None, None
    fee = _friction(r["asset_id"])
    if st and (not tt or st <= tt):
        return "stop", -stop - fee
    if tt:
        return "target", target - fee
    if end and start > 0:
        # 'Flat' is only a result if the end of the window was actually
        # watched. One print at +5 min and silence until resolution is not
        # a +16% trade; it is a gap in coverage.
        end_ts, start_ts = r.get("end_ts"), r.get("start_ts") or 0
        deadline = start_ts + config.P30_WINDOW_SEC
        if end_ts is not None and end_ts < deadline - 3 * config.TRACK_INTERVAL_SEC:
            return None, None
        ret = end / start - 1
        return "flat", (-ret if short else ret) - fee
    return "flat", -fee


def _resolve(forecast_id):
    r = journal.query("SELECT * FROM forecast_tracking WHERE forecast_id=?",
                      (forecast_id,))
    if not r:
        return
    r = r[0]
    if _unsampled(r):
        # Never sampled: no outcome, or it scores as 'went nowhere' and
        # poisons the model's calibration. Just close the row.
        journal.log_event("track_unsampled", r["asset_id"], {"forecast_id": forecast_id})
        with journal._lock:
            journal.conn().execute(
                "UPDATE forecast_tracking SET resolved=1 WHERE forecast_id=?", (forecast_id,))
            journal.conn().commit()
        return
    start, top = r["start_price"] or 0, r["max_price"] or 0
    mult = (top / start) if start > 0 else None
    top6 = r["max_6h"] if "max_6h" in r.keys() else None
    m6 = (top6 / start) if (top6 and start > 0) else None
    if (r["asset_id"] or "").startswith("perp:"):
        # a short thesis scores on the LOW inside the window
        low6 = r["min_6h"] if "min_6h" in r.keys() else None
        hit30 = int(bool(low6 and start > 0 and low6 / start <= 1 - config.HL_TARGET))
    else:
        hit30 = int(bool(m6 and m6 >= 1 + config.P30_TARGET))
    sim_result, sim_ret = _sim(r)
    journal.log_outcome(forecast_id=forecast_id, max_multiple=mult,
                        hit_30=hit30,
                        hit_2x=int(bool(mult and mult >= 2)),
                        hit_3x=int(bool(mult and mult >= 3)),
                        hit_5x=int(bool(mult and mult >= 5)),
                        hit_10x=int(bool(mult and mult >= 10)),
                        exit_result=r["action"], realized_pnl_usd=None,
                        slippage_vs_plan=None,
                        sim_result=sim_result,
                        sim_return=None if sim_ret is None else round(sim_ret, 4))
    with journal._lock:
        journal.conn().execute(
            "UPDATE forecast_tracking SET resolved=1 WHERE forecast_id=?", (forecast_id,))
        journal.conn().commit()


def _unsampled(r):
    """A forecast that was never observed inside its thesis window cannot
    score the thesis: max_6h is only ever set by a sample inside the window,
    so NULL there means the first look came too late (the pre-fix backlog
    resolved this way, all at exactly 1.00x). Rows written before the samples
    column existed are judged by their timestamps."""
    keys = r.keys() if hasattr(r, "keys") else []
    if "max_6h" in keys and r["max_6h"] is None:
        return True
    if "samples" in keys and r["samples"] is not None:
        return int(r["samples"]) == 0
    return (r["last_ts"] or 0) - (r["start_ts"] or 0) < 1.0


def purge_unsampled():
    """One-time repair: outcomes written for forecasts that were never
    sampled (peak exactly 1.00x, every hit 0) are not data. Remove them so
    SCORE and the model's calibration see only real observations."""
    with journal._lock:
        c = journal.conn()
        cur = c.execute(
            "DELETE FROM outcomes WHERE forecast_id IN "
            "(SELECT forecast_id FROM forecast_tracking WHERE max_6h IS NULL "
            "AND (COALESCE(samples,0) = 0 OR last_ts - start_ts > ?))",
            (config.P30_WINDOW_SEC,))
        n = cur.rowcount
        c.commit()
    if n:
        journal.log_event("outcomes_purged_unsampled", detail={"rows": n})
    return n


def scorecard(days=30):
    """What the bot predicted vs what happened, split by the action it chose.
    The BUY_NOW vs PASS comparison is the whole point: passing on things that
    went to 3x is a different failure from buying things that went to zero."""
    since = time.time() - days * 86400
    rows = journal.query(
        f"SELECT {_GROUP} a, COUNT(*) n, AVG(o.max_multiple) avg_mult, "
        "SUM(o.hit_30) h30, SUM(o.hit_2x) h2, SUM(o.hit_3x) h3, SUM(o.hit_5x) h5, "
        "SUM(CASE WHEN o.sim_result='target' THEN 1 ELSE 0 END) won, "
        "SUM(CASE WHEN o.sim_result='stop' THEN 1 ELSE 0 END) stopped, "
        "SUM(CASE WHEN o.sim_result IS NOT NULL THEN 1 ELSE 0 END) n_sim, "
        "AVG(o.sim_return) sim_ret "
        "FROM outcomes o JOIN forecast_tracking t ON t.forecast_id=o.forecast_id "
        f"WHERE o.ts > ? GROUP BY {_GROUP} ORDER BY n DESC", (since,))
    if not rows:
        return f"No forecasts have resolved yet (window {config.TRACK_WINDOW_SEC/3600:.0f}h)."
    out = [f"SCORECARD {days}d — resolved forecasts by the action taken "
           f"({config.TRACK_INTERVAL_SEC // 60}-min samples; sim assumes the stop fills "
           f"at -{config.STOP_LOSS_PCT:.0%}, which a rug does not honour)"]
    for r in rows:
        n = r["n"] or 1
        perp = (r["a"] or "").endswith("/perp")
        tgt = f"-{config.HL_TARGET:.0%}" if perp else f"+{config.P30_TARGET:.0%}"
        out.append(f"{r['a'] or '?'} n={r['n']}: {tgt}/6h {100*(r['h30'] or 0)/n:.0f}%, "
                   f"peak {r['avg_mult'] or 0:.2f}x, 2x {100*(r['h2'] or 0)/n:.0f}%")
        ns = r["n_sim"] or 0
        if ns:
            won, stopped = (r["won"] or 0), (r["stopped"] or 0)
            per10 = 10 * (r["sim_ret"] or 0)
            out.append(f"  as $10 trades ({ns}): won {100*won/ns:.0f}%, stopped "
                       f"{100*stopped/ns:.0f}%, flat {100*(ns-won-stopped)/ns:.0f}% -> "
                       f"${per10:+.2f} each after costs")
    # Where should the buy bar sit? The same sim, bucketed by the p30 the
    # model stated, tokens only. The bucket where the money turns positive
    # is the answer; BUY_P30_MIN is a guess until this says otherwise.
    buckets = journal.query(
        "SELECT ROUND(f.p30, 1) b, COUNT(*) n, "
        "SUM(CASE WHEN o.sim_result='target' THEN 1 ELSE 0 END) won, "
        "SUM(CASE WHEN o.sim_result='stop' THEN 1 ELSE 0 END) stopped, "
        "AVG(o.sim_return) sim_ret "
        "FROM outcomes o JOIN forecasts f ON f.forecast_id=o.forecast_id "
        "JOIN forecast_tracking t ON t.forecast_id=o.forecast_id "
        "WHERE o.ts > ? AND o.sim_result IS NOT NULL AND f.p30 IS NOT NULL "
        "AND t.asset_id NOT LIKE 'perp:%' GROUP BY b ORDER BY b", (since,))
    if buckets:
        out.append(f"By the p30 the model stated (buy bar is {config.BUY_P30_MIN:.2f}):")
        for b in buckets:
            n = b["n"] or 1
            out.append(f"  p30~{b['b']:.1f} n={b['n']}: won {100*(b['won'] or 0)/n:.0f}%, "
                       f"stopped {100*(b['stopped'] or 0)/n:.0f}% -> "
                       f"${10*(b['sim_ret'] or 0):+.2f} per $10")
    return "\n".join(out)


# Perps are scored on a short thesis, tokens on a long one; one PASS bucket
# holding both would average 'fell 8%' wins with 'rose 30%' wins.
_GROUP = "(t.action || CASE WHEN t.asset_id LIKE 'perp:%' THEN '/perp' ELSE '' END)"


def feedback(days=14):
    """The model's own recent record, per action, in the form it can act on:
    what it said (mean stated p2x) against what happened (share that reached
    2x). The first scorecard showed PASSes reaching 2x 31% of the time while
    the stated p2x on them averaged 0.05 -- a model six times too pessimistic
    about the very things it was seeing. Nothing was telling it. This does."""
    since = time.time() - days * 86400
    rows = journal.query(
        f"SELECT {_GROUP} a, COUNT(*) n, AVG(f.p2x) stated, AVG(o.hit_2x) hit2, "
        "AVG(f.p30) stated30, AVG(o.hit_30) hit30, "
        "AVG(o.hit_3x) hit3, AVG(o.max_multiple) peak, "
        "AVG(CASE WHEN o.sim_result='target' THEN 1.0 WHEN o.sim_result IS NULL THEN NULL "
        "ELSE 0.0 END) won, "
        "AVG(CASE WHEN o.sim_result='stop' THEN 1.0 WHEN o.sim_result IS NULL THEN NULL "
        "ELSE 0.0 END) stopped, AVG(o.sim_return) sim_ret "
        "FROM outcomes o JOIN forecast_tracking t ON t.forecast_id=o.forecast_id "
        "JOIN forecasts f ON f.forecast_id=o.forecast_id "
        f"WHERE o.ts > ? GROUP BY {_GROUP}", (since,))
    out = {}
    for r in rows:
        if not r["n"]:
            continue
        d = {
            "resolved": r["n"],
            "stated_p30_mean": round(r["stated30"] or 0, 3),
            "reached_30pct_in_6h_share": round(r["hit30"] or 0, 3),
            "stated_p2x_mean": round(r["stated"] or 0, 3),
            "reached_2x_share": round(r["hit2"] or 0, 3),
            "reached_3x_share": round(r["hit3"] or 0, 3),
            "peak_multiple_mean": round(r["peak"] or 0, 2),
        }
        if r["sim_ret"] is not None:
            # the thesis as a trade: target banked before the stop printed?
            d["target_before_stop_share"] = round(r["won"] or 0, 3)
            d["stopped_first_share"] = round(r["stopped"] or 0, 3)
            d["sim_pnl_per_10usd"] = round(10 * r["sim_ret"], 2)   # net of costs
        out[r["a"] or "?"] = d
    return out


def gaps(days=7):
    """Why the bot is not trading. Separates 'nothing qualified' from 'I could
    not tell' -- a cycle of silent PASSes looks identical to blindness until
    you count what the model said it was missing."""
    import collections
    import json as _json
    since = time.time() - days * 86400
    rows = journal.query(
        "SELECT action, evidence_state FROM forecasts WHERE ts > ?", (since,))
    if not rows:
        return f"No forecasts in {days}d."
    actions = collections.Counter(r["action"] for r in rows)
    missing, reasons = collections.Counter(), collections.Counter()
    for r in rows:
        try:
            c = _json.loads(r["evidence_state"] or "{}")
        except (TypeError, ValueError):
            continue
        for m in (c.get("missing_evidence") or []):
            missing[str(m).strip().lower()[:40]] += 1
        if c.get("action") == "PASS" and c.get("pass_reason"):
            reasons[str(c["pass_reason"]).strip()[:70]] += 1
    out = [f"GAPS {days}d — {len(rows)} forecasts",
           "  " + ", ".join(f"{a}={n}" for a, n in actions.most_common())]
    out.append("\nMost-cited missing evidence (what better data would buy):")
    out += [f"  {n:4d}x  {m}" for m, n in missing.most_common(10)] or ["  (none cited)"]
    out.append("\nMost common PASS reasons:")
    out += [f"  {n:4d}x  {m}" for m, n in reasons.most_common(8)] or ["  (none given)"]
    return "\n".join(out)
