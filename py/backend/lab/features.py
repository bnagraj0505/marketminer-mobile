"""Causal feature set for one instrument and timeframe.

`Features(bars)` computes every indicator and structure event once over the whole series. Indicators at index i
use candles 0..i only. Structure events (swings, breaks, sweeps, FVGs, order blocks) carry the time of the candle
that CONFIRMED them; the `*_at(t)` helpers only return what was known at time t. Live analysis asks for the last
candle; the backtester walks i forward. Same code both ways, so a backtested signal is exactly what the live screen
would have shown on that candle: nothing repaints.
"""
from __future__ import annotations

import bisect
from dataclasses import asdict

from .candle import Candle, TF_SECONDS, resample, closed_only
from . import ind as I
from . import smc as S
from . import ict as ICT


class Features:
    def __init__(self, bars: list[Candle], tf: str, session_of=None, anchor_min: int = 0):
        self.bars, self.tf, self.step = bars, tf, TF_SECONDS[tf]
        self.t = [b.time for b in bars]
        c = self.c = [b.close for b in bars]
        self.atr = I.atr_list(bars)
        self.ema = {n: I.ema(c, n) for n in (9, 20, 50, 100, 200)}
        self.sma = {n: I.sma(c, n) for n in (20, 50, 200)}
        self.rsi = I.rsi(c)
        self.srsi_k, self.srsi_d = I.stoch_rsi(c)
        self.macd, self.macd_sig, self.macd_hist = I.macd(c)
        self.adx, self.pdi, self.mdi = I.adx_dmi(bars)
        self.bb_mid, self.bb_up, self.bb_lo, self.bb_bw = I.bollinger(c)
        self.kc_mid, self.kc_up, self.kc_lo = I.keltner(bars)
        self.st_line, self.st_dir = I.supertrend(bars)
        self.mfi = I.mfi(bars)
        self.obv = I.obv(bars)
        self.vwap = I.vwap(bars, session_of or (lambda t: t // 86400))
        self.rvol = I.rvol(bars)
        self.roc = I.roc(c)
        self.vol_avg = I.sma([b.volume for b in bars], 20)
        self.has_volume = sum(1 for b in bars[-50:] if b.volume > 0) > 25
        self.disp = ICT.displacement(bars)
        self.ext = S.analyze_smc(bars, ICT.EXTERNAL_SPAN)
        self.intl = S.analyze_smc(bars, ICT.INTERNAL_SPAN)
        self.obs = ICT.order_blocks(bars, self.ext)
        self.fvgs = ICT.fvg_extras(bars, self.ext)
        self.mss = ICT.mss(bars, self.ext)
        self.anchor_min = anchor_min
        # bandwidth percentile series (for squeeze / breakout regime)
        self.bw_rank: list[float | None] = [None] * len(bars)
        for i in range(len(bars)):
            w = [x for x in self.bb_bw[max(0, i - 119):i + 1] if x is not None]
            if len(w) >= 30:
                self.bw_rank[i] = 100 * sum(1 for x in w if x < w[-1]) / len(w)
        self._brk_t = [s.time for s in self.ext.structure]

    # ---- known-at-time helpers
    def idx(self, t: int) -> int:
        return bisect.bisect_right(self.t, t) - 1

    def trend_at(self, t: int, internal: bool = False) -> str:
        rep = self.intl if internal else self.ext
        cur = "neutral"
        for s in rep.structure:
            if s.time > t:
                break
            cur = s.direction
        return cur

    def breaks_between(self, t0: int, t1: int, internal: bool = False):
        rep = self.intl if internal else self.ext
        return [s for s in rep.structure if t0 < s.time <= t1]

    def sweeps_between(self, t0: int, t1: int):
        return [s for s in self.ext.sweeps if t0 < s.time <= t1] + [s for s in self.intl.sweeps if t0 < s.time <= t1]

    def mss_between(self, t0: int, t1: int):
        return [m for m in self.mss if t0 < m["time"] <= t1]

    def swings_at(self, t: int, kind: str, internal: bool = False):
        rep = self.intl if internal else self.ext
        return [s for s in rep.swings if s.kind == kind and s.confirmed_time <= t]

    def open_fvgs_at(self, t: int, direction: str | None = None):
        return [g for g in self.fvgs if g["created_time"] <= t and (g["filled_time"] is None or g["filled_time"] > t)
                and (g["inverse_time"] is None or g["inverse_time"] > t) and (direction is None or g["direction"] == direction)]

    def obs_at(self, t: int, direction: str | None = None, fresh_only: bool = False):
        out = []
        for o in self.obs:
            if o.break_time > t or (direction and o.direction != direction):
                continue
            if o.breaker_time and o.breaker_time <= t:
                continue
            mit = o.mitigated_time is not None and o.mitigated_time <= t
            if fresh_only and mit and o.mitigated_time < t:
                continue
            out.append(o)
        return out

    def liquidity_at(self, t: int, price: float):
        """Untouched swing highs above / lows below `price`, as known at t (nearest first)."""
        hs = self.swings_at(t, "high")
        ls = self.swings_at(t, "low")
        k = self.idx(t)
        above, below = [], []
        for s in hs:
            if s.price > price and not any(b.high > s.price for b in self.bars[self.idx(s.confirmed_time) + 1:k + 1]):
                above.append(s.price)
        for s in ls:
            if s.price < price and not any(b.low < s.price for b in self.bars[self.idx(s.confirmed_time) + 1:k + 1]):
                below.append(s.price)
        return sorted(above), sorted(below, reverse=True)

    def bias_at(self, i: int) -> int:
        """Directional bias of this timeframe at candle i: structure trend, EMA20 vs EMA50, close vs EMA50 and the
        Supertrend direction each vote +1/-1; >= +2 bullish (+1), <= -2 bearish (-1), else 0."""
        if i < 0:
            return 0
        tr = self.trend_at(self.t[i])
        v = (1 if tr == "bullish" else -1 if tr == "bearish" else 0)
        e20, e50 = self.ema[20][i], self.ema[50][i]
        if e20 is not None and e50 is not None:
            v += 1 if e20 > e50 else -1
            v += 1 if self.c[i] > e50 else -1
        if self.st_dir[i] is not None:
            v += self.st_dir[i]
        return 1 if v >= 2 else -1 if v <= -2 else 0


def htf_series(base: list[Candle], base_tf: str, htf: str, session_of=None, anchor_min: int = 0) -> tuple[list[int], list[int], Features | None]:
    """Bias of a higher timeframe built from the base candles, indexed by the HTF candle CLOSE time, so that at a
    base candle closing at t only HTF candles that had closed by t are used."""
    if TF_SECONDS[htf] <= TF_SECONDS[base_tf]:
        return [], [], None
    h = resample(base, htf, anchor_min)
    if not h:
        return [], [], None
    f = Features(h, htf, session_of, anchor_min)
    closes = [b.time + TF_SECONDS[htf] for b in h]
    return closes, [f.bias_at(i) for i in range(len(h))], f


def htf_from_candles(hbars: list[Candle], htf: str, session_of=None, anchor_min: int = 0) -> tuple[list[int], list[int], Features | None]:
    """Same as htf_series but from candles fetched on the higher timeframe itself (longer history)."""
    if not hbars:
        return [], [], None
    f = Features(hbars, htf, session_of, anchor_min)
    return [b.time + TF_SECONDS[htf] for b in hbars], [f.bias_at(i) for i in range(len(hbars))], f


def bias_lookup(closes: list[int], biases: list[int], t_close: int) -> int:
    k = bisect.bisect_right(closes, t_close) - 1
    return biases[k] if k >= 0 else 0
