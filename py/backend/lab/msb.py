"""Market Structure Break & Order Blocks: MSB, BU-OB, BU-BB, BU-MB, BE-OB, BE-BB, BE-MB.

The same idea as the well-known "Market Structure Break & Order Block" chart indicator (the labels Sahi shows), worked
out on CLOSED candles only, so a label or box never appears and then disappears.

Rules (parameters at the top):
  1. Zig-zag legs: the leg turns DOWN when a candle's low is the lowest low of the last ZIGZAG_LEN candles while the
     leg is up, and turns UP when a candle's high is the highest of the last ZIGZAG_LEN while the leg is down. When a
     leg ends, its extreme becomes a zig-zag point (a high H or a low L).
  2. MSB (market structure break), on the close:
       bullish: structure is bearish and a candle CLOSES above the latest high H by more than FIB_FACTOR x the size of
                the last down leg (H - L0).                     -> structure becomes bullish
       bearish: structure is bullish and a candle CLOSES below the latest low L by more than FIB_FACTOR x the last up
                leg (H0 - L).                                    -> structure becomes bearish
  3. Zones created by a bullish MSB (they act as demand below price):
       BU-OB  bullish order block: the last DOWN candle (close < open) of the leg from the broken high H down to the
              low L0 the break started from.
       BU-BB  bullish breaker block: the last UP candle of the leg from the previous low L1 up to H, when L0 made a
              LOWER low than L1 (sellers' last push failed, the old supply flips to demand).
       BU-MB  bullish mitigation block: the same candle when L0 was a HIGHER low than L1 (no new low was made).
     A bearish MSB mirrors it: BE-OB = the last UP candle of the leg from L up to H0; BE-BB / BE-MB = the last DOWN
     candle of the leg from the previous high H1 down to L (BB when H0 made a higher high than H1, MB when not).
  4. A zone is the candle's low..high. It is ACTIVE until a candle CLOSES beyond its far side (a bullish zone closed
     below its bottom, a bearish one above its top): then it is BROKEN and no longer drawn by default.
  5. Retest (what the alerts use): the first closed candle after the zone formed that trades into the zone and
     closes back out on the zone's side (bullish: low <= top and close > top; bearish: high >= bottom and close <
     bottom) - a confirmed rejection, not a candle still forming. Later rejections are counted as well.
Analysis only - nothing here places or sizes a trade.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from . import data as D
from .candle import Candle, TF_SECONDS
from .ind import atr_list

ZIGZAG_LEN = 9
FIB_FACTOR = 0.33
MAX_ZONES = 40               # kept per symbol/timeframe (newest)
NEAR_ATR = 0.5               # "at the zone" when price is within this many ATR of it
_cache: dict = {}


@dataclass
class Pivot:
    kind: str                # high | low
    price: float
    time: int
    i: int


@dataclass
class Break:
    direction: str           # bullish | bearish
    level: float             # the high (low) that was broken
    from_time: int           # that high's (low's) candle
    time: int                # the candle whose close broke it
    close: float
    threshold: float         # the close had to clear this (level +/- FIB_FACTOR x the last leg)


@dataclass
class Zone:
    kind: str                # BU-OB | BU-BB | BU-MB | BE-OB | BE-BB | BE-MB
    direction: str           # bullish (demand) | bearish (supply)
    top: float
    bottom: float
    time: int                # the order-block candle
    msb_time: int            # the MSB candle that created it
    created_i: int = field(default=0, repr=False)
    broken_time: int | None = None
    touched_time: int | None = None
    retest_time: int | None = None   # first confirmed rejection
    retests: int = 0
    last_retest_time: int | None = None

    @property
    def state(self) -> str:
        return "broken" if self.broken_time else "retested" if self.retest_time else "touched" if self.touched_time else "fresh"


def _last_candle(bars: list[Candle], a: int, b: int, up: bool) -> int | None:
    """Index of the last up (up=True) or down candle with a <= index <= b."""
    for k in range(min(b, len(bars) - 1), max(a, 0) - 1, -1):
        c = bars[k]
        if (c.close > c.open) if up else (c.close < c.open):
            return k
    return None


def analyze(bars: list[Candle], zigzag_len: int = ZIGZAG_LEN, fib: float = FIB_FACTOR) -> dict:
    n = len(bars)
    pivots: list[Pivot] = []
    breaks: list[Break] = []
    zones: list[Zone] = []
    leg = 0                    # +1 up, -1 down, 0 unknown
    start = 0                  # first candle of the current leg
    market = 0                 # +1 bullish structure, -1 bearish, 0 not known yet
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]

    def last(kind, k=0):
        ps = [p for p in pivots if p.kind == kind]
        return ps[-1 - k] if len(ps) > k else None

    for i, b in enumerate(bars):
        # 1. leg turns (zig-zag point = the finished leg's extreme)
        if i >= zigzag_len - 1:
            w0 = i - zigzag_len + 1
            to_up = b.high >= max(highs[w0:i + 1])
            to_down = b.low <= min(lows[w0:i + 1])
            if leg >= 0 and to_down and not (leg == 0 and to_up):
                if leg == 1:
                    j = max(range(start, i + 1), key=lambda k: (highs[k], k))
                    pivots.append(Pivot("high", highs[j], bars[j].time, j))
                    start = j
                leg = -1
            elif leg <= 0 and to_up:
                if leg == -1:
                    j = min(range(start, i + 1), key=lambda k: (lows[k], -k))
                    pivots.append(Pivot("low", lows[j], bars[j].time, j))
                    start = j
                leg = 1

        # 2. zones: broken / touched / retested by this candle (zones made on this candle are added after)
        for z in zones:
            if z.broken_time or i <= z.created_i:
                continue
            bull = z.direction == "bullish"
            if (b.close < z.bottom) if bull else (b.close > z.top):
                z.broken_time = b.time
                continue
            inside = b.low <= z.top and b.high >= z.bottom
            if inside and z.touched_time is None:
                z.touched_time = b.time
            if (b.low <= z.top and b.close > z.top) if bull else (b.high >= z.bottom and b.close < z.bottom):
                z.retests += 1
                z.last_retest_time = b.time
                if z.retest_time is None:
                    z.retest_time = b.time

        # 3. market structure break on the close
        H, L = last("high"), last("low")
        if market <= 0 and H and L and L.i > H.i:
            thr = H.price + abs(H.price - L.price) * fib
            if b.close > thr and b.close > H.price:
                market = 1
                breaks.append(Break("bullish", H.price, H.time, b.time, b.close, thr))
                ob = _last_candle(bars, H.i, L.i, up=False)
                if ob is not None:
                    zones.append(Zone("BU-OB", "bullish", bars[ob].high, bars[ob].low, bars[ob].time, b.time, i))
                L1 = last("low", 1)
                if L1 and L1.i < H.i:
                    k = _last_candle(bars, L1.i, H.i, up=True)
                    if k is not None:
                        zones.append(Zone("BU-BB" if L.price < L1.price else "BU-MB", "bullish", bars[k].high, bars[k].low, bars[k].time, b.time, i))
        if market >= 0 and H and L and H.i > L.i:
            thr = L.price - abs(H.price - L.price) * fib
            if b.close < thr and b.close < L.price:
                market = -1
                breaks.append(Break("bearish", L.price, L.time, b.time, b.close, thr))
                ob = _last_candle(bars, L.i, H.i, up=True)
                if ob is not None:
                    zones.append(Zone("BE-OB", "bearish", bars[ob].high, bars[ob].low, bars[ob].time, b.time, i))
                H1 = last("high", 1)
                if H1 and H1.i < L.i:
                    k = _last_candle(bars, H1.i, L.i, up=False)
                    if k is not None:
                        zones.append(Zone("BE-BB" if H.price > H1.price else "BE-MB", "bearish", bars[k].high, bars[k].low, bars[k].time, b.time, i))

    a = atr_list(bars)
    at = next((x for x in reversed(a) if x), None) if bars else None
    price = bars[-1].close if bars else None
    zones = zones[-MAX_ZONES:]
    active = [z for z in zones if not z.broken_time]

    def dist(z):
        if price is None:
            return None
        d = (z.bottom - price) if z.bottom > price else (price - z.top) if price > z.top else 0.0
        return round(d / at, 2) if at else None

    def zd(z: Zone) -> dict:
        return {**{k: v for k, v in asdict(z).items() if k != "created_i"}, "state": z.state,
                "label": z.kind, "distance_atr": dist(z),
                "at_price": bool(price is not None and z.bottom <= price <= z.top)}

    zs = [zd(z) for z in zones]
    act = [x for x in zs if x["state"] != "broken"]
    near = lambda x: x["distance_atr"] if x["distance_atr"] is not None else 9e9
    p0 = price if price is not None else 0.0
    support = sorted([x for x in act if x["direction"] == "bullish" and (x["top"] <= p0 or x["at_price"])], key=near)
    resist = sorted([x for x in act if x["direction"] == "bearish" and (x["bottom"] >= p0 or x["at_price"])], key=near)
    last_bar = bars[-1].time if bars else None
    fresh_retests = [x for x in act if x["last_retest_time"] == last_bar]
    out = {
        "structure": "bullish" if market == 1 else "bearish" if market == -1 else "not known yet",
        "msb": [asdict(x) for x in breaks[-30:]],
        "last_msb": asdict(breaks[-1]) if breaks else None,
        "zones": zs,
        "active": len(act),
        "nearest_demand": support[0] if support else None,
        "nearest_supply": resist[0] if resist else None,
        "in_zone": [x for x in act if x["at_price"]],
        "near_zone": [x for x in act if x["distance_atr"] is not None and x["distance_atr"] <= NEAR_ATR],
        "retest_now": fresh_retests,                 # zones whose confirmed rejection is the LAST closed candle
        "broken_now": [x for x in zs if x["broken_time"] is not None and x["broken_time"] == last_bar],
        "pivots": [{"kind": p.kind, "price": p.price, "time": p.time} for p in pivots[-40:]],
        "atr": at, "last": price, "last_time": last_bar,
        "params": {"zigzag_len": zigzag_len, "fib_factor": fib},
    }
    out["summary"] = _summary(out)
    out["vote"] = vote(out)
    return out


def vote(r: dict) -> int | None:
    """Location evidence for the agent: +1 when price sits at (or just rejected from) an active demand zone and
    structure is bullish, -1 for supply in bearish structure, None otherwise (no opinion away from a zone)."""
    at = r.get("in_zone", []) + r.get("near_zone", []) + r.get("retest_now", [])
    bull = any(z["direction"] == "bullish" for z in at)
    bear = any(z["direction"] == "bearish" for z in at)
    if bull and not bear and r.get("structure") == "bullish":
        return 1
    if bear and not bull and r.get("structure") == "bearish":
        return -1
    return None


def _f(x) -> str:
    return "—" if x is None else f"{x:,.2f}" if abs(x) >= 10 else f"{x:,.4g}"


def _summary(r: dict) -> str:
    m = r.get("last_msb")
    if not m:
        return "No market structure break yet on these candles."
    parts = [f"Structure {r['structure']}: last MSB {m['direction']} through {_f(m['level'])}."]
    for z in r["retest_now"]:
        parts.append(f"{z['kind']} {_f(z['bottom'])}–{_f(z['top'])} just rejected price (retest #{z['retests']}).")
    if r["in_zone"]:
        parts.append("Price is inside " + ", ".join(f"{z['kind']} {_f(z['bottom'])}–{_f(z['top'])}" for z in r["in_zone"]) + ".")
    if r["nearest_demand"] and not r["nearest_demand"]["at_price"]:
        z = r["nearest_demand"]
        parts.append(f"Nearest demand {z['kind']} {_f(z['bottom'])}–{_f(z['top'])} ({z['distance_atr']} ATR below).")
    if r["nearest_supply"] and not r["nearest_supply"]["at_price"]:
        z = r["nearest_supply"]
        parts.append(f"Nearest supply {z['kind']} {_f(z['bottom'])}–{_f(z['top'])} ({z['distance_atr']} ATR above).")
    return " ".join(parts)


async def compute(symbol: str, tf: str = "5m", bars: int = 600) -> dict:
    """MSB / order blocks for one symbol and timeframe (closed candles; shares the Lab's candle cache)."""
    symbol = D.norm_symbol(symbol.strip().upper())
    tf = tf if tf in TF_SECONDS else "5m"
    key = (symbol, tf)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < min(60, TF_SECONDS[tf] / 5):
        return hit[1]
    cs, q = await D.candles(symbol, tf, max(200, min(int(bars or 600), 1500)))
    out = {"symbol": symbol, "tf": tf, "market": D.market_of(symbol), **analyze(cs),
           "as_of": q.get("as_of"), "source": q.get("source"), "candles": len(cs), "method": __doc__.split("\n\n")[0]}
    _cache[key] = (time.time(), out)
    return out


def brief(r: dict) -> dict:
    """One scanner row."""
    pick = ("kind", "direction", "top", "bottom", "time", "state", "retests", "last_retest_time", "broken_time", "distance_atr", "at_price", "msb_time")
    z = lambda x: {k: x.get(k) for k in pick} if x else None
    return {"symbol": r["symbol"], "tf": r["tf"], "market": r["market"], "structure": r["structure"], "last": r["last"],
            "last_time": r["last_time"], "as_of": r.get("as_of"), "source": r.get("source"),
            "last_msb": r["last_msb"], "active": r["active"], "nearest_demand": z(r["nearest_demand"]),
            "nearest_supply": z(r["nearest_supply"]), "in_zone": [z(x) for x in r["in_zone"]],
            "retest_now": [z(x) for x in r["retest_now"]], "broken_now": [z(x) for x in r["broken_now"]], "vote": r["vote"], "summary": r["summary"]}


async def scan(symbols: list[str], tf: str = "5m") -> dict:
    """Scanner: one row per symbol, fetched one after another (FYERS rate limits). A symbol that fails is reported."""
    rows = []
    for s in [x for x in symbols if x][:120]:
        try:
            rows.append(brief(await compute(s, tf)))
        except Exception as exc:                      # noqa: BLE001 - one symbol failing must not hide the rest
            rows.append({"symbol": s, "tf": tf, "error": str(getattr(exc, "detail", exc))[:200]})
    return {"tf": tf, "rows": rows, "at": int(time.time())}
