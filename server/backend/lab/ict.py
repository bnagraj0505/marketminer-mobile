"""ICT / SMC layer on top of smc.py (swings, BOS/CHoCH, sweeps, FVGs). Every concept below has an exact rule, its
parameters at the top, and a timestamp; all of it runs on closed candles only.

Definitions
- Displacement candle: body >= DISP_BODY_ATR x ATR(14) and body >= DISP_BODY_SHARE of its range.
- MSS (market structure shift): a CHoCH whose breaking candle, or one of the DISP_LOOKBACK candles before it, is a
  displacement candle in the break's direction.
- Internal / external structure: smc.py run with swing span INTERNAL_SPAN (2) / EXTERNAL_SPAN (5).
- Protected high/low: in a bullish external trend, the swing low the latest bullish break came from (the lowest
  swing low between the previous break and this one); its loss is the CHoCH level. Mirror for bearish.
- Order block (bullish): for a bullish BOS/CHoCH, the last down-close candle (close < open) in the
  OB_SEARCH candles before the displacement leg began. Zone = that candle's low..high. Bearish mirror.
  Mitigated: a later candle trades into the zone. Breaker: a later candle CLOSES beyond the far side (bullish OB
  closed below its low), which flips it into a bearish breaker block. Mitigation block: an OB that was mitigated
  and then price closed away in the OB's direction (the zone held).
  Volume confirmation: the breaking leg's volume >= OB_VOL_MULT x the 20-candle average.
- Inverse FVG: an FVG that a candle CLOSES through (bullish FVG closed below its bottom) flips polarity.
- Consequent encroachment (CE): the FVG's midpoint.
- FVG quality 0-100: size vs ATR (40), displacement of the middle candle (30), volume (15), untouched (15).
- Equal highs/lows: two confirmed swing highs (lows) within EQ_TOL_ATR x ATR of each other, not yet traded through.
- Dealing range: the latest external swing high and low. Premium above its 50% (equilibrium), discount below.
- OTE: 0.62-0.79 retracement of the latest impulse leg (swing to the BOS candle's extreme); the "sweet spot" 0.705.
- Asian range: high/low of the Asian session (00:00-06:00 UTC = 05:30-11:30 IST) of the current UTC day.
- Kill zones: London open 07:00-10:00 UTC, New York open 12:00-15:00 UTC (12:00-15:00 UTC in winter means
  07:00-10:00 New York time; US daylight saving is applied), London close 15:00-17:00 UTC; for NSE/BSE: opening
  drive 09:15-10:15 IST and closing hour 14:30-15:30 IST.
- Judas swing: in the first kill zone of the day, price trades beyond one side of the Asian range (or for NSE the
  opening range of the first 15 minutes) and then a candle closes back beyond the session open the other way.
- Power of three (AMD): accumulation = Asian range (or NSE opening 30 minutes); manipulation = the first sweep of
  one side of it; distribution = a later close beyond the opposite side. The current phase is reported.
- SMT divergence: two correlated instruments over the same window; one sweeps its previous swing high (low) while
  the other fails to, -> bearish (bullish) SMT, timestamped at the sweeping candle.
- Inducement: the latest internal swing (span 2) between price and the nearest unmitigated order block / FVG in
  the retracement direction; "taken" once swept.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass

from .candle import Candle
from .ind import atr_list, sma
from . import smc as S

DISP_BODY_ATR = 1.2
DISP_BODY_SHARE = 0.6
DISP_LOOKBACK = 3
INTERNAL_SPAN = 2
EXTERNAL_SPAN = 5
OB_SEARCH = 10
OB_VOL_MULT = 1.5
EQ_TOL_ATR = 0.1


def displacement(bars: list[Candle]) -> list[int]:
    """+1 bullish displacement, -1 bearish, 0 none, per candle."""
    a = atr_list(bars)
    out = []
    for i, b in enumerate(bars):
        body, rng = abs(b.close - b.open), b.high - b.low
        ok = a[i] is not None and rng > 0 and body >= DISP_BODY_ATR * a[i] and body >= DISP_BODY_SHARE * rng
        out.append((1 if b.close > b.open else -1) if ok else 0)
    return out


@dataclass
class OrderBlock:
    direction: str  # bullish | bearish
    top: float
    bottom: float
    time: int  # the order-block candle
    break_time: int  # the BOS/CHoCH that created it
    break_type: str
    volume_confirmed: bool
    mitigated_time: int | None = None
    breaker_time: int | None = None  # closed through: now a breaker of the opposite direction
    held_time: int | None = None  # mitigated, then closed away in its direction (mitigation block)

    @property
    def state(self) -> str:
        if self.breaker_time:
            return "breaker"
        if self.held_time:
            return "mitigation block"
        if self.mitigated_time:
            return "mitigated"
        return "fresh"


def order_blocks(bars: list[Candle], rep: S.SmcReport) -> list[OrderBlock]:
    idx = {b.time: i for i, b in enumerate(bars)}
    vavg = sma([b.volume for b in bars], 20)
    out: list[OrderBlock] = []
    for br in rep.structure:
        k = idx.get(br.time)
        if k is None:
            continue
        bull = br.direction == "bullish"
        # start of the leg: walk back while candles move with the break
        j = k
        while j > 0 and (bars[j].close > bars[j].open) == bull and k - j < OB_SEARCH:
            j -= 1
        ob = None
        for m in range(j, max(-1, j - OB_SEARCH), -1):
            if (bars[m].close < bars[m].open) if bull else (bars[m].close > bars[m].open):
                ob = m
                break
        if ob is None:
            continue
        leg_vol = sum(b.volume for b in bars[ob + 1:k + 1]) / max(1, k - ob)
        vc = bool(vavg[k] and leg_vol >= OB_VOL_MULT * vavg[k])
        o = OrderBlock("bullish" if bull else "bearish", bars[ob].high, bars[ob].low, bars[ob].time, br.time, br.type, vc)
        for b in bars[k + 1:]:
            inside = b.low <= o.top and b.high >= o.bottom
            if o.mitigated_time is None and inside:
                o.mitigated_time = b.time
            if (b.close < o.bottom) if bull else (b.close > o.top):
                o.breaker_time = b.time
                break
            if o.mitigated_time and o.held_time is None and b.time > o.mitigated_time and ((b.close > o.top) if bull else (b.close < o.bottom)):
                o.held_time = b.time
        out.append(o)
    return out


def fvg_extras(bars: list[Candle], rep: S.SmcReport) -> list[dict]:
    """FVGs with CE, inverse-FVG flips and a 0-100 quality score."""
    a = atr_list(bars)
    idx = {b.time: i for i, b in enumerate(bars)}
    vavg = sma([b.volume for b in bars], 20)
    disp = displacement(bars)
    out = []
    for g in rep.fvgs:
        i = idx.get(g.time)
        if i is None:
            continue
        size = g.top - g.bottom
        at = a[i] or size or 1
        mid = bars[i]
        q = min(40.0, 40 * size / (at * 1.0))
        q += 30 if disp[i] == (1 if g.direction == "bullish" else -1) else 10 if abs(mid.close - mid.open) > 0.6 * (mid.high - mid.low or 1) else 0
        q += 15 if vavg[i] and mid.volume >= 1.5 * vavg[i] else 5 if vavg[i] and mid.volume >= vavg[i] else 0
        q += 15 if not g.touched else 0
        inv = None
        for b in bars[idx.get(g.created_time, i) + 1:]:
            if (b.close < g.bottom) if g.direction == "bullish" else (b.close > g.top):
                inv = b.time
                break
        d = asdict(g)
        d.update(ce=(g.top + g.bottom) / 2, quality=round(min(100.0, q)), inverse_time=inv,
                 inverse_direction=("bearish" if g.direction == "bullish" else "bullish") if inv else None)
        out.append(d)
    return out


def mss(bars: list[Candle], rep: S.SmcReport) -> list[dict]:
    idx = {b.time: i for i, b in enumerate(bars)}
    disp = displacement(bars)
    out = []
    for br in rep.structure:
        if br.type != "CHoCH":
            continue
        k = idx.get(br.time)
        if k is None:
            continue
        want = 1 if br.direction == "bullish" else -1
        if any(disp[j] == want for j in range(max(0, k - DISP_LOOKBACK), k + 1)):
            out.append({"direction": br.direction, "level": br.level, "time": br.time})
    return out


def protected_levels(rep: S.SmcReport) -> dict:
    """The swing the latest external break came from; losing it is the CHoCH level."""
    if not rep.structure:
        return {}
    last = rep.structure[-1]
    prev_t = rep.structure[-2].time if len(rep.structure) > 1 else 0
    kind = "low" if last.direction == "bullish" else "high"
    cands = [s for s in rep.swings if s.kind == kind and prev_t <= s.time <= last.time]
    if not cands:
        return {}
    s = min(cands, key=lambda x: x.price) if kind == "low" else max(cands, key=lambda x: x.price)
    return {"protected_" + kind: s.price, "time": s.time, "trend": last.direction}


def equal_levels(bars: list[Candle], rep: S.SmcReport) -> list[dict]:
    a = atr_list(bars)
    tol = (next((x for x in reversed(a) if x), None) or 0) * EQ_TOL_ATR
    out = []
    for kind, live in (("high", rep.buy_side), ("low", rep.sell_side)):
        pts = sorted(live, key=lambda s: s.price)
        for x, y in zip(pts, pts[1:]):
            if tol and abs(x.price - y.price) <= tol:
                out.append({"kind": "equal " + kind + "s", "price": (x.price + y.price) / 2, "times": [x.time, y.time],
                            "side": "buy" if kind == "high" else "sell"})
    return out


def dealing_range(rep: S.SmcReport, price: float) -> dict:
    hs = [s for s in rep.swings if s.kind == "high"]
    ls = [s for s in rep.swings if s.kind == "low"]
    if not hs or not ls:
        return {}
    hi, lo = hs[-1].price, ls[-1].price
    if hi <= lo:
        return {}
    eq = (hi + lo) / 2
    pct = 100 * (price - lo) / (hi - lo)
    zone = "premium" if price > eq else "discount" if price < eq else "equilibrium"
    return {"high": hi, "low": lo, "equilibrium": eq, "position_pct": round(pct, 1), "zone": zone,
            "high_time": hs[-1].time, "low_time": ls[-1].time}


def ote(bars: list[Candle], rep: S.SmcReport) -> dict:
    """OTE of the latest impulse: from the swing before the last break to the break leg's extreme."""
    if not rep.structure:
        return {}
    br = rep.structure[-1]
    idx = {b.time: i for i, b in enumerate(bars)}
    k = idx.get(br.time)
    if k is None:
        return {}
    bull = br.direction == "bullish"
    kind = "low" if bull else "high"
    prev = [s for s in rep.swings if s.kind == kind and s.time <= br.time]
    if not prev:
        return {}
    start = prev[-1].price
    ext = max(b.high for b in bars[k:]) if bull else min(b.low for b in bars[k:])
    rng = ext - start
    lvl = lambda r: ext - r * rng
    return {"direction": br.direction, "leg_start": start, "leg_end": ext, "ote_top": lvl(0.62), "ote_bottom": lvl(0.79),
            "sweet_spot": lvl(0.705), "fib_50": lvl(0.5), "time": br.time} if bull else \
           {"direction": br.direction, "leg_start": start, "leg_end": ext, "ote_top": lvl(0.79), "ote_bottom": lvl(0.62),
            "sweet_spot": lvl(0.705), "fib_50": lvl(0.5), "time": br.time}


