"""Quantitative indicator engine. Every function takes closed candles (oldest first) and returns a list aligned with
them: value i uses candles 0..i only, so an indicator never changes once its candle has closed. None = not enough
data yet. Formulas are the standard textbook ones; the tests in tests/test_lab_indicators.py check them against
hand-computed values.
"""
from __future__ import annotations

import math
from typing import Callable

from .candle import Candle

Num = float | None


def sma(xs: list[float], n: int) -> list[Num]:
    out: list[Num] = [None] * len(xs)
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(xs: list[float], n: int) -> list[Num]:
    """Exponential moving average, seeded with the SMA of the first n values (k = 2 / (n + 1))."""
    out: list[Num] = [None] * len(xs)
    if len(xs) < n:
        return out
    k = 2 / (n + 1)
    v = sum(xs[:n]) / n
    out[n - 1] = v
    for i in range(n, len(xs)):
        v = xs[i] * k + v * (1 - k)
        out[i] = v
    return out


def wilder(xs: list[float], n: int, start: int = 0) -> list[Num]:
    """Wilder smoothing (RMA): first value = mean of xs[start:start+n], then v = (v*(n-1) + x) / n."""
    out: list[Num] = [None] * len(xs)
    if len(xs) < start + n:
        return out
    v = sum(xs[start:start + n]) / n
    out[start + n - 1] = v
    for i in range(start + n, len(xs)):
        v = (v * (n - 1) + xs[i]) / n
        out[i] = v
    return out


def true_range(bars: list[Candle]) -> list[float]:
    tr = []
    for i, b in enumerate(bars):
        if i == 0:
            tr.append(b.high - b.low)
        else:
            pc = bars[i - 1].close
            tr.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    return tr


def atr_list(bars: list[Candle], period: int = 14) -> list[Num]:
    """Wilder's ATR. Same convention as SmartEntry: first value at index `period` from TR[1..period]."""
    out: list[Num] = [None] * len(bars)
    if len(bars) <= period:
        return out
    trs = true_range(bars)
    v = sum(trs[1:period + 1]) / period
    out[period] = v
    for i in range(period + 1, len(bars)):
        v = (v * (period - 1) + trs[i]) / period
        out[i] = v
    return out


