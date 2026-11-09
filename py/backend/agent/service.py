"""Agent orchestrator: the autonomous decision pipeline, run inside the MarketMiner server (no browser needed).

Every loop (loop_seconds, default 60 s):
  1. POSITION MONITORING   open agent positions are advanced on 1-minute candles (always, even when the agent is
                           stopped, so stops and targets keep working); closed ones go to POST-TRADE ANALYSIS
                           (learning.review), the drawdown halt check and the strategy degradation check.
  2. For each watch-list symbol whose next candle has closed (and whose market is open):
       MARKET DATA                 lab.analysis.analyze(closed candles only)
       DATA QUALITY VALIDATION     intel.quality_gate
       MARKET REGIME DETECTION     agents.regime_agent
       MARKET STRUCTURE ANALYSIS   agents.structure_agent / options_agent
       STRATEGY SELECTION          agents.strategy_agent (regime fit, suspensions, own record)
       TRADE SETUP GENERATION      agents.setup_agent (best eligible setup only: one idea per symbol per candle)
       INDEPENDENT RISK VALIDATION guardian.evaluate -> APPROVED / REJECTED / OBSERVE (intelligence mode)
       TRADE APPROVAL + PAPER EXECUTION   execution.open_position (single-use approval)
  3. Rejected / declined setups are followed up (resolve_untaken): did T1 or the stop come first? -> missed
     opportunities vs good rejections, shown in the Learning Centre.
  4. Optional nightly research (research_enabled): backtests, experiments, factor correlation. Proposals only.

Every observation, decision, check and action is written to the agent tables and the activity log.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
import traceback

from backend.lab import analysis as A
from backend.lab import data as D
from backend.lab.candle import TF_SECONDS
from . import agents as AG
from . import config as CFG
from . import db
from . import execution as EX
from . import guardian as G
from . import ml as ML
from . import intel
from . import learning as L
from . import options as OP
from . import performance as PF
from . import research as RS

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
STATUS: dict = {"loop_running": False, "last_cycle": None, "last_error": None, "cycles": 0, "next_due": {}, "last_bar": {},
                "symbol_state": {}, "research": {"running": False, "last": None, "last_day": None}, "reconcile": None}
_task: asyncio.Task | None = None


def market_open(symbol: str, now: float) -> bool:
    m = D.market_of(symbol)
    if m not in ("NSE", "FNO", "MCX"):
        return True
    t = dt.datetime.fromtimestamp(now, IST)
    mins = t.hour * 60 + t.minute
    if t.weekday() >= 5:
        return False
    return 555 <= mins <= 930 if m != "MCX" else 540 <= mins <= 1410


def _due(symbol: str, tf: str, now: float) -> bool:
    nd = STATUS["next_due"].get(f"{symbol}|{tf}")
    return nd is None or now >= nd


async def process_symbol(symbol: str, tf: str, now: float | None = None) -> dict:
    now = now or time.time()
    cfg, r = CFG.agent(), CFG.risk()
    mode = cfg["mode"]
    step = TF_SECONDS[tf]
    key = f"{symbol}|{tf}"
    try:
        a = await A.analyze(symbol, tf, 600, True, False)
    except D.DataUnavailable as exc:
        STATUS["symbol_state"][key] = {"t": int(now), "state": "DATA UNAVAILABLE", "detail": str(exc)[:200]}
        STATUS["next_due"][key] = now + 120
        db.log("data", f"DATA UNAVAILABLE {symbol} {tf}: {str(exc)[:200]} - no decisions", symbol, level="warn")
        return {"state": "data unavailable"}
    candles = a.get("candles") or []
    bar_time = candles[-1]["time"] if candles else 0
    STATUS["last_bar"][key] = bar_time
    STATUS["next_due"][key] = bar_time + 2 * step + 5          # next candle's close (+5 s for the feed)
    q = intel.quality_gate(a.get("quality") or {}, tf, (a.get("quality") or {}).get("candles") or len(candles), now)
    pic = intel.picture(a)
    reg = AG.regime_agent(a, pic)
    struct = AG.structure_agent(a)
    opts = AG.options_agent(a, pic)
    obs = {"picture": pic, "regime": reg, "structure": struct, "options": opts, "quality": q}
    new = db.execute("INSERT OR IGNORE INTO agent_observations(t, symbol, tf, bar_time, regime, volatility, quality_ok, data) VALUES(?,?,?,?,?,?,?,?)",
                     (int(now), symbol, tf, bar_time, reg["regime"], reg["volatility"], int(q["ok"]), json.dumps(obs, default=str)))
    STATUS["symbol_state"][key] = {"t": int(now), "bar_time": bar_time, "state": "observed", "regime": reg["label"], "volatility": reg["volatility"],
                                   "bias": pic.get("bias"), "quality_ok": q["ok"], "quality_blocks": q["blocks"], "close": pic.get("close"),
                                   "source": q.get("source"), "as_of": q.get("as_of")}
    if not new and db.q("SELECT id FROM agent_decisions WHERE symbol=? AND tf=? AND bar_time=? LIMIT 1", (symbol, tf, bar_time), one=True):
        return {"state": "already processed"}
    eligible, declined = AG.strategy_agent(a, reg)
    for s in declined:
        _decision(symbol, tf, bar_time, s, "declined_strategy", "DECLINED", {"why": s.get("_why")})
    if not eligible:
        n_setups = len([s for s in a.get("setups") or [] if s.get("time") == bar_time])
        db.log("analysis", f"{symbol} {tf}: {reg['label']}, bias {pic.get('bias')}; {n_setups} setup(s) on this candle, none eligible"
               + ("" if q["ok"] else f"; data: {'; '.join(q['blocks'])}"), symbol)
        return {"state": "no eligible setup"}
    eligible, ranked = _ml_rank(eligible)
    best, rest = eligible[0], eligible[1:]
    for s in rest:
        did_ = _decision(symbol, tf, bar_time, s, "not_selected", "DECLINED",
                         {"why": f"lower {'ML probability' if ranked else 'score'} than {best['strategy']} on the same candle"})
        ML.record(did_, symbol, tf, s, s.get("_ml"))
    setup = AG.setup_agent(best, a, pic, reg)
    setup["ml"] = best.get("_ml")
    thr = RS.thresholds().get(f"{symbol}|{tf}|{best['strategy']}")
    ctx = {"quality": q, "picture": pic, "evidence": setup["evidence"], "avg_volume": intel.avg_volume(a), "mode": mode,
           "strategy_status": AG.strategy_status(best["strategy"]), "min_score": thr, "ml": best.get("_ml")}
    g = G.evaluate(setup, ctx, now)
    g["quality"] = q
    stage = {"APPROVED": "approved", "REJECTED": "rejected_guardian", "OBSERVE": "observe"}[g["verdict"]]
    did = _decision(symbol, tf, bar_time, setup, stage, g["verdict"], g)
    ML.record(did, symbol, tf, best, best.get("_ml"))
    if g["verdict"] == "REJECTED":
        db.log("guardian", f"REJECTED {setup['direction']} {symbol} ({setup['strategy']}, score {setup['score']:.0f}): " + "; ".join(g["failed"]),
               symbol, {"decision_id": did})
        return {"state": "rejected", "decision_id": did, "failed": g["failed"]}
    if g["verdict"] == "OBSERVE":
        db.log("guardian", f"Intelligence mode: {setup['direction']} {symbol} ({setup['strategy']}) passed all checks - no order", symbol, {"decision_id": did})
        return {"state": "observe", "decision_id": did}
    db.log("guardian", f"APPROVED {setup['direction']} {symbol} ({setup['strategy']}, score {setup['score']:.0f}), size {g['sizing']['qty']:g}, "
           f"max loss {g['sizing']['max_loss']:,.2f} {g['sizing']['currency']}", symbol, {"decision_id": did})
    res = await EX.open_position(did, now)
    return {"state": "executed" if res.get("ok") else "execution skipped", "decision_id": did, **res}


def _ml_rank(eligible: list[dict]) -> tuple[list[dict], bool]:
    """ML predictions for the eligible setups (shadow: recorded only; active: best probability first). Never adds or
    removes a setup; any ML failure leaves the rules' order untouched."""
    try:
        if ML.cfg()["mode"] == "off":
            return eligible, False
        out, info = ML.rank(eligible)
        return out, info["ranked"]
    except Exception as exc:                              # noqa: BLE001
        db.log("learning", f"ML ranking skipped: {str(exc)[:160]}", level="warn")
        return eligible, False


