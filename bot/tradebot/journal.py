"""trade-journal skill: append-only SQLite record. Rows are never edited or
deleted; corrections are new rows. Every skill writes here."""
import json
import os
import sqlite3
import threading
import time

from . import config

_lock = threading.Lock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
  forecast_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, asset_id TEXT NOT NULL, action TEXT NOT NULL,
  entry_price REAL, buy_zone_lo REAL, buy_zone_hi REAL,
  target_2x REAL, target_higher REAL, predicted_window TEXT,
  p2x REAL, p3x REAL, p5x REAL, p10x REAL, confidence REAL,
  size_usd REAL, evidence_state TEXT, shadow INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders (
  order_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, client_oid TEXT UNIQUE, venue TEXT, asset_id TEXT,
  side TEXT, notional_usd REAL, limit_price REAL, status TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS fills (
  fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, client_oid TEXT, asset_id TEXT, side TEXT,
  qty REAL, price REAL, fee_usd REAL, venue TEXT, tx_ref TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
  approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, code TEXT, asset_id TEXT, kind TEXT,
  event TEXT, raw_text TEXT, sender TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
  alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, kind TEXT, asset_id TEXT, body TEXT, delivered INTEGER
);
CREATE TABLE IF NOT EXISTS outcomes (
  outcome_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, forecast_id INTEGER, max_multiple REAL,
  hit_2x INTEGER, hit_3x INTEGER, hit_5x INTEGER, hit_10x INTEGER,
  exit_result TEXT, realized_pnl_usd REAL, slippage_vs_plan REAL
);
CREATE TABLE IF NOT EXISTS exit_checks (
  check_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, asset_id TEXT, contract_address TEXT,
  result TEXT, fail_reason TEXT, measured_values TEXT
);
CREATE TABLE IF NOT EXISTS discovery_inputs (
  input_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, asset_id TEXT, source TEXT, payload TEXT
);
CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, kind TEXT NOT NULL, asset_id TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON events(kind, ts);
CREATE INDEX IF NOT EXISTS idx_discovery_ts ON discovery_inputs(ts);
"""


def conn():
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
    return _conn


def _insert(table, row):
    with _lock:
        c = conn()
        cols = ",".join(row)
        q = ",".join("?" * len(row))
        cur = c.execute(f"INSERT INTO {table} ({cols}) VALUES ({q})", list(row.values()))
        c.commit()
        return cur.lastrowid


def now():
    return time.time()


def log_event(kind, asset_id=None, detail=None):
    return _insert("events", {"ts": now(), "kind": kind, "asset_id": asset_id,
                              "detail": json.dumps(detail) if isinstance(detail, (dict, list)) else detail})


def log_forecast(f):
    f.setdefault("ts", now())
    return _insert("forecasts", f)


def log_order(**kw):
    kw.setdefault("ts", now())
    return _insert("orders", kw)


def log_fill(**kw):
    kw.setdefault("ts", now())
    return _insert("fills", kw)


def log_approval(**kw):
    kw.setdefault("ts", now())
    return _insert("approvals", kw)


def log_alert(kind, body, asset_id=None, delivered=1):
    return _insert("alerts", {"ts": now(), "kind": kind, "asset_id": asset_id,
                              "body": body, "delivered": delivered})


def log_exit_check(asset_id, contract_address, result, fail_reason=None, measured=None):
    return _insert("exit_checks", {"ts": now(), "asset_id": asset_id,
                                   "contract_address": contract_address, "result": result,
                                   "fail_reason": fail_reason,
                                   "measured_values": json.dumps(measured or {})})


def log_discovery(asset_id, source, payload):
    return _insert("discovery_inputs", {"ts": now(), "asset_id": asset_id, "source": source,
                                        "payload": json.dumps(payload)[:20000]})


def log_outcome(**kw):
    kw.setdefault("ts", now())
    return _insert("outcomes", kw)


def query(sql, args=()):
    with _lock:
        cur = conn().execute(sql, args)
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, r)) for r in cur.fetchall()]
