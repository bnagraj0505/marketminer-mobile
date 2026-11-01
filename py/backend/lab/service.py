"""Signal Lab operations without any web framework. The PC's FastAPI routes (routes.py) and the phone build
(mobile_api.py running in the browser with Pyodide) both call these, so the two versions run the same logic."""
from __future__ import annotations

import asyncio
import json

from .candle import TF_SECONDS
from . import data as D
from . import store
from . import strategies as ST
from . import protect as P
from . import costs as C
from . import paper
from . import recorder
from . import analysis as A
from . import backtest as B
from .engine import Engine, MTF


class BadRequest(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


WATCH = {
    "India": ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX", "NSE:FINNIFTY-INDEX", "BSE:SENSEX-INDEX", "NSE:NIFTY-FUT", "NSE:BANKNIFTY-FUT",
              "NSE:RELIANCE-EQ", "NSE:HDFCBANK-EQ", "NSE:SBIN-EQ", "MCX:CRUDEOIL-FUT", "MCX:GOLD-FUT"],
    "Crypto": ["SHARK:BTCUSDT", "SHARK:ETHUSDT", "SHARK:SOLUSDT", "DELTAF:BTCUSD", "DELTAF:ETHUSD", "BINANCE:BTCUSDT"],
    "US / Global": ["YF:^GSPC", "YF:^NDX", "YF:^DJI", "YF:SPY", "YF:QQQ", "YF:AAPL", "YF:NVDA", "YF:^N225", "YF:^FTSE", "YF:GC=F", "YF:CL=F"],
    "Forex": ["YF:EURUSD=X", "YF:USDJPY=X", "YF:GBPUSD=X", "YF:USDINR=X", "YF:DX-Y.NYB"],
}
PLATFORM = {"name": "pc", "threads": True}             # the phone build sets name="mobile", threads=False


def meta() -> dict:
    return {"timeframes": list(TF_SECONDS), "mtf": MTF, "watch": WATCH, "user_watch": store.get_setting("watch") or [],
            "strategies": {k: {"name": v[0], "description": v[1], "params": {**ST.PARAMS[k], **((store.get_setting("strategies") or {}).get(k) or {})}}
                           for k, v in ST.META.items()},
            "protection": P.settings(), "recorder": recorder.settings(), "costs": {**C.DEFAULTS, **(store.get_setting("costs") or {})},
            "paper_only": True, "platform": PLATFORM["name"]}


def _sym(s: str) -> str:
    s = (s or "").strip()
    if not 3 <= len(s) <= 60:
        raise BadRequest(422, "symbol must be 3-60 characters, e.g. NSE:NIFTY50-INDEX")
    return s


def _tf(tf: str) -> str:
    if tf not in TF_SECONDS:
        raise BadRequest(400, f"timeframe must be one of {', '.join(TF_SECONDS)}")
    return tf


async def analyze(symbol: str, tf: str = "15m", bars: int = 600, fresh: bool = False, live: bool = False) -> dict:
    return await A.analyze(_sym(symbol), _tf(tf), max(200, min(2000, int(bars))), bool(fresh), bool(live))


async def live(symbol: str, tf: str = "15m") -> dict:
    return await D.live(_sym(symbol), _tf(tf))


async def mtf(symbol: str) -> dict:
    return await A.mtf_matrix(_sym(symbol))


async def derivatives(symbol: str) -> dict:
    return await A.derivatives(_sym(symbol))


async def backtest(body: dict) -> dict:
    sym, tf = _sym(body.get("symbol")), _tf(body.get("tf") or "15m")
    bars = max(400, min(20000, int(body.get("bars") or 3000)))
    strategy = body.get("strategy") or None
    if strategy and strategy not in ST.MODULES:
        raise BadRequest(400, "unknown strategy")
    min_score = body.get("min_score")
    min_score = None if min_score is None else max(0.0, min(100.0, float(min_score)))
    main, q = await D.candles(sym, tf, bars)
    itf, htf = MTF[tf]
    ib = (await D.candles(sym, itf, min(5000, bars)))[0] if itf else None
    hb = (await D.candles(sym, htf, min(3000, bars)))[0] if htf and htf != itf else ib
    E = Engine(main, tf, D.market_of(sym), D.session_key(sym), D.anchor_minutes(sym), ib, hb)
    cost_cfg = {**C.DEFAULTS, **(store.get_setting("costs") or {})}
    args = (E, sym, store.get_setting("strategies") or {}, min_score, cost_cfg, strategy)
    res = await asyncio.to_thread(B.run, *args) if PLATFORM["threads"] else B.run(*args)
    if res.get("error"):
        raise BadRequest(422, res["error"])
    res["data_quality"] = q
    params = {"symbol": sym, "tf": tf, "bars": bars, "strategy": strategy, "min_score": min_score}
    bid = store.execute("INSERT INTO backtests(created,symbol,tf,params,result) VALUES(?,?,?,?,?)",
                        (store.now(), sym, tf, json.dumps(params), json.dumps(res)))
    res["id"] = bid
    if not strategy:
        o = res["overall"]
        store.set_setting(f"validation:{sym}:{tf}", {"status": res["validation_status"], "backtest_id": bid, "at": store.now(),
                          "summary": {"trades": o.get("trades"), "expectancy_r": o.get("expectancy_r"), "test": res["test"].get("expectancy_r"), "backtest_id": bid}})
    return res


