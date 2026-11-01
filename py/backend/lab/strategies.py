"""Strategy library: 12 independently testable modules.

Each module is `fn(F, i, d, ctx) -> dict | None` for direction d (+1 long, -1 short), evaluated on the CLOSED
candle i using only what was known at its close (see features.py). It returns the raw idea: the stop anchor and the
reasons. `build_setup` then turns it into a trade plan (entry, stop with ATR buffer, three targets from liquidity /
order blocks / FVGs / R-multiples) and rejects it if any part cannot be established or the reward:risk is too low.
In backtests the entry is the NEXT candle's open, never the signal candle's close.

Common rules (configurable per strategy in PARAMS / the settings page)
- Stop buffer: 0.1 x ATR beyond the anchor. Risk must be 0.25 - 4 x ATR.
- Targets: T1 = nearest structural level (untouched swing, opposing order block / FVG edge) at >= 1R, else 1.5R;
  T2 = the next level at >= T1 + 0.5R, else 2.5R; T3 = 4R or the next level beyond T2.
- Minimum reward:risk to T2 = min_rr (1.5). Indian intraday entries only 09:30-15:00 IST.
- Minimum liquidity: if the instrument has volume, the signal candle's 20-candle average volume must be above
  min_avg_volume (0 = off). Indices have no volume and skip this check.
"""
from __future__ import annotations

import datetime as dt

from .candle import TF_SECONDS
from .features import Features
from . import ind as I

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
INTRADAY = ("1m", "3m", "5m", "15m", "30m", "1h")
META = {
    "sweep_mss_fvg": ("Liquidity sweep + MSS + FVG", "Swept liquidity, a market-structure shift with displacement, then a pullback into the fresh FVG that holds its midpoint (CE)."),
    "ob_bos_retest": ("Order block + BOS + retest", "First return to a fresh order block that caused a break of structure, closing back out of it in the trend's direction."),
    "vwap_reclaim": ("VWAP reclaim + volume expansion", "Close crosses back over session VWAP on 1.5x relative volume (range expansion for indices), closing in the strong half of the candle."),
    "trend_pullback": ("EMA trend pullback + price action", "EMA20 > EMA50 > EMA200 stack, a pullback that tags EMA20 and a rejection / engulfing candle closing back above it."),
    "orb_retest": ("Opening range breakout + retest", "Close beyond the first 15-minute range, then a retest of the range edge that holds within 6 candles."),
    "pdhl_sweep": ("Previous day high/low sweep", "Trades through yesterday's low (high) and closes back inside with a reversal candle."),
    "ote": ("Premium/discount + OTE entry", "Higher-timeframe bias agrees, price retraces into the 0.62-0.79 OTE of the last impulse inside discount (premium) and a candle closes in the trend's direction."),
    "breakout_volume": ("Breakout + volume confirmation", "Close beyond the 20-candle high/low with 2x relative volume and range expansion."),
    "divergence_mss": ("RSI/MACD divergence + structure shift", "A regular RSI or MACD divergence followed within 15 candles by a CHoCH in its direction."),
    "options_momentum": ("Options momentum + OI + IV", "Live only: price above VWAP with EMA9 > EMA20, OI flow +25 or better (put writing) and IV not collapsing. Mirror for shorts."),
    "range_reversal": ("Range reversal + liquidity sweep", "In a range/choppy regime, a sweep of the 30-candle range low (high) that closes back inside with a rejection."),
    "trend_mtf": ("Trend continuation + multi-timeframe confluence", "Higher and intermediate timeframes agree; the execution timeframe's internal structure turns back with the trend after a pullback."),
}
PARAMS = {k: {"enabled": True, "stop_buffer_atr": 0.1, "min_rr": 1.5, "min_avg_volume": 0} for k in META}
PARAMS["options_momentum"]["live_only"] = True


def _ist_minutes(t: int) -> int:
    x = dt.datetime.fromtimestamp(t, IST)
    return x.hour * 60 + x.minute


