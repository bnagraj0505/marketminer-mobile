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
    if p == "/candles":
        return await SV.candles_only(q.get("symbol", ""), q.get("tf", "5m"), int(q.get("bars") or 500))
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
    if p == "/auction":
        from backend.lab import auction as AU
        return await AU.compute(q.get("symbol", ""), q.get("tf", ""), int(q.get("days") or 10))
    if p == "/sr":
        from backend.lab import srmtf
        return await srmtf.compute(q.get("symbol", ""))
    if p == "/perf":                                       # paper P&L (perf.html); the bot's trade ids come from js/bot.js
        from backend.bot import perf
        ids = json.loads(q.get("bot_ids") or "{}")
        return await perf.report(q.get("source") or "bot", ids)
    if p.startswith("/agent/"):                            # dashboard blocks (the PC's agent modules, information only)
        return await _agent(p[len("/agent/"):], q, method, body)
    if p == "/fyers_store":
        from backend.brokers.fyers import client as FC
        return FC.rate_status()
    raise SV.BadRequest(404, "Not available in the phone version: /api/lab" + p)


# ---------------------------------------------------------------- AI agent (the PC's backend/agent package)
_AGENT = {"phone_defaults": False, "started": False}


def _phone_agent_defaults() -> None:
    """Phone differences, applied once: crypto through Delta perpetuals (Shark does not answer phone browsers), and
    the observe-only sweep / all-F&O scan off by default (they would use most of the FYERS budget from one phone)."""
    if _AGENT["phone_defaults"]:
        return
    from backend.agent import config as CFG, sweep as SW, retrace as RT
    CFG.AGENT_DEFAULTS["symbols"] = ["DELTAF:BTCUSD", "DELTAF:ETHUSD"]
    CFG.AGENT_DEFAULTS["intel_sweep"] = {"enabled": False}
    CFG.AGENT_DEFAULTS["options"]["scan_all"] = False
    SW.CRYPTO = ["DELTAF:BTCUSD", "DELTAF:ETHUSD", "DELTAF:SOLUSD", "DELTAF:XRPUSD"]
    RT.CRYPTO = [("BTC", "DELTAF:BTCUSD"), ("ETH", "DELTAF:ETHUSD")]
    _AGENT["phone_defaults"] = True


def start_agent() -> dict:
    """Called by js/lab.js on the AI Agent page: the agent loop (it trades only after you press Start), the straddle
    recorder and the sweep (off by default on the phone) run while the page is open."""
    _phone_agent_defaults()
    from backend.agent import service as AS
    if not _AGENT["started"]:
        AS.start()
        _AGENT["started"] = True
    return {"started": True}


async def _agent(p: str, q: dict, method: str, body: dict | None = None):
    import time
    _phone_agent_defaults()
    from backend.agent import straddle as SD
    if p == "straddle" and method == "GET" and not _AGENT["started"]:
        # dashboard without the agent page: take the snapshot when the block asks, at the budget-aware pace
        now = time.time()
        if SD.nse_open(now) and now - (SD.STATUS.get("last") or 0) >= SD.budget_interval(60):
            await SD.run_once()
        SD.STATUS["running"] = True
        return SD.report()
    from backend.agent import routes as AR
    return await AR.router.dispatch(method, "/api/agent/" + p if p else "/api/agent/", q, body or {})


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
