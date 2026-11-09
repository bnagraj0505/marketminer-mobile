"""/api/agent/* - AI agent API. Reads are open to the local network like the rest of MarketMiner; every WRITE
(start/stop, mode, config, emergency stop, manual close, research, approvals) is accepted from this PC only.
There is no endpoint that can enable live trading."""
from __future__ import annotations
import time

import asyncio

from fastapi import APIRouter, Body, HTTPException, Query, Request

from . import config as CFG
from . import db
from . import execution as EX
from . import guardian as G
from . import performance as PF
from . import research as RS
from . import service as SV

router = APIRouter(prefix="/api/agent", tags=["ai-agent"])
LOCAL = ("127.0.0.1", "::1", "localhost", "testclient")


def _local(req: Request) -> None:
    if (req.client.host if req.client else "") not in LOCAL:
        raise HTTPException(403, "Agent write actions are allowed from this PC only")


def _rows(sql: str, args: tuple = (), json_cols: tuple = ()) -> list[dict]:
    out = db.q(sql, args)
    for r in out:
        for c in json_cols:
            r[c] = db.jload(r.get(c))
    return out


@router.get("/status")
async def status():
    return SV.status()


@router.post("/start")
async def start(req: Request):
    _local(req)
    if CFG.emergency().get("on"):
        raise HTTPException(409, "Emergency stop is on: clear it first")
    CFG.save({"enabled": True})
    db.log("control", "Agent started by user")
    return SV.status()


@router.post("/stop")
async def stop(req: Request):
    _local(req)
    CFG.save({"enabled": False})
    db.log("control", "Agent stopped by user (open positions keep being managed)")
    return SV.status()


@router.post("/mode")
async def mode(req: Request, body: dict = Body(...)):
    _local(req)
    try:
        CFG.save({"mode": body.get("mode")})
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    db.log("control", f"Mode set to {body.get('mode')} by user")
    return SV.status()


@router.post("/config")
async def config(req: Request, body: dict = Body(...)):
    _local(req)
    try:
        return CFG.save(body.get("agent"), body.get("risk"))
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.post("/run")
async def run_now(req: Request):
    """One full cycle now (all due symbols), e.g. to test the pipeline."""
    _local(req)
    SV.STATUS["next_due"].clear()
    return await SV.cycle()


@router.post("/emergency-stop")
async def emergency(req: Request, body: dict = Body(default={})):
    _local(req)
    return await SV.emergency_stop(str(body.get("reason") or "manual"))


@router.post("/emergency-clear")
async def emergency_clear(req: Request):
    _local(req)
    return SV.clear_emergency()


@router.post("/halt-clear")
async def halt_clear(req: Request):
    _local(req)
    db.log("control", "Drawdown halt cleared by user")
    return G.set_halt(False, "cleared by user")


@router.get("/opportunities")
async def opportunities(limit: int = Query(60, ge=1, le=500), stage: str | None = None):
    sql = "SELECT * FROM agent_decisions" + (" WHERE stage=?" if stage else "") + " ORDER BY id DESC LIMIT ?"
    return _rows(sql, ((stage, limit) if stage else (limit,)), ("setup", "checks"))


@router.get("/decisions/{did}")
async def decision(did: int):
    r = _rows("SELECT * FROM agent_decisions WHERE id=?", (did,), ("setup", "checks"))
    if not r:
        raise HTTPException(404, "not found")
    return r[0]


