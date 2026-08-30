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
  forecast_id INTEGER, detail TEXT
);
CREATE TABLE IF NOT EXISTS pending_approvals (
  code TEXT PRIMARY KEY, ts REAL, expires REAL, kind TEXT, asset_id TEXT,
  ticket_id INTEGER, status TEXT
);
"""


def init():
    journal.conn().executescript(STATE_SCHEMA)
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
            c.execute("UPDATE positions SET qty=qty+?, cost_basis_usd=cost_basis_usd+? WHERE asset_id=?",
                      (dqty, dcost, asset_id))
        else:
            c.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (asset_id, venue, chain, dqty, dcost, time.time(), invalidation,
                       json.dumps(plan or {}), entry_liq, group))
        c.commit()
    journal.log_event("position_change", asset_id, {"dqty": dqty, "dcost": dcost})


def close_position(asset_id):
    with journal._lock:
        journal.conn().execute("DELETE FROM positions WHERE asset_id=?", (asset_id,))
        journal.conn().commit()
    journal.log_event("position_closed", asset_id)


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
