"""0-100 scoring.

Directional score (bull / bear / neutral, summing to 100) at candle i from weighted factor votes. Each factor votes
+1 (bullish), -1 (bearish) or 0 and has a weight; factors whose data is missing are left out and the weights
re-normalised (they are listed as "unavailable" so nothing is silently assumed).

  factor                    weight  bullish when
  higher-timeframe bias       20    HTF bias_at = +1 (structure, EMA20/50, close vs EMA50, Supertrend)
  intermediate-TF bias        15    ITF bias_at = +1
  execution structure         10    external structure trend bullish
  EMA stack                   10    close > EMA20 > EMA50
  momentum                     8    RSI > 55 and MACD histogram > 0  (bearish: RSI < 45 and histogram < 0)
  VWAP                         7    close above session VWAP (intraday only)
  volume / OBV                 5    OBV above its 20-candle average (instruments with volume only)
  liquidity event             10    sell-side sweep or bullish MSS in the last 10 candles (buy-side / bearish mirror)
  premium / discount           5    close in discount of the external dealing range (bullish), premium (bearish)
  options flow                10    OI flow >= +25 (put writing) / <= -25 (live only, when a chain is available)

Setup score (0-100) for a qualified trade plan in direction d:
  the directional score for d
  + 10 if the strategy suits the current regime, -10 if it does not
  + up to +10 for reward:risk to T2 above 1.5 (5 points per extra R)
  + 5 inside an active kill zone (intraday)
  - 15 if the data report is stale or has gaps on the last 20 candles
  then CAPPED at 55 when the higher-timeframe bias opposes the trade, and at 40 in an event-driven regime.
Grades: >= 75 A (high conviction), 60-74 B, 45-59 C (watch only), < 45 no trade. Signals below the minimum score
(setting, default 60) are listed as watch-only, never as trade signals. Scores rank setups; they are not win
probabilities.
"""
from __future__ import annotations

WEIGHTS = {"htf": 20, "itf": 15, "structure": 10, "ema": 10, "momentum": 8, "vwap": 7, "volume": 5, "liquidity": 10,
           "zone": 5, "options": 10}
LABELS = {"htf": "Higher-timeframe bias", "itf": "Intermediate-timeframe bias", "structure": "Execution structure",
          "ema": "EMA stack", "momentum": "Momentum (RSI/MACD)", "vwap": "Session VWAP", "volume": "Volume (OBV)",
          "liquidity": "Liquidity event", "zone": "Premium / discount", "options": "Options flow"}


def factors(F, i: int, ctx: dict) -> dict:
    v: dict[str, int | None] = {k: None for k in WEIGHTS}
    v["htf"] = ctx.get("htf_bias") if ctx.get("htf_ok") else None
    v["itf"] = ctx.get("itf_bias") if ctx.get("itf_ok") else None
    tr = F.trend_at(F.t[i])
    v["structure"] = 1 if tr == "bullish" else -1 if tr == "bearish" else 0
    c, e20, e50 = F.c[i], F.ema[20][i], F.ema[50][i]
    if e20 is not None and e50 is not None:
        v["ema"] = 1 if c > e20 > e50 else -1 if c < e20 < e50 else 0
    r, h = F.rsi[i], F.macd_hist[i]
    if r is not None and h is not None:
        v["momentum"] = 1 if r > 55 and h > 0 else -1 if r < 45 and h < 0 else 0
    if F.step < 86400 and F.vwap[i] is not None:
        v["vwap"] = 1 if c > F.vwap[i] else -1
    if F.has_volume and i >= 20:
        avg = sum(F.obv[i - 19:i + 1]) / 20
        v["volume"] = 1 if F.obv[i] > avg else -1
    t0 = F.t[max(0, i - 10)]
    ev = 0
    for s in F.sweeps_between(t0, F.t[i]):
        ev += 1 if s.side == "sell" else -1
    for m in F.mss_between(t0, F.t[i]):
        ev += 2 if m["direction"] == "bullish" else -2
    v["liquidity"] = 1 if ev > 0 else -1 if ev < 0 else 0
    hs, ls = F.swings_at(F.t[i], "high"), F.swings_at(F.t[i], "low")
    if hs and ls:
        hi, lo = hs[-1].price, ls[-1].price
        if hi > lo:
            eq = (hi + lo) / 2
            v["zone"] = 1 if c < eq else -1
    opt = ctx.get("options")
    if opt and i == len(F.bars) - 1 and opt.get("oi_flow") is not None:
        f = opt["oi_flow"]
        v["options"] = 1 if f >= 25 else -1 if f <= -25 else 0
    return v