@router.get("/positions")
async def positions():
    rows = _rows("SELECT * FROM agent_trades WHERE status='open' ORDER BY id DESC", (), ("payload", "targets"))
    last = {}
    for r in rows:
        st = r["payload"] or {}
        if st.get("kind") == "options":                   # valued every minute from leg quotes by the monitor
            v = st.get("value_now")
            r["structure"], r["legs"] = st.get("label"), st.get("legs")
            r["price"], r["price_source"], r["price_as_of"] = st.get("und_now"), "FYERS quotes", None
            r["value_now"], r["stop_now"], r["take_profit"] = v, st.get("sl"), st.get("tp")
            if v is not None:
                r["unrealised"] = round((v - st["v0"]) * r["qty"], 2)
                r["unrealised_r"] = round((v - st["v0"]) / (st.get("planned_unit") or 1), 2)
            r["lots_left"] = st.get("lots_left")
            r["exit_hint"] = (f"Lot 1 booked; lot 2 stop {st.get('sl'):,.2f} trailing {st.get('trail_unit', 0):,.2f} behind the best value" if st.get("parts") else
                              f"Stop at value {st.get('sl'):,.2f}; lot 1 books at {st.get('tp1', st.get('tp')):,.2f} (1:2); closes 15:15 IST")
            r["monitor_gap"] = EX.MONITOR.get(r["id"])
            continue
        try:
            if r["symbol"] not in last:
                bars, q = await EX._price(r["symbol"], r["market"], 3)
                last[r["symbol"]] = (bars[-1].close, q.get("source"), bars[-1].time + 60)
            px, src, at = last[r["symbol"]]
            d = 1 if r["side"] == "LONG" else -1
            st = r["payload"] or {}
            r["price"], r["price_source"], r["price_as_of"] = px, src, at
            r["unrealised"] = round(st.get("realised", 0) * r["qty"] + st.get("left", 1) * (px - r["entry"]) * d * r["qty"], 2)
            r["unrealised_r"] = round(r["unrealised"] / (abs(r["entry"] - r["stop"]) * r["qty"] or 1), 2)
            r["stop_now"] = st.get("stop_now")
            r["exit_hint"] = ("Near stop" if abs(px - st.get("stop_now", r["stop"])) < 0.25 * abs(r["entry"] - r["stop"]) else
                              "Lot 1 booked at 1:2: lot 2 at breakeven, trailing" if st.get("hit") else "Hold: plan unchanged")
        except Exception as exc:                          # noqa: BLE001
            r["price"], r["price_error"] = None, f"price unavailable: {str(exc)[:100]}"
        r["monitor_gap"] = EX.MONITOR.get(r["id"])
    return rows


@router.post("/positions/{tid}/close")
async def close(tid: int, req: Request):
    _local(req)
    r = await EX.close_position(tid, "manual close (user)")
    if r.get("ok"):
        await SV.after_close([tid])
    return r


@router.get("/trades")
async def trades(limit: int = Query(200, ge=1, le=1000)):
    return _rows("SELECT * FROM agent_trades ORDER BY id DESC LIMIT ?", (limit,), ("payload", "targets"))


@router.get("/performance")
async def performance():
    return PF.summary("paper")


@router.get("/learning")
async def learning():
    reviews = _rows("SELECT * FROM agent_reviews ORDER BY id DESC LIMIT 100", (), ("answers",))
    cls: dict = {}
    for r in db.q("SELECT classification, COUNT(*) n FROM agent_reviews GROUP BY classification"):
        cls[r["classification"]] = r["n"]
    untaken = db.q("SELECT stage, outcome, COUNT(*) n, AVG(outcome_r) r FROM agent_decisions WHERE outcome IS NOT NULL AND outcome!='n/a' GROUP BY stage, outcome")
    return {"reviews": reviews, "classifications": cls, "untaken_outcomes": untaken,
            "strategies": db.q("SELECT * FROM agent_strategy_status ORDER BY strategy"),
            "experiments": _rows("SELECT * FROM agent_experiments ORDER BY id DESC LIMIT 100", (), ("params", "verdict")),
            "alerts": db.q("SELECT * FROM agent_log WHERE kind IN ('learning','research') ORDER BY id DESC LIMIT 50"),
            "thresholds": RS.thresholds(),
            "history": db.q("SELECT id, t, who, key, before, after FROM agent_config_history ORDER BY id DESC LIMIT 50")}


@router.get("/diagnosis")
async def diagnosis(fresh: bool = False):
    """Where the agent is failing: segments, veto audit, exits, data health, ML/research, learned filters, suggestions."""
    from . import diagnose as DG
    if fresh:
        await DG.refresh()
    return DG.report()


@router.post("/diagnosis/settings")
async def diagnosis_settings(req: Request, body: dict = Body(...)):
    _local(req)
    from . import diagnose as DG
    before = DG.settings()
    after = DG.save_settings(body)
    db.config_change("user", "agent_diagnosis", before, after)
    return DG.report(force=True)


