"""Post-trade intelligence. Every closed agent trade gets a review built ONLY from recorded data: the decision row
(setup, evidence, every Risk Guardian check), the position state (fills, exits, MAE/MFE in R) and the agent's market
observations before and after. Nothing is invented; questions the data cannot answer say so.

Outcome classification (first rule that matches):
  Data-quality failure            the entry was taken on a fallback source or a data-quality warning
  Execution failure               entry filled > 0.25R worse than planned
  Risk-management failure         loss beyond -1.3R (gap / slippage through the stop: the plan's max loss was exceeded)
  Correct strategy, profitable    P&L > 0
  Poor exit                       a loss after the trade had been >= +1R in favour (MFE)
  Poor entry                      a loss with entry slippage > 0.15R, or MAE >= 0.9R before any favourable move of 0.3R
  Incorrect market interpretation a loss that never went 0.25R in favour, or the regime flipped against the trade
  Correct strategy, losing outcome every other loss (it moved the right way, then the stop was hit: normal variance)
A losing trade is therefore NOT automatically a strategy failure.

LLM narrative: optional (agent setting llm_reviews and AI configured in .env). The model receives the review facts as
JSON and is told to use nothing else; its text is stored separately (narrative_source = llm:<provider>) and never
changes the classification or any decision.
"""
from __future__ import annotations

import json

from . import db

BULL = ("strong_bull", "weak_bull")
BEAR = ("strong_bear", "weak_bear")


def _regime_side(rg: str | None) -> int:
    return 1 if rg in BULL else -1 if rg in BEAR else 0


def classify(t: dict, st: dict, regime_after: str | None) -> str:
    d = 1 if t["side"] == "LONG" else -1
    risk_u = abs(t["entry"] - t["stop"]) or 1e-9
    slip_r = (st.get("entry_slippage") or 0) / risk_u
    r = t.get("r_result") or 0
    if "fallback" in str(st.get("fill_source", "")).lower() or ((st.get("quality_at_entry") or {}).get("ok") is False):
        return "Data-quality failure"
    if slip_r > 0.25:
        return "Execution failure"
    if r < -1.3:
        return "Risk-management failure"
    if (t.get("pnl") or 0) > 0:
        return "Correct strategy, profitable execution"
    mfe, mae = st.get("mfe_r") or 0, st.get("mae_r") or 0
    if mfe >= 1.0:
        return "Poor exit"
    if slip_r > 0.15 or (mae >= 0.9 and mfe < 0.3):
        return "Poor entry"
    if mfe < 0.25 or _regime_side(regime_after) == -d:
        return "Incorrect market interpretation"
    return "Correct strategy, losing outcome"