def in_session(F: Features, i: int, market: str) -> bool:
    if market not in ("NSE", "FNO", "MCX") or F.tf not in INTRADAY:
        return True
    close_m = _ist_minutes(F.t[i] + F.step)
    hi = 900 if market != "MCX" else 1380
    return 570 <= close_m <= hi


def _green(b, d: int) -> bool:
    return (b.close > b.open) if d > 0 else (b.close < b.open)


def _rejection(F: Features, i: int, d: int) -> bool:
    b, p = F.bars[i], F.bars[i - 1]
    body = abs(b.close - b.open)
    rng = b.high - b.low or 1e-9
    wick = (min(b.open, b.close) - b.low) if d > 0 else (b.high - max(b.open, b.close))
    engulf = _green(b, d) and not _green(p, d) and body > abs(p.close - p.open) and ((b.close >= p.open) if d > 0 else (b.close <= p.open))
    return _green(b, d) and (wick >= body or wick >= 0.4 * rng or engulf)


# ---------------------------------------------------------------- the 12 modules

def sweep_mss_fvg(F, i, d, ctx):
    t, w = F.t[i], F.t[max(0, i - 20)]
    side = "sell" if d > 0 else "buy"
    sw = [s for s in F.sweeps_between(w, t) if s.side == side]
    if not sw:
        return None
    s = sw[-1]
    ms = [m for m in F.mss_between(s.time - 1, t) if m["direction"] == ("bullish" if d > 0 else "bearish")]
    if not ms:
        return None
    gaps = [g for g in F.open_fvgs_at(F.t[i - 1], "bullish" if d > 0 else "bearish") if g["created_time"] >= s.time]
    b = F.bars[i]
    for g in reversed(gaps):
        enters = b.low <= g["top"] if d > 0 else b.high >= g["bottom"]
        holds = b.close > g["ce"] if d > 0 else b.close < g["ce"]
        if enters and holds and _green(b, d):
            return {"anchor": s.extreme, "reasons": [f"{'Sell' if d > 0 else 'Buy'}-side liquidity swept at {s.level:,.2f}",
                    f"MSS {ms[-1]['direction']} at {ms[-1]['level']:,.2f}", f"Retest of FVG {g['bottom']:,.2f}-{g['top']:,.2f} held its CE"],
                    "zone": (g["bottom"], g["top"])}
    return None


def ob_bos_retest(F, i, d, ctx):
    t = F.t[i]
    if F.trend_at(F.t[i - 1]) != ("bullish" if d > 0 else "bearish"):
        return None
    b = F.bars[i]
    for o in reversed(F.obs_at(F.t[i - 1], "bullish" if d > 0 else "bearish", fresh_only=True)):
        if o.mitigated_time is not None and o.mitigated_time < t:
            continue
        touch = b.low <= o.top if d > 0 else b.high >= o.bottom
        out = b.close > o.top if d > 0 else b.close < o.bottom
        if touch and out:
            return {"anchor": o.bottom if d > 0 else o.top, "reasons": [f"Fresh {o.direction} order block {o.bottom:,.2f}-{o.top:,.2f} from a {o.break_type}",
                    "First retest closed back out of the block" + (" (volume-confirmed)" if o.volume_confirmed else "")], "zone": (o.bottom, o.top)}
    return None