@router.post("/diagnosis/filters/lift")
async def diagnosis_lift(req: Request, body: dict = Body(...)):
    _local(req)
    from . import diagnose as DG
    r = DG.lift(str(body.get("id") or ""))
    if not r.get("ok"):
        raise HTTPException(404, r.get("error"))
    return DG.report(force=True)


@router.get("/experiments/{eid}")
async def experiment(eid: int):
    r = _rows("SELECT * FROM agent_experiments WHERE id=?", (eid,), ("params", "result", "verdict"))
    if not r:
        raise HTTPException(404, "not found")
    return r[0]


@router.post("/strategies/{key}")
async def strategy_status(key: str, req: Request, body: dict = Body(...)):
    _local(req)
    try:
        return PF.set_strategy_status(key, body.get("status"), str(body.get("reason") or "set by user"))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.post("/research/run")
async def research_run(req: Request, body: dict = Body(default={})):
    """Run research now in the background for one symbol (or the whole watch-list). Results arrive as experiments."""
    _local(req)
    cfg = CFG.agent()
    syms = [body["symbol"]] if body.get("symbol") else cfg["symbols"]
    tf = body.get("tf") or cfg["tf"]
    if SV.STATUS["research"]["running"]:
        raise HTTPException(409, "research is already running")

    async def go():
        SV.STATUS["research"]["running"] = True
        try:
            for s in syms:
                await RS.research_symbol(s, tf)
        except Exception as exc:                          # noqa: BLE001
            db.log("research", f"Research failed: {exc}", level="error")
        finally:
            SV.STATUS["research"].update(running=False, last=db.now())
    asyncio.create_task(go())
    return {"started": True, "symbols": syms, "tf": tf}


@router.post("/research/{eid}/approve")
async def approve(eid: int, req: Request):
    _local(req)
    try:
        return RS.approve(eid, "user")
    except PermissionError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.post("/research/{eid}/reject")
async def reject(eid: int, req: Request):
    _local(req)
    db.execute("UPDATE agent_experiments SET status='rejected_by_user' WHERE id=? AND status='eligible'", (eid,))
    return {"ok": True}


@router.post("/rollback/{hid}")
async def rollback(hid: int, req: Request):
    _local(req)
    try:
        return RS.rollback(hid, "user")
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.get("/log")
async def log(limit: int = Query(200, ge=1, le=2000), kind: str | None = None, since_id: int = 0):
    sql = "SELECT * FROM agent_log WHERE id>?" + (" AND kind=?" if kind else "") + " ORDER BY id DESC LIMIT ?"
    return _rows(sql, ((since_id, kind, limit) if kind else (since_id, limit)), ("data",))


@router.get("/observations/latest")
async def observations_latest(days: float = Query(4, ge=0.1, le=14)):
    """Market Intelligence overview: the newest observation of EVERY symbol (trading list, options desk and the observe-only
    sweep), compacted to one row each. Full detail for one symbol: /observations?symbol=...&limit=1"""
    from . import sweep as SW
    tf = CFG.agent()["tf"]
    since = int(time.time() - days * 86400)
    rows = db.q("SELECT o.* FROM agent_observations o JOIN (SELECT symbol, MAX(id) AS mid FROM agent_observations "
                "WHERE tf=? AND t>=? GROUP BY symbol) m ON o.id=m.mid ORDER BY o.symbol", (tf, since))
    out = []
    for r in rows:
        d = db.jload(r.get("data")) or {}
        p, rg, q, op = d.get("picture") or {}, d.get("regime") or {}, d.get("quality") or {}, d.get("options") or {}
        mo, tr, v = p.get("momentum") or {}, p.get("trend") or {}, p.get("volatility_detail") or {}
        out.append({"symbol": r["symbol"], "tf": r["tf"], "t": r["t"], "bar_time": r["bar_time"] if (r["bar_time"] or 0) > 0 else None,
                    "group": d.get("group") or SW.group_of(r["symbol"]), "observe_only": bool(d.get("observe_only")),
                    "unavailable": d.get("unavailable"), "regime": rg.get("label") or r.get("regime"), "volatility": p.get("volatility"),
                    "bias": p.get("bias"), "bull": p.get("bull"), "bear": p.get("bear"), "close": p.get("close"), "source": p.get("source"),
                    "rsi": mo.get("rsi"), "adx": (tr.get("adx") or {}).get("adx"), "atr_pct": v.get("atr_pct"),
                    "quality_ok": bool(q.get("ok")), "quality_blocks": q.get("blocks") or [],
                    "options": (op.get("reads") or []) if op.get("available") else [],
                    "has_chain": bool(op.get("chain")), "has_perp": bool(op.get("perp"))})
    return {"tf": tf, "rows": out, "sweep": {k: v for k, v in SW.STATUS.items()}}