def _decision(symbol, tf, bar_time, s, stage, verdict, checks) -> int | None:
    try:
        return db.execute("INSERT INTO agent_decisions(t, symbol, tf, bar_time, strategy, direction, stage, verdict, setup, checks) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (db.now(), symbol, tf, bar_time, s["strategy"], s["direction"], stage, verdict,
                           json.dumps({k: v for k, v in s.items() if k != "_why"}, default=str), json.dumps(checks, default=str)))
    except Exception:                                     # noqa: BLE001 - UNIQUE: this exact setup was already decided
        return None


async def after_close(trade_ids: list[int]) -> None:
    for tid in trade_ids:
        try:
            L.review(tid)
            if CFG.agent().get("llm_reviews"):
                await L.llm_narrative(tid)
        except Exception as exc:                          # noqa: BLE001
            db.log("review", f"Review of #{tid} failed: {exc}", level="error")
    if trade_ids:
        for m in G.post_trade_limits():
            db.log("guardian", m, level="warn")
        PF.degradation_check()


async def resolve_untaken(limit: int = 60) -> int:
    """For setups the agent did NOT trade: did price reach T1 or the stop first? (measured from closed candles)."""
    rows = db.q("SELECT * FROM agent_decisions WHERE outcome IS NULL AND trade_id IS NULL AND stage IN "
                "('rejected_guardian','declined_strategy','not_selected','observe','execution_skipped') AND t > ? ORDER BY id DESC LIMIT ?",
                (db.now() - 5 * 86400, limit))
    cache: dict = {}
    n = 0
    for d in rows:
        s = db.jload(d["setup"], {})
        if not s.get("stop") or not s.get("targets"):
            db.execute("UPDATE agent_decisions SET outcome='n/a' WHERE id=?", (d["id"],))
            continue
        k = (d["symbol"], d["tf"])
        if k not in cache:
            try:
                cache[k] = (await D.candles(d["symbol"], d["tf"], 500))[0]
            except Exception:                             # noqa: BLE001
                cache[k] = None
        bars = cache[k]
        if not bars:
            continue
        dr = 1 if d["direction"] == "LONG" else -1
        e, st, t1 = s["entry"], s["stop"], s["targets"][0]
        after = [b for b in bars if b.time > d["bar_time"]]
        out = None
        for i, b in enumerate(after):
            if (b.low <= st) if dr > 0 else (b.high >= st):
                out = ("stop first", -1.0)
                break
            if (b.high >= t1) if dr > 0 else (b.low <= t1):
                out = ("T1 first", abs(t1 - e) / (abs(e - st) or 1))
                break
            if i >= 60:
                out = ("no resolution", 0.0)
                break
        if out:
            db.execute("UPDATE agent_decisions SET outcome=?, outcome_r=? WHERE id=?", (out[0], out[1], d["id"]))
            n += 1
    return n


