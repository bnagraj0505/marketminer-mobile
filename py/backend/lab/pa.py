"""Price action engine: candle patterns, candle strength, volatility contraction/expansion, support/resistance and
supply/demand zones, breakout-retest and false breakouts, ranges and trend exhaustion. Patterns are reported with
their context (where they formed) instead of as stand-alone signals. Closed candles only.

Definitions (ATR = Wilder ATR14 at that candle)
- Bullish engulfing: previous candle red, this candle green, body covers the previous body. Bearish mirror.
- Pin bar: one wick >= 2 x body and >= 60% of the range, the other wick <= 25% of the range. Hammer = lower wick
  (bullish rejection), shooting star = upper wick (bearish rejection).
- Rejection candle: a pin bar whose long wick traded through a support/resistance zone or a swing level and
  closed back inside.
- Inside bar: high <= previous high and low >= previous low. Outside bar: high > previous high and low < previous low.
- Momentum candle: body >= 1 x ATR and closes in the outer 25% of its range. Displacement: see ict.py.
- Consecutive strength: the run of same-colour candles ending at the latest candle, with their summed body / ATR.
- Range expansion: range >= 1.5 x ATR. Volume expansion: volume >= 1.5 x the 20-candle average.
- Volatility contraction: Bollinger bandwidth in the lowest 20% of its last 120 values (squeeze); expansion when
  the latest bandwidth exceeds the 80th percentile.
- S/R zones: confirmed swing highs/lows (span 3) clustered within 0.25 x ATR, scored by touches; a zone the last
  close is above is support, below is resistance.
- Supply/demand: the base candle(s) before a displacement candle; zone = base low..high.
- Breakout + retest: a close beyond a zone, then within 10 candles a candle touches the zone and closes back on the
  breakout side.
- False breakout: from smc.py traps (close beyond a structure level reversed within 3 candles).
- Range-bound: ADX < 20 and the last 30 candles' high-low range < 4 x ATR.
- Trend exhaustion: 3+ consecutive momentum candles in one direction ending at an RSI extreme (>75 / <25), or a
  regular RSI divergence at the latest pivot.
"""
from __future__ import annotations

from .candle import Candle
from .ind import atr_list, bollinger, sma, rsi, adx_dmi, pivots, divergences


def candle_patterns(bars: list[Candle], lookback: int = 30) -> list[dict]:
    a = atr_list(bars)
    vavg = sma([b.volume for b in bars], 20)
    out = []
    for i in range(max(1, len(bars) - lookback), len(bars)):
        b, p = bars[i], bars[i - 1]
        rng = b.high - b.low
        if rng <= 0:
            continue
        body = abs(b.close - b.open)
        up_w = b.high - max(b.open, b.close)
        lo_w = min(b.open, b.close) - b.low
        green, pgreen = b.close > b.open, p.close > p.open
        tags = []
        if green and not pgreen and b.close >= p.open and b.open <= p.close and body > abs(p.close - p.open):
            tags.append(("bullish engulfing", "bullish"))
        if not green and pgreen and b.close <= p.open and b.open >= p.close and body > abs(p.close - p.open):
            tags.append(("bearish engulfing", "bearish"))
        if lo_w >= 2 * body and lo_w >= 0.6 * rng and up_w <= 0.25 * rng:
            tags.append(("hammer / bullish pin bar", "bullish"))
        if up_w >= 2 * body and up_w >= 0.6 * rng and lo_w <= 0.25 * rng:
            tags.append(("shooting star / bearish pin bar", "bearish"))
        if b.high <= p.high and b.low >= p.low:
            tags.append(("inside bar", "neutral"))
        if b.high > p.high and b.low < p.low:
            tags.append(("outside bar", "bullish" if green else "bearish"))
        if a[i] and body >= a[i] and ((green and b.close >= b.high - 0.25 * rng) or (not green and b.close <= b.low + 0.25 * rng)):
            tags.append(("momentum candle", "bullish" if green else "bearish"))
        if a[i] and rng >= 1.5 * a[i]:
            tags.append(("range expansion", "bullish" if green else "bearish"))
        if vavg[i] and b.volume >= 1.5 * vavg[i]:
            tags.append(("volume expansion", "bullish" if green else "bearish"))
        for name, d in tags:
            out.append({"time": b.time, "pattern": name, "direction": d, "wick_body_ratio": round((up_w + lo_w) / body, 2) if body else None})
    return out


def consecutive(bars: list[Candle]) -> dict:
    a = atr_list(bars)
    if not bars:
        return {}
    green = bars[-1].close > bars[-1].open
    n, body = 0, 0.0
    for b in reversed(bars):
        if (b.close > b.open) != green or b.close == b.open:
            break
        n += 1
        body += abs(b.close - b.open)
    at = next((x for x in reversed(a) if x), None)
    return {"direction": "bullish" if green else "bearish", "count": n, "body_atr": round(body / at, 2) if at else None}