def rsi(closes: list[float], n: int = 14) -> list[Num]:
    """Wilder's RSI."""
    out: list[Num] = [None] * len(closes)
    if len(closes) <= n:
        return out
    g = [0.0] + [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    l = [0.0] + [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[1:n + 1]) / n, sum(l[1:n + 1]) / n
    out[n] = 100 - 100 / (1 + ag / al) if al else 100.0
    for i in range(n + 1, len(closes)):
        ag, al = (ag * (n - 1) + g[i]) / n, (al * (n - 1) + l[i]) / n
        out[i] = 100 - 100 / (1 + ag / al) if al else 100.0
    return out


def stoch_rsi(closes: list[float], n: int = 14, k: int = 3, d: int = 3) -> tuple[list[Num], list[Num]]:
    r = rsi(closes, n)
    raw: list[Num] = [None] * len(closes)
    for i in range(len(r)):
        w = [x for x in r[max(0, i - n + 1):i + 1] if x is not None]
        if r[i] is None or len(w) < n:
            continue
        lo, hi = min(w), max(w)
        raw[i] = 100 * (r[i] - lo) / (hi - lo) if hi > lo else 50.0
    kk = _sma_opt(raw, k)
    return kk, _sma_opt(kk, d)


def _sma_opt(xs: list[Num], n: int) -> list[Num]:
    out: list[Num] = [None] * len(xs)
    for i in range(len(xs)):
        w = xs[max(0, i - n + 1):i + 1]
        if len(w) == n and all(v is not None for v in w):
            out[i] = sum(w) / n
    return out


def _ema_opt(xs: list[Num], n: int) -> list[Num]:
    out: list[Num] = [None] * len(xs)
    first = next((i for i, v in enumerate(xs) if v is not None), None)
    if first is None:
        return out
    vals = [v for v in xs[first:]]
    if any(v is None for v in vals):
        return out
    e = ema(vals, n)  # type: ignore[arg-type]
    out[first:] = e
    return out


def macd(closes: list[float], fast: int = 12, slow: int = 26, sig: int = 9) -> tuple[list[Num], list[Num], list[Num]]:
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [a - b if a is not None and b is not None else None for a, b in zip(ef, es)]
    signal = _ema_opt(line, sig)
    hist = [a - b if a is not None and b is not None else None for a, b in zip(line, signal)]
    return line, signal, hist


def adx_dmi(bars: list[Candle], n: int = 14) -> tuple[list[Num], list[Num], list[Num]]:
    """Wilder's ADX with +DI / -DI."""
    L = len(bars)
    pdm, mdm = [0.0] * L, [0.0] * L
    for i in range(1, L):
        up, dn = bars[i].high - bars[i - 1].high, bars[i - 1].low - bars[i].low
        pdm[i] = up if up > dn and up > 0 else 0.0
        mdm[i] = dn if dn > up and dn > 0 else 0.0
    tr = true_range(bars)
    str_, sp, sm = wilder(tr, n, 1), wilder(pdm, n, 1), wilder(mdm, n, 1)
    pdi: list[Num] = [None] * L
    mdi: list[Num] = [None] * L
    dx: list[Num] = [None] * L
    for i in range(L):
        if str_[i]:
            pdi[i] = 100 * sp[i] / str_[i]  # type: ignore[operator]
            mdi[i] = 100 * sm[i] / str_[i]  # type: ignore[operator]
            s = pdi[i] + mdi[i]  # type: ignore[operator]
            dx[i] = 100 * abs(pdi[i] - mdi[i]) / s if s else 0.0  # type: ignore[operator]
    first = next((i for i, v in enumerate(dx) if v is not None), None)
    adx: list[Num] = [None] * L
    if first is not None and L >= first + n:
        v = sum(dx[first:first + n]) / n  # type: ignore[arg-type]
        adx[first + n - 1] = v
        for i in range(first + n, L):
            v = (v * (n - 1) + dx[i]) / n  # type: ignore[operator]
            adx[i] = v
    return adx, pdi, mdi


def bollinger(closes: list[float], n: int = 20, k: float = 2.0) -> tuple[list[Num], list[Num], list[Num], list[Num]]:
    """Middle, upper, lower (population standard deviation, as in Bollinger's definition) and bandwidth %."""
    mid = sma(closes, n)
    up: list[Num] = [None] * len(closes)
    lo: list[Num] = [None] * len(closes)
    bw: list[Num] = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        w = closes[i - n + 1:i + 1]
        m = mid[i]
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)  # type: ignore[operator]
        up[i], lo[i] = m + k * sd, m - k * sd  # type: ignore[operator]
        bw[i] = 100 * (up[i] - lo[i]) / m if m else None  # type: ignore[operator]
    return mid, up, lo, bw


def keltner(bars: list[Candle], n: int = 20, atr_n: int = 10, k: float = 2.0) -> tuple[list[Num], list[Num], list[Num]]:
    mid = ema([b.close for b in bars], n)
    a = atr_list(bars, atr_n)
    up = [m + k * x if m is not None and x is not None else None for m, x in zip(mid, a)]
    lo = [m - k * x if m is not None and x is not None else None for m, x in zip(mid, a)]
    return mid, up, lo


def supertrend(bars: list[Candle], n: int = 10, mult: float = 3.0) -> tuple[list[Num], list[int | None]]:
    """Supertrend line and direction (+1 up, -1 down), standard final-band rules."""
    a = atr_list(bars, n)
    L = len(bars)
    line: list[Num] = [None] * L
    dirn: list[int | None] = [None] * L
    fu = fl = None
    d = 1
    for i in range(L):
        if a[i] is None:
            continue
        hl2 = (bars[i].high + bars[i].low) / 2
        bu, bl = hl2 + mult * a[i], hl2 - mult * a[i]  # type: ignore[operator]
        pc = bars[i - 1].close if i else bars[i].close
        fu = bu if fu is None or bu < fu or pc > fu else fu
        fl = bl if fl is None or bl > fl or pc < fl else fl
        if i == 0 or line[i - 1] is None:
            d = 1 if bars[i].close >= hl2 else -1
        elif d == 1 and bars[i].close < fl:
            d = -1
        elif d == -1 and bars[i].close > fu:
            d = 1
        line[i] = fl if d == 1 else fu
        dirn[i] = d
    return line, dirn


