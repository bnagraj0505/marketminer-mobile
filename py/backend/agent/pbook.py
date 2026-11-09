"""Profit booking or fresh selling? For every options-desk index, read the pullback from today's extreme together with
what option writers did over the same minutes. Information only.

Inputs (all real, nothing assumed):
  * today's 15m move from retrace.py: direction, start, extreme (and when it printed), depth given back, Fibonacci levels
  * the per-minute option-chain snapshots recorded by straddle.py (open interest per strike, ATM straddle IV)
OI change is measured from the snapshot at the time of today's extreme to the latest one, over the strikes present in
both within +/- 6 strikes of the current ATM (so a moving ATM window does not create fake changes).

Reading for a pullback in an UP-move (mirror for a bounce in a down-move: call writers <-> put writers):
  TRENDING                 given back < 10% of today's leg: no profit booking yet
  PROFIT BOOKING           price off the high, call OI not building (< +3%), put OI holding (> -3%): longs taking profit,
                           writers are not betting against the move -> pullback inside the trend
  PROFIT BOOKING + UNWIND  call OI not building but put OI falling: put writers (support) are leaving -> deeper pullback risk
  FRESH SELLING            call OI building (>= +3%) while price falls: writers adding against the move
  REVERSAL RISK            call OI building AND put OI falling, or today's 78.6% level broken
Depth zones: 23.6-38.2% normal profit booking, 38.2-61.8% deeper, 61.8-78.6% last line, beyond 78.6% the move is reversing.
"""
from __future__ import annotations

import time

from . import db
from . import straddle as SD

TH = 3.0          # % change in OI that counts as building / unwinding
BAND = 6          # strikes either side of the current ATM


def _oi(row: dict) -> dict:
    return (db.jload(row.get("payload"), {}) or {}).get("oi") or {}


def oi_change(a: dict, b: dict, atm: float, step: float) -> dict | None:
    """CE / PE OI change (%, absolute) over the strikes in both snapshots within BAND strikes of atm."""
    if not a or not b or not step:
        return None
    ks = [k for k in b if k in a and abs(float(k) - atm) <= BAND * step + 1e-6]
    if len(ks) < 3:
        return None
    ce0, pe0 = sum(a[k][0] for k in ks), sum(a[k][1] for k in ks)
    ce1, pe1 = sum(b[k][0] for k in ks), sum(b[k][1] for k in ks)
    pc = lambda x0, x1: (x1 - x0) / x0 * 100 if x0 else None
    return {"strikes": len(ks), "ce_from": ce0, "ce_now": ce1, "pe_from": pe0, "pe_now": pe1, "ce_chg": ce1 - ce0, "pe_chg": pe1 - pe0,
            "ce_pct": pc(ce0, ce1), "pe_pct": pc(pe0, pe1), "pcr_from": pe0 / ce0 if ce0 else None, "pcr_now": pe1 / ce1 if ce1 else None}


def classify(trend: str, depth: float | None, status: str | None, oi: dict | None) -> tuple[str, str]:
    """Pure: (verdict, plain-language reading)."""
    if trend not in ("bullish", "bearish") or depth is None:
        return "RANGE", "No clear move today: nothing to book yet"
    up = trend == "bullish"
    move, back = ("up-move", "off the high") if up else ("down-move", "off the low")
    if status == "REVERSAL":
        return "REVERSAL", f"Today's {move} has given back more than 78.6%: this is no longer profit booking"
    if depth < 10:
        return "TRENDING", f"Price is near today's {'high' if up else 'low'} ({depth:.0f}% given back): no profit booking yet"
    if not oi or oi.get("ce_pct") is None or oi.get("pe_pct") is None:
        return "PULLBACK", f"Pulling back {depth:.0f}% {back}; option OI since the extreme is not recorded yet, so who is driving it is unknown"
    against, support = (oi["ce_pct"], oi["pe_pct"]) if up else (oi["pe_pct"], oi["ce_pct"])
    wa, ws = ("call", "put") if up else ("put", "call")
    if against >= TH and support <= -TH:
        return "REVERSAL_RISK", (f"{wa.title()} writing +{against:.1f}% and {ws} OI {support:.1f}% while price is {depth:.0f}% {back}: "
                                 f"writers are switching sides - reversal risk")
    if against >= TH:
        return "FRESH_SELLING" if up else "FRESH_BUYING", (f"{wa.title()} writers adding +{against:.1f}% while price is {depth:.0f}% {back}: "
                                                          f"fresh {'selling' if up else 'buying'} against the move, not just profit booking")
    if support <= -TH:
        return "BOOKING_UNWIND", (f"{'Profit booking' if up else 'Short covering'} with {ws} writers leaving ({support:.1f}%): "
                                  f"support is thinning - a deeper pullback is possible")
    return ("PROFIT_BOOKING" if up else "SHORT_COVERING"), (f"{'Profit booking' if up else 'Short covering'}: price {depth:.0f}% {back}, {wa} OI "
                                                            f"{against:+.1f}% (not building), {ws} OI {support:+.1f}% (holding) - a pullback inside today's {move}")