def vwap_reclaim(F, i, d, ctx):
    if F.tf not in INTRADAY or F.vwap[i] is None or F.vwap[i - 1] is None:
        return None
    b, p = F.bars[i], F.bars[i - 1]
    crossed = (p.close < F.vwap[i - 1] and b.close > F.vwap[i]) if d > 0 else (p.close > F.vwap[i - 1] and b.close < F.vwap[i])
    if not crossed:
        return None
    strong = (b.close - b.low) >= 0.5 * (b.high - b.low) if d > 0 else (b.high - b.close) >= 0.5 * (b.high - b.low)
    if F.has_volume:
        vol_ok = (F.rvol[i] or 0) >= 1.5
        why = f"relative volume {F.rvol[i]:.1f}x" if F.rvol[i] else ""
    else:
        vol_ok = bool(F.atr[i]) and (b.high - b.low) >= 1.2 * F.atr[i]
        why = "range expansion (index: no volume)"
    if not (vol_ok and strong):
        return None
    lows = [x.low for x in F.bars[i - 2:i + 1]] if d > 0 else [x.high for x in F.bars[i - 2:i + 1]]
    return {"anchor": min(lows) if d > 0 else max(lows), "reasons": [f"{'Reclaimed' if d > 0 else 'Lost'} session VWAP {F.vwap[i]:,.2f}", why]}


def trend_pullback(F, i, d, ctx):
    e20, e50, e200 = F.ema[20][i], F.ema[50][i], F.ema[200][i]
    if e20 is None or e50 is None:
        return None
    stack = (e20 > e50 and (e200 is None or e50 > e200)) if d > 0 else (e20 < e50 and (e200 is None or e50 < e200))
    b = F.bars[i]
    touched = b.low <= e20 if d > 0 else b.high >= e20
    back = b.close > e20 if d > 0 else b.close < e20
    if stack and touched and back and _rejection(F, i, d):
        w = F.bars[i - 4:i + 1]
        return {"anchor": min(x.low for x in w) if d > 0 else max(x.high for x in w),
                "reasons": [f"EMA20 {'>' if d > 0 else '<'} EMA50{' > EMA200' if d > 0 and e200 else ' < EMA200' if e200 else ''}", "Pullback tagged EMA20 and was rejected"]}
    return None


def _session_start(F, i, market):
    if market in ("NSE", "FNO", "MCX"):
        x = dt.datetime.fromtimestamp(F.t[i], IST)
        st = dt.datetime(x.year, x.month, x.day, 9, 0 if market == "MCX" else 15, tzinfo=IST)
    else:
        x = dt.datetime.fromtimestamp(F.t[i], dt.timezone.utc)
        st = dt.datetime(x.year, x.month, x.day, tzinfo=dt.timezone.utc)
    return int(st.timestamp())