def mfi(bars: list[Candle], n: int = 14) -> list[Num]:
    tp = [(b.high + b.low + b.close) / 3 for b in bars]
    out: list[Num] = [None] * len(bars)
    for i in range(n, len(bars)):
        pos = neg = 0.0
        for j in range(i - n + 1, i + 1):
            f = tp[j] * bars[j].volume
            if tp[j] > tp[j - 1]:
                pos += f
            elif tp[j] < tp[j - 1]:
                neg += f
        out[i] = 100.0 if neg == 0 else 100 - 100 / (1 + pos / neg)
    return out


def obv(bars: list[Candle]) -> list[float]:
    out, v = [], 0.0
    for i, b in enumerate(bars):
        if i:
            pc = bars[i - 1].close
            v += b.volume if b.close > pc else -b.volume if b.close < pc else 0.0
        out.append(v)
    return out


def vwap(bars: list[Candle], session_of: Callable[[int], object]) -> list[Num]:
    """Session VWAP of typical price, reset whenever `session_of(time)` changes. Candles without volume are weighted
    equally (indices have no traded volume), which makes it a time-weighted average for them."""
    out: list[Num] = []
    pv = vol = 0.0
    cur = object()
    for b in bars:
        s = session_of(b.time)
        if s != cur:
            pv = vol = 0.0
            cur = s
        w = b.volume if b.volume > 0 else 1.0
        pv += (b.high + b.low + b.close) / 3 * w
        vol += w
        out.append(pv / vol)
    return out


def anchored_vwap(bars: list[Candle], start: int) -> list[Num]:
    out: list[Num] = [None] * len(bars)
    pv = vol = 0.0
    for i in range(start, len(bars)):
        b = bars[i]
        w = b.volume if b.volume > 0 else 1.0
        pv += (b.high + b.low + b.close) / 3 * w
        vol += w
        out[i] = pv / vol
    return out


def volume_profile(bars: list[Candle], bins: int = 40, value_area: float = 0.70) -> dict | None:
    """Volume at price over `bars`: each candle's volume is spread evenly over the bins its range covers. POC = the
    fullest bin; the value area grows from the POC towards the larger neighbour until it holds `value_area` of the
    volume (the standard CBOT method)."""
    if not bars:
        return None
    lo, hi = min(b.low for b in bars), max(b.high for b in bars)
    if hi <= lo:
        return None
    w = (hi - lo) / bins
    vol = [0.0] * bins
    for b in bars:
        v = b.volume if b.volume > 0 else 1.0
        i0, i1 = min(bins - 1, int((b.low - lo) / w)), min(bins - 1, int((b.high - lo) / w))
        share = v / (i1 - i0 + 1)
        for i in range(i0, i1 + 1):
            vol[i] += share
    poc = max(range(bins), key=lambda i: vol[i])
    total, got, a, z = sum(vol), vol[poc], poc, poc
    while got < value_area * total and (a > 0 or z < bins - 1):
        down = vol[a - 1] if a > 0 else -1
        up = vol[z + 1] if z < bins - 1 else -1
        if up >= down:
            z += 1
            got += vol[z]
        else:
            a -= 1
            got += vol[a]
    mid = lambda i: lo + (i + 0.5) * w
    return {"poc": mid(poc), "vah": lo + (z + 1) * w, "val": lo + a * w, "low": lo, "high": hi, "bin": w,
            "bins": [round(v, 4) for v in vol]}


