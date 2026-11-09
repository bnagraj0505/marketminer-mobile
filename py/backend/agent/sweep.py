"""Market Intelligence sweep: OBSERVE-ONLY coverage of every market the agent can read, beyond its trading list.

The trading loop (service.cycle) analyses its own watch-list and options desk and may paper-trade them. This sweep adds
the rest of the picture to the Market Intelligence tab and never places, approves or records a trade decision:

  index options   all six option indices (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX, BANKEX) with their chains
  stock options   every NSE F&O stock (from the FYERS symbol master) with its chain
  MCX             CRUDEOIL, GOLD, SILVER, NATURALGAS, COPPER front-month futures
  crypto          BTC, ETH, SOL, XRP, DOGE, BNB perpetuals (Shark; Delta fallback is marked in the data quality)
                  BTC / ETH also carry their option chains where the analysis provides them

FYERS budget: MarketMiner paces FYERS at MARKETMINER_FYERS_PER_MINUTE (default 30) and MARKETMINER_FYERS_PER_DAY
(default 5,000, the Standard plan). The sweep uses at most half of the daily allowance. The trading loop always comes first:
the sweep pauses while a trading cycle runs, for the first 3 minutes after every candle close, and whenever fewer than
SWEEP_HEADROOM calls are free in the current minute. So every F&O stock is refreshed in rotation (oldest first), not on
every candle; each card shows its own candle time. Crypto costs no FYERS calls and is refreshed every candle.
"""
from __future__ import annotations

import asyncio
import json
import time
import traceback

from backend.lab import analysis as A
from backend.lab import data as D
from backend.lab.candle import TF_SECONDS
from . import agents as AG
from . import config as CFG
from . import db
from . import intel
from . import options as OP

MCX = ["MCX:CRUDEOIL-FUT", "MCX:GOLD-FUT", "MCX:SILVER-FUT", "MCX:NATURALGAS-FUT", "MCX:COPPER-FUT"]
CRYPTO = ["SHARK:BTCUSDT", "SHARK:ETHUSDT", "SHARK:SOLUSDT", "SHARK:XRPUSDT", "SHARK:DOGEUSDT", "SHARK:BNBUSDT"]
SWEEP_HEADROOM = 10          # FYERS calls that must be free in the current minute before a sweep analysis starts
GRACE_AFTER_CLOSE = 180      # seconds after a candle close reserved for the trading loop

STATUS: dict = {"running": False, "covered": 0, "universe": 0, "last_symbol": None, "last_t": None, "pass_started": None,
                "last_error": None, "waiting": None, "fno_count": 0}
_task: asyncio.Task | None = None
_tried: dict[str, float] = {}   # last attempt per symbol (an unchanged candle is not re-inserted, so the DB time alone is not enough)


def group_of(symbol: str) -> str:
    s = symbol.upper()
    m = D.market_of(s)
    if m in ("SHARK", "DELTAF", "DELTA", "BINANCE"):
        return "crypto"
    if m == "MCX":
        return "mcx"
    if s.endswith("-INDEX"):
        return "index"
    if s.endswith("-EQ"):
        return "stock"
    return "other"


async def universe(cfg: dict) -> list[str]:
    """Everything the sweep covers, cheapest / most important first. Trading-list symbols are left to the trading loop."""
    traded = set(cfg.get("symbols") or [])
    o = cfg.get("options") or {}
    if o.get("enabled"):
        traded |= {OP.spot_symbol(u) for u in (o.get("index") or []) + (o.get("stocks") or [])}
    out = list(CRYPTO) + [OP.INDEX_SPOT[k] for k in OP.INDEX_SPOT] + list(MCX)
    try:
        stocks = await OP.fno_stocks()
        STATUS["fno_count"] = len(stocks)
        out += [f"NSE:{u}-EQ" for u in sorted(stocks)]
    except Exception as exc:                              # noqa: BLE001 - FYERS master unavailable: the rest still runs
        STATUS["last_error"] = f"F&O stock list unavailable: {str(getattr(exc, 'detail', exc))[:160]}"
    return [s for s in dict.fromkeys(out) if s not in traded]


SWEEP_DAY_SHARE = 0.5         # the sweep stops for the day once MarketMiner has used half of the FYERS daily limit


def _headroom() -> int:
    """Free FYERS calls this minute, or 0 once half of today's FYERS allowance is used (the rest is kept for trading)."""
    try:
        from backend.brokers.fyers.client import rate_status
        r = rate_status()
        if int(r.get("today", 0)) >= int(r.get("per_day_cap", 5000)) * SWEEP_DAY_SHARE:
            STATUS["day_budget_used"] = True
            return 0
        STATUS["day_budget_used"] = False
        return int(r["per_minute_cap"]) - int(r["last_minute"])
    except Exception:                                     # noqa: BLE001
        return 0


