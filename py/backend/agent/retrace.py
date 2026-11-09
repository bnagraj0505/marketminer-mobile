"""Retracement or reversal? INTRADAY LEVELS ONLY. For every options-desk index, on 5m and 15m (closed candles, Signal Lab
swing / structure engine), with every level taken from TODAY'S session; 1h is shown as trend context only (no levels).
Information only.

Per timeframe, in the direction of the structure trend:
  leg start  today's latest higher low (lower high in a down-trend) once price has broken above the swing high before it
             (BOS); before that, today's low that launched that swing high. No swing yet today -> the day's low (high).
  leg        leg start -> today's extreme since;  depth = share of that leg given back by the current price
  LEVELS ARE FIBONACCI RETRACEMENTS OF TODAY'S LEG:  pullback = 61.8%,  reversal = 78.6%
Status (first rule that matches):
  REVERSAL           close beyond the 78.6% level, or a CHoCH against the trend today in the last 10 candles
  POSSIBLE REVERSAL  a market-structure shift against the trend today with the close beyond EMA50
  DEEP RETRACEMENT   depth 61.8 - 78.6% (OTE): still a pullback, must hold the 78.6% level
  RETRACEMENT        depth 23.6 - 61.8%
  TRENDING           depth < 23.6%
  RANGE              no structure trend: today's high / low shown
Verdict: 15m = the intraday swing, 5m = timing.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

from backend.lab import data as D
from backend.lab.engine import Engine
from . import options as OP
from . import straddle as SD

TFS = ("5m", "15m", "1h")
_cache: dict = {}


def _today(bars, day0: int) -> list:
    return [x for x in bars if x.time >= day0]


def _leg(F, up: bool, t: int, live: list, internal: bool, day0: int) -> tuple:
    """(protected level, leg extreme) from TODAY's swings only. Up: if price has broken above today's swing high that
    came before today's latest swing low (BOS), that latest higher low is protected; before such a break, the low that
    launched that swing high. No swing low today -> the day's low. Mirror for a down-trend."""
    opp = [x for x in F.swings_at(t, "low" if up else "high", internal) if x.time >= day0]
    ends = [x for x in F.swings_at(t, "high" if up else "low", internal) if x.time >= day0]
    today = _today(F.bars, day0) + live
    if not today:
        return None, None
    pick = lambda bs: (max(b.high for b in bs) if up else min(b.low for b in bs)) if bs else None
    if not opp:
        k = min(range(len(today)), key=lambda j: today[j].low) if up else max(range(len(today)), key=lambda j: today[j].high)
        x = today[k]
        return SimpleNamespace(price=x.low if up else x.high, time=x.time, day_extreme=True), pick(today[k:])
    last = opp[-1]
    prior = [h for h in ends if h.time < last.time]
    ext_since = pick([b for b in today if b.time >= last.time])
    broke = not prior or (ext_since > prior[-1].price if up else ext_since < prior[-1].price)
    if broke:
        return last, ext_since
    href = prior[-1]
    start = next((x for x in reversed(opp) if x.time < href.time), None)
    if start is None:                                       # the swing high was launched from the day's low
        k = min(range(len(today)), key=lambda j: today[j].low) if up else max(range(len(today)), key=lambda j: today[j].high)
        start = SimpleNamespace(price=today[k].low if up else today[k].high, time=today[k].time, day_extreme=True)
    return start, pick([b for b in today if b.time >= start.time])