# ---------------------------------------------------------------- sessions, kill zones, Asian range, Judas, AMD

def _us_dst(d: dt.date) -> bool:
    """US daylight saving: 2nd Sunday of March to 1st Sunday of November."""
    def nth_sunday(y, m, n):
        x = dt.date(y, m, 1)
        x += dt.timedelta(days=(6 - x.weekday()) % 7)
        return x + dt.timedelta(days=7 * (n - 1))
    return nth_sunday(d.year, 3, 2) <= d < nth_sunday(d.year, 11, 1)


def kill_zones(market: str, day: dt.date) -> list[dict]:
    """Kill-zone windows (epoch seconds) for a UTC/IST calendar day."""
    def utc(h, m=0, dd=day):
        return int(dt.datetime(dd.year, dd.month, dd.day, h, m, tzinfo=dt.timezone.utc).timestamp())
    if market in ("NSE", "FNO", "BSE"):
        ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
        t = lambda h, m: int(dt.datetime(day.year, day.month, day.day, h, m, tzinfo=ist).timestamp())
        return [{"name": "NSE opening drive", "start": t(9, 15), "end": t(10, 15)},
                {"name": "NSE closing hour", "start": t(14, 30), "end": t(15, 30)}]
    shift = -1 if _us_dst(day) else 0  # New York open is one hour earlier in UTC during US summer time
    return [{"name": "Asian range", "start": utc(0), "end": utc(6)},
            {"name": "London open", "start": utc(7 + shift), "end": utc(10 + shift)},
            {"name": "New York open", "start": utc(12 + shift), "end": utc(15 + shift)},
            {"name": "London close", "start": utc(15 + shift), "end": utc(17 + shift)}]


