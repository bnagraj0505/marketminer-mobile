"""AI risk engine for the INR book (user rule 2026-11-04: "let AI decide the SL, per-day loss and lots; per-day capital
limit 2 lakh").

The only fixed user limit is `inr_day_capital` (capital in open INR positions). Everything else is decided here for
each trade, from evidence the agent has at that moment, and every factor is recorded with the decision:

  per-trade risk = base x confidence x track record x today's state x volatility
      base          ai_base_risk_pct of the day capital (2.5% of 2L = 5,000)
      confidence    ML probability of reaching 1:2 before the stop (when a model exists), else setup score + evidence
      track record  last 20 closed INR agent trades: losing expectancy halves risk, a proven edge adds up to +25%
      today         x0.7 per losing trade today (risk only shrinks after losses - never martingale)
      volatility    x0.75 in high volatility
    kept between ai_min_risk_pct and ai_max_risk_pct of the day capital (1,000 .. 10,000 on 2L).

  day stop (decided each time, not fixed): ai_day_loss_trades x base x track-record factor, between 1 base and 10% of
    the day capital (15,000 on 2L with a neutral record, 7,500 after a losing run, up to 18,750 with a proven edge).
    Today's realised loss + every open trade's risk + a new trade's risk must stay inside it, and the day also closes
    when it gives back ai_profit_lock (50%) of a peak day profit of at least 2 x base.

  stop-loss: the setup's own invalidation level (structure / ATR from Signal Lab; for options the underlying stop
    translated through delta). Size follows the stop, so a wider stop means fewer units, not more risk.

  lots: as many as fit the risk and the capital left (minimum 1). With 2+ lots half of them book at 1:2 after costs and
    the rest trail; with 1 lot the stop moves to breakeven at 1:2 and the whole position trails.
"""
from __future__ import annotations

import datetime as dt

from . import db

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def track_record(cur: str = "INR", n: int = 20) -> dict:
    rows = db.q("SELECT r_result FROM agent_trades WHERE status='closed' AND currency=? AND mode='paper' ORDER BY closed_at DESC, id DESC LIMIT ?",
                (cur, n))
    rs = [float(x["r_result"] or 0) for x in rows]
    k = len(rs)
    exp = sum(rs) / k if k else 0.0
    if k >= 10:
        mult = 0.5 if exp < 0 else 1.0 + min(0.25, exp / 2)
    elif k >= 5 and exp < 0:
        mult = 0.75
    else:
        mult = 1.0
    why = (f"last {k} trades expectancy {exp:+.2f}R" if k else "no closed trades yet") + f" -> x{mult:.2f}"
    return {"trades": k, "expectancy_r": round(exp, 3), "mult": mult, "why": why}


def base(r: dict) -> float:
    return r["inr_day_capital"] * r["ai_base_risk_pct"] / 100


def day_limit(r: dict, tr: dict | None = None) -> float:
    tr = tr or track_record()
    b = base(r)
    return _clamp(r["ai_day_loss_trades"] * b * tr["mult"], b, 0.10 * r["inr_day_capital"])


def _today_peak(day0: int) -> float:
    eq = peak = 0.0
    for t in db.q("SELECT pnl FROM agent_trades WHERE status='closed' AND currency='INR' AND mode='paper' AND closed_at>=? ORDER BY closed_at, id", (day0,)):
        eq += t["pnl"] or 0
        peak = max(peak, eq)
    return peak


def day_closed(bk: dict, r: dict, day0: int) -> str | None:
    lim = day_limit(r)
    if bk["day_pnl"] <= -lim:
        return f"today's loss {-bk['day_pnl']:,.0f} reached the AI day stop {lim:,.0f}"
    peak = _today_peak(day0)
    if peak >= 2 * base(r) and bk["day_pnl"] <= peak * (1 - r["ai_profit_lock"]):
        return f"gave back {r['ai_profit_lock'] * 100:.0f}% of today's peak profit {peak:,.0f} (now {bk['day_pnl']:,.0f}): profit locked"
    return None


def confidence(setup: dict | None, ctx: dict | None) -> tuple[float, str]:
    if not setup:
        return 1.0, "no setup (neutral)"
    ml = (ctx or {}).get("ml") or {}
    p = ml.get("p")
    if p is not None:
        b = 2.0                                            # 1:2 payoff
        edge = p - 1 / (1 + b)                             # above the 33% break-even: 0.25x at break-even, 1x at 48%, 2x at 68%+
        m = _clamp(0.25 + edge / 0.2, 0.25, 2.0)
        return m, f"ML {p * 100:.0f}% chance of 1:2 (break-even 33%) -> x{m:.2f}"
    ev = ((ctx or {}).get("evidence") or {}).get("independent_confirmations", 2)
    sc = float(setup.get("score") or 60)
    m = _clamp(0.5 + (sc - 60) / 40 + 0.15 * (ev - 2), 0.25, 1.5)
    return m, f"score {sc:.0f}, {ev} evidence families (no ML model) -> x{m:.2f}"


def decide(bk: dict, r: dict, setup: dict | None = None, ctx: dict | None = None) -> dict:
    """Per-trade risk and day stop for the INR book right now."""
    b = base(r)
    tr = track_record()
    conf, cwhy = confidence(setup, ctx)
    lt = int(bk.get("losses_today") or 0)
    daym = 0.7 ** lt
    vol = ((ctx or {}).get("picture") or {}).get("volatility")
    volm = 0.75 if vol == "high" else 1.0
    raw = b * conf * tr["mult"] * daym * volm
    lo, hi = r["inr_day_capital"] * r["ai_min_risk_pct"] / 100, r["inr_day_capital"] * r["ai_max_risk_pct"] / 100
    per = _clamp(raw, lo, hi)
    lim = day_limit(r, tr)
    factors = [f"base {b:,.0f} ({r['ai_base_risk_pct']:g}% of the {r['inr_day_capital']:,.0f} day capital)", cwhy, tr["why"],
               f"{lt} losing trade(s) today -> x{daym:.2f}", f"volatility {vol or 'normal'} -> x{volm:.2f}",
               f"= {raw:,.0f}, kept within {lo:,.0f}..{hi:,.0f} -> {per:,.0f}"]
    return {"per_trade": per, "day_limit": lim, "factors": factors, "track": tr, "confidence": conf}