def _status(F, tf: str, live: list | None = None, day0: int = 0) -> dict:
    live = live or []
    i = len(F.bars) - 1
    t = F.t[i]
    c = live[-1].close if live else F.c[i]                    # current price (latest closed 5m) on every timeframe
    trend = F.trend_at(t)
    internal = F.trend_at(t, internal=True)
    e50 = F.ema[50][i]
    rsi = F.rsi[i]
    win = max(t - 10 * F.step, day0 - 1)
    today = _today(F.bars, day0) + live
    if trend not in ("bullish", "bearish"):
        hi = max(b.high for b in today) if today else None
        lo = min(b.low for b in today) if today else None
        pos = (c - lo) / (hi - lo) * 100 if hi and lo and hi > lo else 50
        return {"tf": tf, "trend": "neutral", "status": "RANGE", "label": "Range / no trend today", "depth_pct": None, "close": c,
                "range_high": hi, "range_low": lo, "position_pct": round(pos),
                "evidence": [f"no structure trend; price at {pos:.0f}% of today's {lo:,.0f}–{hi:,.0f} range" if hi else "no candles today yet"]}
    up = trend == "bullish"
    d = 1 if up else -1
    minor = tf == "5m"                                         # 5m = timing: minor (2-candle) swings; 15m: structure
    start, ext = _leg(F, up, t, live, minor, day0)
    if start is None:
        return {"tf": tf, "trend": trend, "status": "TRENDING", "label": "No candles today yet", "depth_pct": 0, "close": c, "evidence": []}
    w_sw = [x for x in F.swings_at(t, "low" if up else "high", True) if x.time >= day0]
    warn = w_sw[-1].price if w_sw else None                     # today's latest minor higher low (lower high)
    if warn is not None and ((warn - start.price) * d <= 0 or (c - warn) * d <= 0):
        warn = None
    rng = abs(ext - start.price) or 1e-9
    depth = (ext - c) / rng * d * 100
    fib = {f"{r:.1f}": ext - d * rng * r / 100 for r in (23.6, 38.2, 50.0, 61.8, 78.6)}
    flip = [x for x in F.ext.structure if x.time > win and x.type == "CHoCH" and x.direction != trend]
    mss = [m for m in F.mss_between(win, t) if m["direction"] != trend]
    wrong_side = e50 is not None and (c - e50) * d < 0
    what = ("today's low" if up else "today's high") if getattr(start, "day_extreme", False) else f"today's {'higher low' if up else 'lower high'}"
    pull, rev = fib["61.8"], fib["78.6"]
    ev = [f"today's {'up' if up else 'down'}-leg {start.price:,.1f} ({what}) → {ext:,.1f}; given back {depth:.0f}%",
          f"reversal level = 78.6% of today's leg {rev:,.1f}" + (" BROKEN" if (c - rev) * d < 0 else " holding"),
          f"pullback level = 61.8% of today's leg {pull:,.1f}" + (" broken" if (c - pull) * d < 0 else " holding"),
          (f"nearest swing: today's latest minor {'higher low' if up else 'lower high'} {warn:,.1f}" if warn is not None
           else "no minor swing today between the price and the start of the leg"),
          f"internal structure {internal}", f"close {'above' if c > (e50 or c) else 'below'} EMA50 {e50:,.1f}" if e50 else "EMA50 n/a",
          f"RSI {rsi:.0f}" if rsi is not None else "RSI n/a"]
    if flip:
        ev.append(f"CHoCH {flip[-1].direction} today at {flip[-1].level:,.1f}")
    if mss:
        ev.append(f"market-structure shift {mss[-1]['direction']} today at {mss[-1]['level']:,.1f}")
    ext_rsi = rsi is not None and ((rsi >= 75) if up else (rsi <= 25))
    if ext_rsi:
        ev.append(f"RSI {rsi:.0f}: {'overbought' if up else 'oversold'} - the move is extended, a pullback is likely")
    if depth > 78.6 or flip:
        st, lab = "REVERSAL", (f"Reversal: today's {'up' if up else 'down'}-move given back beyond 78.6% ({'below' if up else 'above'} {rev:,.0f})"
                               if depth > 78.6 else f"Reversal: structure flipped (CHoCH {flip[-1].direction}) today")
    elif mss and wrong_side:
        st, lab = "POSSIBLE_REVERSAL", "Possible reversal: structure shifting against today's move"
    elif depth >= 61.8:
        st, lab = "DEEP_RETRACEMENT", f"Deep retracement (61.8–78.6%) - must hold {rev:,.0f}"
    elif depth >= 23.6:
        st, lab = "RETRACEMENT", f"Retracement in today's {'up' if up else 'down'}-move"
    else:
        st, lab = "TRENDING", f"Trending {'up' if up else 'down'} today"
    if st in ("RETRACEMENT", "DEEP_RETRACEMENT") and internal == trend:
        lab += " · resuming (internal structure back with the trend)"
    if st == "TRENDING" and ext_rsi:
        lab += f" · extended (RSI {rsi:.0f})"
    return {"tf": tf, "trend": trend, "status": st, "label": lab, "depth_pct": round(depth, 1), "close": c, "leg_start": start.price,
            "leg_end": ext, "protected": round(rev, 2), "warning": round(pull, 2), "level_basis": "fibonacci 61.8% / 78.6% of today's leg",
            "leg_start_is_day_extreme": bool(getattr(start, "day_extreme", False)), "nearest_swing": warn, "swing_basis": "minor (2-candle)" if minor else "structure (5-candle)",
            "fib": {k: round(v, 2) for k, v in fib.items()}, "internal": internal,
            "rsi": round(rsi, 1) if rsi is not None else None, "evidence": ev, "as_of": t + F.step,
            "leg_start_time": start.time, "leg_end_time": next((b.time for b in today if b.time >= start.time and (b.high if up else b.low) == ext), None)}