def zone(depth: float | None) -> str | None:
    if depth is None:
        return None
    return ("shallow (< 23.6%)" if depth < 23.6 else "normal profit booking (23.6-38.2%)" if depth < 38.2 else
            "deeper (38.2-61.8%)" if depth < 61.8 else "last line (61.8-78.6%)" if depth <= 78.6 else "beyond 78.6%: reversing")


def index_view(x: dict) -> dict:
    """x: one index from retrace.report()."""
    und = x["und"]
    m = (x.get("timeframes") or {}).get("15m") or {}
    out = {"und": und, "close": m.get("close"), "trend": m.get("trend"), "depth_pct": m.get("depth_pct"), "status": m.get("status"),
           "leg_start": m.get("leg_start"), "leg_end": m.get("leg_end"), "leg_end_time": m.get("leg_end_time"), "fib": m.get("fib"),
           "zone": zone(m.get("depth_pct")), "oi": None, "iv_chg": None}
    SD._init()
    last = db.q("SELECT * FROM straddle_snapshots WHERE und=? ORDER BY t DESC LIMIT 1", (und,), one=True)
    if last and m.get("leg_end_time"):
        d0, d1 = SD._session_bounds(last["t"])
        at = max(m["leg_end_time"], d0)
        ref = (db.q("SELECT * FROM straddle_snapshots WHERE und=? AND t>=? AND t<? AND t<=? ORDER BY t DESC LIMIT 1",
                    (und, d0, d1, at + 900), one=True)
               or db.q("SELECT * FROM straddle_snapshots WHERE und=? AND t>=? AND t<? ORDER BY t LIMIT 1", (und, d0, d1), one=True))
        if ref and ref["t"] < last["t"] and ref.get("expiry") == last.get("expiry"):
            out["oi"] = oi_change(_oi(ref), _oi(last), last["atm"], last["step"] or 0)
            if out["oi"]:
                out["oi"].update(since=ref["t"], to=last["t"], expiry=last.get("expiry"))
            if ref.get("iv") and last.get("iv"):
                out["iv_chg"] = last["iv"] - ref["iv"]
        out["snapshot_at"] = last["t"]
    v, txt = classify(m.get("trend"), m.get("depth_pct"), m.get("status"), out["oi"])
    out.update(verdict=v, reading=txt)
    if out["iv_chg"] is not None and v not in ("TRENDING", "RANGE"):
        out["reading"] += f"; ATM IV {out['iv_chg']:+.1f} since the extreme" + (" (fear rising)" if out["iv_chg"] > 0.5 else "")
    return out


async def report() -> dict:
    from . import retrace as RT
    r = await RT.report()
    return {"indices": [index_view(x) for x in r.get("indices") or [] if x.get("timeframes")], "updating": r.get("updating"),
            "note": "Move and depth: today's 15m leg (closed candles). OI: FYERS option chain snapshots taken every minute, from the time of "
                    f"today's extreme to now, over +/-{BAND} strikes of ATM; +/-{TH:g}% counts as building / unwinding. Information only.",
            "at": int(time.time())}