def fib_levels(lo: float, hi: float, up: bool = True) -> dict:
    """Retracements (0.236 .. 0.786, plus OTE 0.62/0.705/0.79) and extensions (1.272, 1.618) of the leg lo->hi
    (up=True: retracing down from hi) or hi->lo."""
    rng = hi - lo
    ret = {f"{r:g}": (hi - r * rng if up else lo + r * rng) for r in (0.236, 0.382, 0.5, 0.618, 0.62, 0.705, 0.786, 0.79)}
    ext = {f"{r:g}": (lo + r * rng if up else hi - r * rng) for r in (1.272, 1.618)}
    return {"retracement": ret, "extension": ext}


def hist_vol(closes: list[float], n: int = 20, periods_per_year: float = 252) -> list[Num]:
    """Annualised close-to-close volatility (%, sample standard deviation of log returns)."""
    out: list[Num] = [None] * len(closes)
    r = [0.0] + [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    for i in range(n, len(closes)):
        w = r[i - n + 1:i + 1]
        m = sum(w) / n
        out[i] = 100 * math.sqrt(sum((x - m) ** 2 for x in w) / (n - 1)) * math.sqrt(periods_per_year)
    return out


def parkinson_vol(bars: list[Candle], n: int = 20, periods_per_year: float = 252) -> list[Num]:
    """Realised (range-based, Parkinson) volatility, annualised %."""
    out: list[Num] = [None] * len(bars)
    k = 1 / (4 * math.log(2))
    lr = [math.log(b.high / b.low) ** 2 for b in bars]
    for i in range(n - 1, len(bars)):
        out[i] = 100 * math.sqrt(k * sum(lr[i - n + 1:i + 1]) / n) * math.sqrt(periods_per_year)
    return out


def rvol(bars: list[Candle], n: int = 20) -> list[Num]:
    """Relative volume: this candle's volume / the average of the previous n candles."""
    out: list[Num] = [None] * len(bars)
    for i in range(n, len(bars)):
        avg = sum(b.volume for b in bars[i - n:i]) / n
        out[i] = bars[i].volume / avg if avg > 0 else None
    return out


def roc(closes: list[float], n: int = 10) -> list[Num]:
    return [None if i < n or not closes[i - n] else 100 * (closes[i] / closes[i - n] - 1) for i in range(len(closes))]


def pivots(values: list[float], span: int, kind: str) -> list[int]:
    """Indexes of confirmed pivot highs/lows (strictly beyond the `span` values on each side)."""
    out = []
    for j in range(span, len(values) - span):
        w = values[j - span:j + span + 1]
        if kind == "high" and values[j] == max(w) and w.count(values[j]) == 1:
            out.append(j)
        if kind == "low" and values[j] == min(w) and w.count(values[j]) == 1:
            out.append(j)
    return out


def divergences(bars: list[Candle], osc: list[Num], name: str, span: int = 3, lookback: int = 60) -> list[dict]:
    """Regular and hidden divergences between price and an oscillator, from the last two confirmed price pivots
    (each confirmed `span` candles after it). Regular bearish: price higher high, oscillator lower high. Regular
    bullish: price lower low, oscillator higher low. Hidden bullish: price higher low, oscillator lower low. Hidden
    bearish: price lower high, oscillator higher high."""
    out = []
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    start = max(0, len(bars) - lookback)
    for kind, series in (("high", highs), ("low", lows)):
        piv = [j for j in pivots(series, span, kind) if j >= start and osc[j] is not None]
        if len(piv) < 2:
            continue
        a, b = piv[-2], piv[-1]
        pa, pb, oa, ob = series[a], series[b], osc[a], osc[b]
        t = None
        if kind == "high" and pb > pa and ob < oa:
            t = ("regular", "bearish")
        elif kind == "low" and pb < pa and ob > oa:
            t = ("regular", "bullish")
        elif kind == "low" and pb > pa and ob < oa:
            t = ("hidden", "bullish")
        elif kind == "high" and pb < pa and ob > oa:
            t = ("hidden", "bearish")
        if t:
            out.append({"indicator": name, "type": t[0], "direction": t[1], "from": bars[a].time, "to": bars[b].time,
                        "confirmed": bars[min(len(bars) - 1, b + span)].time, "price": [pa, pb], "osc": [round(oa, 2), round(ob, 2)]})
    return out


def last(xs: list) -> object:
    return next((x for x in reversed(xs) if x is not None), None)