def session_model(bars: list[Candle], market: str) -> dict:
    """Asian range (or NSE opening range), Judas swing and AMD phase for the latest day in `bars`."""
    if not bars:
        return {}
    india = market in ("NSE", "FNO", "BSE", "MCX")
    tz = dt.timezone(dt.timedelta(hours=5, minutes=30)) if india else dt.timezone.utc
    day = dt.datetime.fromtimestamp(bars[-1].time, tz).date()
    today = [b for b in bars if dt.datetime.fromtimestamp(b.time, tz).date() == day]
    if not today:
        return {}
    zones = kill_zones(market, day)
    if india:
        acc = [b for b in today if b.time < today[0].time + 30 * 60]
        name = "opening 30 minutes"
        kz = zones[0]
    else:
        acc = [b for b in today if zones[0]["start"] <= b.time < zones[0]["end"]]
        name = "Asian range"
        kz = zones[1]
    if not acc:
        return {"day": day.isoformat(), "kill_zones": zones}
    hi, lo = max(b.high for b in acc), min(b.low for b in acc)
    after = [b for b in today if b.time > acc[-1].time]
    manip = dist = None
    for b in after:
        if manip is None:
            if b.high > hi:
                manip = ("up", b.time, b.high)
            elif b.low < lo:
                manip = ("down", b.time, b.low)
        elif dist is None:
            if manip[0] == "up" and b.close < lo:
                dist = ("down", b.time)
            elif manip[0] == "down" and b.close > hi:
                dist = ("up", b.time)
    phase = "distribution" if dist else "manipulation" if manip else "accumulation"
    judas = None
    open_px = today[0].open
    if manip and kz["start"] <= manip[1] < kz["end"]:
        for b in after:
            if b.time <= manip[1]:
                continue
            if (manip[0] == "up" and b.close < open_px) or (manip[0] == "down" and b.close > open_px):
                judas = {"fake_move": manip[0], "extreme": manip[2], "time": manip[1], "confirmed": b.time,
                         "bias": "bearish" if manip[0] == "up" else "bullish"}
                break
    return {"day": day.isoformat(), "range_name": name, "range_high": hi, "range_low": lo, "session_open": open_px,
            "amd_phase": phase, "manipulation": {"side": manip[0], "time": manip[1], "extreme": manip[2]} if manip else None,
            "distribution": {"side": dist[0], "time": dist[1]} if dist else None, "judas": judas, "kill_zones": zones,
            "in_kill_zone": next((z["name"] for z in zones if z["start"] <= bars[-1].time < z["end"]), None)}