def volatility_state(bars: list[Candle]) -> dict:
    _, _, _, bw = bollinger([b.close for b in bars])
    hist = [x for x in bw[-120:] if x is not None]
    if len(hist) < 30:
        return {"state": "unknown"}
    cur = hist[-1]
    rank = 100 * sum(1 for x in hist if x < cur) / len(hist)
    state = "contraction (squeeze)" if rank <= 20 else "expansion" if rank >= 80 else "normal"
    return {"state": state, "bandwidth_pct": round(cur, 3), "bandwidth_rank": round(rank)}


def sr_zones(bars: list[Candle], span: int = 3, max_zones: int = 8) -> list[dict]:
    a = atr_list(bars)
    at = next((x for x in reversed(a) if x), None)
    if not at or len(bars) < 2 * span + 1:
        return []
    pts = [(bars[j].high, bars[j].time) for j in pivots([b.high for b in bars], span, "high")]
    pts += [(bars[j].low, bars[j].time) for j in pivots([b.low for b in bars], span, "low")]
    pts.sort()
    zones = []
    for px, t in pts:
        if zones and px - zones[-1]["top"] <= 0.25 * at:
            z = zones[-1]
            z["top"] = max(z["top"], px)
            z["touches"] += 1
            z["last_time"] = max(z["last_time"], t)
        else:
            zones.append({"bottom": px, "top": px, "touches": 1, "last_time": t})
    last = bars[-1].close
    for z in zones:
        z["kind"] = "support" if last > z["top"] else "resistance" if last < z["bottom"] else "inside"
        z["mid"] = (z["top"] + z["bottom"]) / 2
    zones.sort(key=lambda z: (-z["touches"], abs(z["mid"] - last)))
    return sorted(zones[:max_zones], key=lambda z: z["mid"])


def supply_demand(bars: list[Candle], disp: list[int], lookback: int = 150) -> list[dict]:
    out = []
    for i in range(max(1, len(bars) - lookback), len(bars)):
        if disp[i] == 0:
            continue
        base = bars[i - 1]
        z = {"kind": "demand" if disp[i] > 0 else "supply", "top": base.high, "bottom": base.low, "time": base.time, "fresh": True}
        for b in bars[i + 1:]:
            if b.low <= z["top"] and b.high >= z["bottom"]:
                z["fresh"] = False
                break
        out.append(z)
    return out[-10:]


def breakout_retest(bars: list[Candle], zones: list[dict], window: int = 10) -> list[dict]:
    out = []
    for z in zones:
        for i in range(len(bars) - 60 if len(bars) > 60 else 1, len(bars)):
            b, p = bars[i], bars[i - 1]
            up = p.close <= z["top"] < b.close
            dn = p.close >= z["bottom"] > b.close
            if not (up or dn):
                continue
            for j in range(i + 1, min(len(bars), i + 1 + window)):
                c = bars[j]
                if up and c.low <= z["top"] and c.close > z["top"]:
                    out.append({"direction": "bullish", "level": z["top"], "break_time": b.time, "retest_time": c.time})
                    break
                if dn and c.high >= z["bottom"] and c.close < z["bottom"]:
                    out.append({"direction": "bearish", "level": z["bottom"], "break_time": b.time, "retest_time": c.time})
                    break
    return sorted(out, key=lambda x: x["retest_time"])[-6:]


def range_and_exhaustion(bars: list[Candle]) -> dict:
    a = atr_list(bars)
    adx, _, _ = adx_dmi(bars)
    r = rsi([b.close for b in bars])
    at = next((x for x in reversed(a) if x), None)
    w = bars[-30:]
    out = {}
    if at and adx[-1] is not None and len(w) == 30:
        span = max(b.high for b in w) - min(b.low for b in w)
        out["range_bound"] = adx[-1] < 20 and span < 4 * at
        out["range_high"], out["range_low"] = max(b.high for b in w), min(b.low for b in w)
    c = consecutive(bars)
    exh = None
    if c.get("count", 0) >= 3 and r[-1] is not None and ((c["direction"] == "bullish" and r[-1] > 75) or (c["direction"] == "bearish" and r[-1] < 25)):
        exh = f"{c['count']} {c['direction']} candles into RSI {r[-1]:.0f}"
    dv = [d for d in divergences(bars, r, "RSI") if d["type"] == "regular"]
    if dv and dv[-1]["to"] >= bars[max(0, len(bars) - 10)].time:
        exh = (exh + "; " if exh else "") + f"regular {dv[-1]['direction']} RSI divergence"
    out["exhaustion"] = exh
    return out


def analyze(bars: list[Candle], disp: list[int], traps: list[dict]) -> dict:
    zones = sr_zones(bars)
    return {"patterns": candle_patterns(bars)[-15:], "consecutive": consecutive(bars), "volatility": volatility_state(bars),
            "zones": zones, "supply_demand": supply_demand(bars, disp), "breakout_retest": breakout_retest(bars, zones),
            "false_breakouts": traps[-5:], **range_and_exhaustion(bars)}
