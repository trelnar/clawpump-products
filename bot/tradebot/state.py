"""portfolio-state skill: single source of truth. Positions, cash, whitelist,
halt modes, pending approvals/tickets, and the flow-adjusted rolling 24h value
series that risk reads for the halt."""
import json
import time

from . import config, journal

MODES = ("NORMAL", "EMERGENCY_HALT", "USER_STOP", "RECON_FREEZE", "SELL_ONLY")

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
  asset_id TEXT PRIMARY KEY, venue TEXT, chain TEXT, qty REAL, cost_basis_usd REAL,
  entry_ts REAL, invalidation_price REAL, plan TEXT, entry_liquidity_usd REAL,
  correlation_group TEXT
);
CREATE TABLE IF NOT EXISTS cash (venue TEXT PRIMARY KEY, usd REAL);
CREATE TABLE IF NOT EXISTS whitelist (
  asset_id TEXT PRIMARY KEY, venue_or_chain TEXT, approved_ts REAL, revoked_ts REAL
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS value_series (ts REAL, adj_value REAL, flows REAL);
CREATE TABLE IF NOT EXISTS tickets (
  ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, asset_id TEXT, venue TEXT, chain TEXT, action TEXT, status TEXT,
  notional_usd REAL, buy_zone_lo REAL, buy_zone_hi REAL, invalidation_price REAL,
  forecast_id INTEGER, detail TEXT, plan TEXT, sell_fraction REAL
);
CREATE TABLE IF NOT EXISTS pending_approvals (
  code TEXT PRIMARY KEY, ts REAL, expires REAL, kind TEXT, asset_id TEXT,
  ticket_id INTEGER, status TEXT
);
"""


def _migrate():
    """Additive column migrations. The DB outlives any single deploy."""
    for table, col, decl in (("tickets", "plan", "TEXT"),
                             ("tickets", "sell_fraction", "REAL")):
        cols = {r["name"] for r in journal.query(f"PRAGMA table_info({table})")}
        if col not in cols:
            with journal._lock:
                journal.conn().execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                journal.conn().commit()
            journal.log_event("schema_migrate", detail=f"{table}.{col}")


def init():
    journal.conn().executescript(STATE_SCHEMA)
    _migrate()
    if get_mode() is None:
        set_mode("SELL_ONLY", reason="cold start; awaiting reconciliation")
    if get_kv("phase") is None:
        set_kv("phase", "0")


# --- kv ---------------------------------------------------------------------
def get_kv(k, default=None):
    r = journal.query("SELECT v FROM kv WHERE k=?", (k,))
    return r[0]["v"] if r else default


def set_kv(k, v):
    with journal._lock:
        journal.conn().execute(
            "INSERT INTO kv (k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, str(v)))
        journal.conn().commit()


# --- halt modes -------------------------------------------------------------
def get_mode():
    return get_kv("mode")


def set_mode(mode, reason=""):
    assert mode in MODES
    set_kv("mode", mode)
    journal.log_event("mode_change", detail={"mode": mode, "reason": reason})


def buying_allowed():
    return get_mode() == "NORMAL"


# --- phase ------------------------------------------------------------------
def phase():
    return int(get_kv("phase", "0"))


def size_factor():
    return config.PHASE_SIZE_FACTOR.get(phase(), 0.0)


# --- positions & cash -------------------------------------------------------
def positions():
    return journal.query("SELECT * FROM positions WHERE qty > 0")


def get_position(asset_id):
    r = journal.query("SELECT * FROM positions WHERE asset_id=?", (asset_id,))
    return r[0] if r else None


def upsert_position(asset_id, venue, chain, dqty, dcost, entry_liq=None,
                    invalidation=None, plan=None, group=None):
    with journal._lock:
        c = journal.conn()
        cur = c.execute("SELECT qty, cost_basis_usd FROM positions WHERE asset_id=?", (asset_id,))
        row = cur.fetchone()
        if row:
            c.execute("UPDATE positions SET qty=qty+?, cost_basis_usd=cost_basis_usd+? "
                      "WHERE asset_id=?", (dqty, dcost, asset_id))
            # An ADD raises the stop. Dropping the new invalidation left a
            # doubled position protected at the original entry's level.
            if invalidation:
                c.execute("UPDATE positions SET invalidation_price=? WHERE asset_id=?",
                          (invalidation, asset_id))
        else:
            # A fresh entry starts with a clean slate: any completion markers
            # left by a previous life of this asset must not disarm its legs.
            c.execute("DELETE FROM kv WHERE k=?", (f"plan_done:{asset_id}",))
            c.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (asset_id, venue, chain, dqty, dcost, time.time(), invalidation,
                       json.dumps(plan or {}), entry_liq, group))
        c.commit()
    journal.log_event("position_change", asset_id, {"dqty": dqty, "dcost": dcost})


def close_position(asset_id):
    with journal._lock:
        journal.conn().execute("DELETE FROM positions WHERE asset_id=?", (asset_id,))
        journal.conn().execute("DELETE FROM kv WHERE k=?", (f"plan_done:{asset_id}",))
        journal.conn().commit()
    journal.log_event("position_closed", asset_id)


def position_plan(asset_id_or_row):
    """The standing profit plan as a dict. Never raises on malformed JSON --
    a bad plan must not take the monitor loop down with it."""
    row = (asset_id_or_row if isinstance(asset_id_or_row, dict)
           else get_position(asset_id_or_row))
    if not row:
        return {}
    try:
        return json.loads(row.get("plan") or "{}") or {}
    except (TypeError, ValueError):
        journal.log_event("bad_plan_json", row.get("asset_id"))
        return {}


def set_position_plan(asset_id, plan):
    """Completion survives a plan revision. Legs are identified by content, so
    a re-sent level stays done and only a genuinely new level arms -- clearing
    completion here would re-sell the same position on every model tweak."""
    plan = plan or {}
    if position_plan(asset_id) == plan:
        return False
    with journal._lock:
        journal.conn().execute("UPDATE positions SET plan=? WHERE asset_id=?",
                               (json.dumps(plan), asset_id))
        journal.conn().commit()
    journal.log_event("plan_update", asset_id, plan)
    return True


def leg_key(leg):
    """Completion is keyed by the LEVEL, not the list index and not the size.

    An index shifts whenever the model revises a plan, which re-armed legs that
    had already sold. Including the fraction has the same flaw one step down: a
    model that nudges 50% to 25% at the same 2x would sell there twice. A price
    level, once taken, is taken -- a second sale needs a second level."""
    return f"{float(leg['multiple']):.6g}x"


def plan_legs_done(asset_id):
    try:
        return set(json.loads(get_kv(f"plan_done:{asset_id}", "[]")))
    except (TypeError, ValueError):
        return set()


def mark_plan_leg_done(asset_id, leg):
    """No-op once the position is gone: writing after close_position resurrects
    the row it just deleted and pre-disarms the leg on the next entry."""
    if not get_position(asset_id):
        return False
    done = plan_legs_done(asset_id) | {leg_key(leg)}
    set_kv(f"plan_done:{asset_id}", json.dumps(sorted(done)))
    return True


def cash(venue=None):
    if venue:
        r = journal.query("SELECT usd FROM cash WHERE venue=?", (venue,))
        return r[0]["usd"] if r else 0.0
    return {r["venue"]: r["usd"] for r in journal.query("SELECT * FROM cash")}


def set_cash(venue, usd):
    with journal._lock:
        journal.conn().execute(
            "INSERT INTO cash (venue,usd) VALUES (?,?) ON CONFLICT(venue) DO UPDATE SET usd=excluded.usd",
            (venue, usd))
        journal.conn().commit()


# --- whitelist --------------------------------------------------------------
def is_whitelisted(asset_id):
    r = journal.query(
        "SELECT 1 FROM whitelist WHERE asset_id=? AND revoked_ts IS NULL", (asset_id,))
    return bool(r)


def whitelist_add(asset_id, venue_or_chain):
    with journal._lock:
        journal.conn().execute(
            "INSERT INTO whitelist (asset_id,venue_or_chain,approved_ts,revoked_ts) VALUES (?,?,?,NULL) "
            "ON CONFLICT(asset_id) DO UPDATE SET revoked_ts=NULL, approved_ts=excluded.approved_ts",
            (asset_id, venue_or_chain, time.time()))
        journal.conn().commit()
    journal.log_event("whitelist_add", asset_id)


def whitelist_revoke(asset_id):
    with journal._lock:
        journal.conn().execute("UPDATE whitelist SET revoked_ts=? WHERE asset_id=?",
                               (time.time(), asset_id))
        journal.conn().commit()
    journal.log_event("whitelist_revoke", asset_id)


# --- portfolio value & rolling series ---------------------------------------
def total_value(marks):
    """marks: {asset_id: usd_price_per_unit}. Conservative: unpriced positions
    count at cost basis * 0 only if stale beyond policy (caller handles)."""
    total = sum(cash().values())
    for p in positions():
        m = marks.get(p["asset_id"])
        total += (p["qty"] * m) if m is not None else 0.0
    return total


def sample_value(value, flows=0.0):
    with journal._lock:
        c = journal.conn()
        c.execute("INSERT INTO value_series (ts, adj_value, flows) VALUES (?,?,?)",
                  (time.time(), value, flows))
        c.execute("DELETE FROM value_series WHERE ts < ?", (time.time() - 8 * 86400,))
        c.commit()


def trailing_max(window_sec):
    r = journal.query("SELECT MAX(adj_value) m FROM value_series WHERE ts >= ?",
                      (time.time() - window_sec,))
    return r[0]["m"] if r and r[0]["m"] is not None else None


# --- tickets ----------------------------------------------------------------
def add_ticket(**kw):
    kw.setdefault("ts", time.time())
    kw.setdefault("status", "new")
    with journal._lock:
        c = journal.conn()
        cols = ",".join(kw)
        cur = c.execute(f"INSERT INTO tickets ({cols}) VALUES ({','.join('?'*len(kw))})",
                        list(kw.values()))
        c.commit()
        return cur.lastrowid


def tickets(status="new"):
    return journal.query("SELECT * FROM tickets WHERE status=? ORDER BY ts", (status,))


def set_ticket_status(ticket_id, status):
    with journal._lock:
        journal.conn().execute("UPDATE tickets SET status=? WHERE ticket_id=?", (status, ticket_id))
        journal.conn().commit()


# --- pending approvals ------------------------------------------------------
def add_pending(code, kind, asset_id, ticket_id, expiry_sec):
    with journal._lock:
        journal.conn().execute(
            "INSERT INTO pending_approvals VALUES (?,?,?,?,?,?,?)",
            (code, time.time(), time.time() + expiry_sec, kind, asset_id, ticket_id, "pending"))
        journal.conn().commit()


def get_pending(code):
    r = journal.query("SELECT * FROM pending_approvals WHERE code=?", (code,))
    return r[0] if r else None


def resolve_pending(code, status):
    with journal._lock:
        journal.conn().execute("UPDATE pending_approvals SET status=? WHERE code=?", (status, code))
        journal.conn().commit()


def expire_pendings():
    expired = journal.query(
        "SELECT * FROM pending_approvals WHERE status='pending' AND expires < ?", (time.time(),))
    for p in expired:
        resolve_pending(p["code"], "expired")
        set_ticket_status(p["ticket_id"], "expired")
        journal.log_approval(code=p["code"], asset_id=p["asset_id"], kind=p["kind"],
                             event="expired", raw_text=None, sender=None)
    return expired
