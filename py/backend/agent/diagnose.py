"""Where the agent is failing, and what it has learned. PAPER ONLY: it reads the agent's memory and, at most, adds
"learned filters" that make the agent MORE selective. It never adds trades, sizes up or loosens a risk rule; anything
that would loosen risk is written as a suggestion for you to approve.

Evidence used (all of it, not just the few closed trades)
  * closed agent trades: R after costs, regime, session, MFE / MAE, exit reason, entry slippage, stop-fill slippage,
    and (refresh) whether price reached T1 after a stop-out (stop too tight)
  * every setup NOT traded (declined by a strategy rule, rejected by the Risk Guardian, not selected, blocked by a
    learned filter): did price reach T1 or the stop first ("shadow" outcome, measured by service.resolve_untaken)
  * the Risk Guardian checks that failed and the decline reasons -> is each veto protecting money or costing it?
  * option setups blocked because one lot risks more than the AI's per-trade risk
  * data health: data gaps, FYERS daily-quota blackouts, errors, how many candles were analysed
  * the ML model's training attempts (which promotion gates failed) and the research engine's experiments

Honest scoring of shadow outcomes (they are estimates, not trades)
  stop first -> -1R - haircut;  T1 first -> min(R to T1, SHADOW_CAP) - haircut   (haircut = costs, default 0.10R)
  Trades count with their real R after costs. Every segment gets a 95% interval (mean R: t-interval; win rate:
  Wilson). A segment is a RELIABLE weakness only when it has >= min_n outcomes and the whole interval is below 0
  (a reliable strength: whole interval above 0). Smaller or mixed evidence is shown as "leaning", never acted on.

Learned filters (auto, can be switched off)
  * added for a reliable weakness in a dimension the guardian can see at decision time (strategy, options
    structure, market type, direction, session, regime, symbol, and strategy x market / direction / session / regime)
  * never more than max_filters, and never a filter that would have covered more than max_cover of the last 14 days'
    setups (the agent must not block itself into doing nothing)
  * blocked setups keep being tracked; a filter is LIFTED when its blocked setups would have made money (>= 5,
    mean > 0), or after lift_days (a fresh trial). A lifted filter returns only on >= min_n/2 NEW losing outcomes.
  * you can lift any filter yourself.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import time

from backend.lab import store

from . import db

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
CRYPTO = ("SHARK", "DELTAF", "DELTA", "BINANCE")
DEFAULTS = {"auto_filters": True, "min_n": 8, "cost_haircut_r": 0.10, "shadow_cap_r": 2.0, "lift_days": 14, "max_filters": 6,
            "max_cover": 0.4, "window_days": 45}
FILTER_DIMS = ("strategy", "structure", "market", "direction", "session", "regime", "symbol",
               "strategy×market", "strategy×direction", "strategy×session", "strategy×regime")
DIM_LABEL = {"strategy": "Strategy", "structure": "Options structure", "market": "Market type", "direction": "Direction",
             "session": "Session", "regime": "Regime", "symbol": "Symbol", "score": "Score band", "evidence": "Evidence agreeing",
             "weekday": "Weekday", "stage": "What the agent did", "strategy×market": "Strategy × market",
             "strategy×direction": "Strategy × direction", "strategy×session": "Strategy × session", "strategy×regime": "Strategy × regime"}
REPEAT_BARS = 4                    # a setup re-flagged within 4 candles (same symbol, strategy, direction) is the same idea
_TFS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}
SHADOW_STAGES = ("rejected_guardian", "declined_strategy", "not_selected", "observe", "execution_skipped")
_CACHE: dict = {"t": 0, "rep": None}


def settings() -> dict:
    return {**DEFAULTS, **(store.get_setting("agent_diagnosis") or {})}


def save_settings(patch: dict) -> dict:
    c = settings()
    for k, v in (patch or {}).items():
        if k in DEFAULTS:
            c[k] = type(DEFAULTS[k])(v) if not isinstance(DEFAULTS[k], bool) else bool(v)
    c["min_n"] = max(4, min(50, int(c["min_n"])))
    c["max_filters"] = max(0, min(20, int(c["max_filters"])))
    c["max_cover"] = max(0.05, min(0.8, float(c["max_cover"])))
    c["cost_haircut_r"] = max(0.0, min(1.0, float(c["cost_haircut_r"])))
    c["lift_days"] = max(3, min(60, int(c["lift_days"])))
    store.set_setting("agent_diagnosis", {k: c[k] for k in DEFAULTS})
    _CACHE["t"] = 0
    return c


# ---------------------------------------------------------------- segment keys (same function at decision time and in review)
def market_class(symbol: str, market: str | None = None) -> str:
    s = (symbol or "").upper()
    head = s.split(":", 1)[0]
    if head in CRYPTO:
        return "crypto"
    if head == "YF":
        return "US / global"
    if head == "MCX" or market == "MCX":
        return "MCX"
    if s.endswith("-INDEX"):
        return "index options"
    return "stock" if head in ("NSE", "BSE") else (market or head or "?").lower()


def session_of(symbol: str, t: float | None) -> str:
    if not t:
        return "?"
    head = (symbol or "").upper().split(":", 1)[0]
    if head in CRYPTO or head == "YF":
        h = dt.datetime.fromtimestamp(t, dt.timezone.utc).hour
        return "asia (00-07 UTC)" if h < 7 else "europe (07-13 UTC)" if h < 13 else "us (13-21 UTC)" if h < 21 else "late (21-24 UTC)"
    m = dt.datetime.fromtimestamp(t, IST)
    mm = m.hour * 60 + m.minute
    return "open 09:15-10:30" if mm < 630 else "midday 10:30-13:30" if mm < 810 else "late 13:30-close"


def keys(strategy: str, symbol: str, market: str | None, direction: str, t: float | None, regime: str | None) -> dict:
    base, _, struct = (strategy or "?").partition("+")
    k = {"strategy": base, "structure": struct or "direct", "market": market_class(symbol, market), "direction": direction or "?",
         "session": session_of(symbol, t), "regime": regime or "?", "symbol": symbol or "?"}
    for d in ("market", "direction", "session", "regime"):
        k[f"strategy×{d}"] = f"{base} · {k[d]}"
    return k


def _band(score) -> str:
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "?"
    return "<60" if s < 60 else "60-69" if s < 70 else "70-79" if s < 80 else "80+"


# ---------------------------------------------------------------- statistics
_T95 = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31, 9: 2.26, 10: 2.23, 12: 2.18, 15: 2.13, 20: 2.09, 30: 2.04}


def _t(df: int) -> float:
    if df >= 30:
        return 1.96 if df >= 120 else 2.0
    return _T95.get(df) or _T95[max(k for k in _T95 if k <= df)]


def wilson(w: int, n: int) -> tuple[float, float]:
    if not n:
        return 0.0, 1.0
    z, p = 1.96, w / n
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, c - h), min(1.0, c + h)


def summarize(rs: list[float]) -> dict:
    n = len(rs)
    if not n:
        return {"n": 0}
    mean = sum(rs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in rs) / (n - 1)) if n > 1 else float("inf")
    half = _t(n - 1) * sd / math.sqrt(n) if n > 1 else float("inf")
    w = sum(1 for x in rs if x > 0)
    lo, hi = wilson(w, n)
    gw, gl = sum(x for x in rs if x > 0), -sum(x for x in rs if x <= 0)
    return {"n": n, "wins": w, "win_rate": round(100 * w / n, 1), "win_lo": round(100 * lo, 1), "win_hi": round(100 * hi, 1),
            "mean_r": round(mean, 3), "ci_lo": round(mean - half, 3) if n > 1 else None, "ci_hi": round(mean + half, 3) if n > 1 else None,
            "total_r": round(sum(rs), 2), "pf": round(gw / gl, 2) if gl else None}


def grade(st: dict, min_n: int) -> str:
    if not st.get("n"):
        return "no data"
    if st["n"] >= min_n and st.get("ci_hi") is not None and st["ci_hi"] < 0:
        return "reliable weakness"
    if st["n"] >= min_n and st.get("ci_lo") is not None and st["ci_lo"] > 0:
        return "reliable strength"
    if st["n"] >= 4 and st["mean_r"] <= -0.25:
        return "leaning weak"
    if st["n"] >= 4 and st["mean_r"] >= 0.25:
        return "leaning strong"
    return "unclear"


# ---------------------------------------------------------------- samples: trades + shadow outcomes
def _failed(checks: dict | None) -> list[str]:
    if not isinstance(checks, dict):
        return []
    out = []
    for c in checks.get("checks") or []:
        if not isinstance(c, dict) or c.get("ok", True) or not c.get("hard", True):
            continue
        name, det = c.get("check") or "?", (c.get("detail") or "").lower()
        if name == "Position size":                       # the rule changed over time: keep its versions apart
            name = ("Position size: one lot above AI risk" if "one lot" in det and "ai's" in det else
                    "Position size: old always-2-lots rule" if "always 2 lots" in det else
                    "Position size: no prices for every leg" if "no executable prices" in det else "Position size: other")
        out.append(name)
    return out


def _why_class(why: str) -> str:
    w = (why or "").lower()
    if "does not suit" in w:
        return "regime mismatch: " + (w.split("suit the", 1)[-1].replace("regime", "").strip() or "?")
    if "writers are positioned against" in w:
        return "options: writers positioned against"
    if "wall" in w:
        return "options: writer wall in the way"
    if "switched off" in w:
        return "options: structure switched off"
    if w.startswith("options:"):
        return "options: " + w[8:60].strip()
    if "lower" in w and "than" in w:
        return "not selected (another setup ranked higher)"
    return (why or "other")[:60]


def build_samples(trades: list[dict], decisions: list[dict], cfg: dict) -> list[dict]:
    hc, cap = cfg["cost_haircut_r"], cfg["shadow_cap_r"]
    dec_by_id = {d["id"]: d for d in decisions}
    out = []
    for t in trades:
        if t.get("status") != "closed" or t.get("r_result") is None:
            continue
        d = dec_by_id.get(t.get("decision_id")) or {}
        s = db.jload(d.get("setup"), {}) if d else {}
        k = keys(t["strategy"], t["symbol"], t.get("market"), t["side"] if t["side"] in ("LONG", "SHORT") else (d.get("direction") or t["side"]),
                 t.get("opened_at"), t.get("regime") or s.get("regime"))
        k.update(score=_band(s.get("score")), evidence=str((s.get("evidence") or {}).get("independent_confirmations", "?")),
                 weekday=dt.datetime.fromtimestamp(t["opened_at"], IST).strftime("%a"), stage="traded")
        out.append({"kind": "trade", "id": t["id"], "t": t["closed_at"] or t["opened_at"], "r": float(t["r_result"]), "keys": k})
    last_seen: dict = {}
    for d in sorted(decisions, key=lambda x: x.get("bar_time") or x["t"]):
        if d.get("trade_id") or d.get("stage") not in SHADOW_STAGES or d.get("outcome") not in ("T1 first", "stop first"):
            continue
        s = db.jload(d.get("setup"), {})
        r = -1.0 - hc if d["outcome"] == "stop first" else min(float(d.get("outcome_r") or 1.0), cap) - hc
        k = keys(d["strategy"], d["symbol"], s.get("market"), d["direction"], d.get("bar_time") or d["t"], s.get("regime"))
        ch = db.jload(d.get("checks"), {})
        k.update(score=_band(s.get("score")), evidence=str((s.get("evidence") or {}).get("independent_confirmations", "?")),
                 weekday=dt.datetime.fromtimestamp(d.get("bar_time") or d["t"], IST).strftime("%a"), stage=d["stage"])
        bt = d.get("bar_time") or d["t"]
        cl = (d["symbol"], k["strategy"], d["direction"])
        prev = last_seen.get(cl)
        if prev is not None and bt - prev[0] <= REPEAT_BARS * _TFS.get(d.get("tf") or "15m", 900):
            prev[1]["repeats"] = prev[1].get("repeats", 0) + 1     # the same idea re-flagged on the next candles: one outcome, not many
            last_seen[cl] = (bt, prev[1])
            continue
        smp = {"kind": "shadow", "id": d["id"], "t": d["t"], "r": r, "keys": k, "failed": _failed(ch),
               "why": _why_class(ch.get("why", "")) if isinstance(ch, dict) and d["stage"] != "rejected_guardian" else None}
        last_seen[cl] = (bt, smp)
        out.append(smp)
    return out


# ---------------------------------------------------------------- sections
def segments(samples: list[dict], cfg: dict) -> dict:
    dims = list(FILTER_DIMS) + ["score", "evidence", "weekday", "stage"]
    out = {}
    for dim in dims:
        g: dict = {}
        for s in samples:
            g.setdefault(s["keys"].get(dim, "?"), []).append(s)
        rows = []
        for v, ss in g.items():
            st = summarize([x["r"] for x in ss])
            st.update(value=v, trades=sum(1 for x in ss if x["kind"] == "trade"), shadow=sum(1 for x in ss if x["kind"] == "shadow"),
                      grade=grade(st, cfg["min_n"]))
            rows.append(st)
        out[dim] = sorted(rows, key=lambda r: r["mean_r"])
    return out


def veto_audit(samples: list[dict], cfg: dict) -> list[dict]:
    """Each guardian check / decline reason: if the agent had taken those setups, would it have made money?"""
    g: dict = {}
    for s in samples:
        if s["kind"] != "shadow":
            continue
        for name in (s.get("failed") or []):
            g.setdefault(("Risk Guardian", name), []).append(s["r"])
        if s.get("why"):
            g.setdefault(("Strategy rule", s["why"]), []).append(s["r"])
    out = []
    for (who, name), rs in g.items():
        st = summarize(rs)
        gr = grade(st, cfg["min_n"])
        verdict = ("protecting you" if gr in ("reliable weakness", "leaning weak") else
                   "costing trades" if gr in ("reliable strength", "leaning strong") else "unclear yet")
        if "old " in name:
            verdict = "retired rule"
        out.append({"by": who, "check": name, **st, "grade": gr, "verdict": verdict})
    return sorted(out, key=lambda r: (-r["n"], r["mean_r"]))


_LOT = re.compile(r"one lot \(([\d,.]+)\) risks ([\d,.]+) at the stop, more than the AI's ([\d,.]+)")


def lot_blocks(decisions: list[dict], cfg: dict) -> dict:
    hits, rs, seen = [], [], {}
    for d in sorted(decisions, key=lambda x: x.get("bar_time") or x["t"]):
        bt, cl = d.get("bar_time") or d["t"], (d["symbol"], (d["strategy"] or "").split("+")[0], d["direction"])
        if cl in seen and bt - seen[cl] <= REPEAT_BARS * _TFS.get(d.get("tf") or "15m", 900):
            seen[cl] = bt
            continue
        seen[cl] = bt
        ch = db.jload(d.get("checks"), {})
        if not isinstance(ch, dict):
            continue
        for c in ch.get("checks") or []:
            m = _LOT.search(c.get("detail") or "") if not c.get("ok", True) else None
            if m:
                f = lambda x: float(x.replace(",", ""))
                hits.append({"symbol": d["symbol"], "lot_risk": f(m.group(2)), "ai_risk": f(m.group(3))})
                if d.get("outcome") in ("T1 first", "stop first"):
                    rs.append(-1 - cfg["cost_haircut_r"] if d["outcome"] == "stop first" else min(float(d["outcome_r"] or 1), cfg["shadow_cap_r"]) - cfg["cost_haircut_r"])
    if not hits:
        return {"n": 0}
    lr = sorted(h["lot_risk"] for h in hits)
    return {"n": len(hits), "symbols": sorted({h["symbol"] for h in hits}), "median_lot_risk": lr[len(lr) // 2],
            "max_lot_risk": lr[-1], "ai_risk": hits[-1]["ai_risk"], "shadow": summarize(rs)}


def exit_audit(trades: list[dict], cache: dict | None = None) -> dict:
    closed = [t for t in trades if t.get("status") == "closed" and t.get("r_result") is not None]
    if not closed:
        return {"n": 0}
    give, stops, slip_e, tstop, rows = [], [], [], [], []
    for t in closed:
        p = db.jload(t.get("payload"), {})
        mfe, r = p.get("mfe_r"), float(t["r_result"])
        risk_pu = abs((t["entry"] or 0) - (t["stop"] or 0)) or 1
        row = {"id": t["id"], "symbol": t["symbol"], "strategy": t["strategy"], "r": round(r, 2), "mfe": round(mfe, 2) if mfe is not None else None,
               "mae": round(p.get("mae_r"), 2) if p.get("mae_r") is not None else None, "exit": t.get("exit_reason"), "flags": []}
        if mfe is not None and mfe >= 1.5 and mfe - r >= 1.0:
            give.append(mfe - r)
            row["flags"].append(f"gave back {mfe - r:.1f}R (best +{mfe:.1f}R, closed {r:+.2f}R)")
        if "stop" in (t.get("exit_reason") or "") and "trail" not in (t.get("exit_reason") or "") and "time" not in (t.get("exit_reason") or ""):
            stops.append(r)
            if r < -1.25:
                row["flags"].append(f"stop filled at {r:+.2f}R (planned -1R)")
            after = (cache or {}).get(str(t["id"]))
            if after == "T1 after stop":
                row["flags"].append("price reached T1 after the stop-out (stop too tight?)")
        if "time" in (t.get("exit_reason") or ""):
            tstop.append(r)
        if p.get("entry_slippage") is not None:
            slip_e.append(abs(p["entry_slippage"]) / risk_pu)
        rows.append(row)
    stopped_then_t1 = sum(1 for t in closed if (cache or {}).get(str(t["id"])) == "T1 after stop")
    return {"n": len(closed), "giveback_trades": len(give), "avg_giveback_r": round(sum(give) / len(give), 2) if give else 0,
            "stop_exits": len(stops), "avg_stop_r": round(sum(stops) / len(stops), 2) if stops else None,
            "bad_stop_fills": sum(1 for x in stops if x < -1.25), "time_stops": len(tstop),
            "avg_time_stop_r": round(sum(tstop) / len(tstop), 2) if tstop else None,
            "avg_entry_slippage_r": round(sum(slip_e) / len(slip_e), 2) if slip_e else None,
            "stopped_then_t1": stopped_then_t1, "trades": sorted(rows, key=lambda x: -x["id"])[:40]}


def data_health(logs: list[dict], decisions: list[dict], now: float) -> dict:
    days: dict = {}
    for lg in logs:
        d = dt.datetime.fromtimestamp(lg["t"], IST).strftime("%Y-%m-%d")
        x = days.setdefault(d, {"day": d, "data_gaps": 0, "quota_at": None, "errors": 0, "analysed": 0, "decisions": 0})
        txt = lg.get("text") or ""
        if lg["kind"] == "data":
            x["data_gaps"] += 1
            if "FYERS requests" in txt and "pausing" in txt:
                tm = dt.datetime.fromtimestamp(lg["t"], IST).strftime("%H:%M")
                x["quota_at"] = min(x["quota_at"] or tm, tm)
        elif lg["kind"] == "error":
            x["errors"] += 1
        elif lg["kind"] == "analysis":
            x["analysed"] += 1
    for dcs in decisions:
        d = dt.datetime.fromtimestamp(dcs["t"], IST).strftime("%Y-%m-%d")
        days.setdefault(d, {"day": d, "data_gaps": 0, "quota_at": None, "errors": 0, "analysed": 0, "decisions": 0})["decisions"] += 1
    rows = sorted(days.values(), key=lambda r: r["day"], reverse=True)[:10]
    for r in rows:
        if r["quota_at"]:
            h, m = map(int, r["quota_at"].split(":"))
            r["blind_min"] = max(0, 15 * 60 + 30 - (h * 60 + m))
    return {"days": rows, "quota_days": [r["day"] for r in rows if r.get("quota_at")],
            "blind_min_total": sum(r.get("blind_min", 0) for r in rows)}


def ml_status(models: list[dict]) -> dict:
    if not models:
        return {"models": 0}
    m = models[-1]
    met = db.jload(m.get("metrics"), {}) or {}
    gates = met.get("gates") or []
    return {"models": len(models), "approved": sum(1 for x in models if x.get("status") in ("approved", "active")),
            "latest": {"id": m["id"], "status": m["status"], "t": m["t"], "samples": met.get("samples"), "base_rate": met.get("base_rate"),
                       "failed_gates": [g.get("text") for g in gates if not g.get("ok")], "passed_gates": [g.get("text") for g in gates if g.get("ok")]}}


def research_status(exps: list[dict]) -> dict:
    if not exps:
        return {"experiments": 0}
    reasons: dict = {}
    for e in exps:
        v = db.jload(e.get("verdict"), {}) or {}
        for r in v.get("reasons") or []:
            if str(r).startswith("FAIL"):
                k = re.sub(r"-?\d+(\.\d+)?", "#", str(r))[:70]
                reasons[k] = reasons.get(k, 0) + 1
    st: dict = {}
    for e in exps:
        st[e.get("status") or "?"] = st.get(e.get("status") or "?", 0) + 1
    return {"experiments": len(exps), "by_status": st, "top_fail_reasons": sorted(reasons.items(), key=lambda x: -x[1])[:5]}


# ---------------------------------------------------------------- learned filters
def _filters() -> dict:
    return store.get_setting("agent_learned_filters") or {"active": [], "lifted": []}


def _save_filters(f: dict) -> None:
    f["lifted"] = f.get("lifted", [])[-60:]
    store.set_setting("agent_learned_filters", f)
    _CACHE["t"] = 0


def active_filters() -> list[dict]:
    return _filters().get("active", [])


def filter_hit(setup: dict, ctx: dict | None = None, now: float | None = None) -> dict | None:
    """Called by the Risk Guardian. A learned filter only ever blocks; switching auto_filters off stops it."""
    if not settings()["auto_filters"]:
        return None
    act = active_filters()
    if not act:
        return None
    pic = (ctx or {}).get("picture") or {}
    k = keys(setup.get("strategy") or "", setup.get("symbol") or "", setup.get("market"), setup.get("direction") or "",
             setup.get("bar_time") or setup.get("time") or now or time.time(), setup.get("regime") or pic.get("regime"))
    for f in act:
        if k.get(f["dim"]) == f["value"]:
            return f
    return None


def improve(samples: list[dict], decisions: list[dict], now: float | None = None) -> dict:
    """Add filters for reliable weaknesses, lift filters the evidence no longer supports. Returns what changed."""
    now = now or time.time()
    cfg = settings()
    F = _filters()
    changes = {"added": [], "lifted": []}
    # 1) lift
    keep = []
    for f in F.get("active", []):
        blocked = [s["r"] for s in samples if s["kind"] == "shadow" and s["t"] >= f["t"] and "Learned filter" in (s.get("failed") or [])
                   and s["keys"].get(f["dim"]) == f["value"]]
        st = summarize(blocked)
        why = None
        if st.get("n", 0) >= 5 and st["mean_r"] > 0:
            why = f"its {st['n']} blocked setups would have made {st['mean_r']:+.2f}R each"
        elif now - f["t"] > cfg["lift_days"] * 86400:
            why = f"{cfg['lift_days']}-day trial period over: tested again"
        if why:
            changes["lifted"].append({**f, "lifted_t": int(now), "lift_why": why})
            db.log("learning", f"Learned filter LIFTED: {DIM_LABEL.get(f['dim'], f['dim'])} = {f['value']} ({why})")
        else:
            f["blocked"] = st
            keep.append(f)
    F["active"] = keep
    F["lifted"] = F.get("lifted", []) + changes["lifted"]
    # 2) add (only if switched on)
    if cfg["auto_filters"]:
        recent = [d for d in decisions if d["t"] >= now - 14 * 86400 and d.get("stage") != "declined_strategy"]
        rkeys = []
        for d in recent:
            s = db.jload(d.get("setup"), {})
            rkeys.append(keys(d["strategy"], d["symbol"], s.get("market"), d["direction"], d.get("bar_time") or d["t"], s.get("regime")))
        segs = segments([s for s in samples if s["keys"].get("stage") != "declined_strategy"], cfg)
        cands = []
        for dim in FILTER_DIMS:
            for row in segs.get(dim, []):
                if row["grade"] != "reliable weakness" or row["value"] in ("?", "direct"):
                    continue
                if any(f["dim"] == dim and f["value"] == row["value"] for f in F["active"]):
                    continue
                last = next((x for x in reversed(F["lifted"]) if x["dim"] == dim and x["value"] == row["value"]), None)
                if last:                                       # back only on NEW losing evidence since it was lifted
                    new = summarize([s["r"] for s in samples if s["t"] >= last["lifted_t"] and s["keys"].get(dim) == row["value"]])
                    if new.get("n", 0) < max(3, cfg["min_n"] // 2) or new["mean_r"] >= 0:
                        continue
                cover = sum(1 for k in rkeys if k.get(dim) == row["value"]) / len(rkeys) if rkeys else 0
                if cover > cfg["max_cover"]:
                    continue
                cands.append((row["mean_r"], dim, row, cover))
        cands.sort(key=lambda c: c[0])
        for _, dim, row, cover in cands:
            if len(F["active"]) >= cfg["max_filters"]:
                break
            if any(f["value"] == row["value"] for f in F["active"]):      # e.g. same symbol already blocked another way
                continue
            f = {"id": f"{dim}:{row['value']}", "dim": dim, "value": row["value"], "t": int(now), "cover": round(cover, 3),
                 "label": f"{DIM_LABEL.get(dim, dim)} = {row['value']}",
                 "why": f"{row['n']} outcomes, {row['mean_r']:+.2f}R each (95% up to {row['ci_hi']:+.2f}R), win {row['win_rate']}%",
                 "evidence": {k: row[k] for k in ("n", "trades", "shadow", "mean_r", "ci_lo", "ci_hi", "win_rate")}}
            F["active"].append(f)
            changes["added"].append(f)
            db.log("learning", f"Learned filter ADDED: {f['label']} - {f['why']}")
    if changes["added"] or changes["lifted"]:
        _save_filters(F)
    return changes


def lift(fid: str, who: str = "user") -> dict:
    F = _filters()
    hit = [f for f in F.get("active", []) if f["id"] == fid]
    if not hit:
        return {"ok": False, "error": "no such active filter"}
    F["active"] = [f for f in F["active"] if f["id"] != fid]
    F["lifted"] = F.get("lifted", []) + [{**hit[0], "lifted_t": int(time.time()), "lift_why": f"lifted by {who}"}]
    _save_filters(F)
    db.log("learning", f"Learned filter lifted by {who}: {hit[0]['label']}")
    return {"ok": True}


# ---------------------------------------------------------------- suggestions (plain language, each with its evidence)
def suggestions(segs: dict, vetoes: list[dict], lots: dict, exits: dict, health: dict, ml: dict, act: list, n_trades: int, cfg: dict) -> list[dict]:
    out = []

    def add(sev, title, detail, action, auto=False):
        out.append({"severity": sev, "title": title, "detail": detail, "action": action, "auto": auto})

    if health.get("quota_days"):
        add("high", "Blind on Indian markets after the FYERS daily limit",
            f"On {', '.join(health['quota_days'])} MarketMiner used up the 5,000 FYERS requests and stopped Indian data "
            f"(about {health['blind_min_total']} market minutes without data).",
            "Cut requests: fewer F&O stocks in the intelligence sweep, slower sweep refresh, or turn the Signal Lab bot's all-markets scan off during market hours. "
            "SmartEntry shares the same limit.")
    errs = sum(r["errors"] for r in health.get("days", []))
    if errs:
        add("low", "Agent errors", f"{errs} error(s) in the agent log over the last days ({', '.join(r['day'] for r in health['days'] if r['errors'])}).",
            "See the Activity log for the message; a symbol that errors is retried after 2 minutes.")
    if lots.get("n"):
        sh = lots["shadow"]
        add("high" if lots["n"] >= 5 else "medium", "Option trades blocked: one lot risks more than the AI allows",
            f"{lots['n']} option setup(s) ({', '.join(x.split(':')[-1] for x in lots['symbols'][:6])}) were rejected: one lot risks about ₹{lots['median_lot_risk']:,.0f} "
            f"(up to ₹{lots['max_lot_risk']:,.0f}) vs the AI's ₹{lots['ai_risk']:,.0f} per trade."
            + (f" Had they been taken: {sh['n']} resolved, win {sh['win_rate']}%, {sh['mean_r']:+.2f}R each (estimate)." if sh.get("n") else ""),
            ("So far these blocks PROTECTED you (the skipped setups mostly lost): keep the limit. " if sh.get("n", 0) >= 4 and sh["mean_r"] < 0 else
             "Your decision: allow a higher risk for one option lot (e.g. up to ₹" + f"{lots['median_lot_risk']:,.0f}" + ") or prefer debit spreads (lower risk per lot). ")
            + "Not changed automatically: raising it would loosen risk.")
    for v in vetoes:
        if v["n"] < 4 or "old " in v["check"]:            # a retired rule: shown in the table, no advice
            continue
        if v["verdict"] == "costing trades":
            add("medium" if v["grade"] == "reliable strength" else "low", f"{v['by']} check “{v['check']}” is blocking winners",
                f"{v['n']} setups it stopped: win {v['win_rate']}%, {v['mean_r']:+.2f}R each if taken (estimate, after a {cfg['cost_haircut_r']}R cost haircut).",
                "Review this check (needs your approval: loosening it adds risk). Keep watching if the sample is small.")
        elif v["verdict"] == "protecting you" and v["grade"] == "reliable weakness":
            add("info", f"{v['by']} check “{v['check']}” is protecting you",
                f"{v['n']} setups it stopped would have lost {v['mean_r']:+.2f}R each.", "Keep it.")
    if exits.get("giveback_trades"):
        add("medium", "Winners given back",
            f"{exits['giveback_trades']} of {exits['n']} closed trades reached +1.5R or more and gave back {exits['avg_giveback_r']}R on average before closing.",
            "Book part of the position earlier or trail the stop sooner on time-stop exits.")
    if exits.get("bad_stop_fills"):
        add("medium", "Stops filling worse than planned",
            f"{exits['bad_stop_fills']} stop-outs closed beyond -1.25R (average stop exit {exits['avg_stop_r']}R).",
            "Widen the slippage allowance in sizing for that market, or avoid it in fast sessions.")
    if exits.get("stopped_then_t1"):
        add("medium", "Stops too tight", f"{exits['stopped_then_t1']} trades were stopped out and price then reached T1.",
            "Place the stop beyond the structure plus an ATR buffer.")
    for f in act:
        add("info", f"Blocking: {f['label']}", f"Learned filter since {dt.datetime.fromtimestamp(f['t'], IST):%d %b} - {f['why']}.",
            "Applied automatically; lifted when blocked setups would have won, or after the trial period.", auto=True)
    for dim in ("strategy", "strategy×market", "market", "session", "regime", "direction"):
        for row in segs.get(dim, []):
            if row["grade"] == "reliable strength":
                add("info", f"Strength: {DIM_LABEL[dim]} = {row['value']}",
                    f"{row['n']} outcomes, {row['mean_r']:+.2f}R each (95% from {row['ci_lo']:+.2f}R), win {row['win_rate']}%.", "Keep trading it.")
    if ml.get("latest") and ml["latest"]["status"] != "approved" and ml["latest"].get("failed_gates"):
        add("low", "ML model can't tell winners from losers yet",
            f"{ml['models']} training attempt(s), none approved. Latest fails: " + "; ".join(ml["latest"]["failed_gates"][:3]) + ".",
            "Nothing to do: it stays in shadow. More resolved setups (and the filters above) give it better data.")
    if n_trades < 15:
        add("info", "Little real trade evidence", f"Only {n_trades} closed agent trade(s): most findings come from the setups it did NOT take (estimates).",
            "Findings firm up as trades close. Reliable findings need ≥ " + str(cfg["min_n"]) + " outcomes and a 95% interval clear of zero.")
    order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    return sorted(out, key=lambda s: order[s["severity"]])


# ---------------------------------------------------------------- report + refresh
def _load(now: float, cfg: dict) -> dict:
    since = now - cfg["window_days"] * 86400
    return {"trades": db.q("SELECT * FROM agent_trades WHERE (mode='paper' OR mode IS NULL) AND opened_at >= ? ORDER BY id", (since,)),
            "decisions": db.q("SELECT * FROM agent_decisions WHERE t >= ? ORDER BY id", (since,)),
            "logs": db.q("SELECT t, kind, text FROM agent_log WHERE t >= ? AND kind IN ('data','error','analysis')", (now - 10 * 86400,)),
            "models": _safe("SELECT id, t, tf, status, metrics FROM ml_models ORDER BY id"),
            "exps": db.q("SELECT id, t, strategy, kind, verdict, status FROM agent_experiments WHERE t >= ? ORDER BY id", (since,))}


def _safe(sql: str) -> list[dict]:
    try:
        return db.q(sql)
    except Exception:                                     # noqa: BLE001 - optional table not created yet
        return []


def report(now: float | None = None, force: bool = False) -> dict:
    now = now or time.time()
    if not force and _CACHE["rep"] and now - _CACHE["t"] < 60:
        return _CACHE["rep"]
    cfg = settings()
    L = _load(now, cfg)
    smp = build_samples(L["trades"], L["decisions"], cfg)
    segs = segments(smp, cfg)
    vet = veto_audit(smp, cfg)
    lots = lot_blocks(L["decisions"], cfg)
    ex = exit_audit(L["trades"], store.get_setting("agent_diag_exit_cache") or {})
    hl = data_health(L["logs"], L["decisions"], now)
    ml = ml_status(L["models"])
    rs = research_status(L["exps"])
    act = active_filters()
    n_tr = sum(1 for s in smp if s["kind"] == "trade")
    weak = [{"dim": d, "label": DIM_LABEL.get(d, d), **r} for d in FILTER_DIMS + ("score", "evidence", "weekday") for r in segs[d] if r["grade"] in ("reliable weakness", "leaning weak") and r["value"] != "?"]
    strong = [{"dim": d, "label": DIM_LABEL.get(d, d), **r} for d in FILTER_DIMS + ("score", "evidence", "weekday") for r in segs[d] if r["grade"] in ("reliable strength", "leaning strong") and r["value"] != "?"]
    dec = L["decisions"]
    rep = {"generated": int(now), "settings": cfg, "window_days": cfg["window_days"],
           "overview": {"decisions": len(dec), "executed": sum(1 for d in dec if d.get("trade_id")),
                        "rejected_guardian": sum(1 for d in dec if d["stage"] == "rejected_guardian"),
                        "declined_strategy": sum(1 for d in dec if d["stage"] == "declined_strategy"),
                        "trades_closed": n_tr, "shadow_resolved": sum(1 for s in smp if s["kind"] == "shadow"),
                        "all": summarize([s["r"] for s in smp]), "real": summarize([s["r"] for s in smp if s["kind"] == "trade"]),
                        "shadow_taken_would": summarize([s["r"] for s in smp if s["kind"] == "shadow" and s["keys"]["stage"] == "rejected_guardian"]),
                        "shadow_declined_would": summarize([s["r"] for s in smp if s["kind"] == "shadow" and s["keys"]["stage"] == "declined_strategy"])},
           "suggestions": suggestions(segs, vet, lots, ex, hl, ml, act, n_tr, cfg),
           "weaknesses": sorted(weak, key=lambda r: (r["grade"] != "reliable weakness", r["mean_r"]))[:25],
           "strengths": sorted(strong, key=lambda r: (r["grade"] != "reliable strength", -r["mean_r"]))[:25],
           "segments": segs, "vetoes": vet, "lot_blocks": lots, "exits": ex, "data_health": hl, "ml": ml, "research": rs,
           "filters": {"active": act, "lifted": list(reversed(_filters().get("lifted", [])))[:15]},
           "dim_labels": DIM_LABEL}
    _CACHE.update(t=now, rep=rep)
    return rep


async def _exit_followup(trades: list[dict]) -> None:
    """For stop-outs: did price reach T1 within 30 candles after the exit? (cached; one candle request per symbol)."""
    from backend.lab import data as D
    cache = store.get_setting("agent_diag_exit_cache") or {}
    todo = [t for t in trades if t.get("status") == "closed" and str(t["id"]) not in cache and "stop" in (t.get("exit_reason") or "")
            and "trail" not in (t.get("exit_reason") or "") and "time" not in (t.get("exit_reason") or "")][-20:]
    bars_by: dict = {}
    for t in todo:
        k = (t["symbol"], t.get("tf") or "15m")
        if k not in bars_by:
            try:
                bars_by[k] = (await D.candles(k[0], k[1], 500))[0]
            except Exception:                             # noqa: BLE001
                bars_by[k] = None
        bars = bars_by[k]
        if not bars:
            continue
        tg = (db.jload(t.get("targets"), []) or [None])[0]
        if tg is None:
            cache[str(t["id"])] = "n/a"
            continue
        d = 1 if t["side"] == "LONG" else -1
        after = [b for b in bars if b.time > (t["closed_at"] or 0)][:30]
        if len(after) < 30 and (time.time() - (t["closed_at"] or 0)) < 30 * 900:
            continue                                      # not enough candles yet: look again later
        hit = any(((b.high >= tg) if d > 0 else (b.low <= tg)) for b in after)
        cache[str(t["id"])] = "T1 after stop" if hit else "stop was right"
    store.set_setting("agent_diag_exit_cache", cache)


async def refresh(now: float | None = None) -> dict:
    """Called by the agent loop (every 15 minutes and after closes): exit follow-up, then filters, then a fresh report."""
    now = now or time.time()
    cfg = settings()
    L = _load(now, cfg)
    try:
        await _exit_followup(L["trades"])
    except Exception as exc:                              # noqa: BLE001
        db.log("learning", f"Diagnosis exit follow-up skipped: {str(exc)[:160]}", level="warn")
    ch = improve(build_samples(L["trades"], L["decisions"], cfg), L["decisions"], now)
    report(now, force=True)
    return ch
