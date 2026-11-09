"""Machine-learning features and labels for Signal Lab setups (pure functions, no I/O).

The SAME function computes the features in the backtest data set and on the live closed candle (Engine.signals_at
attaches them to every setup as setup["ml_features"]), so training and trading see identical inputs.

Features (all known at the close of the signal candle i; "dir" = signed in the trade's direction, so + means "on the
side of the trade"):
  score, rr, rr_t1, stop_atr, atr_pct              the setup itself
  rsi_dir, macd_dir, adx, bb_bw, rvol              momentum / volatility (rvol is missing for instruments without volume)
  ema20_dir, ema50_dir, ema200_dir, ema_stack      trend location in ATRs; stack -2..+2
  vwap_dir, ret12_dir                              distance from session VWAP and the last 12 candles' move, in ATRs
  trend_dir, internal_dir, htf_dir, itf_dir        structure trend on this / internal / higher / intermediate timeframe
  dscore_dir                                       Signal Lab directional score (bull - bear) in the trade's direction
  day_pos_dir, prev_day_pos_dir                    position in today's / yesterday's range (1 = at the trade-side extreme)
  mins_open, weekday                               minutes since the session opened (IST for India, UTC for crypto)
  strategy, regime, volatility, market, side       categories
Label: does the price reach 2R (lot 1's 1:2 target, after slippage) before the stop? Entry = the NEXT candle's open,
the stop is assumed first when both are inside one candle, Indian trades end at the session's last candle, others after
3 x the expected holding period (10 - 96 candles). Fees are not in the label (the Risk Guardian adds them).
"""
from __future__ import annotations

import datetime as dt
import math

from . import costs as C

NUMERIC = ["score", "rr", "rr_t1", "stop_atr", "atr_pct", "rsi_dir", "macd_dir", "adx", "bb_bw", "rvol", "ema20_dir", "ema50_dir",
           "ema200_dir", "ema_stack", "vwap_dir", "ret12_dir", "trend_dir", "internal_dir", "htf_dir", "itf_dir", "dscore_dir",
           "day_pos_dir", "prev_day_pos_dir", "mins_open", "weekday"]
CATEGORICAL = ["strategy", "regime", "volatility", "market", "side"]
FIELDS = NUMERIC + CATEGORICAL
DESCRIBE = {
    "score": "Signal Lab setup score (0-100)", "rr": "reward:risk to T2", "rr_t1": "reward:risk to T1", "stop_atr": "stop distance in ATRs",
    "atr_pct": "ATR as % of price", "rsi_dir": "RSI - 50, in the trade's direction", "macd_dir": "MACD histogram / ATR, in the trade's direction",
    "adx": "ADX (trend strength)", "bb_bw": "Bollinger bandwidth", "rvol": "relative volume (missing without volume)",
    "ema20_dir": "close vs EMA20 in ATRs, trade side +", "ema50_dir": "close vs EMA50 in ATRs, trade side +", "ema200_dir": "close vs EMA200 in ATRs, trade side +",
    "ema_stack": "EMA20/50/200 stacked with the trade (+2) or against it (-2)", "vwap_dir": "close vs session VWAP in ATRs, trade side +",
    "ret12_dir": "move over the last 12 candles in ATRs, trade side +", "trend_dir": "structure trend with (+1) / against (-1) the trade",
    "internal_dir": "internal (minor) structure with / against", "htf_dir": "higher-timeframe bias with / against", "itf_dir": "intermediate-timeframe bias with / against",
    "dscore_dir": "directional score bull - bear, trade side +", "day_pos_dir": "position in today's range (1 = at the trade-side extreme)",
    "prev_day_pos_dir": "position in yesterday's range (1 = at the trade-side extreme)", "mins_open": "minutes since the session opened",
    "weekday": "day of week (0 = Monday)", "strategy": "strategy", "regime": "market regime", "volatility": "volatility state", "market": "India or crypto", "side": "LONG or SHORT",
}
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
INDIA = ("NSE", "FNO", "MCX")


def _f(x):
    try:
        x = float(x)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _sgn(s: str) -> int:
    return 1 if s == "bullish" else -1 if s == "bearish" else 0