@router.get("/observations")
async def observations(symbol: str | None = None, limit: int = Query(20, ge=1, le=200)):
    sql = "SELECT * FROM agent_observations" + (" WHERE symbol=?" if symbol else "") + " ORDER BY id DESC LIMIT ?"
    return _rows(sql, ((symbol, limit) if symbol else (limit,)), ("data",))


@router.get("/straddle")
async def straddle(market: str = Query("india", pattern="^(india|crypto)$")):
    """Straddle monitor: options-desk indices (market=india, FYERS) or BTC / ETH (market=crypto, Delta Exchange):
    current ATM straddle, its centre and how both have shifted."""
    if market == "crypto":
        from . import cstraddle as CS
        return CS.report()
    from . import straddle as SD
    return SD.report()


_last_refresh = {"t": 0.0}


@router.post("/straddle/refresh")
async def straddle_refresh(req: Request, market: str = Query("india", pattern="^(india|crypto)$")):
    """Take a snapshot now (at most every 20 s, to protect the FYERS limits)."""
    import time as _t
    from . import straddle as SD
    _local(req)
    if market == "crypto":
        from . import cstraddle as CS
        if _t.time() - (CS.STATUS.get("last") or 0) >= 20:
            await CS.run_once()
        return CS.report()
    if _t.time() - _last_refresh["t"] < 20:
        return SD.report()
    _last_refresh["t"] = _t.time()
    await SD.run_once()
    return SD.report()


@router.get("/retrace")
async def retrace(market: str = Query("india", pattern="^(india|crypto)$")):
    """Retracement or reversal (market=india: options-desk indices; market=crypto: BTC, ETH), 5m / 15m with levels from today only, 1h trend context (cached 60 s)."""
    from . import retrace as RT
    return await RT.report(market)


@router.get("/pbook")
async def pbook():
    """Index level: is today's pullback profit booking or fresh selling (price depth + option writers' OI since the extreme)?"""
    from . import pbook as PB
    return await PB.report()


@router.get("/bookings")
async def bookings(limit: int = Query(100, ge=1, le=500)):
    """Profit-booking tracker: lot 1 at 1:2, lot 2 breakeven + trail, per position, with a summary."""
    from . import bookings as BK
    return BK.report(limit)


# ---------------------------------------------------------------- ML layer
@router.get("/ml")
async def ml_status():
    from . import ml as ML
    return ML.status()


@router.post("/ml/train")
async def ml_train(req: Request, body: dict = Body(default={})):
    """Rebuild the training set from Signal Lab history and train (background). The result is a model to review."""
    from . import ml as ML
    _local(req)
    ok, why = ML.available()
    if not ok:
        raise HTTPException(409, why)
    if ML.STATE["training"]:
        raise HTTPException(409, "training is already running")
    syms = [str(x).strip().upper() for x in (body.get("symbols") or []) if str(x).strip()] or None
    asyncio.create_task(ML.train(syms, body.get("tf")))
    return {"started": True, "symbols": syms or ML.training_symbols()}


@router.post("/ml/mode")
async def ml_mode(req: Request, body: dict = Body(...)):
    from . import ml as ML
    _local(req)
    try:
        return ML.set_mode(str(body.get("mode")))
    except PermissionError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.post("/ml/models/{mid}/approve")
async def ml_approve(mid: int, req: Request):
    from . import ml as ML
    _local(req)
    try:
        return ML.approve(mid)
    except PermissionError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.post("/ml/models/{mid}/reject")
async def ml_reject(mid: int, req: Request):
    from . import ml as ML
    _local(req)
    return ML.reject(mid)


