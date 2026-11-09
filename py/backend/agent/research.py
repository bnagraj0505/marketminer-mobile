"""Strategy Research and Evolution Engine (sandboxed, approval-gated).

The research engine may only PROPOSE. It runs the Signal Lab backtester (next-open entries, costs, 60/20/20 split,
parameter grid on train only, 3-fold walk-forward, Monte Carlo) and records each result as an experiment. A proposal
changes nothing until the user approves it (POST /api/agent/research/{id}/approve); every approved change is versioned
in agent_config_history and can be rolled back. It can never touch capital limits, risk policy, execution permissions
or code: the only things an approved experiment can change are, per symbol/timeframe/strategy,
  * the agent's minimum score for that strategy        (kind "threshold")
  * whether the agent may trade that strategy at all   (kind "suspend" / "activate")

Promotion rules (judge): a challenger is ELIGIBLE only when ALL hold
  1. out-of-sample (validation + test) trades >= 30
  2. validation AND test expectancy > 0
  3. out-of-sample expectancy beats the champion's by >= 0.05R
  4. >= 2 of 3 walk-forward folds positive
  5. Monte Carlo share of negative runs <= 20%
  6. max drawdown (R) no worse than 1.25 x the champion's
  7. not overfit: test expectancy >= 30% of train expectancy, and >= 50% of the parameter-grid cells positive on
     validation (a lone good cell is luck)
Suspension proposals need >= 30 out-of-sample trades with validation and test expectancy both < 0 and Monte Carlo
negative share >= 60%.

factor_correlation measures the Signal Lab score factors' votes on real history and records pairs with |r| >= 0.6,
which intel.families_for then counts as one piece of evidence.
"""
from __future__ import annotations

import asyncio
import json
import math

from backend.lab import backtest as B
from backend.lab import costs as C
from backend.lab import data as D
from backend.lab import scoring as SC
from backend.lab import store
from backend.lab.engine import Engine, MTF
from . import config as CFG
from . import db

MIN_OOS = 30


def _oos(res: dict) -> dict:
    v, t = res.get("validation") or {}, res.get("test") or {}
    n = (v.get("trades") or 0) + (t.get("trades") or 0)
    e = ((v.get("expectancy_r") or 0) * (v.get("trades") or 0) + (t.get("expectancy_r") or 0) * (t.get("trades") or 0)) / n if n else None
    return {"trades": n, "expectancy_r": e, "val": v.get("expectancy_r"), "test": t.get("expectancy_r")}


def judge(champ: dict, chall: dict) -> dict:
    """Pure function over two backtest results. Returns {eligible, reasons, facts}."""
    co, cc = _oos(champ), _oos(chall)
    reasons, ok = [], True

    def need(cond: bool, text: str):
        nonlocal ok
        reasons.append(("PASS " if cond else "FAIL ") + text)
        ok = ok and cond

    need(cc["trades"] >= MIN_OOS, f"out-of-sample trades {cc['trades']} >= {MIN_OOS}")
    need((cc["val"] or 0) > 0 and (cc["test"] or 0) > 0, f"validation {cc['val']} and test {cc['test']} expectancy > 0")
    need(cc["expectancy_r"] is not None and cc["expectancy_r"] >= (co["expectancy_r"] or 0) + 0.05,
         f"OOS expectancy {cc['expectancy_r'] and round(cc['expectancy_r'], 3)} vs champion {co['expectancy_r'] and round(co['expectancy_r'], 3)} (+0.05R needed)")
    wf = chall.get("walk_forward") or []
    pos = sum(1 for f in wf if (f.get("expectancy_r") or 0) > 0 and (f.get("trades") or 0) > 0)
    need(pos >= 2, f"{pos}/3 walk-forward folds positive")
    mc = chall.get("monte_carlo") or {}
    need(mc.get("available") and (mc.get("prob_negative") or 100) <= 20, f"Monte Carlo negative runs {mc.get('prob_negative')}% <= 20%")
    dd_c, dd_h = abs((chall.get("overall") or {}).get("max_drawdown_r") or 0), abs((champ.get("overall") or {}).get("max_drawdown_r") or 0)
    need(dd_h == 0 or dd_c <= 1.25 * dd_h, f"max drawdown {dd_c}R vs champion {dd_h}R (<= 1.25x)")
    tr = (chall.get("train") or {}).get("expectancy_r") or 0
    te = (chall.get("test") or {}).get("expectancy_r") or 0
    need(tr <= 0 or te >= 0.3 * tr, f"test expectancy {te} >= 30% of train {tr} (overfit check)")
    sens = chall.get("sensitivity") or []
    good = sum(1 for c in sens if ((c.get("validation") or {}).get("expectancy_r") or 0) > 0)
    need(not sens or good >= len(sens) / 2, f"{good}/{len(sens)} parameter cells positive on validation (robustness)")
    return {"eligible": ok, "reasons": reasons, "facts": {"champion_oos": co, "challenger_oos": cc}}