def backtests() -> list:
    out = []
    for r in store.q("SELECT id, created, symbol, tf, params, result FROM backtests ORDER BY id DESC LIMIT 50"):
        res = json.loads(r["result"])
        out.append({"id": r["id"], "created": r["created"], "symbol": r["symbol"], "tf": r["tf"], "params": json.loads(r["params"]),
                    "overall": res.get("overall"), "test": res.get("test")})
    return out


def backtest_get(bid: int) -> dict:
    r = store.q("SELECT result FROM backtests WHERE id=?", (int(bid),), one=True)
    if not r:
        raise BadRequest(404, "not found")
    return json.loads(r["result"])


async def paper_open(b: dict) -> dict:
    if b.get("direction") not in ("LONG", "SHORT"):
        raise BadRequest(422, "direction must be LONG or SHORT")
    t = b.get("targets") or []
    if len(t) != 3:
        raise BadRequest(422, "three targets are required")
    sig = {"symbol": _sym(b.get("symbol")), "tf": b.get("tf") or "15m", "strategy": str(b.get("strategy") or ""), "direction": b["direction"],
           "entry": float(b["entry"]), "stop": float(b["stop"]), "targets": [float(x) for x in t], "score": max(0.0, min(100.0, float(b.get("score") or 0)))}
    return await paper.open_trade(sig)


async def paper_close(tid: int) -> dict:
    return await paper.close_trade(int(tid))


async def paper_list() -> dict:
    try:
        await paper.update_open()
    except Exception:                                     # noqa: BLE001 - a data gap must not hide the ledger
        pass
    return paper.ledger()


def protection(symbol: str = "NSE:NIFTY50-INDEX", direction: str = "LONG") -> dict:
    return {"settings": P.settings(), "status": P.check(symbol, D.market_of(symbol), direction, 100)}


def alerts(unseen: bool = False) -> list:
    return store.q("SELECT * FROM alerts" + (" WHERE seen=0" if unseen else "") + " ORDER BY t DESC, id DESC LIMIT 200")


def alerts_seen() -> dict:
    store.execute("UPDATE alerts SET seen=1 WHERE seen=0")
    return {"ok": True}


def journal() -> dict:
    rows = store.q("SELECT * FROM signals ORDER BY id DESC LIMIT 400")
    for r in rows:
        r["payload"] = json.loads(r["payload"] or "{}")
    by: dict = {}
    for r in rows:
        if r["status"] != "closed" or r["outcome"] == "expired":
            continue
        b = by.setdefault(r["strategy"], {"resolved": 0, "t1_first": 0, "r": 0.0})
        b["resolved"] += 1
        b["t1_first"] += r["outcome"] in ("T1", "T2")
        b["r"] += r["r_result"] or 0
    for b in by.values():
        b["hit_rate"] = round(100 * b["t1_first"] / b["resolved"], 1)
        b["avg_r_gross"] = round(b["r"] / b["resolved"], 2)
    return {"signals": rows, "measured": by,
            "note": "Measured from this app's own journal: T1 reached before the stop, gross of costs. Small samples are noise."}


NUM_LIMITS = {"risk_pct": (0.05, 5), "daily_loss_pct": (0.1, 20), "weekly_loss_pct": (0.1, 40), "max_open": (1, 20), "max_correlated": (1, 20),
              "loss_streak_lockout": (1, 20), "cooldown_min": (0, 1440), "min_score": (0, 100), "capital_inr": (1000, 1e9), "capital_usd": (10, 1e8),
              "stop_buffer_atr": (0, 2), "min_rr": (0.5, 10), "min_avg_volume": (0, 1e12), "interval_min": (5, 240)}


def _clean(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = _clean(v)
        elif isinstance(v, (bool, list, str)):
            out[k] = v
        elif isinstance(v, (int, float)):
            lo, hi = NUM_LIMITS.get(k, (-1e12, 1e12))
            out[k] = min(hi, max(lo, v))
    return out


def save_settings(body: dict) -> dict:
    for k in ("protection", "strategies", "recorder", "costs", "watch"):
        v = body.get(k)
        if v is None:
            continue
        if k == "watch":
            store.set_setting("watch", [s.strip().upper() if not s.upper().startswith("YF:") else "YF:" + s.split(":", 1)[1].strip()
                                        for s in v if isinstance(s, str) and s.strip()][:40])
        elif isinstance(v, dict):
            store.set_setting(k, {**(store.get_setting(k) or {}), **_clean(v)})
    return meta()


def recorder_status() -> dict:
    return {**recorder.STATUS, "settings": recorder.settings(), "db_candles": store.q("SELECT COUNT(*) n FROM candles", one=True)["n"],
            "db_snapshots": store.q("SELECT COUNT(*) n FROM chain_snapshots", one=True)["n"]}


async def recorder_run() -> dict:
    return await recorder.run_once()

async def candles_only(symbol: str, tf: str = "5m", bars: int = 500) -> dict:
    """Candles for the MM Trend page (no analysis)."""
    sym, t = _sym(symbol), _tf(tf)
    bs, q = await D.candles(sym, t, max(50, min(2000, int(bars))), False, True)
    return {"symbol": sym, "tf": t, "market": D.market_of(sym), "quality": q,
            "candles": [{"time": b.time, "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume} for b in bs]}