def features(F, i: int, ctx: dict, dscore: dict, s: dict) -> dict:
    """Feature dict for setup s formed on closed candle i (None = not available, never guessed)."""
    d = 1 if s["direction"] == "LONG" else -1
    c = F.c[i]
    at = F.atr[i] or None
    t = F.t[i]
    market = ctx.get("market") or ""
    rel = lambda x: None if (x is None or not at) else d * (c - x) / at
    e20, e50, e200 = F.ema[20][i], F.ema[50][i], F.ema[200][i]
    stack = None
    if None not in (e20, e50, e200):
        stack = d * ((1 if e20 > e50 else -1) + (1 if e50 > e200 else -1))
    vw = F.vwap[i] if F.step < 86400 else None
    ret12 = d * (c - F.c[i - 12]) / at if at and i >= 12 else None
    mh = F.macd_hist[i]
    rv = F.rvol[i] if F.has_volume else None
    lo = hi = None                                       # today's range up to this candle
    k = i
    if F.step < 86400:
        tz = IST if market in INDIA else dt.timezone.utc
        day = dt.datetime.fromtimestamp(t, tz).date()
        while k >= 0 and dt.datetime.fromtimestamp(F.t[k], tz).date() == day:
            b = F.bars[k]
            lo = b.low if lo is None else min(lo, b.low)
            hi = b.high if hi is None else max(hi, b.high)
            k -= 1
        start = F.t[k + 1]
        mins = (t - start) // 60 + (F.step // 60)
    else:
        mins = None
    pos = None
    if lo is not None and hi is not None and hi > lo:
        pos = (c - lo) / (hi - lo)
        pos = pos if d > 0 else 1 - pos
    pdp = None
    pd_ = ctx.get("prev_day")
    if pd_ and pd_[1] > pd_[0]:
        pdp = (c - pd_[0]) / (pd_[1] - pd_[0])
        pdp = pdp if d > 0 else 1 - pdp
    tz = IST if market in INDIA else dt.timezone.utc
    out = {
        "score": _f(s.get("score")), "rr": _f(s.get("rr")), "rr_t1": _f(s.get("rr_t1")), "stop_atr": _f(s.get("stop_atr")),
        "atr_pct": _f(at / c * 100) if at and c else None,
        "rsi_dir": _f(d * (F.rsi[i] - 50)) if F.rsi[i] is not None else None,
        "macd_dir": _f(d * mh / at) if mh is not None and at else None,
        "adx": _f(F.adx[i]), "bb_bw": _f(F.bb_bw[i]), "rvol": _f(rv),
        "ema20_dir": _f(rel(e20)), "ema50_dir": _f(rel(e50)), "ema200_dir": _f(rel(e200)), "ema_stack": _f(stack),
        "vwap_dir": _f(rel(vw)), "ret12_dir": _f(ret12),
        "trend_dir": float(d * _sgn(F.trend_at(t))), "internal_dir": float(d * _sgn(F.trend_at(t, internal=True))),
        "htf_dir": float(d * (ctx.get("htf_bias") or 0)) if ctx.get("htf_ok") else None,
        "itf_dir": float(d * (ctx.get("itf_bias") or 0)) if ctx.get("itf_ok") else None,
        "dscore_dir": _f(d * ((dscore or {}).get("bull", 0) - (dscore or {}).get("bear", 0))) if dscore else None,
        "day_pos_dir": _f(pos), "prev_day_pos_dir": _f(pdp), "mins_open": _f(mins),
        "weekday": float(dt.datetime.fromtimestamp(t, tz).weekday()),
        "strategy": s.get("strategy"), "regime": ctx.get("regime"), "volatility": ctx.get("volatility"),
        "market": "india" if market in INDIA else "crypto" if market in ("SHARK", "DELTAF", "DELTA", "BINANCE") else "other",
        "side": s["direction"],
    }
    return out


def _session_last(E, j: int) -> bool:
    F = E.F
    if E.market not in INDIA or F.step >= 86400 or j + 1 >= len(F.bars):
        return False
    return E.session_of(F.t[j + 1]) != E.session_of(F.t[j])


def label(E, i: int, s: dict, cost_cfg: dict | None = None, k_r: float = 2.0) -> dict | None:
    """{y: 1 if k_r R came before the stop else 0, r: lot-1 R outcome, bars, exit} or None when not simulable."""
    F = E.F
    if i + 1 >= len(F.bars):
        return None
    d = 1 if s["direction"] == "LONG" else -1
    cfg = cost_cfg or C.DEFAULTS
    entry = C.slip_price(F.bars[i + 1].open, d, E.market, cfg)
    stop = s["stop"]
    if (entry - stop) * d <= 0:
        return None                                       # opened through the stop: not a tradable setup
    slip_out = abs(C.slip_price(stop, -d, E.market, cfg) - stop)
    risk = (entry - stop) * d + slip_out
    tgt = entry + d * (k_r * risk + slip_out)
    exp_hold = max(1, round(abs((s.get("targets") or [entry, entry])[1] - s["entry"]) / (s.get("atr") or 1) * 1.5)) if len(s.get("targets") or []) > 1 else 10
    hold = min(96, max(10, 3 * exp_hold))
    for j in range(i + 1, len(F.bars)):
        b = F.bars[j]
        if (b.low <= stop) if d > 0 else (b.high >= stop):
            return {"y": 0, "r": -1.0, "bars": j - i, "exit": "stop"}
        if (b.high >= tgt) if d > 0 else (b.low <= tgt):
            return {"y": 1, "r": k_r, "bars": j - i, "exit": "target"}
        if j - i >= hold or _session_last(E, j):
            r = (C.slip_price(b.close, -d, E.market, cfg) - entry) * d / risk
            return {"y": 0, "r": max(-1.0, min(k_r, r)), "bars": j - i, "exit": "session end" if _session_last(E, j) else "time"}
    return None                                           # not resolved yet (end of data): left out, not guessed