def context_1h(F) -> dict:
    """1h: trend context only - no levels (levels are intraday)."""
    i = len(F.bars) - 1
    tr = F.trend_at(F.t[i])
    rsi = F.rsi[i]
    return {"tf": "1h", "trend": tr, "status": "TRENDING" if tr in ("bullish", "bearish") else "RANGE", "context_only": True,
            "label": f"1h trend {'up' if tr == 'bullish' else 'down' if tr == 'bearish' else 'neutral'}" + (f" · RSI {rsi:.0f}" if rsi is not None else ""),
            "close": F.c[i], "rsi": round(rsi, 1) if rsi is not None else None, "evidence": [f"1h structure {tr} (context; no levels)"]}


def verdict(rows: dict) -> tuple[str, str]:
    """Intraday: 15m = the swing, 5m = timing; 1h only colours the wording."""
    m, l, h = rows.get("15m") or {}, rows.get("5m") or {}, rows.get("1h") or {}
    ms, ls = m.get("status"), l.get("status")
    word = "up-move" if m.get("trend") == "bullish" else "down-move" if m.get("trend") == "bearish" else "range"
    ctx = f" (1h trend {'up' if h.get('trend') == 'bullish' else 'down' if h.get('trend') == 'bearish' else 'neutral'})" if h else ""
    if ms == "REVERSAL":
        return "REVERSAL", f"Reversing: 15m has given back more than 78.6% of today's {word} ({m.get('protected', 0):,.0f}){ctx}"
    if ms == "POSSIBLE_REVERSAL":
        return "POSSIBLE_REVERSAL", f"Reversal risk: 15m structure shifting against today's move - 78.6% at {m.get('protected', 0):,.0f}{ctx}"
    if ls in ("REVERSAL", "POSSIBLE_REVERSAL") and ms in ("RETRACEMENT", "DEEP_RETRACEMENT", "TRENDING"):
        return "RETRACEMENT", f"Retracing: 5m has turned, 15m still holds its 78.6% level {m.get('protected', 0):,.0f}{ctx}"
    if ms in ("RETRACEMENT", "DEEP_RETRACEMENT") or ls in ("RETRACEMENT", "DEEP_RETRACEMENT"):
        res = " and resuming" if "resuming" in (m.get("label") or l.get("label") or "") else ""
        return "RETRACEMENT", f"Retracing{res} within today's {word}" + (" (deep: OTE zone)" if ms == "DEEP_RETRACEMENT" else "") + ctx
    if ms == "TRENDING" or ls == "TRENDING":
        return "TRENDING", f"Trending: no meaningful pullback in today's {word}{ctx}"
    return "RANGE", f"No clear trend today: ranging{ctx}"


CRYPTO = [("BTC", "SHARK:BTCUSDT"), ("ETH", "SHARK:ETHUSDT")]     # Shark (the agent's crypto feed); day = UTC day


def day_start(sym: str, t: int) -> int:
    """Start of the session that contains t: IST midnight for India, 00:00 UTC (05:30 IST) for crypto."""
    off = 19800 if D.market_of(sym) in ("NSE", "FNO", "MCX") else 0
    return (t + off) // 86400 * 86400 - off