def period_levels(daily: list[Candle], now_t: int, tz_ist: bool) -> dict:
    """Previous day / week / month high and low from daily candles (the current period excluded)."""
    tz = dt.timezone(dt.timedelta(hours=5, minutes=30)) if tz_ist else dt.timezone.utc
    if not daily:
        return {}
    today = dt.datetime.fromtimestamp(now_t, tz).date()
    d = [(dt.datetime.fromtimestamp(b.time, tz).date(), b) for b in daily]
    prev_days = [b for day, b in d if day < today]
    out = {}
    if prev_days:
        out.update(pdh=prev_days[-1].high, pdl=prev_days[-1].low, pdc=prev_days[-1].close)
    wk = today.isocalendar()[:2]
    pw = [b for day, b in d if day.isocalendar()[:2] < wk]
    if pw:
        last_wk = max(day.isocalendar()[:2] for day, b in d if day.isocalendar()[:2] < wk)
        w = [b for day, b in d if day.isocalendar()[:2] == last_wk]
        out.update(pwh=max(b.high for b in w), pwl=min(b.low for b in w))
    pm = [(day, b) for day, b in d if (day.year, day.month) < (today.year, today.month)]
    if pm:
        ym = max((day.year, day.month) for day, _ in pm)
        m = [b for day, b in pm if (day.year, day.month) == ym]
        out.update(pmh=max(b.high for b in m), pml=min(b.low for b in m))
    return out


