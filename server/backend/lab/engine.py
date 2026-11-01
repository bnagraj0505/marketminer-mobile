"""Shared signal engine used by the live screen and the backtester (same code path, so no look-ahead difference).

Context per candle i (all known at the candle's close):
  htf_bias / itf_bias   bias of the higher and intermediate timeframe candles that had CLOSED by this candle's close
  regime                regime.classify_at(F, i)
  prev_day              previous session's (low, high)
  kill_zone             name of the kill zone the candle closed in (intraday only)
  options               live chain analysis (last candle only)
"""
from __future__ import annotations

import datetime as dt

from .candle import Candle, TF_SECONDS
from .features import Features, htf_from_candles, htf_series, bias_lookup
from . import regime as R
from . import strategies as ST
from . import scoring as SC
from . import ict as ICT

MTF = {"1m": ("15m", "1h"), "3m": ("15m", "1h"), "5m": ("15m", "1h"), "15m": ("1h", "4h"), "30m": ("1h", "1d"),
       "1h": ("4h", "1d"), "4h": ("1d", "1w"), "1d": ("1w", "1w"), "1w": (None, None)}
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


class Engine:
    def __init__(self, bars: list[Candle], tf: str, market: str, session_of, anchor_min: int = 0,
                 itf_bars: list[Candle] | None = None, htf_bars: list[Candle] | None = None):
        self.F = Features(bars, tf, session_of, anchor_min)
        self.tf, self.market, self.session_of = tf, market, session_of
        itf, htf = MTF[tf]
        self.itf_name, self.htf_name = itf, htf
        self.itf = self._bias(bars, tf, itf, itf_bars, session_of, anchor_min)
        self.htf = self._bias(bars, tf, htf, htf_bars, session_of, anchor_min)
        # previous session high/low per candle
        self.prev_day: list[tuple[float, float] | None] = []
        if TF_SECONDS[tf] >= 86400:
            self.prev_day = [None] + [(b.low, b.high) for b in bars[:-1]]
        else:
            cur_k, cur, prev = None, None, None
            for b in bars:
                k = session_of(b.time)
                if k != cur_k:
                    prev, cur_k, cur = cur, k, [b.low, b.high]
                else:
                    cur[0], cur[1] = min(cur[0], b.low), max(cur[1], b.high)
                self.prev_day.append(tuple(prev) if prev else None)

    @staticmethod
    def _bias(bars, tf, name, fetched, session_of, anchor_min):
        if not name:
            return None
        if fetched:
            c, b, f = htf_from_candles(fetched, name, session_of, anchor_min)
        else:
            c, b, f = htf_series(bars, tf, name, session_of, anchor_min)
        return (c, b, f) if c else None

    def ctx(self, i: int, options: dict | None = None) -> dict:
        F = self.F
        t_close = F.t[i] + F.step
        reg = R.classify_at(F, i)
        c = {"market": self.market, "regime": reg["regime"], "regime_label": reg["label"], "suits": reg["suits"],
             "volatility": reg["volatility"], "prev_day": self.prev_day[i] if i < len(self.prev_day) else None,
             "options": options, "htf_ok": False, "itf_ok": False, "htf_bias": 0, "itf_bias": 0}
        for key, src in (("htf", self.htf), ("itf", self.itf)):
            if src:
                closes, biases, _ = src
                k = __import__("bisect").bisect_right(closes, t_close) - 1
                if k >= 50:                       # need enough HTF candles for EMA50 / structure
                    c[f"{key}_ok"] = True
                    c[f"{key}_bias"] = biases[k]
        if F.step < 86400:
            tz = IST if self.market in ("NSE", "FNO", "MCX") else dt.timezone.utc
            day = dt.datetime.fromtimestamp(F.t[i], tz).date()
            c["kill_zone"] = next((z["name"] for z in ICT.kill_zones(self.market, day) if z["start"] <= F.t[i] < z["end"] and z["name"] != "Asian range"), None)
        c["regime_full"] = reg
        return c

    def signals_at(self, i: int, options: dict | None = None, settings: dict | None = None, quality: dict | None = None) -> dict:
        ctx = self.ctx(i, options)
        dscore = SC.directional(self.F, i, ctx)
        out, rejected = [], []
        for s in ST.evaluate(self.F, i, ctx, settings):
            if s.get("rejected"):
                rejected.append(s)
                continue
            s.update(SC.setup_score(s, dscore, ctx, quality))
            s["regime"] = ctx["regime"]
            s["htf_bias"], s["itf_bias"] = ctx["htf_bias"], ctx["itf_bias"]
            out.append(s)
        out.sort(key=lambda s: -s["score"])
        return {"ctx": ctx, "score": dscore, "setups": out, "rejected": rejected}
