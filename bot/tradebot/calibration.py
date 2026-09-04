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
    with journal._lock:
        journal.conn().execute(
            "INSERT OR IGNORE INTO forecast_tracking "
            "(forecast_id, asset_id, action, start_ts, start_price, max_price, last_ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (forecast_id, asset_id, action, time.time(), price, price, time.time()))
        journal.conn().commit()


def _rows():
    return journal.query(
        "SELECT * FROM forecast_tracking WHERE resolved=0 ORDER BY start_ts LIMIT ?",
        (config.TRACK_BATCH,))


def tick():
    """One sampling pass. Cheap: it reuses marks the monitor already fetches."""
    rows = _rows()
    if not rows:
        return 0
    marks, _ = marketdata.marks([r["asset_id"] for r in rows])
    now = time.time()
    updated = 0
    for r in rows:
        px = marks.get(r["asset_id"])
        if px and px > 0:
            with journal._lock:
                journal.conn().execute(
                    "UPDATE forecast_tracking SET max_price=MAX(max_price,?), last_ts=? "
                    "WHERE forecast_id=?", (px, now, r["forecast_id"]))
                journal.conn().commit()
            updated += 1
        if now - r["start_ts"] >= config.TRACK_WINDOW_SEC:
            _resolve(r["forecast_id"])
    return updated


def _resolve(forecast_id):
    r = journal.query("SELECT * FROM forecast_tracking WHERE forecast_id=?",
                      (forecast_id,))
    if not r:
        return
    r = r[0]
    start, top = r["start_price"] or 0, r["max_price"] or 0
    mult = (top / start) if start > 0 else None
    journal.log_outcome(forecast_id=forecast_id, max_multiple=mult,
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


def scorecard(days=30):
    """What the bot predicted vs what happened, split by the action it chose.
    The BUY_NOW vs PASS comparison is the whole point: passing on things that
    went to 3x is a different failure from buying things that went to zero."""
    since = time.time() - days * 86400
    rows = journal.query(
        "SELECT t.action a, COUNT(*) n, AVG(o.max_multiple) avg_mult, "
        "SUM(o.hit_2x) h2, SUM(o.hit_3x) h3, SUM(o.hit_5x) h5 "
        "FROM outcomes o JOIN forecast_tracking t ON t.forecast_id=o.forecast_id "
        "WHERE o.ts > ? GROUP BY t.action ORDER BY n DESC", (since,))
    if not rows:
        return f"No forecasts have resolved yet (window {config.TRACK_WINDOW_SEC/3600:.0f}h)."
    out = [f"SCORECARD {days}d — resolved forecasts by the action taken"]
    for r in rows:
        n = r["n"] or 1
        out.append(f"{r['a'] or '?':10s} n={r['n']:4d}  avg peak {r['avg_mult'] or 0:.2f}x  "
                   f"2x {100*(r['h2'] or 0)/n:4.0f}%  3x {100*(r['h3'] or 0)/n:4.0f}%  "
                   f"5x {100*(r['h5'] or 0)/n:4.0f}%")
    return "\n".join(out)


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
