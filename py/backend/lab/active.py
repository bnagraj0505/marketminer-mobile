"""Locked trade setups. Once a setup qualifies as a SIGNAL on a CLOSED candle, its entry, stop and targets are frozen
and it stays on screen as the ACTIVE setup for that symbol and timeframe until price hits the stop or T3 (or the
setup expires). New candles never move its levels. While one is active, fresh setups in the same direction are not
shown as new trades; fresh setups in the opposite direction are shown as counter-signals (not locked).

Hits are checked candle by candle with each candle's high/low after the signal candle, including the candle still
forming, so a level counts as tapped the moment price trades through it. If one candle touches both the stop and a
target, the stop is counted first (no intrabar data, so the conservative reading).

Expiry: Indian intraday setups (NSE/BSE/MCX below 1d) expire when their session ends; everything else after
MAX_CANDLES candles without hitting the stop or T3."""
from __future__ import annotations

import json

from . import store

MAX_CANDLES = 120
INTRADAY_SESSION_MARKETS = ("NSE", "FNO", "MCX")

SCHEMA = """
CREATE TABLE IF NOT EXISTS active_setups (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, tf TEXT, strategy TEXT, direction TEXT,
  bar_time INTEGER, created INTEGER, entry REAL, stop REAL, t1 REAL, t2 REAL, t3 REAL, hits INTEGER DEFAULT 0, best REAL,
  status TEXT, outcome TEXT, exit REAL, r_result REAL, closed_at INTEGER, payload TEXT, UNIQUE(symbol, tf, strategy, bar_time));
"""
_ready = False


def _init() -> None:
    global _ready
    if not _ready:
        with store._lock, store.conn() as c:
            c.executescript(SCHEMA)
        _ready = True


def _row_out(r: dict, last: float | None) -> dict:
    s = json.loads(r["payload"] or "{}")
    d = 1 if r["direction"] == "LONG" else -1
    risk = abs(r["entry"] - r["stop"]) or 1e-12
    s.update({"locked": True, "active_id": r["id"], "status": r["status"], "outcome": r["outcome"], "hits": r["hits"],
              "entry": r["entry"], "stop": r["stop"], "targets": [r["t1"], r["t2"], r["t3"]], "time": r["bar_time"],
              "locked_at": r["bar_time"], "closed_at": r["closed_at"], "exit": r["exit"], "r_result": r["r_result"],
              "best_r": round((r["best"] - r["entry"]) * d / risk, 2) if r["best"] is not None else 0.0,
              "now_r": round((last - r["entry"]) * d / risk, 2) if last is not None and r["status"] == "active" else None,
              "provisional": False})
    return s


def _walk(r: dict, bars, step: int, session_of, market: str, tf_seconds: int) -> dict | None:
    """Replay the candles after the signal candle. Returns the fields to update, or None if nothing changed."""
    d = 1 if r["direction"] == "LONG" else -1
    tg = [r["t1"], r["t2"], r["t3"]]
    hits, best = r["hits"] or 0, r["best"] if r["best"] is not None else r["entry"]
    after = [b for b in bars if b.time > r["bar_time"]]
    sess0 = session_of(r["bar_time"]) if session_of else None
    upd: dict = {}
    for k, b in enumerate(after):
        if (session_of and market in INTRADAY_SESSION_MARKETS and tf_seconds < 86400 and session_of(b.time) != sess0):
            upd.update(status="closed", outcome="expired (session ended)", exit=after[k - 1].close if k else r["entry"], closed_at=b.time)
            break
        if (b.low <= r["stop"]) if d > 0 else (b.high >= r["stop"]):
            upd.update(status="closed", outcome=f"stop hit{' after T' + str(hits) if hits else ''}", exit=r["stop"], closed_at=b.time)
            break
        best = max(best, b.high) if d > 0 else min(best, b.low)
        while hits < 3 and ((b.high >= tg[hits]) if d > 0 else (b.low <= tg[hits])):
            hits += 1
        if hits == 3:
            upd.update(status="closed", outcome="T3 hit", exit=tg[2], closed_at=b.time)
            break
        if k + 1 >= MAX_CANDLES:
            upd.update(status="closed", outcome=f"expired ({MAX_CANDLES} candles)", exit=b.close, closed_at=b.time)
            break
    upd["hits"], upd["best"] = hits, best
    if "exit" in upd:
        upd["r_result"] = round((upd["exit"] - r["entry"]) * d / (abs(r["entry"] - r["stop"]) or 1e-12), 2)
    return upd if (upd.get("status") or hits != (r["hits"] or 0) or best != r["best"]) else None


def update(symbol: str, tf: str, bars, session_of, market: str, tf_seconds: int) -> None:
    """Advance every active setup of this symbol/timeframe through the latest candles."""
    _init()
    if not bars:
        return
    step = bars[1].time - bars[0].time if len(bars) > 1 else tf_seconds
    for r in store.q("SELECT * FROM active_setups WHERE symbol=? AND tf=? AND status='active'", (symbol, tf)):
        u = _walk(r, bars, step, session_of, market, tf_seconds)
        if u:
            cols = ", ".join(f"{k}=?" for k in u)
            store.execute(f"UPDATE active_setups SET {cols} WHERE id=?", (*u.values(), r["id"]))


def lock(symbol: str, tf: str, setups: list[dict], min_score: float) -> None:
    """Lock the best qualifying closed-candle setup, unless this symbol/timeframe already has an active one."""
    _init()
    if store.q("SELECT id FROM active_setups WHERE symbol=? AND tf=? AND status='active' LIMIT 1", (symbol, tf), one=True):
        return
    for s in sorted(setups, key=lambda x: -x["score"]):
        if s["score"] < min_score:
            continue
        keep = {k: v for k, v in s.items() if k not in ("signal", "provisional")}
        try:
            store.execute("INSERT OR IGNORE INTO active_setups(symbol,tf,strategy,direction,bar_time,created,entry,stop,t1,t2,t3,hits,best,status,payload)"
                          " VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?,'active',?)",
                          (symbol, tf, s["strategy"], s["direction"], s["time"], store.now(), s["entry"], s["stop"],
                           s["targets"][0], s["targets"][1], s["targets"][2], s["entry"], json.dumps(keep, default=str)))
        except Exception:                                 # noqa: BLE001 - locking must never break analysis
            pass
        return


def merge(symbol: str, tf: str, fresh: list[dict], last: float | None) -> tuple[list[dict], list[dict]]:
    """Setups to show: the active (locked) one first, then fresh setups that are not in its direction. Also returns
    the last few closed locked setups for this symbol/timeframe."""
    _init()
    act = [_row_out(r, last) for r in store.q("SELECT * FROM active_setups WHERE symbol=? AND tf=? AND status='active' ORDER BY id DESC", (symbol, tf))]
    recent = [_row_out(r, None) for r in store.q("SELECT * FROM active_setups WHERE symbol=? AND tf=? AND status='closed' ORDER BY closed_at DESC LIMIT 5",
                                                 (symbol, tf))]
    if not act:
        return fresh, recent
    a = act[0]
    out = [a]
    a["counter_signals"] = []
    for s in fresh:
        if s["direction"] == a["direction"]:
            continue                                      # same direction: the locked trade already covers it
        s["counter"] = True
        if s.get("signal"):
            a["counter_signals"].append(f"{s['direction']} {s['name']} {s['score']:.0f}")
        out.append(s)
    return out, recent