def directional(F, i: int, ctx: dict) -> dict:
    v = factors(F, i, ctx)
    have = {k: x for k, x in v.items() if x is not None}
    tot = sum(WEIGHTS[k] for k in have) or 1
    bull = sum(WEIGHTS[k] for k, x in have.items() if x > 0) / tot * 100
    bear = sum(WEIGHTS[k] for k, x in have.items() if x < 0) / tot * 100
    return {"bull": round(bull, 1), "bear": round(bear, 1), "neutral": round(100 - bull - bear, 1),
            "bias": "bullish" if bull - bear >= 20 else "bearish" if bear - bull >= 20 else "neutral",
            "factors": [{"key": k, "label": LABELS[k], "weight": WEIGHTS[k], "vote": v[k]} for k in WEIGHTS],
            "unavailable": [LABELS[k] for k, x in v.items() if x is None]}


def grade(score: float) -> str:
    return "A" if score >= 75 else "B" if score >= 60 else "C" if score >= 45 else "No trade"


def setup_score(setup: dict, dscore: dict, ctx: dict, quality: dict | None = None) -> dict:
    d = 1 if setup["direction"] == "LONG" else -1
    base = dscore["bull"] if d > 0 else dscore["bear"]
    adj = []
    s = base
    if setup.get("regime_ok"):
        s += 10
        adj.append("+10 strategy suits the regime")
    else:
        s -= 10
        adj.append("-10 strategy not suited to the regime")
    bonus = max(0.0, min(10.0, (setup["rr"] - 1.5) * 5))
    if bonus:
        s += bonus
        adj.append(f"+{bonus:.0f} reward:risk {setup['rr']:.1f}")
    if ctx.get("kill_zone"):
        s += 5
        adj.append(f"+5 in {ctx['kill_zone']}")
    if quality and (quality.get("stale") or (quality.get("recent_gaps") or 0) > 0):
        s -= 15
        adj.append("-15 stale data or gaps in the last 20 candles")
    s = max(0.0, min(100.0, s))
    if ctx.get("htf_ok") and ctx.get("htf_bias") == -d:
        if s > 55:
            adj.append("capped at 55: higher timeframe opposes")
        s = min(s, 55.0)
    if ctx.get("regime") == "event":
        if s > 40:
            adj.append("capped at 40: event-driven candle")
        s = min(s, 40.0)
    reasons_against = []
    if ctx.get("htf_ok") and ctx.get("htf_bias") == -d:
        reasons_against.append("Higher-timeframe bias is against the trade")
    if ctx.get("itf_ok") and ctx.get("itf_bias") == -d:
        reasons_against.append("Intermediate-timeframe bias is against the trade")
    opp = dscore["bear"] if d > 0 else dscore["bull"]
    if opp >= 35:
        reasons_against.append(f"{opp:.0f}% of the weighted factors point the other way")
    if not setup.get("regime_ok"):
        reasons_against.append(f"Regime ({ctx.get('regime_label', ctx.get('regime'))}) is not where this strategy works best")
    for f in dscore["factors"]:
        if f["vote"] == -d and f["weight"] >= 10:
            reasons_against.append(f"{f['label']} disagrees")
    return {"score": round(s, 1), "grade": grade(s), "adjustments": adj, "reasons_against": list(dict.fromkeys(reasons_against))}