# ---------------------------------------------------------------- options desk
def options_universe_static(cfg: dict) -> list[str]:
    o = cfg.get("options") or {}
    return list(dict.fromkeys((o.get("index") or []) + (o.get("stocks") or [])))


async def options_universe(cfg: dict, now: float) -> list[str]:
    """Fixed index + stock list, plus (scan_all) the top movers among all NSE F&O stocks, refreshed once per candle."""
    o = cfg.get("options") or {}
    fixed = options_universe_static(cfg)
    if not o.get("scan_all") or not o.get("scan_top"):
        return fixed
    step = TF_SECONDS[cfg["tf"]]
    sc = STATUS.setdefault("scan", {"bucket": None, "movers": [], "error": None, "t": None})
    if sc["bucket"] != int(now // step):
        try:
            mv = await OP.scan_movers(fixed, int(o["scan_top"]))
            sc.update(bucket=int(now // step), movers=[m["underlying"] for m in mv], detail=mv, error=None, t=int(now))
            db.log("scan", "F&O scan: top movers " + ", ".join(f"{m['underlying']} {m['chp']:+.2f}%" for m in mv))
        except Exception as exc:                          # noqa: BLE001 - the fixed list still runs
            sc.update(bucket=int(now // step), error=str(getattr(exc, "detail", exc))[:160], t=int(now))
    return list(dict.fromkeys(fixed + sc["movers"]))


async def process_option_underlying(und: str, tf: str, now: float | None = None) -> dict:
    now = now or time.time()
    cfg, r = CFG.agent(), CFG.risk()
    mode = cfg["mode"]
    spot = OP.spot_symbol(und)
    key = f"OPT:{und}|{tf}"
    step = TF_SECONDS[tf]
    try:
        a = await A.analyze(spot, tf, 600, True, False)
    except D.DataUnavailable as exc:
        STATUS["symbol_state"][key] = {"t": int(now), "state": "DATA UNAVAILABLE", "detail": str(exc)[:200]}
        STATUS["next_due"][key] = now + 120
        db.log("data", f"DATA UNAVAILABLE {spot} {tf} (options desk): {str(exc)[:200]}", spot, level="warn")
        return {"state": "data unavailable"}
    candles = a.get("candles") or []
    bar_time = candles[-1]["time"] if candles else 0
    STATUS["next_due"][key] = bar_time + 2 * step + 5
    q = intel.quality_gate(a.get("quality") or {}, tf, (a.get("quality") or {}).get("candles") or len(candles), now)
    pic = intel.picture(a)
    reg = AG.regime_agent(a, pic)
    db.execute("INSERT OR IGNORE INTO agent_observations(t, symbol, tf, bar_time, regime, volatility, quality_ok, data) VALUES(?,?,?,?,?,?,?,?)",
               (int(now), spot, tf, bar_time, reg["regime"], reg["volatility"], int(q["ok"]),
                json.dumps({"picture": pic, "regime": reg, "structure": AG.structure_agent(a), "options": AG.options_agent(a, pic), "quality": q}, default=str)))
    STATUS["symbol_state"][key] = {"t": int(now), "bar_time": bar_time, "state": "observed", "regime": reg["label"], "volatility": reg["volatility"],
                                   "bias": pic.get("bias"), "quality_ok": q["ok"], "quality_blocks": q["blocks"], "close": pic.get("close"),
                                   "source": q.get("source"), "as_of": q.get("as_of"), "options": True}
    if db.q("SELECT id FROM agent_decisions WHERE symbol=? AND tf=? AND bar_time=? AND strategy LIKE '%+%' LIMIT 1", (spot, tf, bar_time), one=True):
        return {"state": "already processed"}
    eligible, declined = AG.strategy_agent(a, reg)
    structs = (cfg.get("options") or {}).get("structures") or {}
    best = None
    if eligible:
        eligible, _ = _ml_rank(eligible)
        best = eligible[0]
        setup = AG.setup_agent(best, a, pic, reg)
        setup["ml"] = best.get("_ml")
        direction = setup["direction"]
    elif OP.is_index(und) and structs.get("iron_condor") and reg["regime"] in ("range", "choppy"):
        direction = "NEUTRAL"
        b_, br_ = pic.get("bull") or 0, pic.get("bear") or 0
        setup = {"symbol": spot, "market": a["market"], "tf": tf, "bar_time": bar_time, "strategy": "range_condor", "strategy_name": "Range iron condor",
                 "direction": "NEUTRAL", "entry": pic.get("close"), "stop": None, "targets": [], "rr": None, "atr": (a.get("indicators") or {}).get("atr"),
                 "regime": reg["regime"], "regime_label": reg["label"], "volatility": reg["volatility"], "score": round(100 - abs(b_ - br_), 1),
                 "reasons_for": [f"{reg['label']} regime", f"bull {b_:.0f} vs bear {br_:.0f}: no directional edge"], "reasons_against": [],
                 "evidence": {"note": "neutral structure: directional evidence families not used"}, "validation": {"status": "n/a"},
                 "expected_hold": "about 10 candles", "data_source": pic.get("source"), "data_as_of": pic.get("as_of"),
                 "confidence_note": "Neutral range structure; no probability is claimed."}
    else:
        n = len([s for s in a.get("setups") or [] if s.get("time") == bar_time])
        db.log("analysis", f"Options {und}: {reg['label']}, bias {pic.get('bias')}; {n} setup(s) on this candle, none eligible"
               + ("" if q["ok"] else f"; data: {'; '.join(q['blocks'])}"), spot)
        return {"state": "no eligible setup"}
    try:
        ch = await OP.chain(und, 0.5)
        ivs, iv_note = OP.iv_state(ch, (a.get("indicators") or {}).get("hist_vol_pct"), r)
        if (ivs == "high" or direction == "NEUTRAL") and ch["days"] < r["opt_min_dte_sell"]:
            ch = await OP.chain(und, r["opt_min_dte_sell"])
            ivs, iv_note = OP.iv_state(ch, (a.get("indicators") or {}).get("hist_vol_pct"), r)
    except Exception as exc:                              # noqa: BLE001 - no chain, no options decision
        db.log("data", f"Option chain unavailable for {und}: {str(getattr(exc, 'detail', exc))[:160]}", spot, level="warn")
        return {"state": "chain unavailable"}
    lens = OP.seller_lens(ch, r)
    uplan = {"entry": setup.get("entry"), "stop": setup.get("stop")} if direction != "NEUTRAL" else None
    st, why = OP.choose(direction, ch, reg["regime"], ivs, r, OP.is_index(und), lens, uplan)
    base = setup["strategy"]
    if st and not structs.get(st["kind"], True):
        st, why = None, f"{OP.LABEL[st['kind']]} is switched off in Settings"
    if not st:
        _decision(spot, tf, bar_time, {**setup, "strategy": f"{base}+none", "seller_lens": lens}, "declined_strategy", "DECLINED",
                  {"why": f"options: {why}"})
        db.log("analysis", f"Options {und} {direction}: no structure ({why})", spot)
        return {"state": "no structure", "why": why}
    pl = OP.plan(st, r)
    setup = {**setup, "options": {"structure": st, "plan": pl}, "strategy": f"{base}+{st['kind']}",
             "strategy_name": f"{setup.get('strategy_name') or base} → {OP.describe(st)}",
             "reasons_for": list(setup.get("reasons_for") or []) + [iv_note, why, "Seller's view: " + lens["view"] + " - " + "; ".join(lens["reads"])],
             "seller_lens": lens}
    if direction == "NEUTRAL":
        sh = [lg for lg in st["legs"] if lg["side"] == "SELL"]
        setup["invalidation"] = "underlying beyond a short strike (" + " / ".join(f"{lg['strike']:g}{lg['type']}" for lg in sh) + ")"
    ss = "suspended" if "suspended" in (AG.strategy_status(base), AG.strategy_status(setup["strategy"])) else "active"
    ctx = {"quality": q, "picture": pic, "evidence": setup.get("evidence") or {}, "avg_volume": None, "mode": mode, "strategy_status": ss,
           "min_score": RS.thresholds().get(f"{spot}|{tf}|{base}"), "options_plan": {"structure": st, "plan": pl},
           "ml": (best or {}).get("_ml")}
    g = G.evaluate(setup, ctx, now)
    g["quality"] = q
    stage = {"APPROVED": "approved", "REJECTED": "rejected_guardian", "OBSERVE": "observe"}[g["verdict"]]
    did = _decision(spot, tf, bar_time, setup, stage, g["verdict"], g)
    if best:
        ML.record(did, spot, tf, best, best.get("_ml"))
    label = OP.describe(st)
    if g["verdict"] == "REJECTED":
        db.log("guardian", f"REJECTED {label}: " + "; ".join(g["failed"]), spot, {"decision_id": did})
        return {"state": "rejected", "decision_id": did, "failed": g["failed"]}
    if g["verdict"] == "OBSERVE":
        db.log("guardian", f"Intelligence mode: {label} passed all checks - no order", spot, {"decision_id": did})
        return {"state": "observe", "decision_id": did}
    db.log("guardian", f"APPROVED {label}: {g['sizing']['lots']} lots, planned loss {g['sizing']['max_loss']:,.0f} INR, "
           f"worst case {g['sizing']['structural_max_loss']:,.0f} INR", spot, {"decision_id": did})
    res = await EX.open_options(did, now)
    return {"state": "executed" if res.get("ok") else "execution skipped", "decision_id": did, **res}


async def cycle() -> dict:
    STATUS["in_cycle"] = True                             # the observe-only intelligence sweep waits while this runs
    try:
        return await _cycle()
    finally:
        STATUS["in_cycle"] = False


async def _cycle() -> dict:
    now = time.time()
    cfg = CFG.agent()
    out = {"positions_closed": [], "symbols": {}}
    closed = await EX.update_positions()
    out["positions_closed"] = closed
    await after_close(closed)
    if cfg["enabled"] and not CFG.emergency().get("on"):
        for sym in cfg["symbols"]:
            if not market_open(sym, now) or not _due(sym, cfg["tf"], now):
                continue
            try:
                out["symbols"][sym] = await process_symbol(sym, cfg["tf"], now)
            except Exception as exc:                      # noqa: BLE001 - one symbol must not stop the others
                db.log("error", f"{sym}: {type(exc).__name__}: {str(exc)[:200]}", sym, {"trace": traceback.format_exc()[-1500:]}, level="error")
                STATUS["next_due"][f"{sym}|{cfg['tf']}"] = now + 120
            await asyncio.sleep(0.5)
        if (cfg.get("options") or {}).get("enabled") and market_open("NSE:NIFTY50-INDEX", now):
            try:
                unds = await options_universe(cfg, now)
            except Exception:                             # noqa: BLE001
                unds = options_universe_static(cfg)
            for und in unds:
                if not _due(f"OPT:{und}", cfg["tf"], now):
                    continue
                try:
                    out["symbols"][f"OPT:{und}"] = await process_option_underlying(und, cfg["tf"], now)
                except Exception as exc:                  # noqa: BLE001
                    db.log("error", f"options {und}: {type(exc).__name__}: {str(exc)[:200]}", OP.spot_symbol(und),
                           {"trace": traceback.format_exc()[-1500:]}, level="error")
                    STATUS["next_due"][f"OPT:{und}|{cfg['tf']}"] = now + 120
                await asyncio.sleep(0.5)
        try:
            await resolve_untaken()
        except Exception:                                 # noqa: BLE001
            pass
        try:
            if ML.cfg()["mode"] != "off":
                await ML.resolve_live()
        except Exception:                                 # noqa: BLE001
            pass
    try:                                                  # what is failing: refresh every 15 min and right after closes
        if closed or now - STATUS.get("diag_t", 0) >= 900:
            STATUS["diag_t"] = now
            from . import diagnose as DG
            await DG.refresh(now)
    except Exception as exc:                              # noqa: BLE001
        db.log("learning", f"Diagnosis refresh failed: {str(exc)[:160]}", level="warn")
    STATUS.update(last_cycle=int(now), cycles=STATUS["cycles"] + 1)
    return out


async def maybe_research(now: float) -> None:
    cfg = CFG.agent()
    t = dt.datetime.fromtimestamp(now, IST)
    day = t.date().isoformat()
    R = STATUS["research"]
    if not cfg.get("research_enabled") or R["running"] or R["last_day"] == day or t.hour != int(cfg.get("research_hour_ist", 1)):
        return
    R.update(running=True, last_day=day)
    try:
        for sym in cfg["symbols"]:
            await RS.research_symbol(sym, cfg["tf"])
            await asyncio.sleep(2)
        if ML.available()[0]:
            await ML.train(tf=cfg["tf"])                  # a new model is only a candidate: it waits for your approval
    except Exception as exc:                              # noqa: BLE001
        db.log("research", f"Nightly research failed: {exc}", level="error")
    finally:
        R.update(running=False, last=int(time.time()))


async def _loop() -> None:
    STATUS["loop_running"] = True
    await asyncio.sleep(25)                               # let the recorder and the app start first
    try:
        STATUS["reconcile"] = EX.reconcile()
    except Exception as exc:                              # noqa: BLE001
        STATUS["reconcile"] = {"error": str(exc)}
    while True:
        try:
            await cycle()
            asyncio.create_task(maybe_research(time.time()))
            STATUS["last_error"] = None
        except Exception as exc:                          # noqa: BLE001 - automatic recovery: log and continue
            STATUS["last_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            db.log("error", f"Agent cycle failed: {STATUS['last_error']}", data={"trace": traceback.format_exc()[-1500:]}, level="error")
        await asyncio.sleep(max(30, int(CFG.agent().get("loop_seconds", 60))))


def start() -> None:
    global _task
    try:
        CFG.migrate_add_indices()
    except Exception as exc:                              # noqa: BLE001 - never block start-up
        db.log("error", f"Index migration failed: {exc}", level="error")
    try:
        CFG.migrate_capital_10l()
    except Exception as exc:                              # noqa: BLE001 - never block start-up
        db.log("error", f"Capital migration failed: {exc}", level="error")
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())
    from . import sweep
    sweep.start()                                         # observe-only Market Intelligence coverage (no trades)
    from . import straddle as SD
    SD.start()                                            # straddle monitor: one chain snapshot per index per minute (NSE hours)


async def emergency_stop(reason: str) -> dict:
    """Global emergency stop: no new entries anywhere in the agent, every open agent position closed at market,
    and the PC Telegram paper bot paused. Stays on until the user clears it."""
    CFG.set_emergency(True, reason or "manual")
    db.log("emergency", f"EMERGENCY STOP: {reason or 'manual'}", level="error")
    exits = await EX.emergency_exit_all(reason or "manual")
    try:
        from backend.bot import service as BOT
        BOT.ST["paused"] = True
        BOT.save_state()
        bot = "Telegram paper bot paused"
    except Exception as exc:                              # noqa: BLE001
        bot = f"bot not paused: {exc}"
    await after_close([e["id"] for e in exits if e.get("ok")])
    return {"emergency": CFG.emergency(), "exits": exits, "bot": bot}


def clear_emergency(who: str = "user") -> dict:
    CFG.set_emergency(False, f"cleared by {who}")
    db.log("emergency", f"Emergency stop cleared by {who}")
    return CFG.emergency()


def status() -> dict:
    cfg, r = CFG.agent(), CFG.risk()
    em, halt = CFG.emergency(), G.halt_state()
    open_n = len(db.q("SELECT id FROM agent_trades WHERE status='open'"))
    state = "EMERGENCY STOP" if em.get("on") else "HALTED (drawdown)" if halt.get("on") else "RUNNING" if cfg["enabled"] else "STOPPED"
    return {"state": state, "mode": cfg["mode"], "config": cfg, "risk": r, "emergency": em, "halt": halt,
            "loop": {k: STATUS[k] for k in ("loop_running", "last_cycle", "last_error", "cycles", "reconcile")},
            "symbols": STATUS["symbol_state"], "next_due": STATUS["next_due"], "open_positions": open_n,
            "monitor_gaps": EX.MONITOR, "research": STATUS["research"], "live": CFG.live_readiness(), "paper_only": True,
            "options": {"config": cfg.get("options"), "scan": {k: v for k, v in (STATUS.get("scan") or {}).items() if k != "bucket"}},
            "rules": _rules_state(r)}


def _rules_state(r: dict) -> dict:
    out = {}
    for cur in ("INR", "USD"):
        bk = G.book(cur)
        b = G.budgets(bk["equity"], r, bk["loss_streak"], cur, bk)
        ai = b.pop("ai", None)
        orisk = G.open_risk(cur)
        used = max(0.0, -bk["day_pnl"])
        closed = G.day_closed(cur, bk, r)
        cap_used = G.capital_in_use(cur) if cur == "INR" else None
        out[cur] = {**{k: round(v, 2) if isinstance(v, float) else v for k, v in b.items()}, "open_risk": round(orisk, 2),
                    "day_loss_used": round(used, 2), "room": round(max(0.0, b["part_budget"] - orisk - used), 2), "day_pnl": round(bk["day_pnl"], 2),
                    "losses_today": bk["losses_today"], "day_closed": closed,
                    "capital_used": round(cap_used, 2) if cap_used is not None else None,
                    "ai_factors": (ai or {}).get("factors"),
                    "can_open": not closed and max(0.0, b["part_budget"] - orisk - used) >= b["per_trade"] - 1e-6}
    return out
