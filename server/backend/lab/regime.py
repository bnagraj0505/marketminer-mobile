"""Market regime from objective rules (closed candles only).

Inputs at the last candle: ADX(14) and +DI/-DI, EMA20/50/200, ATR% of price and its percentile over the last 120
candles, Bollinger bandwidth percentile (from pa.volatility_state), the structure trend from smc.py, the number of
EMA20 crosses in the last 30 candles, relative volume, and the latest candle's size.

Primary regime (first rule that matches):
1. Event-driven: the last candle's range >= 3 x ATR, or it gapped >= 1.5 x ATR from the previous close.
2. Breakout: bandwidth was in its lowest 20% within the last 10 candles and the last close is beyond the prior
   20-candle high/low with ADX rising.
3. Reversal: a MSS/CHoCH in the last 10 candles against the trend of the 30 candles before it.
4. Strong bullish / bearish trend: ADX >= 25, DI in the trend's favour, close > EMA20 > EMA50 (mirror) and the
   structure trend agrees.
5. Weak bullish / bearish trend: close above (below) EMA50 and EMA20 above (below) EMA50, ADX 18-25 or structure
   disagrees.
6. Choppy: ADX < 20 and 6+ EMA20 crosses in the last 30 candles.
7. Sideways/range: ADX < 20 otherwise.
Volatility flag (separate): ATR% percentile >= 80 = high volatility, <= 20 = low volatility, else normal.

Suitable strategies per regime are listed in SUITS; strategies outside the list are scored down, not hidden.
"""
from __future__ import annotations

from .candle import Candle


SUITS = {
    "strong_bull": ["trend_pullback", "trend_mtf", "ob_bos_retest", "ote", "breakout_volume", "orb_retest", "sweep_mss_fvg"],
    "strong_bear": ["trend_pullback", "trend_mtf", "ob_bos_retest", "ote", "breakout_volume", "orb_retest", "sweep_mss_fvg"],
    "weak_bull": ["ote", "ob_bos_retest", "vwap_reclaim", "sweep_mss_fvg", "pdhl_sweep", "trend_pullback", "options_momentum"],
    "weak_bear": ["ote", "ob_bos_retest", "vwap_reclaim", "sweep_mss_fvg", "pdhl_sweep", "trend_pullback", "options_momentum"],
    "range": ["range_reversal", "pdhl_sweep", "sweep_mss_fvg", "vwap_reclaim", "divergence_mss"],
    "choppy": ["range_reversal", "pdhl_sweep"],
    "breakout": ["breakout_volume", "orb_retest", "options_momentum", "trend_mtf"],
    "reversal": ["sweep_mss_fvg", "divergence_mss", "pdhl_sweep", "ob_bos_retest"],
    "event": [],
}
LABEL = {"strong_bull": "Strong bullish trend", "strong_bear": "Strong bearish trend", "weak_bull": "Weak bullish trend",
         "weak_bear": "Weak bearish trend", "range": "Sideways / range", "choppy": "Choppy", "breakout": "Breakout",
         "reversal": "Reversal", "event": "Event-driven"}


def classify_at(F, i: int) -> dict:
    """Regime at candle i of a Features object (uses candles 0..i only)."""
    bars = F.bars
    if i < 60:
        return {"regime": "unknown", "label": "Not enough candles", "volatility": "unknown", "suits": []}
    closes = F.c
    A, P, M, at = F.adx[i] or 0, F.pdi[i] or 0, F.mdi[i] or 0, F.atr[i] or 0
    e20, e50, e200 = F.ema[20], F.ema[50], F.ema[200]
    c = closes[i]
    atrp = [F.atr[j] / closes[j] for j in range(max(0, i - 119), i + 1) if F.atr[j]]
    rank = 100 * sum(1 for x in atrp if x < atrp[-1]) / len(atrp) if atrp else 50
    vol = "high" if rank >= 80 else "low" if rank <= 20 else "normal"
    crosses = sum(1 for j in range(i - 29, i + 1) if e20[j] and e20[j - 1] and (closes[j] - e20[j]) * (closes[j - 1] - e20[j - 1]) < 0)
    structure_trend = F.trend_at(F.t[i])
    facts = {"adx": round(A, 1), "plus_di": round(P, 1), "minus_di": round(M, 1), "atr": at, "atr_pct_rank": round(rank),
             "ema20": e20[i], "ema50": e50[i], "ema200": e200[i], "ema20_crosses_30": crosses, "structure": structure_trend}
    last, prev = bars[i], bars[i - 1]
    reg = None
    if at and ((last.high - last.low) >= 3 * at or abs(last.open - prev.close) >= 1.5 * at):
        reg = "event"
    bwr = [x for x in F.bw_rank[max(0, i - 9):i + 1] if x is not None]
    if reg is None and bwr and min(bwr) <= 20:
        hi20, lo20 = max(b.high for b in bars[i - 20:i]), min(b.low for b in bars[i - 20:i])
        if (c > hi20 or c < lo20) and F.adx[i] and F.adx[i - 3] and F.adx[i] > F.adx[i - 3]:
            reg = "breakout"
    if reg is None and F.mss_between(F.t[i - 10], F.t[i]):
        prior = closes[i - 39] - closes[i - 10]
        if (prior > 0 and closes[i] < closes[i - 10]) or (prior < 0 and closes[i] > closes[i - 10]):
            reg = "reversal"
    if reg is None and e20[i] and e50[i]:
        if A >= 25 and P > M and c > e20[i] > e50[i] and structure_trend != "bearish":
            reg = "strong_bull"
        elif A >= 25 and M > P and c < e20[i] < e50[i] and structure_trend != "bullish":
            reg = "strong_bear"
        elif c > e50[i] and e20[i] > e50[i] and A >= 18:
            reg = "weak_bull"
        elif c < e50[i] and e20[i] < e50[i] and A >= 18:
            reg = "weak_bear"
    if reg is None:
        reg = "choppy" if A < 20 and crosses >= 6 else "range" if A < 20 else ("weak_bull" if c > (e50[i] or c) else "weak_bear")
    return {"regime": reg, "label": LABEL[reg], "volatility": vol, "suits": SUITS[reg], "facts": facts}