def orb_retest(F, i, d, ctx):
    if F.step > 900:
        return None
    s0 = _session_start(F, i, ctx["market"])
    n = max(1, 900 // F.step)
    first = F.idx(s0 - 1) + 1
    if first < 0 or first + n > i or F.t[first] < s0:
        return None
    rng = F.bars[first:first + n]
    if F.t[first + n - 1] >= s0 + 900:
        return None
    oh, ol = max(x.high for x in rng), min(x.low for x in rng)
    brk = None
    for k in range(first + n, i):
        if (F.bars[k].close > oh) if d > 0 else (F.bars[k].close < ol):
            brk = k
            break
    if brk is None or i - brk > 6:
        return None
    b = F.bars[i]
    edge = oh if d > 0 else ol
    if ((b.low <= edge and b.close > edge) if d > 0 else (b.high >= edge and b.close < edge)):
        return {"anchor": (oh + ol) / 2, "reasons": [f"Opening range {ol:,.2f}-{oh:,.2f} broken at {dt.datetime.fromtimestamp(F.t[brk], IST):%H:%M}", "Retest of the range edge held"]}
    return None


def pdhl_sweep(F, i, d, ctx):
    lv = ctx.get("prev_day")
    if not lv:
        return None
    b = F.bars[i]
    lvl = lv[1] if d > 0 else lv[0]
    swept = (b.low < lvl and b.close > lvl) if d > 0 else (b.high > lvl and b.close < lvl)
    if swept and _rejection(F, i, d):
        return {"anchor": b.low if d > 0 else b.high, "reasons": [f"Swept the previous day's {'low' if d > 0 else 'high'} {lvl:,.2f} and closed back inside"]}
    return None


def ote(F, i, d, ctx):
    if ctx.get("htf_bias", 0) != d:
        return None
    t = F.t[i - 1]
    brs = [s for s in F.ext.structure if s.time <= t and s.direction == ("bullish" if d > 0 else "bearish")]
    if not brs:
        return None
    br = brs[-1]
    kind = "low" if d > 0 else "high"
    sw = [s for s in F.swings_at(br.time, kind) if s.time <= br.time]
    if not sw:
        return None
    start = sw[-1].price
    k = F.idx(br.time)
    seg = F.bars[k:i]
    if not seg:
        return None
    end = max(x.high for x in seg) if d > 0 else min(x.low for x in seg)
    rng = end - start
    if rng == 0 or (rng < 0) == (d > 0):
        return None
    top, bot = end - 0.62 * rng, end - 0.79 * rng
    lo_z, hi_z = min(top, bot), max(top, bot)
    eq = (end + start) / 2
    b = F.bars[i]
    inside = (b.low <= hi_z and b.low >= lo_z - 0.1 * abs(rng)) if d > 0 else (b.high >= lo_z and b.high <= hi_z + 0.1 * abs(rng))
    disc = b.close < eq if d > 0 else b.close > eq
    if inside and disc and _green(b, d):
        return {"anchor": start, "reasons": [f"HTF bias {'bullish' if d > 0 else 'bearish'}", f"Price in the OTE {lo_z:,.2f}-{hi_z:,.2f} of the {start:,.2f}->{end:,.2f} leg",
                f"In {'discount' if d > 0 else 'premium'} (equilibrium {eq:,.2f})"], "zone": (lo_z, hi_z), "leg_end": end}
    return None


def breakout_volume(F, i, d, ctx):
    if i < 21 or not F.atr[i]:
        return None
    b = F.bars[i]
    w = F.bars[i - 20:i]
    lvl = max(x.high for x in w) if d > 0 else min(x.low for x in w)
    if not ((b.close > lvl) if d > 0 else (b.close < lvl)):
        return None
    vol_ok = (F.rvol[i] or 0) >= 2 if F.has_volume else True
    if vol_ok and (b.high - b.low) >= 1.2 * F.atr[i] and _green(b, d):
        return {"anchor": b.low if d > 0 else b.high, "reasons": [f"Closed beyond the 20-candle {'high' if d > 0 else 'low'} {lvl:,.2f}",
                (f"relative volume {F.rvol[i]:.1f}x" if F.has_volume and F.rvol[i] else "range expansion (no volume data)")]}
    return None


def _causal_div(F, i, d, series, name, span=3):
    piv_src = [x.low for x in F.bars[:i + 1]] if d > 0 else [x.high for x in F.bars[:i + 1]]
    piv = [j for j in I.pivots(piv_src, span, "low" if d > 0 else "high") if j + span <= i and series[j] is not None and j >= i - 60]
    if len(piv) < 2:
        return None
    a, b = piv[-2], piv[-1]
    if d > 0 and piv_src[b] < piv_src[a] and series[b] > series[a]:
        return {"to": b, "price": piv_src[b], "name": name}
    if d < 0 and piv_src[b] > piv_src[a] and series[b] < series[a]:
        return {"to": b, "price": piv_src[b], "name": name}
    return None


def divergence_mss(F, i, d, ctx):
    brk = [s for s in F.ext.structure + F.intl.structure if s.time == F.t[i] and s.type == "CHoCH" and s.direction == ("bullish" if d > 0 else "bearish")]
    if not brk:
        return None
    for series, name in ((F.rsi, "RSI"), (F.macd_hist, "MACD histogram")):
        dv = _causal_div(F, i, d, series, name)
        if dv and i - dv["to"] <= 15:
            return {"anchor": dv["price"], "reasons": [f"Regular {'bullish' if d > 0 else 'bearish'} {name} divergence", f"CHoCH at {brk[-1].level:,.2f} confirmed it"]}
    return None


def options_momentum(F, i, d, ctx):
    opt = ctx.get("options")
    if not opt or i != len(F.bars) - 1 or F.vwap[i] is None or F.ema[9][i] is None or F.ema[20][i] is None:
        return None
    b = F.bars[i]
    trend = (b.close > F.vwap[i] and F.ema[9][i] > F.ema[20][i]) if d > 0 else (b.close < F.vwap[i] and F.ema[9][i] < F.ema[20][i])
    flow = opt.get("oi_flow") or 0
    iv_state = (opt.get("trends") or {}).get("iv_state")
    if trend and (flow >= 25 if d > 0 else flow <= -25) and iv_state != "contracting":
        return {"anchor": F.vwap[i], "reasons": [f"OI flow {flow:+d} ({'put writing' if d > 0 else 'call writing'} dominates)",
                f"Price {'above' if d > 0 else 'below'} VWAP with EMA9 {'>' if d > 0 else '<'} EMA20", f"IV {iv_state or 'n/a'}"]}
    return None


def range_reversal(F, i, d, ctx):
    if ctx.get("regime") not in ("range", "choppy") or i < 31:
        return None
    w = F.bars[i - 30:i]
    hi, lo = max(x.high for x in w), min(x.low for x in w)
    b = F.bars[i]
    edge = lo if d > 0 else hi
    if ((b.low < edge and b.close > edge) if d > 0 else (b.high > edge and b.close < edge)) and _rejection(F, i, d):
        return {"anchor": b.low if d > 0 else b.high, "reasons": [f"Range {lo:,.2f}-{hi:,.2f}: swept the {'low' if d > 0 else 'high'} and closed back inside"],
                "range_target": (hi + lo) / 2}
    return None


def trend_mtf(F, i, d, ctx):
    if ctx.get("htf_bias", 0) != d or ctx.get("itf_bias", 0) != d:
        return None
    brk = [s for s in F.intl.structure if s.time == F.t[i] and s.direction == ("bullish" if d > 0 else "bearish")]
    if not brk or F.trend_at(F.t[i - 1], internal=True) == brk[-1].direction:
        return None
    sw = F.swings_at(F.t[i], "low" if d > 0 else "high", internal=True)
    if not sw:
        return None
    return {"anchor": sw[-1].price, "reasons": ["Higher and intermediate timeframes aligned", f"Internal structure turned {brk[-1].direction} again at {brk[-1].level:,.2f}"]}


MODULES = {"sweep_mss_fvg": sweep_mss_fvg, "ob_bos_retest": ob_bos_retest, "vwap_reclaim": vwap_reclaim, "trend_pullback": trend_pullback,
           "orb_retest": orb_retest, "pdhl_sweep": pdhl_sweep, "ote": ote, "breakout_volume": breakout_volume,
           "divergence_mss": divergence_mss, "options_momentum": options_momentum, "range_reversal": range_reversal, "trend_mtf": trend_mtf}


# ---------------------------------------------------------------- setup builder

def _levels_in_direction(F, i, d, entry):
    t = F.t[i]
    above, below = F.liquidity_at(t, entry)
    lv = list(above if d > 0 else below)
    for o in F.obs_at(t, "bearish" if d > 0 else "bullish", fresh_only=True):
        lv.append(o.bottom if d > 0 else o.top)
    for g in F.open_fvgs_at(t, "bearish" if d > 0 else "bullish"):
        lv.append(g["bottom"] if d > 0 else g["top"])
    lv = [x for x in lv if (x > entry if d > 0 else x < entry)]
    return sorted(set(lv), reverse=d < 0)


def build_setup(key: str, raw: dict, F, i: int, d: int, params: dict, market: str, entry: float | None = None) -> tuple[dict | None, str | None]:
    """Turn a raw idea into a full plan, or (None, reason) if it must be rejected."""
    at = F.atr[i]
    if not at:
        return None, "no ATR yet"
    p = {**PARAMS[key], **(params or {})}
    e = F.bars[i].close if entry is None else entry
    stop = raw["anchor"] - p["stop_buffer_atr"] * at if d > 0 else raw["anchor"] + p["stop_buffer_atr"] * at
    risk = (e - stop) if d > 0 else (stop - e)
    if risk <= 0:
        return None, "stop is on the wrong side of the entry"
    if risk < 0.25 * at:
        return None, f"stop only {risk / at:.2f} ATR away: too tight"
    if risk > 4 * at:
        return None, f"stop {risk / at:.1f} ATR away: too wide"
    if p.get("min_avg_volume") and F.has_volume and (F.vol_avg[i] or 0) < p["min_avg_volume"]:
        return None, "below the minimum liquidity"
    R = lambda r: e + d * r * risk
    lv = _levels_in_direction(F, i, d, e)
    if raw.get("range_target"):
        lv = sorted(set(lv + [raw["range_target"]]), reverse=d < 0)
    if raw.get("leg_end"):
        lv = sorted(set(lv + [raw["leg_end"]]), reverse=d < 0)
    t1 = next((x for x in lv if abs(x - e) >= risk), None) or R(1.5)
    d1 = abs(t1 - e) / risk
    t2 = next((x for x in lv if abs(x - e) >= abs(t1 - e) + 0.5 * risk), None) or R(max(2.5, d1 + 1))
    d2 = abs(t2 - e) / risk
    t3 = next((x for x in lv if abs(x - e) >= max(3 * risk, abs(t2 - e) + 0.5 * risk)), None) or R(max(4, d2 + 1))
    rr = abs(t2 - e) / risk
    if rr < p["min_rr"]:
        return None, f"reward:risk to T2 {rr:.2f} is under {p['min_rr']}"
    name, desc = META[key]
    hold = max(1, round(abs(t2 - e) / at * 1.5))
    return {"strategy": key, "name": name, "description": desc, "direction": "LONG" if d > 0 else "SHORT", "time": F.t[i],
            "entry": e, "trigger": e, "entry_zone": list(raw.get("zone") or (min(e, F.bars[i].low if d > 0 else e), max(e, F.bars[i].high if d < 0 else e))),
            "stop": stop, "targets": [t1, t2, t3], "risk": risk, "rr_t1": abs(t1 - e) / risk, "rr": rr, "rr_t3": abs(t3 - e) / risk,
            "atr": at, "stop_atr": risk / at, "max_spread": max(0.1 * at, 0.0005 * e),
            "expected_hold": f"about {hold} candles ({hold * F.step // 60} minutes)" if F.step < 86400 else f"about {hold} days",
            "invalidation": f"A close {'below' if d > 0 else 'above'} {stop:,.2f}, or no follow-through within {hold} candles",
            "trailing": "After T1: stop to breakeven. After T2: trail behind the last internal swing (or 2 x ATR, whichever is tighter).",
            "reasons_for": raw["reasons"]}, None


def evaluate(F, i: int, ctx: dict, settings: dict | None = None) -> list[dict]:
    """Every enabled strategy, both directions, on candle i. Returns qualified setups and rejected ideas."""
    out = []
    st = settings or {}
    if not in_session(F, i, ctx["market"]):
        return out
    for key, fn in MODULES.items():
        p = {**PARAMS[key], **(st.get(key) or {})}
        if not p.get("enabled", True):
            continue
        for d in (1, -1):
            try:
                raw = fn(F, i, d, ctx)
            except (IndexError, ZeroDivisionError, TypeError):
                raw = None
            if not raw:
                continue
            setup, why = build_setup(key, raw, F, i, d, p, ctx["market"])
            if setup:
                setup["regime_ok"] = key in (ctx.get("suits") or [])
                out.append(setup)
            else:
                out.append({"strategy": key, "name": META[key][0], "direction": "LONG" if d > 0 else "SHORT", "rejected": why, "time": F.t[i]})
    return out