def judge_suspend(res: dict) -> dict:
    o = _oos(res)
    mc = res.get("monte_carlo") or {}
    cond = o["trades"] >= MIN_OOS and (o["val"] or 0) < 0 and (o["test"] or 0) < 0 and (mc.get("prob_negative") or 0) >= 60
    return {"eligible": cond, "reasons": [f"OOS trades {o['trades']}", f"validation {o['val']}, test {o['test']}",
                                          f"Monte Carlo negative {mc.get('prob_negative')}%"], "facts": o}


async def _engine(symbol: str, tf: str, bars: int):
    main, q = await D.candles(symbol, tf, bars)
    itf, htf = MTF[tf]
    ib = (await D.candles(symbol, itf, min(5000, bars)))[0] if itf else None
    hb = (await D.candles(symbol, htf, min(3000, bars)))[0] if htf and htf != itf else ib
    return Engine(main, tf, D.market_of(symbol), D.session_key(symbol), D.anchor_minutes(symbol), ib, hb), q


async def backtest(symbol: str, tf: str, strategy: str | None, min_score: float | None, bars: int = 3000) -> dict:
    E, q = await _engine(symbol, tf, bars)
    cost_cfg = {**C.DEFAULTS, **(store.get_setting("costs") or {})}
    res = await asyncio.to_thread(B.run, E, symbol, store.get_setting("strategies") or {}, min_score, cost_cfg, strategy)
    res["data_quality"] = q
    return res


def _save(symbol, tf, strategy, kind, params, result_summary, verdict, status) -> int:
    return db.execute("INSERT INTO agent_experiments(t, symbol, tf, strategy, kind, params, result, verdict, status) VALUES(?,?,?,?,?,?,?,?,?)",
                      (db.now(), symbol, tf, strategy, kind, json.dumps(params), json.dumps(result_summary, default=str), json.dumps(verdict), status))


def _brief(res: dict) -> dict:
    return {k: res.get(k) for k in ("overall", "train", "validation", "test", "walk_forward", "monte_carlo", "params", "notes", "candles", "from", "to")}


def thresholds() -> dict:
    return store.get_setting("agent_thresholds") or {}


async def research_strategy(symbol: str, tf: str, strategy: str, bars: int = 3000) -> list[int]:
    """Champion = the agent's current minimum score for this strategy. Challengers = +5 and +10 points."""
    r = CFG.risk()
    key = f"{symbol}|{tf}|{strategy}"
    cur = float(thresholds().get(key, r["min_score"]))
    champ = await backtest(symbol, tf, strategy, cur, bars)
    if champ.get("error"):
        return [_save(symbol, tf, strategy, "threshold", {"min_score": cur}, {"error": champ["error"]}, {"eligible": False, "reasons": [champ["error"]]}, "rejected")]
    ids = []
    sus = judge_suspend(champ)
    if sus["eligible"]:
        ids.append(_save(symbol, tf, strategy, "suspend", {}, _brief(champ), sus, "eligible"))
    for ch in (cur + 5, cur + 10):
        if ch > 95:
            continue
        chall = await backtest(symbol, tf, strategy, ch, bars)
        v = judge(champ, chall) if not chall.get("error") else {"eligible": False, "reasons": [chall["error"]]}
        ids.append(_save(symbol, tf, strategy, "threshold", {"min_score": ch, "champion_min_score": cur},
                         {"champion": _brief(champ), "challenger": _brief(chall)}, v, "eligible" if v["eligible"] else "rejected"))
    db.log("research", f"Research {strategy} on {symbol} {tf}: {len(ids)} experiment(s) recorded", symbol)
    return ids


