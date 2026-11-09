"""Agent trade memory: new tables in the Signal Lab database (.lab/lab.db). Nothing in the lab's own tables is
changed. Every observation, decision (accepted or rejected, with each Risk Guardian check), trade, review, strategy
status change, research experiment and config change is kept permanently.

  agent_log(id, t, level, kind, symbol, text, data)                         activity log (reasoning, actions)
  agent_observations(id, t, symbol, tf, bar_time, regime, volatility, quality_ok, data)   one per symbol per closed bar
  agent_decisions(id, t, symbol, tf, bar_time, strategy, direction, stage, verdict, setup, checks, trade_id, outcome, outcome_r)
        outcome: for setups NOT traded, what happened next (T1 first = missed opportunity, stop first = good rejection)
        UNIQUE(symbol, tf, strategy, direction, bar_time)  -> the same setup can never be acted on twice
  agent_trades(id, decision_id UNIQUE, ...)                                 paper (and, one day, live) positions
  agent_reviews(id, trade_id UNIQUE, t, classification, answers, narrative, narrative_source)
  agent_strategy_status(strategy PK, status, reason, t)                     active | suspended
  agent_experiments(id, t, symbol, tf, strategy, kind, params, result, verdict, status)
  agent_config_history(id, t, who, key, before, after)                      versioned config / strategy params
"""
from __future__ import annotations

import json
import time

from backend.lab import store

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_log (id INTEGER PRIMARY KEY AUTOINCREMENT, t INTEGER, level TEXT, kind TEXT, symbol TEXT, text TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS agent_observations (id INTEGER PRIMARY KEY AUTOINCREMENT, t INTEGER, symbol TEXT, tf TEXT, bar_time INTEGER,
  regime TEXT, volatility TEXT, quality_ok INTEGER, data TEXT, UNIQUE(symbol, tf, bar_time));
CREATE TABLE IF NOT EXISTS agent_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, t INTEGER, symbol TEXT, tf TEXT, bar_time INTEGER,
  strategy TEXT, direction TEXT, stage TEXT, verdict TEXT, setup TEXT, checks TEXT, trade_id INTEGER, outcome TEXT, outcome_r REAL,
  UNIQUE(symbol, tf, strategy, direction, bar_time));
CREATE TABLE IF NOT EXISTS agent_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id INTEGER UNIQUE, symbol TEXT, market TEXT, tf TEXT,
  strategy TEXT, side TEXT, qty REAL, planned_entry REAL, entry REAL, stop REAL, targets TEXT, opened_at INTEGER, closed_at INTEGER,
  exit REAL, exit_reason TEXT, gross REAL, costs REAL, pnl REAL, r_result REAL, risk_amount REAL, status TEXT, currency TEXT,
  regime TEXT, session TEXT, mode TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS agent_reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER UNIQUE, t INTEGER, classification TEXT,
  answers TEXT, narrative TEXT, narrative_source TEXT);
CREATE TABLE IF NOT EXISTS agent_strategy_status (strategy TEXT PRIMARY KEY, status TEXT, reason TEXT, t INTEGER);
CREATE TABLE IF NOT EXISTS agent_experiments (id INTEGER PRIMARY KEY AUTOINCREMENT, t INTEGER, symbol TEXT, tf TEXT, strategy TEXT,
  kind TEXT, params TEXT, result TEXT, verdict TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS agent_config_history (id INTEGER PRIMARY KEY AUTOINCREMENT, t INTEGER, who TEXT, key TEXT, before TEXT, after TEXT);
CREATE INDEX IF NOT EXISTS agent_log_t ON agent_log(t);
CREATE INDEX IF NOT EXISTS agent_trades_status ON agent_trades(status);
"""
_ready_for = None


def init() -> None:
    global _ready_for
    if _ready_for == str(store.DB):
        return
    with store._lock, store.conn() as c:
        c.executescript(SCHEMA)
    _ready_for = str(store.DB)


def q(sql: str, args: tuple = (), one: bool = False):
    init()
    return store.q(sql, args, one)


def execute(sql: str, args: tuple = ()) -> int:
    init()
    return store.execute(sql, args)


def now() -> int:
    return int(time.time())


def log(kind: str, text: str, symbol: str | None = None, data: dict | None = None, level: str = "info") -> None:
    try:
        execute("INSERT INTO agent_log(t, level, kind, symbol, text, data) VALUES(?,?,?,?,?,?)",
                (now(), level, kind, symbol, text[:2000], json.dumps(data, default=str) if data is not None else None))
    except Exception:                                     # noqa: BLE001 - logging must never stop the agent
        pass


def config_change(who: str, key: str, before, after) -> None:
    if before != after:
        execute("INSERT INTO agent_config_history(t, who, key, before, after) VALUES(?,?,?,?,?)",
                (now(), who, key, json.dumps(before, default=str), json.dumps(after, default=str)))


def jload(s, default=None):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default
