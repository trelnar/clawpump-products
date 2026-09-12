"""Forecast resolution. The strategy skill's self-calibration loop needs to
know whether a forecast came true; nothing was writing that, so nothing could.

Tracks EVERY forecast, not only the ones that became trades -- the PASSes and
COMING_UPs are where most of the signal is, and they cost nothing to observe.
Prices are sampled forward rather than fetched historically, so this works for
any asset the bot can already mark.
"""
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
    for r in pending:
        if r["asset_id"] not in seen:
            assets.append(r["asset_id"])
    if not assets and not rows:
        return 0
    marks, _ = marketdata.marks(assets) if assets else ({}, True)
    now = time.time()
    ratchet.track(marks, now)
    updated = 0
    with journal._lock:
        c = journal.conn()
        for asset, px in marks.items():
            if not px or px > 0:
                c.execute(
                    "UPDATE forecast_tracking SET max_price=MAX(max_price,?), last_ts=?, "
                    "max_6h=CASE WHEN ? - start_ts <= ? THEN MAX(COALESCE(max_6h,0),?) "
                    "ELSE max_6h END, samples=COALESCE(samples,0)+1 "
                    "WHERE resolved=0 AND asset_id=?",
                    (px, now, now, config.P30_WINDOW_SEC, px, asset))
                updated += 1
        c.commit()
    for r in rows:
        if now - r["start_ts"] >= config.TRACK_WINDOW_SEC:
            _resolve(r["forecast_id"])
    return updated


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
    journal.log_outcome(forecast_id=forecast_id, max_multiple=mult,
                        hit_30=int(bool(m6 and m6 >= 1 + config.P30_TARGET)),
                        hit_2x=int(bool(mult and mult >= 2)),
                        hit_3x=int(bool(mult and mult >= 3)),
                        hit_5x=int(bool(mult and mult >= 5)),
                        hit_10x=int(bool(mult and mult >= 10)),
                        exit_result=r["action"], realized_pnl_usd=None,
                        slippage_vs_plan=None)
    with journal._lock:
        journal.conn().execute(
            "UPDATE forecast_tracking SET resolved=1 WHERE forecast_id=?", (forecast_id,))
        journal.conn().commit()


def _unsampled(r):
    """Rows written before the samples column existed are judged by their
    timestamps (never updated means last_ts is within a second of start_ts)."""
    if r.keys() and "samples" in r.keys() and r["samples"] is not None:
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
            "(SELECT forecast_id FROM forecast_tracking WHERE COALESCE(samples,0) = 0 "
            "AND last_ts - start_ts < 1.0)")
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
        "SELECT t.action a, COUNT(*) n, AVG(o.max_multiple) avg_mult, "
        "SUM(o.hit_30) h30, SUM(o.hit_2x) h2, SUM(o.hit_3x) h3, SUM(o.hit_5x) h5 "
        "FROM outcomes o JOIN forecast_tracking t ON t.forecast_id=o.forecast_id "
        "WHERE o.ts > ? GROUP BY t.action ORDER BY n DESC", (since,))
    if not rows:
        return f"No forecasts have resolved yet (window {config.TRACK_WINDOW_SEC/3600:.0f}h)."
    out = [f"SCORECARD {days}d — resolved forecasts by the action taken"]
    for r in rows:
        n = r["n"] or 1
        out.append(f"{r['a'] or '?':10s} n={r['n']:4d}  +30%/6h {100*(r['h30'] or 0)/n:4.0f}%  "
                   f"peak {r['avg_mult'] or 0:.2f}x  2x {100*(r['h2'] or 0)/n:4.0f}%  "
                   f"3x {100*(r['h3'] or 0)/n:4.0f}%")
    return "\n".join(out)


def feedback(days=14):
    """The model's own recent record, per action, in the form it can act on:
    what it said (mean stated p2x) against what happened (share that reached
    2x). The first scorecard showed PASSes reaching 2x 31% of the time while
    the stated p2x on them averaged 0.05 -- a model six times too pessimistic
    about the very things it was seeing. Nothing was telling it. This does."""
    since = time.time() - days * 86400
    rows = journal.query(
        "SELECT t.action a, COUNT(*) n, AVG(f.p2x) stated, AVG(o.hit_2x) hit2, "
        "AVG(f.p30) stated30, AVG(o.hit_30) hit30, "
        "AVG(o.hit_3x) hit3, AVG(o.max_multiple) peak "
        "FROM outcomes o JOIN forecast_tracking t ON t.forecast_id=o.forecast_id "
        "JOIN forecasts f ON f.forecast_id=o.forecast_id "
        "WHERE o.ts > ? GROUP BY t.action", (since,))
    out = {}
    for r in rows:
        if not r["n"]:
            continue
        out[r["a"] or "?"] = {
            "resolved": r["n"],
            "stated_p30_mean": round(r["stated30"] or 0, 3),
            "reached_30pct_in_6h_share": round(r["hit30"] or 0, 3),
            "stated_p2x_mean": round(r["stated"] or 0, 3),
            "reached_2x_share": round(r["hit2"] or 0, 3),
            "reached_3x_share": round(r["hit3"] or 0, 3),
            "peak_multiple_mean": round(r["peak"] or 0, 2),
        }
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
