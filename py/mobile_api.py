"""Signal Lab on the phone: the PC's backend/lab package, unchanged, running in the browser with Pyodide.
js/lab.js sends every /api/lab/* request here as JSON and gets JSON back. Data comes through the browser bridge
(_bridge.py); the database is SQLite in this browser's storage (IndexedDB), on this device only. PAPER ONLY."""
from __future__ import annotations

import json
import traceback
from pathlib import Path

from fastapi import HTTPException
from _bridge import js
from backend.lab import store, data as D, service as SV, recorder
from backend.lab.candle import resample, from_rows

store.DB = Path("/data/lab.db")
D.LIVE_LTP_TTL = 3.0                                  # the phone shares FYERS' per-minute budget with other pages
SV.PLATFORM.update(name="mobile", threads=False)
SV.WATCH = {
    "India": ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX", "NSE:FINNIFTY-INDEX", "BSE:SENSEX-INDEX", "NSE:NIFTY-FUT", "NSE:BANKNIFTY-FUT",
              "NSE:RELIANCE-EQ", "NSE:HDFCBANK-EQ", "NSE:SBIN-EQ", "MCX:CRUDEOIL-FUT", "MCX:GOLD-FUT"],
    "Crypto (Delta perpetuals)": ["DELTAF:BTCUSD", "DELTAF:ETHUSD", "DELTAF:SOLUSD", "DELTAF:XRPUSD", "BINANCE:BTCUSDT", "BINANCE:ETHUSDT"],
    "US / Global (PC only)": ["YF:^GSPC", "YF:^NDX", "YF:AAPL", "YF:EURUSD=X"],
}
recorder.DEFAULTS.update(symbols=["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX", "DELTAF:BTCUSD", "DELTAF:ETHUSD"])


async def _yahoo(sym, tf, bars):
    """Yahoo's chart API does not allow browser requests from other sites; the bridge tries and reports honestly."""
    base_tf = tf if tf in D.YF_IV else {"3m": "1m", "4h": "1h"}[tf]
    iv, rng = D.YF_IV[base_tf]
    j = await js("yahoo", sym, iv, rng)
    res = ((j.get("chart") or {}).get("result") or [None])[0]
    if not res:
        raise D.DataUnavailable(f"Yahoo Finance has no data for {sym}")
    ts = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    rows = []
    for k, t in enumerate(ts):
        o, h, l, c, v = (q.get(x, [None] * len(ts))[k] for x in ("open", "high", "low", "close", "volume"))
        if None not in (o, h, l, c):
            rows.append([int(t), o, h, l, c, v or 0])
    if base_tf != tf:
        rows = [[b.time, b.open, b.high, b.low, b.close, b.volume] for b in resample(from_rows(rows), tf)]
    return rows


D._yahoo = _yahoo


async def _route(path: str, q: dict, method: str, body: dict):
    p = path.split("/api/lab", 1)[-1] or "/"
    if p == "/meta":
        return SV.meta()
    if p == "/analyze":
        return await SV.analyze(q.get("symbol", ""), q.get("tf", "15m"), int(q.get("bars") or 600), q.get("fresh") in ("1", "true"), q.get("live") in ("1", "true"))
    if p == "/live":
        return await SV.live(q.get("symbol", ""), q.get("tf", "15m"))
    if p == "/mtf":
        return await SV.mtf(q.get("symbol", ""))
    if p == "/derivatives":
        return await SV.derivatives(q.get("symbol", ""))
    if p == "/backtest" and method == "POST":
        return await SV.backtest(body)
    if p == "/backtests":
        return SV.backtests()
    if p.startswith("/backtests/"):
        return SV.backtest_get(int(p.rsplit("/", 1)[1]))
    if p == "/paper/open" and method == "POST":
        return await SV.paper_open(body)
    if p.startswith("/paper/close/") and method == "POST":
        return await SV.paper_close(int(p.rsplit("/", 1)[1]))
    if p == "/paper":
        return await SV.paper_list()
    if p == "/protection":
        return SV.protection(q.get("symbol") or "NSE:NIFTY50-INDEX", q.get("direction") or "LONG")
    if p == "/alerts":
        return SV.alerts(q.get("unseen") in ("true", "1"))
    if p == "/alerts/seen" and method == "POST":
        return SV.alerts_seen()
    if p == "/journal":
        return SV.journal()
    if p == "/settings" and method == "POST":
        return SV.save_settings(body)
    if p == "/recorder":
        return SV.recorder_status()
    if p == "/recorder/run" and method == "POST":
        return await SV.recorder_run()
    raise SV.BadRequest(404, "Not available in the phone version: /api/lab" + p)


async def handle_json(req: str) -> str:
    r = json.loads(req)
    try:
        out = await _route(r.get("path", ""), r.get("query") or {}, (r.get("method") or "GET").upper(), r.get("body") or {})
        return json.dumps({"status": 200, "body": out}, default=str)
    except D.DataUnavailable as exc:
        return json.dumps({"status": 503, "body": {"detail": f"DATA UNAVAILABLE: {str(exc)[:300]}"}})
    except SV.BadRequest as exc:
        return json.dumps({"status": exc.status, "body": {"detail": str(exc)}})
    except HTTPException as exc:
        return json.dumps({"status": exc.status_code, "body": {"detail": str(exc.detail)}})
    except Exception as exc:                              # noqa: BLE001 - report, never crash the page
        traceback.print_exc()
        return json.dumps({"status": 500, "body": {"detail": f"{type(exc).__name__}: {exc}"}})