async def index_report(und: str, sym: str | None = None) -> dict:
    """ONE history call per instrument (~3000 closed 5m candles, for warm-up of the indicators); 15m and 1h are built
    from them. Levels come from today's session only."""
    from backend.lab.candle import resample, closed_only, TF_SECONDS
    import datetime as _dt
    sym = sym or OP.spot_symbol(und)
    rows, errs = {}, {}
    try:
        base, q = await D.candles(sym, "5m", 3000)
    except Exception as exc:                              # noqa: BLE001
        msg = str(getattr(exc, "detail", exc))[:140]
        return {"und": und, "symbol": sym, "verdict": "UNAVAILABLE", "summary": f"DATA UNAVAILABLE: {msg}", "timeframes": {},
                "errors": {tf: msg for tf in TFS}, "at": int(time.time())}
    now = time.time()
    anchor = D.anchor_minutes(sym)
    day0 = day_start(sym, base[-1].time)
    dd = _dt.datetime.fromtimestamp(day0 + 43200, _dt.timezone.utc)
    for tf in TFS:
        try:
            bars = base if tf == "5m" else closed_only(resample(base, tf, anchor), TF_SECONDS[tf], now)
            if len(bars) < (30 if tf == "1h" else 60):
                raise ValueError(f"only {len(bars)} {tf} candles")
            F = Engine(bars, tf, D.market_of(sym), D.session_key(sym), anchor).F
            if tf == "1h":
                rows[tf] = {**context_1h(F), "source": q.get("source")}
                continue
            end = bars[-1].time + TF_SECONDS[tf]
            live = [b for b in base if b.time >= end]
            rows[tf] = {**_status(F, tf, live, day0), "source": q.get("source")}
        except Exception as exc:                          # noqa: BLE001
            errs[tf] = str(getattr(exc, "detail", exc))[:140]
    v, txt = verdict(rows) if rows else ("UNAVAILABLE", "DATA UNAVAILABLE")
    return {"und": und, "symbol": sym, "verdict": v, "summary": txt, "session": dd.date().isoformat(), "day_start": day0, "timeframes": rows,
            "errors": errs, "at": int(time.time())}


STATE: dict = {"busy": False, "task": None}
CSTATE: dict = {"busy": False, "task": None}
NOTE = "Closed candles; levels from today's session only. Levels = Fibonacci 61.8% (pullback) / 78.6% (reversal) of today's leg. Information only."


async def _compute(market: str = "india") -> dict:
    if market == "crypto":
        out = {"indices": [await index_report(u, s) for u, s in CRYPTO], "market": "crypto",
               "note": NOTE + " Crypto day = 00:00 UTC (05:30 IST), as on the crypto page.", "at": int(time.time())}
    else:
        out = {"indices": [await index_report(u) for u in SD.indices()], "market": "india", "note": NOTE, "at": int(time.time())}
    _cache["c" if market == "crypto" else "r"] = (time.time(), out)
    return out


async def _refresh(market: str = "india") -> None:
    st = CSTATE if market == "crypto" else STATE
    st["busy"] = True
    try:
        await _compute(market)
    except Exception:                                     # noqa: BLE001
        pass
    finally:
        st["busy"] = False


async def report(market: str = "india") -> dict:
    """Never makes the page wait behind the data-provider rate limits: returns the last result at once and refreshes it
    in the background when it is older than 60 s. Only the very first call (nothing cached yet) waits, up to 25 s."""
    import asyncio
    crypto = market == "crypto"
    key, st = ("c", CSTATE) if crypto else ("r", STATE)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 60:
        return {**hit[1], "updating": False}
    if not st["busy"]:
        st["task"] = asyncio.create_task(_refresh("crypto") if crypto else _refresh())
    if hit:
        return {**hit[1], "updating": True}
    try:
        await asyncio.wait_for(asyncio.shield(st["task"]), 25)
    except asyncio.TimeoutError:
        return {"indices": [], "updating": True, "note": "Fetching candles; this fills in within a minute."}
    hit = _cache.get(key)
    return {**(hit[1] if hit else {"indices": []}), "updating": False}