def review(trade_id: int) -> dict | None:
    t = db.q("SELECT * FROM agent_trades WHERE id=? AND status='closed'", (trade_id,), one=True)
    if not t or db.q("SELECT id FROM agent_reviews WHERE trade_id=?", (trade_id,), one=True):
        return None
    st = db.jload(t["payload"], {})
    dec = db.q("SELECT * FROM agent_decisions WHERE id=?", (t["decision_id"],), one=True) or {}
    s = db.jload(dec.get("setup"), {})
    chk = db.jload(dec.get("checks"), {})
    obs_after = db.q("SELECT regime, volatility, bar_time FROM agent_observations WHERE symbol=? AND tf=? AND t<=? ORDER BY bar_time DESC LIMIT 1",
                     (t["symbol"], t["tf"], (t["closed_at"] or 0) + 120), one=True) or {}
    regime_after = obs_after.get("regime")
    cls = classify(t, st, regime_after)
    risk_u = abs(t["entry"] - t["stop"]) or 1e-9
    ev = s.get("evidence") or {}
    hit = st.get("hit") or []
    soft = [c["check"] for c in (chk.get("checks") or []) if not c["ok"] and not c.get("hard")]
    targets = s.get("targets") or json.loads(t["targets"] or "[]")
    win = (t["pnl"] or 0) > 0
    answers = {
        "1_why_initiated": f"{s.get('strategy_name') or t['strategy']} {t['side']} at score {s.get('score')}: " + "; ".join((s.get("reasons_for") or [])[:4])
                           + f". Independent evidence: {', '.join(ev.get('agree') or []) or 'none'}.",
        "2_regime_correct": f"Entry regime {t['regime']}; latest observed regime at exit {regime_after or 'not observed'}"
                            + (" (flipped against the trade)" if _regime_side(regime_after) == -(1 if t['side'] == 'LONG' else -1) else ""),
        "3_entry_optimal": f"Planned {t['planned_entry']:,.4g}, filled {t['entry']:,.4g} ({(st.get('entry_slippage') or 0) / risk_u:+.2f}R slippage); "
                           f"worst excursion before exit {st.get('mae_r', 0):.2f}R",
        "4_stop_appropriate": f"Stop {(s.get('atr') and f'{risk_u / s['atr']:.2f} ATR') or 'n/a'} from entry; "
                              + ("stop hit" if "stop" in (t["exit_reason"] or "") else f"exit by {t['exit_reason']}"),
        "5_target_realistic": f"Targets reached: {', '.join(hit) or 'none'}; best excursion {st.get('mfe_r', 0):.2f}R vs T1 at "
                              f"{abs(targets[0] - t['entry']) / risk_u:.2f}R" if targets else "no targets recorded",
        "6_expected_price": f"Entry slippage {(st.get('entry_slippage') or 0) / risk_u:+.2f}R; total costs {t['costs']:,.2f} {t['currency']}",
        "7_strategy_as_expected": f"Backtest status for this symbol/timeframe: {(s.get('validation') or {}).get('status', 'not backtested')}; "
                                  f"result {t['r_result']:+.2f}R",
        "8_misleading_indicators": ("Families that agreed but did not deliver follow-through: " + ", ".join(ev.get("agree") or [])) if not win and (st.get("mfe_r") or 0) < 0.25
                                   else "No evidence of misleading inputs from the recorded data",
        "9_avoidable": ("Marginal setup: " + ", ".join(soft)) if soft else ("Opposing evidence was present: " + ", ".join(ev.get("oppose") or [])) if ev.get("oppose")
                       else "Not avoidable on the information at entry (all checks passed)",
        "10_improvement": {
            "Correct strategy, profitable execution": "None required; keep sampling.",
            "Correct strategy, losing outcome": "None from one trade: normal variance. Watch the strategy's rolling expectancy.",
            "Incorrect market interpretation": "Check whether this strategy should be filtered harder in this regime (research: regime breakdown).",
            "Poor exit": "Research a different trailing rule (e.g. trail after +1R) for this strategy.",
            "Poor entry": "Research entering on a retest / limit inside the entry zone instead of at market.",
            "Risk-management failure": "Loss exceeded plan: raise gap allowance or avoid holding through known events.",
            "Execution failure": "Slippage too large: tighten the allowed entry drift or avoid fast markets.",
            "Data-quality failure": "Tighten the data-quality gate for this source.",
        }[cls],
    }
    narrative = (f"#{t['id']} {t['symbol']} {t['side']} ({t['strategy']}, {t['regime']}): {cls}. "
                 f"{t['r_result']:+.2f}R, P&L {t['pnl']:,.2f} {t['currency']}, exit {t['exit_reason']}. "
                 f"MFE {st.get('mfe_r', 0):.2f}R / MAE {st.get('mae_r', 0):.2f}R.")
    rid = db.execute("INSERT OR IGNORE INTO agent_reviews(trade_id, t, classification, answers, narrative, narrative_source) VALUES(?,?,?,?,?,?)",
                     (trade_id, db.now(), cls, json.dumps(answers), narrative, "deterministic"))
    db.log("review", f"Review #{trade_id}: {cls}", t["symbol"], {"trade_id": trade_id})
    return {"id": rid, "trade_id": trade_id, "classification": cls, "answers": answers, "narrative": narrative}


async def llm_narrative(trade_id: int) -> str | None:
    """Optional plain-language write-up from the configured LLM, grounded in the stored review facts only."""
    import os
    if not (os.getenv("AI_PROVIDER") and os.getenv("AI_API_KEY") and os.getenv("AI_MODEL")):
        return None
    r = db.q("SELECT * FROM agent_reviews WHERE trade_id=?", (trade_id,), one=True)
    t = db.q("SELECT symbol, side, strategy, regime, entry, exit, stop, r_result, pnl, currency, exit_reason FROM agent_trades WHERE id=?", (trade_id,), one=True)
    if not r or not t:
        return None
    facts = {"trade": t, "classification": r["classification"], "answers": db.jload(r["answers"], {})}
    prompt = ("You are reviewing a PAPER trade. Use ONLY the JSON facts below; do not add prices, news or reasons that are not in them. "
              "Reply as JSON {\"review\": \"<=120 words\"}.\nFACTS: " + json.dumps(facts, default=str))
    try:
        from backend.app import ai_analysis, AIRequest   # lazy: avoids a circular import at start-up
        res = await ai_analysis(AIRequest(prompt=prompt[:12000]))
        text = res.get("text") or ""
        try:
            text = json.loads(text).get("review", text)
        except ValueError:
            pass
        db.execute("UPDATE agent_reviews SET narrative=?, narrative_source=? WHERE trade_id=?",
                   (r["narrative"] + "\n\nLLM: " + text[:1500], f"llm:{res.get('provider')}", trade_id))
        return text
    except Exception as exc:                              # noqa: BLE001 - optional
        db.log("review", f"LLM narrative unavailable: {str(exc)[:120]}", level="warn")
        return None
