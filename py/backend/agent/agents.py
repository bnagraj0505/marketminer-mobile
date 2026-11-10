"""The specialised agents. Each is a deterministic function over one Signal Lab analysis (plus the agent's own
record); none of them can place or approve a trade - that is guardian.py and execution.py.

  regime_agent      market regime, volatility, whether the market is predictable enough to trade at all
  structure_agent   trend, structure shifts, liquidity, support/resistance, reversal / breakout zones
  options_agent     option-chain and perpetual positioning (when the data exists; says so when it does not)
  strategy_agent    which of the Lab's setups on this closed candle are eligible: strategy suits the regime, strategy
                    not suspended, and the agent's own record of that strategy in this regime is not negative
  setup_agent       turns an eligible Lab setup into the full trade proposal (all fields the Risk Guardian needs)
"""
from __future__ import annotations

from backend.lab import costs as C
from . import db, intel


def regime_agent(a: dict, pic: dict) -> dict:
    reg = a.get("regime") or {}
    facts = reg.get("facts") or {}
    label = pic.get("regime_label")
    states = []
    rg = reg.get("regime")
    if rg in ("strong_bull",):
        states += ["strong bullish", "trending"]
    if rg in ("strong_bear",):
        states += ["strong bearish", "trending"]
    if rg in ("weak_bull", "weak_bear"):
        states += ["trending (weak)"]
    if rg in ("range", "choppy"):
        states += ["sideways", "mean reverting"]
    if rg == "event":
        states += ["event-driven"]
    if rg in ("breakout",):
        states += ["breakout / expansion"]
    if rg in ("reversal",):
        states += ["reversal"]
    if reg.get("volatility") == "high":
        states.append("high volatility")
    if reg.get("volatility") == "low":
        states.append("low volatility")
    if pic.get("unpredictable"):
        states.append("unpredictable")
    return {"regime": rg, "label": label, "states": states, "volatility": reg.get("volatility"), "suits": reg.get("suits") or [],
            "tradeable": not pic.get("unpredictable"), "facts": facts}


def structure_agent(a: dict) -> dict:
    ictr = a.get("ict") or {}
    pa = a.get("price_action") or {}
    mtf = a.get("mtf") or {}
    ctx = a.get("context") or {}
    marks = []
    for k in ("bos", "choch", "mss"):
        v = ictr.get(k)
        if v:
            marks.append({k: v[-3:] if isinstance(v, list) else v})
    return {"execution_bias": mtf.get("execution_bias"), "intermediate": mtf.get("intermediate"), "higher": mtf.get("higher"),
            "prev_day": ctx.get("prev_day"), "kill_zone": ctx.get("kill_zone"),
            "zones": (pa.get("zones") or [])[:6] if isinstance(pa.get("zones"), list) else pa.get("zones"),
            "structure_events": marks, "levels": a.get("levels") or {}}


def options_agent(a: dict, pic: dict) -> dict:
    o, p = pic.get("options"), pic.get("perp")
    if not o and not p:
        return {"available": False, "note": "No option chain or perpetual data for this symbol right now (left out, not assumed)."}
    out = {"available": True, "chain": o, "perp": p, "reads": []}
    if o:
        pcr = o.get("pcr")
        if pcr is not None:
            out["reads"].append(f"PCR {pcr:.2f} ({'put-heavy' if pcr > 1.2 else 'call-heavy' if pcr < 0.7 else 'balanced'})")
        if o.get("oi_flow") is not None:
            out["reads"].append(f"OI flow {o['oi_flow']:+.0f} ({'put writing' if o['oi_flow'] >= 25 else 'call writing' if o['oi_flow'] <= -25 else 'no clear writing'})")
        if o.get("max_pain") is not None:
            out["reads"].append(f"max pain {o['max_pain']}")
    if p and p.get("funding_rate") is not None:
        out["reads"].append(f"funding {p['funding_rate']}")
    return out


def _profile(strategy: str, regime: str) -> dict:
    rows = db.q("SELECT r_result FROM agent_trades WHERE status='closed' AND strategy=? AND regime=?", (strategy, regime))
    n = len(rows)
    return {"trades": n, "expectancy_r": round(sum(r["r_result"] or 0 for r in rows) / n, 3) if n else None}


def strategy_status(strategy: str) -> str:
    r = db.q("SELECT status FROM agent_strategy_status WHERE strategy=?", (strategy,), one=True)
    return (r or {}).get("status") or "active"


def strategy_agent(a: dict, reg: dict) -> tuple[list[dict], list[dict]]:
    """Eligible Lab setups from the latest CLOSED candle, best first; and the ones turned down, with the reason."""
    candles = a.get("candles") or []
    last_t = candles[-1]["time"] if candles else None
    eligible, declined = [], []
    for s in a.get("setups") or []:
        if s.get("time") != last_t or s.get("provisional"):
            continue                                      # only setups formed on the candle that just closed
        why = None
        if strategy_status(s["strategy"]) == "suspended":
            why = "strategy suspended (performance degradation)"
        elif not s.get("regime_ok"):
            why = f"strategy does not suit the {reg.get('label')} regime"
        else:
            pf = _profile(s["strategy"], reg.get("regime") or "")
            if pf["trades"] >= 10 and (pf["expectancy_r"] or 0) < 0:
                why = f"agent's own record: {pf['trades']} trades in this regime at {pf['expectancy_r']:+.2f}R"
        (declined if why else eligible).append({**s, "_why": why} if why else s)
    eligible.sort(key=lambda s: -s["score"])
    return eligible, declined


def setup_agent(s: dict, a: dict, pic: dict, reg: dict) -> dict:
    """The full proposal. Position size is filled in by the Risk Guardian (it alone decides size)."""
    sym, market = a["symbol"], a["market"]
    ev = intel.evidence(a.get("score") or {}, s["direction"], sym, a["tf"], a.get("msb"))
    e, st, tg = s["entry"], s["stop"], s["targets"]
    d = 1 if s["direction"] == "LONG" else -1
    est_cost_unit = abs(C.slip_price(e, d, market) - e) * 2
    return {
        "symbol": sym, "market": market, "tf": a["tf"], "bar_time": s["time"], "strategy": s["strategy"], "strategy_name": s.get("name"),
        "direction": s["direction"], "entry": e, "entry_condition": f"Fill at the next price after the {a['tf']} candle closed at {e:,.4g}",
        "entry_zone": s.get("entry_zone"), "stop": st, "targets": tg, "rr": s.get("rr"), "rr_t1": s.get("rr_t1"),
        "expected_hold": s.get("expected_hold"), "atr": s.get("atr"), "max_spread": s.get("max_spread"),
        "regime": reg.get("regime"), "regime_label": reg.get("label"), "volatility": reg.get("volatility"),
        "invalidation": s.get("invalidation"), "trailing": s.get("trailing"),
        "score": s["score"], "grade": s.get("grade"), "confidence_note": "Score ranks setups; it is not a probability of profit.",
        "reasons_for": s.get("reasons_for") or [], "reasons_against": s.get("reasons_against") or [],
        "score_adjustments": s.get("adjustments") or [], "evidence": ev, "validation": s.get("validation") or {},
        "est_slippage_per_unit": est_cost_unit, "data_source": pic.get("source"), "data_as_of": pic.get("as_of"),
    }
