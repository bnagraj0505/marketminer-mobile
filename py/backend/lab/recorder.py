"""Background recorder: every 15 minutes (setting) it
  1. saves closed candles for the watch-list (symbols x timeframes) to SQLite,
  2. snapshots the option chains (Indian chains only while NSE is open, crypto chains always),
  3. updates open paper trades and resolves journal signals (T1 vs stop, measured - never assumed),
  4. scans the watch-list for new signals and raises de-duplicated alerts.
Requests run one after another with a pause, which keeps FYERS well under its 3/second and 30/minute limits.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import time

from . import data as D
from . import store
from . import options as O
from . import paper

DEFAULTS = {"enabled": True, "interval_min": 15, "symbols": ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX", "SHARK:BTCUSDT", "SHARK:ETHUSDT"],
            "tfs": ["5m", "15m", "1h", "1d"], "chains": ["NIFTY", "BANKNIFTY", "BTC", "ETH"], "scan_tf": "15m"}
STATUS: dict = {"running": False, "last_run": None, "last_error": None, "runs": 0, "saved_candles": 0, "snapshots": 0}
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
_task: asyncio.Task | None = None


def settings() -> dict:
    return {**DEFAULTS, **(store.get_setting("recorder") or {})}


def nse_open(now: float | None = None) -> bool:
    t = dt.datetime.fromtimestamp(now or time.time(), IST)
    m = t.hour * 60 + t.minute
    return t.weekday() < 5 and 555 <= m <= 930


async def resolve_signals() -> int:
    """For journal signals still open: did price reach T1 or the stop first after the signal candle closed?"""
    n = 0
    for s in store.q("SELECT * FROM signals WHERE status='open' ORDER BY id DESC LIMIT 200"):
        try:
            bars, _ = await D.candles(s["symbol"], s["tf"], 500)
        except Exception:                                 # noqa: BLE001
            continue
        d = 1 if s["direction"] == "LONG" else -1
        step = bars[1].time - bars[0].time if len(bars) > 1 else 60
        after = [b for b in bars if b.time > s["bar_time"]]
        p = json.loads(s["payload"] or "{}")
        limit = 3 * max(10, int(str(p.get("expected_hold", "10")).split()[1]) if str(p.get("expected_hold", "")).startswith("about") else 10)
        out = None
        for k, b in enumerate(after):
            stop_hit = (b.low <= s["stop"]) if d > 0 else (b.high >= s["stop"])
            t1_hit = (b.high >= s["t1"]) if d > 0 else (b.low <= s["t1"])
            if stop_hit:
                out = ("stop first", -1.0, b.time)
                break
            if t1_hit:
                t2_hit = (b.high >= s["t2"]) if d > 0 else (b.low <= s["t2"])
                out = ("T2" if t2_hit else "T1", abs(s["t2" if t2_hit else "t1"] - s["entry"]) / abs(s["entry"] - s["stop"]), b.time)
                break
            if k + 1 >= limit:
                out = ("expired", (b.close - s["entry"]) * d / abs(s["entry"] - s["stop"]), b.time)
                break
        if out:
            store.execute("UPDATE signals SET status='closed', outcome=?, r_result=?, closed_at=? WHERE id=?", (out[0], out[1], out[2] + step, s["id"]))
            n += 1
    return n


async def scan(symbols: list[str], tf: str) -> int:
    from .analysis import analyze
    k = 0
    for s in symbols:
        try:
            await analyze(s, tf, 500)
            k += 1
        except Exception:                                 # noqa: BLE001
            pass
        await asyncio.sleep(1.0)
    return k


async def run_once() -> dict:
    cfg = settings()
    saved = snaps = 0
    errors = []
    for sym in cfg["symbols"]:
        for tf in cfg["tfs"]:
            try:
                bars, q = await D.candles(sym, tf, 500)
                saved += store.save_candles(sym, tf, bars, q.get("source", ""))
            except Exception as exc:                      # noqa: BLE001
                errors.append(f"{sym} {tf}: {str(exc)[:80]}")
            await asyncio.sleep(0.6)
    for und in cfg["chains"]:
        crypto = und in ("BTC", "ETH")
        if not crypto and not nse_open():
            continue
        try:
            ch = await (O.crypto_chain(und) if crypto else O.india_chain(und))
            if not ch.get("error"):
                O.save_snapshot(ch)
                snaps += 1
        except Exception as exc:                          # noqa: BLE001
            errors.append(f"chain {und}: {str(exc)[:80]}")
        await asyncio.sleep(0.6)
    closed = await paper.update_open()
    resolved = await resolve_signals()
    scanned = await scan(cfg["symbols"], cfg.get("scan_tf", "15m"))
    STATUS.update(last_run=int(time.time()), runs=STATUS["runs"] + 1, saved_candles=STATUS["saved_candles"] + saved,
                  snapshots=STATUS["snapshots"] + snaps, last_error="; ".join(errors[:5]) or None,
                  last_summary={"candles": saved, "snapshots": snaps, "paper_closed": closed, "signals_resolved": resolved, "scanned": scanned})
    return STATUS


async def _loop():
    STATUS["running"] = True
    await asyncio.sleep(20)                               # let the app finish starting
    while True:
        cfg = settings()
        if cfg["enabled"]:
            try:
                await run_once()
            except Exception as exc:                      # noqa: BLE001
                STATUS["last_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        await asyncio.sleep(max(5, int(cfg["interval_min"])) * 60)


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())
