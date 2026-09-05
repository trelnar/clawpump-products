"""Signal storage and the per-asset features derived from it.

A signal event is one observation of one asset from one source: a Telegram
call, a Reddit post, a pump.fun graduation, a trending-pool listing. Events
are cheap and numerous; what the research layer needs is the SHAPE of them --
is attention accelerating, and is it coming from more than one place -- which
is what features() computes. Raw counts are what paid promotion inflates;
acceleration and breadth are much harder to fake.
"""
import hashlib
import json
import time

from .. import config, journal

SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, source TEXT NOT NULL, asset_id TEXT NOT NULL,
  kind TEXT NOT NULL, weight REAL NOT NULL DEFAULT 1.0,
  ref TEXT, dedupe TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_sig_asset_ts ON signal_events(asset_id, ts);
CREATE INDEX IF NOT EXISTS idx_sig_ts ON signal_events(ts);
CREATE TABLE IF NOT EXISTS signal_first_seen (
  asset_id TEXT PRIMARY KEY, ts REAL NOT NULL, source TEXT
);
CREATE TABLE IF NOT EXISTS signal_source_runs (
  source TEXT PRIMARY KEY, last_ts REAL, last_ok INTEGER, last_count INTEGER, last_error TEXT
);
"""

# How much one event of each kind counts. A graduation is a hard on-chain
# event; a trending listing is a soft one; a mention is a mention.
KIND_WEIGHT = {
    "launch": 1.0, "graduation": 3.0, "trending": 1.5, "new_pool": 1.0,
    "mention": 1.0, "call": 2.0, "post": 1.0, "holder_growth": 2.0,
}


def init():
    journal.conn().executescript(SCHEMA)


def _dedupe_key(source, asset_id, kind, ref):
    raw = f"{source}|{asset_id}|{kind}|{ref or ''}"
    return hashlib.sha1(raw.encode()).hexdigest()


def record(source, asset_id, kind, ref=None, weight=None, ts=None):
    """Insert one event. Idempotent on (source, asset, kind, ref): a source
    re-reporting the same post on every poll must not count as new attention.
    Returns True when the event was new."""
    ts = ts or time.time()
    w = KIND_WEIGHT.get(kind, 1.0) if weight is None else weight
    key = _dedupe_key(source, asset_id, kind, ref)
    with journal._lock:
        c = journal.conn()
        cur = c.execute(
            "INSERT OR IGNORE INTO signal_events (ts, source, asset_id, kind, weight, ref, dedupe) "
            "VALUES (?,?,?,?,?,?,?)", (ts, source, asset_id, kind, w, ref, key))
        new = cur.rowcount > 0
        if new:
            c.execute("INSERT OR IGNORE INTO signal_first_seen (asset_id, ts, source) VALUES (?,?,?)",
                      (asset_id, ts, source))
        c.commit()
    return new


def note_run(source, ok, count=0, error=None):
    with journal._lock:
        journal.conn().execute(
            "INSERT INTO signal_source_runs (source, last_ts, last_ok, last_count, last_error) "
            "VALUES (?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET last_ts=excluded.last_ts, "
            "last_ok=excluded.last_ok, last_count=excluded.last_count, last_error=excluded.last_error",
            (source, time.time(), 1 if ok else 0, count, (error or "")[:200]))
        journal.conn().commit()


def source_health():
    return journal.query("SELECT * FROM signal_source_runs ORDER BY source")


def _sum(asset_id, since, until=None):
    q = "SELECT COALESCE(SUM(weight),0) w, COUNT(*) n FROM signal_events WHERE asset_id=? AND ts>=?"
    args = [asset_id, since]
    if until is not None:
        q += " AND ts<?"
        args.append(until)
    r = journal.query(q, tuple(args))[0]
    return float(r["w"] or 0), int(r["n"] or 0)


def features(asset_id, now=None):
    """The shape of attention on one asset.

    accel   : weighted events in the last hour vs the per-hour rate over the
              six hours before that. >1 means attention is rising; a fresh
              asset with nothing before has a baseline floor so a single
              mention is not infinite acceleration.
    breadth : distinct sources in the last 6h. Two independent channels
              saying the same thing is worth far more than one saying it twice.
    kinds   : the hard events seen (graduation, launch, new_pool...) -- these
              carry timing the model cannot get from a price.
    """
    now = now or time.time()
    h1, _ = _sum(asset_id, now - 3600)
    h6, n6 = _sum(asset_id, now - 6 * 3600)
    prior_w, _ = _sum(asset_id, now - 7 * 3600, now - 3600)
    prior_rate = prior_w / 6.0
    accel = round(h1 / max(prior_rate, 0.5), 2)
    rows = journal.query(
        "SELECT source, kind, COUNT(*) n FROM signal_events WHERE asset_id=? AND ts>=? "
        "GROUP BY source, kind", (asset_id, now - 6 * 3600))
    sources = sorted({r["source"] for r in rows})
    kinds = sorted({r["kind"] for r in rows})
    fs = journal.query("SELECT ts, source FROM signal_first_seen WHERE asset_id=?", (asset_id,))
    first_seen_min = round((now - fs[0]["ts"]) / 60, 1) if fs else None
    return {
        "mentions_1h": round(h1, 1), "mentions_6h": round(h6, 1), "events_6h": n6,
        "accel": accel, "breadth": len(sources), "sources": sources, "kinds": kinds,
        "first_seen_min": first_seen_min, "first_source": fs[0]["source"] if fs else None,
    }


def rising(limit=40, now=None, min_events=1):
    """Assets ranked by (acceleration x breadth), most interesting first. This
    is the discovery list: not 'what is being promoted' but 'where is
    attention moving, from more than one direction'."""
    now = now or time.time()
    rows = journal.query(
        "SELECT asset_id, COUNT(*) n FROM signal_events WHERE ts>=? GROUP BY asset_id "
        "HAVING n>=? ORDER BY n DESC LIMIT 400", (now - 6 * 3600, min_events))
    scored = []
    for r in rows:
        f = features(r["asset_id"], now)
        hard = 2.0 if any(k in f["kinds"] for k in ("graduation", "launch", "new_pool")) else 1.0
        score = f["accel"] * max(f["breadth"], 1) * hard
        scored.append((score, r["asset_id"], f))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [{"asset_id": a, "score": round(s, 2), **f} for s, a, f in scored[:limit]]


def prune(max_age_days=None):
    """Signals are only useful fresh. Keep the store small."""
    days = max_age_days or config.SIGNAL_RETENTION_DAYS
    with journal._lock:
        journal.conn().execute("DELETE FROM signal_events WHERE ts < ?",
                               (time.time() - days * 86400,))
        journal.conn().commit()


def dump(asset_id, limit=20):
    return journal.query(
        "SELECT datetime(ts,'unixepoch') t, source, kind, weight, substr(ref,1,60) ref "
        "FROM signal_events WHERE asset_id=? ORDER BY ts DESC LIMIT ?", (asset_id, limit))


def as_json(x):
    return json.dumps(x, default=str)