def _last_obs(tf: str) -> dict[str, int]:
    rows = db.q("SELECT symbol, MAX(t) AS t FROM agent_observations WHERE tf=? GROUP BY symbol", (tf,))
    return {r["symbol"]: r["t"] or 0 for r in rows}


async def observe(symbol: str, tf: str, now: float | None = None) -> dict:
    """One observe-only analysis -> one agent_observations row (same shape the trading loop writes)."""
    now = now or time.time()
    a = await A.analyze(symbol, tf, 600, True, False)
    candles = a.get("candles") or []
    bar_time = candles[-1]["time"] if candles else 0
    q = intel.quality_gate(a.get("quality") or {}, tf, (a.get("quality") or {}).get("candles") or len(candles), now)
    pic = intel.picture(a)
    reg = AG.regime_agent(a, pic)
    obs = {"picture": pic, "regime": reg, "structure": AG.structure_agent(a), "options": AG.options_agent(a, pic),
           "quality": q, "observe_only": True, "group": group_of(symbol)}
    db.execute("INSERT OR IGNORE INTO agent_observations(t, symbol, tf, bar_time, regime, volatility, quality_ok, data) VALUES(?,?,?,?,?,?,?,?)",
               (int(now), symbol, tf, bar_time, reg["regime"], reg["volatility"], int(q["ok"]), json.dumps(obs, default=str)))
    return {"bar_time": bar_time, "regime": reg.get("label")}


async def _loop() -> None:
    from . import service as SV
    STATUS["running"] = True
    await asyncio.sleep(60)                               # let the app, recorder and trading loop start first
    universe_cache: tuple[float, list[str]] = (0.0, [])
    obs_cache: tuple[float, dict] = (0.0, {})
    while True:
        try:
            cfg = CFG.agent()
            sw = cfg.get("intel_sweep") or {}
            if not cfg.get("enabled") or not sw.get("enabled", True):
                STATUS["waiting"] = "agent stopped" if not cfg.get("enabled") else "sweep switched off"
                await asyncio.sleep(30)
                continue
            tf = cfg["tf"]
            step = TF_SECONDS[tf]
            now = time.time()
            if now - universe_cache[0] > 3600 or not universe_cache[1]:
                universe_cache = (now, await universe(cfg))
            syms = universe_cache[1]
            STATUS["universe"] = len(syms)
            if now - obs_cache[0] > 60:                   # DB read once a minute; attempts in between are tracked in _tried
                obs_cache = (now, _last_obs(tf))
            last = dict(obs_cache[1])
            for k, v in _tried.items():
                if v > last.get(k, 0):
                    last[k] = v
            STATUS["covered"] = sum(1 for s in syms if now - last.get(s, 0) < 2 * 3600)
            # due = open market, not observed during the current candle; oldest observation first
            due = [s for s in syms if SV.market_open(s, now) and now - last.get(s, 0) >= step]
            due.sort(key=lambda s: (group_of(s) != "crypto", last.get(s, 0)))
            if not due:
                STATUS["waiting"] = "all open markets observed for this candle"
                await asyncio.sleep(30)
                continue
            sym = due[0]
            india = group_of(sym) != "crypto"
            since_close = now % step
            if SV.STATUS.get("in_cycle") or (india and since_close < GRACE_AFTER_CLOSE):
                STATUS["waiting"] = "trading loop has priority"
                await asyncio.sleep(10)
                continue
            if india and _headroom() < SWEEP_HEADROOM:
                STATUS["waiting"] = ("Indian markets paused for today: half of the FYERS daily limit is used (kept for trading)"
                                     if STATUS.get("day_budget_used") else "waiting for FYERS headroom")
                await asyncio.sleep(8)
                continue
            STATUS["waiting"] = None
            _tried[sym] = now
            try:
                await observe(sym, tf, now)
            except D.DataUnavailable as exc:
                # record the attempt time so a dead symbol does not block the rotation
                db.execute("INSERT OR IGNORE INTO agent_observations(t, symbol, tf, bar_time, regime, volatility, quality_ok, data) "
                           "VALUES(?,?,?,?,?,?,?,?)", (int(now), sym, tf, -int(now), None, None, 0,
                                                     json.dumps({"observe_only": True, "group": group_of(sym), "unavailable": str(exc)[:200]})))
            STATUS.update(last_symbol=sym, last_t=int(now), last_error=None)
            await asyncio.sleep(1.0 if india else 0.3)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                          # noqa: BLE001 - never stop the sweep on one failure
            STATUS["last_error"] = f"{type(exc).__name__}: {str(getattr(exc, 'detail', exc))[:200]}"
            db.log("error", f"Intelligence sweep: {STATUS['last_error']}", data={"trace": traceback.format_exc()[-1200:]}, level="error")
            await asyncio.sleep(20)


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())
