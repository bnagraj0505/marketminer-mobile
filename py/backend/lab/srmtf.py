"""Multi-timeframe support / resistance ZONES: weekly, daily, 4 hour, 1 hour and 15 minute.

For each timeframe (closed candles only):
  1. swing highs and lows (pivots with `span` candles each side), each with the size of the move away from it in ATR
  2. swings within ZONE_JOIN x ATR of each other are joined into one zone (bottom = lowest, top = highest)
  3. a zone is at least MIN_W x ATR thick (thin zones are padded around their middle) and is split if it would be
     wider than MAX_W x ATR
  4. strength = swings in the zone + 0.5 x later rejections (a wick into the zone, close back outside, capped at 4)
     + the average reaction in ATR (capped at 3) + recency (0..1)
  5. role from the last close: support (below price), resistance (above price) or "at price"; a zone made of both
     swing highs and swing lows is marked as a flip zone (old resistance now support, or the reverse)
  6. kept: the PER_SIDE strongest zones below and above price, nearest first, within RANGE_ATR x ATR
Confluence: every zone lists the other timeframes whose zones overlap it.
Analysis only - nothing here places or sizes a trade.
"""
from __future__ import annotations

import asyncio
import time

from . import data as D
from .ind import atr_list

# tf, candles, pivot span, label
TFS = [("1w", 260, 2, "W"), ("1d", 400, 3, "D"), ("4h", 400, 3, "4H"), ("1h", 500, 4, "1H"), ("15m", 500, 5, "15m")]
ZONE_JOIN, MIN_W, MAX_W = 0.5, 0.2, 1.2          # in ATR of that timeframe
PER_SIDE, RANGE_ATR = 3, 12.0
TTL = {"1w": 1800, "1d": 900, "4h": 300, "1h": 180, "15m": 90}
_cache: dict = {}


def pivots(values: list[float], span: int, kind: str) -> list[int]:
    """Swing highs/lows that also accept flat tops/bottoms (equal highs or lows): the first candle of the flat run
    counts, strictly beyond the `span` candles on its left and not exceeded by the `span` on its right."""
    out = []
    for j in range(span, len(values) - span):
        v, left, right = values[j], values[j - span:j], values[j + 1:j + span + 1]
        if kind == "high" and all(x < v for x in left) and all(x <= v for x in right):
            out.append(j)
        if kind == "low" and all(x > v for x in left) and all(x >= v for x in right):
            out.append(j)
    return out


def _reaction(bars, j: int, kind: str, at: float, look: int) -> float:
    seg = bars[j + 1:j + 1 + look]
    if not seg or not at:
        return 0.0
    if kind == "high":
        return max(0.0, bars[j].high - min(b.low for b in seg)) / at
    return max(0.0, max(b.high for b in seg) - bars[j].low) / at


def zones_for(bars, span: int) -> dict:
    """Zones for one timeframe's candles (oldest first)."""
    if len(bars) < 2 * span + 20:
        return {"zones": [], "atr": None, "last": bars[-1].close if bars else None}
    a = atr_list(bars)
    at = next((x for x in reversed(a) if x), None)
    last = bars[-1].close
    if not at:
        return {"zones": [], "atr": None, "last": last}
    look = span * 3
    pts = [(bars[j].high, bars[j].time, "high", _reaction(bars, j, "high", at, look), j) for j in pivots([b.high for b in bars], span, "high")]
    pts += [(bars[j].low, bars[j].time, "low", _reaction(bars, j, "low", at, look), j) for j in pivots([b.low for b in bars], span, "low")]
    pts.sort()
    groups: list[list] = []
    for p in pts:
        if groups and p[0] - groups[-1][-1][0] <= ZONE_JOIN * at and p[0] - groups[-1][0][0] <= MAX_W * at:
            groups[-1].append(p)
        else:
            groups.append([p])
    t0, t1 = bars[0].time, bars[-1].time
    zones = []
    for g in groups:
        lo, hi = min(x[0] for x in g), max(x[0] for x in g)
        if hi - lo < MIN_W * at:
            m = (hi + lo) / 2
            lo, hi = m - MIN_W * at / 2, m + MIN_W * at / 2
        first_j = min(x[4] for x in g)
        rej = 0
        for b in bars[first_j + span + 1:]:
            if b.low <= hi and b.high >= lo and (b.close > hi or b.close < lo):
                rej += 1
        last_t = max(x[1] for x in g)
        rec = (last_t - t0) / (t1 - t0) if t1 > t0 else 1.0
        react = sum(x[3] for x in g) / len(g)
        strength = len(g) + 0.5 * min(rej, 4) + min(react, 3.0) + rec
        kinds = {x[2] for x in g}
        role = "support" if last > hi else "resistance" if last < lo else "at price"
        zones.append({"bottom": lo, "top": hi, "mid": (lo + hi) / 2, "role": role, "flip": len(kinds) == 2,
                      "touches": len(g), "rejections": rej, "reaction_atr": round(react, 2), "strength": round(strength, 2),
                      "first_time": min(x[1] for x in g), "last_time": last_t,
                      "distance_atr": round(((lo - last) if lo > last else (last - hi) if last > hi else 0.0) / at, 2)})
    near = [z for z in zones if z["distance_atr"] <= RANGE_ATR]
    pick = []
    for side in ("support", "resistance"):
        s = sorted([z for z in near if z["role"] == side], key=lambda z: -z["strength"])[:PER_SIDE]
        pick += sorted(s, key=lambda z: z["distance_atr"])
    inside = sorted([z for z in near if z["role"] == "at price"], key=lambda z: -z["strength"])[:1]
    return {"zones": inside + pick, "atr": at, "last": last}


async def _tf_zones(symbol: str, tf: str, n: int, span: int) -> dict:
    key = (symbol, tf)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < TTL[tf]:
        return hit[1]
    try:
        bars, q = await D.candles(symbol, tf, n)
        out = {**zones_for(bars, span), "as_of": q.get("as_of"), "source": q.get("source"), "candles": len(bars)}
    except Exception as exc:                          # noqa: BLE001 - one timeframe failing must not hide the others
        out = {"zones": [], "error": str(getattr(exc, "detail", exc))[:200]}
    _cache[key] = (time.time(), out)
    return out


async def compute(symbol: str) -> dict:
    symbol = symbol.strip().upper()
    res = await asyncio.gather(*[_tf_zones(symbol, tf, n, sp) for tf, n, sp, _ in TFS])
    out = {"symbol": symbol, "timeframes": {}}
    for (tf, _, _, lab), r in zip(TFS, res):
        out["timeframes"][tf] = {"label": lab, **{k: (round(v, 6) if isinstance(v, float) else v) for k, v in r.items() if k != "zones"},
                                 "zones": [{**z, "tf": tf, "label": lab} for z in r.get("zones", [])]}
    allz = [z for v in out["timeframes"].values() for z in v["zones"]]
    for z in allz:
        z["confluence"] = sorted({o["label"] for o in allz if o["tf"] != z["tf"] and o["bottom"] <= z["top"] and o["top"] >= z["bottom"]})
        for k in ("bottom", "top", "mid"):
            z[k] = round(z[k], 6)
    last = next((v.get("last") for tf, *_ in TFS for v in [out["timeframes"][tf]] if v.get("last")), None)
    out["last"] = out["timeframes"]["15m"].get("last") or last
    out["method"] = __doc__.split("\n\n")[0]
    return out