async def research_symbol(symbol: str, tf: str, bars: int = 3000) -> dict:
    """Full backtest (all strategies) -> per-strategy profile; then per-strategy experiments for the ones with data."""
    full = await backtest(symbol, tf, None, None, bars)
    if full.get("error"):
        db.log("research", f"Research skipped for {symbol} {tf}: {full['error']}", symbol, level="warn")
        return {"error": full["error"]}
    prof = {"by_strategy": full.get("by_strategy"), "by_regime": full.get("by_regime"), "validation_status": full.get("validation_status"),
            "overall": full.get("overall"), "at": db.now()}
    store.set_setting(f"agent_profile:{symbol}:{tf}", prof)
    ids = []
    for k, s in (full.get("by_strategy") or {}).items():
        if (s.get("trades") or 0) >= 20:
            ids += await research_strategy(symbol, tf, k, bars)
    try:
        await factor_correlation(symbol, tf)
    except Exception as exc:                              # noqa: BLE001
        db.log("research", f"Factor correlation failed for {symbol}: {str(exc)[:120]}", symbol, level="warn")
    return {"profile": prof, "experiments": ids}


def _pearson(xs, ys) -> float | None:
    pts = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pts) < 50:
        return None
    mx = sum(p[0] for p in pts) / len(pts)
    my = sum(p[1] for p in pts) / len(pts)
    sx = math.sqrt(sum((p[0] - mx) ** 2 for p in pts))
    sy = math.sqrt(sum((p[1] - my) ** 2 for p in pts))
    if not sx or not sy:
        return None
    return sum((p[0] - mx) * (p[1] - my) for p in pts) / (sx * sy)


def correlation_from_engine(E: Engine, start: int = 210, every: int = 1) -> dict:
    keys = list(SC.WEIGHTS)
    series = {k: [] for k in keys}
    for i in range(start, len(E.F.bars), every):
        v = SC.factors(E.F, i, E.ctx(i))
        for k in keys:
            series[k].append(v.get(k))
    mat, red = {}, []
    for a_ in keys:
        for b_ in keys:
            if a_ < b_:
                r = _pearson(series[a_], series[b_])
                if r is not None:
                    mat[f"{a_}~{b_}"] = round(r, 3)
                    if abs(r) >= 0.6:
                        red.append([a_, b_, round(r, 3)])
    return {"matrix": mat, "redundant": red, "samples": len(series[keys[0]])}


async def factor_correlation(symbol: str, tf: str, bars: int = 2000) -> dict:
    E, _ = await _engine(symbol, tf, bars)
    res = await asyncio.to_thread(correlation_from_engine, E, 210, 2)
    res["at"] = db.now()
    store.set_setting(f"agent_corr:{symbol}:{tf}", res)
    db.log("research", f"Factor correlation {symbol} {tf}: {len(res['redundant'])} redundant pair(s) "
           + ", ".join(f"{a}~{b} {r:+.2f}" for a, b, r in res["redundant"]), symbol)
    return res


def approve(exp_id: int, who: str = "user") -> dict:
    e = db.q("SELECT * FROM agent_experiments WHERE id=?", (exp_id,), one=True)
    if not e:
        raise ValueError("no such experiment")
    if e["status"] != "eligible":
        raise PermissionError(f"experiment is {e['status']}: only eligible experiments can be approved")
    p = db.jload(e["params"], {})
    key = f"{e['symbol']}|{e['tf']}|{e['strategy']}"
    if e["kind"] == "threshold":
        before = thresholds()
        after = {**before, key: float(p["min_score"])}
        db.config_change(who, "agent_thresholds", before, after)
        store.set_setting("agent_thresholds", after)
    elif e["kind"] == "suspend":
        from . import performance as PF
        PF.set_strategy_status(e["strategy"], "suspended", f"approved research #{exp_id}", who)
    else:
        raise ValueError("unknown experiment kind")
    db.execute("UPDATE agent_experiments SET status='approved' WHERE id=?", (exp_id,))
    db.log("research", f"Experiment #{exp_id} ({e['kind']} {key}) APPROVED by {who}")
    return {"ok": True, "id": exp_id}


def rollback(history_id: int, who: str = "user") -> dict:
    h = db.q("SELECT * FROM agent_config_history WHERE id=?", (history_id,), one=True)
    if not h or h["key"] != "agent_thresholds":
        raise ValueError("only agent_thresholds changes can be rolled back here (risk settings: save the old values)")
    before = db.jload(h["before"], {})
    cur = thresholds()
    db.config_change(who, "agent_thresholds", cur, before)
    store.set_setting("agent_thresholds", before)
    db.log("research", f"Rolled back threshold change #{history_id} ({who})")
    return {"ok": True, "restored": before}