def smt(a_bars: list[Candle], b_bars: list[Candle], a_name: str, b_name: str, span: int = 3, lookback: int = 60) -> list[dict]:
    """SMT divergence over a shared time axis."""
    common = sorted(set(x.time for x in a_bars[-lookback * 2:]) & set(x.time for x in b_bars[-lookback * 2:]))[-lookback:]
    A = {x.time: x for x in a_bars}
    B = {x.time: x for x in b_bars}
    a = [A[t] for t in common]
    b = [B[t] for t in common]
    if len(a) < 2 * span + 3:
        return []
    out = []
    from .ind import pivots
    for kind in ("high", "low"):
        pa = pivots([x.high if kind == "high" else x.low for x in a], span, kind)
        pb = pivots([x.high if kind == "high" else x.low for x in b], span, kind)
        if not pa or not pb:
            continue
        la, lb = a[pa[-1]], b[pb[-1]]
        lvl_a = la.high if kind == "high" else la.low
        lvl_b = lb.high if kind == "high" else lb.low
        start = max(pa[-1], pb[-1]) + span
        for i in range(start + 1, len(a)):
            ta = a[i].high > lvl_a if kind == "high" else a[i].low < lvl_a
            tb = b[i].high > lvl_b if kind == "high" else b[i].low < lvl_b
            if ta != tb:
                leader = a_name if ta else b_name
                lagger = b_name if ta else a_name
                out.append({"direction": "bearish" if kind == "high" else "bullish", "time": common[i],
                            "text": f"{leader} took its previous swing {kind} but {lagger} did not"})
                break
    return out


def inducement(bars: list[Candle], obs: list[OrderBlock], fvgs: list[dict]) -> dict | None:
    """Nearest unmitigated POI below (bullish) / above (bearish) price and the internal swing guarding it."""
    if len(bars) < 10:
        return None
    rep = S.analyze_smc(bars, INTERNAL_SPAN)
    price = bars[-1].close
    pois = [(o.top, o.bottom, "bullish order block") for o in obs if o.direction == "bullish" and o.state == "fresh" and o.top < price]
    pois += [(g["top"], g["bottom"], "bullish FVG") for g in fvgs if g["direction"] == "bullish" and not g["filled_time"] and g["top"] < price]
    if not pois:
        return None
    top, bottom, what = max(pois, key=lambda p: p[0])
    guard = [s for s in rep.swings if s.kind == "low" and top < s.price < price]
    if not guard:
        return {"poi": what, "poi_top": top, "poi_bottom": bottom, "inducement": None}
    g = guard[-1]
    taken = any(b.low < g.price for b in bars if b.time > g.confirmed_time)
    return {"poi": what, "poi_top": top, "poi_bottom": bottom, "inducement": g.price, "inducement_time": g.time, "taken": taken}


def analyze(bars: list[Candle], market: str, daily: list[Candle] | None = None, now_t: int | None = None) -> dict:
    """Everything above for one timeframe."""
    ext = S.analyze_smc(bars, EXTERNAL_SPAN)
    intl = S.analyze_smc(bars, INTERNAL_SPAN)
    obs = order_blocks(bars, ext)
    fv = fvg_extras(bars, ext)
    price = bars[-1].close if bars else None
    return {
        "external": S.smc_payload(ext),
        "internal": {"trend": intl.trend, "structure": [asdict(x) for x in intl.structure[-20:]]},
        "mss": mss(bars, ext)[-10:],
        "protected": protected_levels(ext),
        "order_blocks": [{**asdict(o), "state": o.state} for o in obs[-20:]],
        "fvgs": fv[-25:],
        "equal_levels": equal_levels(bars, ext),
        "dealing_range": dealing_range(ext, price) if price else {},
        "ote": ote(bars, ext),
        "session": session_model(bars, market),
        "period_levels": period_levels(daily or [], now_t or (bars[-1].time if bars else 0), market in ("NSE", "FNO", "BSE", "MCX")),
        "inducement": inducement(bars, obs, fv),
        "_reports": (ext, intl, obs),
    }