# ---------------------------------------------------------------- strategy workbench
@router.get("/workbench")
async def workbench():
    from backend.lab import custom as CU, mlfeat as MF, regime as RG
    from backend.lab import strategies as ST
    CU.ensure()
    specs = CU.all_specs()
    bts = {}
    for r in db.q("SELECT id, created, symbol, tf, params, result FROM backtests ORDER BY id DESC LIMIT 200"):
        prm = db.jload(r["params"], {})
        k = prm.get("strategy")
        if k and k.startswith("custom_") and k not in bts:
            res = db.jload(r["result"], {})
            bts[k] = {"id": r["id"], "created": r["created"], "symbol": r["symbol"], "tf": r["tf"], "overall": res.get("overall"),
                      "validation": res.get("validation"), "test": res.get("test"), "status": (res.get("validation_status") or {}).get(k)}
    return {"fields": [{"key": f, "describe": MF.DESCRIBE.get(f, f)} for f in CU.RULE_FIELDS], "ops": list(CU.OPS),
            "regimes": [{"key": k, "label": RG.LABEL.get(k, k)} for k in RG.SUITS], "strategies": specs, "backtests": bts,
            "builtin": [{"key": k, "name": v[0], "description": v[1]} for k, v in ST.META.items() if not k.startswith("custom_")]}


@router.post("/workbench/save")
async def workbench_save(req: Request, body: dict = Body(...)):
    from backend.lab import custom as CU
    _local(req)
    try:
        spec = CU.validate({**body, "status": "draft"})   # any edit goes back to draft: re-test, then approve again
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc))
    specs = [s for s in CU.all_specs() if s["key"] != spec["key"]] + [spec]
    before = next((s for s in CU.all_specs() if s["key"] == spec["key"]), None)
    CU.save_all(specs)
    db.config_change("user", f"strategy:{spec['key']}", before, spec)
    db.log("research", f"Workbench: strategy {spec['name']} saved as draft")
    return spec


@router.post("/workbench/{key}/status")
async def workbench_status(key: str, req: Request, body: dict = Body(...)):
    from backend.lab import custom as CU
    _local(req)
    st = body.get("status")
    if st not in ("draft", "approved"):
        raise HTTPException(422, "status must be draft or approved")
    specs = CU.all_specs()
    sp = next((s for s in specs if s["key"] == key), None)
    if not sp:
        raise HTTPException(404, "no such strategy")
    before = dict(sp)
    sp["status"] = st
    CU.save_all(specs)
    db.config_change("user", f"strategy:{key}", before, sp)
    db.log("research", f"Workbench: {sp['name']} -> {st}" + (" (the agent may now trade it, through the ML filter and the Risk Guardian)" if st == "approved" else ""))
    return sp


@router.post("/workbench/{key}/delete")
async def workbench_delete(key: str, req: Request):
    from backend.lab import custom as CU
    _local(req)
    specs = CU.all_specs()
    sp = next((s for s in specs if s["key"] == key), None)
    CU.save_all([s for s in specs if s["key"] != key])
    db.config_change("user", f"strategy:{key}", sp, None)
    return {"ok": True}


@router.post("/workbench/backtest")
async def workbench_backtest(req: Request, body: dict = Body(...)):
    """Signal Lab backtest of one strategy (built-in or yours) on one symbol: 60/20/20 split, walk-forward, Monte Carlo."""
    from backend.lab import custom as CU, service as LS
    _local(req)
    CU.ensure()
    try:
        res = await LS.backtest({"symbol": body.get("symbol"), "tf": body.get("tf") or "15m", "strategy": body.get("key"), "bars": body.get("bars") or 3000})
    except Exception as exc:                              # noqa: BLE001
        raise HTTPException(getattr(exc, "status", None) or getattr(exc, "status_code", None) or 422, str(getattr(exc, "detail", exc))[:300])
    return {k: res.get(k) for k in ("id", "symbol", "tf", "candles", "from", "to", "overall", "train", "validation", "test", "walk_forward",
                                     "monte_carlo", "validation_status", "by_regime", "by_side", "by_hour", "notes", "params")}


@router.get("/live-readiness")
async def live_readiness():
    return CFG.live_readiness()
