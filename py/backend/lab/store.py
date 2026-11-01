"""SQLite store for the Signal Lab (.lab/lab.db next to the app). One user on one PC: SQLite is enough; the schema
is plain SQL so it can move to PostgreSQL/TimescaleDB unchanged apart from the column types.

Tables
  candles(symbol, tf, t, o, h, l, c, v, source)            recorded history, PRIMARY KEY (symbol, tf, t)
  chain_snapshots(underlying, t, expiry, spot, atm_iv, pcr, payload)   options chain every 15 minutes
  signals(id, created, symbol, tf, strategy, direction, score, bull, bear, neutral, entry, stop, t1, t2, t3,
          rr, status, outcome, r_result, closed_at, payload)          every qualified setup (the journal)
  trades(id, signal_id, symbol, market, side, qty, entry, stop, targets, opened_at, closed_at, exit, exit_reason,
         gross, costs, pnl, r_result, status, currency, payload)      paper trades
  alerts(id, key UNIQUE, t, symbol, tf, kind, text, seen)             de-duplicated by key
  backtests(id, created, symbol, tf, params, result)
  settings(key PRIMARY KEY, value)                                    JSON values
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / ".lab" / "lab.db"
_lock = threading.Lock()
SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (symbol TEXT, tf TEXT, t INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL, source TEXT,
  PRIMARY KEY (symbol, tf, t));
CREATE TABLE IF NOT EXISTS chain_snapshots (underlying TEXT, t INTEGER, expiry TEXT, spot REAL, atm_iv REAL, pcr REAL, payload TEXT,
  PRIMARY KEY (underlying, t));
CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY AUTOINCREMENT, created INTEGER, symbol TEXT, tf TEXT, strategy TEXT,
  direction TEXT, score REAL, bull REAL, bear REAL, neutral REAL, entry REAL, stop REAL, t1 REAL, t2 REAL, t3 REAL, rr REAL,
  status TEXT, outcome TEXT, r_result REAL, closed_at INTEGER, bar_time INTEGER, payload TEXT,
  UNIQUE(symbol, tf, strategy, bar_time));
CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER, symbol TEXT, market TEXT, side TEXT,
  qty REAL, entry REAL, stop REAL, targets TEXT, opened_at INTEGER, closed_at INTEGER, exit REAL, exit_reason TEXT, gross REAL,
  costs REAL, pnl REAL, r_result REAL, status TEXT, currency TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, t INTEGER, symbol TEXT, tf TEXT,
  kind TEXT, text TEXT, seen INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS backtests (id INTEGER PRIMARY KEY AUTOINCREMENT, created INTEGER, symbol TEXT, tf TEXT, params TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""


def conn() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def q(sql: str, args: tuple = (), one: bool = False):
    with _lock, conn() as c:
        cur = c.execute(sql, args)
        rows = cur.fetchall()
        c.commit()
        if one:
            return dict(rows[0]) if rows else None
        return [dict(r) for r in rows]


def execute(sql: str, args: tuple = ()) -> int:
    with _lock, conn() as c:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid


def many(sql: str, rows: list[tuple]) -> None:
    with _lock, conn() as c:
        c.executemany(sql, rows)
        c.commit()


def get_setting(key: str, default=None):
    r = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    if not r:
        return default
    try:
        return json.loads(r["value"])
    except ValueError:
        return default


def set_setting(key: str, value) -> None:
    execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))


def save_candles(symbol: str, tf: str, bars, source: str) -> int:
    rows = [(symbol, tf, b.time, b.open, b.high, b.low, b.close, b.volume, source) for b in bars]
    many("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def load_candles(symbol: str, tf: str, since: int = 0):
    from .candle import Candle
    return [Candle(r["t"], r["o"], r["h"], r["l"], r["c"], r["v"]) for r in
            q("SELECT t,o,h,l,c,v FROM candles WHERE symbol=? AND tf=? AND t>=? ORDER BY t", (symbol, tf, since))]


def add_alert(key: str, t: int, symbol: str, tf: str, kind: str, text: str) -> bool:
    try:
        execute("INSERT INTO alerts(key,t,symbol,tf,kind,text) VALUES(?,?,?,?,?,?)", (key, t, symbol, tf, kind, text))
        return True
    except sqlite3.IntegrityError:
        return False


def now() -> int:
    return int(time.time())
