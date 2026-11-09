"""Execution Agent. PAPER ONLY: PaperBroker fills against real market candles; LiveBroker refuses everything.

Opening (open_position) requires a decision row that the Risk Guardian marked APPROVED, that is younger than
APPROVAL_TTL seconds and has never been used. agent_trades.decision_id is UNIQUE, so the same approval can never
produce two positions (duplicate-order prevention at the database level, not just in code).

Fill: the latest 1-minute close plus slippage and half-spread (lab costs.slip_price) - never the signal price. The
fill is re-checked before the position exists: it must still be on the right side of the stop and short of T1, it
may not be more than 0.25R worse than the planned entry, and the maximum loss at the real fill must still fit the
approved budget (+10%); otherwise the order is skipped and recorded as such.

Management (update_positions, every agent loop, from 1-minute candles): one third at T1 (stop to breakeven), one
third at T2 (stop to T1), the rest at T3; stop assumed first when a candle touches stop and target; time stop at
time_stop_x x the expected holding period; Indian intraday positions are closed at 15:15 IST (MCX 23:15). MAE and MFE
are tracked in R for the post-trade review. If price data is unavailable the position is kept, the gap is logged and
the dashboard shows "monitoring data unavailable" - nothing is assumed.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import time

from backend.lab import costs as C
from backend.lab import data as D
from . import config as CFG
from . import db
from . import guardian as G

APPROVAL_TTL = 600
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
TF_MIN = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}
MONITOR: dict[int, dict] = {}                              # trade id -> {"error": str, "since": t}


class LiveTradingDisabled(Exception):
    pass


class LiveBroker:
    """Placeholder for a future broker adapter. Every method refuses: MarketMiner's FYERS app is data-only and live
    trading is disabled in this build (config.live_readiness lists what is missing)."""

    def __getattr__(self, name):
        def refuse(*a, **k):
            raise LiveTradingDisabled("Live execution is disabled: " + "; ".join(CFG.live_readiness()["missing"]))
        return refuse


async def _price(symbol: str, market: str, n: int = 30):
    return await D.candles(symbol, "1m" if market != "YF" else "5m", n)


async def open_position(decision_id: int, now: float | None = None) -> dict:
    now = now or time.time()
    dec = db.q("SELECT * FROM agent_decisions WHERE id=?", (decision_id,), one=True)
    if not dec:
        return {"ok": False, "why": "no such decision"}
    if dec["verdict"] != "APPROVED":
        return {"ok": False, "why": f"Risk Guardian verdict is {dec['verdict']}: execution refused"}
    if dec["trade_id"]:
        return {"ok": False, "why": f"approval already used by trade #{dec['trade_id']}"}
    if now - dec["t"] > APPROVAL_TTL:
        return {"ok": False, "why": "approval expired"}
    if CFG.emergency().get("on") or G.halt_state().get("on"):
        return {"ok": False, "why": "emergency stop / halt is on"}
    a = CFG.agent()
    if a["mode"] != "paper":
        return {"ok": False, "why": f"mode is {a['mode']}: no orders"}
    s = db.jload(dec["setup"], {})
    chk = db.jload(dec["checks"], {})
    sz = chk.get("sizing") or {}
    qty = sz.get("qty") or 0
    if qty <= 0:
        return {"ok": False, "why": "approved size is zero"}
    sym, market = s["symbol"], s["market"]
    d = 1 if s["direction"] == "LONG" else -1
    try:
        bars, q = await _price(sym, market)
    except Exception as exc:                              # noqa: BLE001 - no price, no order
        _skip(decision_id, f"no live price: {str(exc)[:120]}")
        return {"ok": False, "why": "no live price"}
    if q.get("stale"):
        _skip(decision_id, "price feed stale at execution")
        return {"ok": False, "why": "price feed stale"}
    fill = C.slip_price(bars[-1].close, d, market)
    risk_u = abs(s["entry"] - s["stop"])
    why = None
    if (fill - s["stop"]) * d <= 0:
        why = f"price {fill:,.4g} already through the stop {s['stop']:,.4g}"
    elif (fill - s["targets"][0]) * d >= 0:
        why = f"price {fill:,.4g} already beyond T1 {s['targets'][0]:,.4g}: the move has gone"
    elif (fill - s["entry"]) * d > 0.25 * risk_u:
        why = f"fill {fill:,.4g} is {(fill - s['entry']) * d / risk_u:.2f}R worse than the planned entry {s['entry']:,.4g}"
    else:
        long = d > 0
        ml = qty * (abs(fill - s["stop"]) + abs(C.slip_price(s["stop"], -d, market) - s["stop"]) + CFG.risk()["gap_allowance_atr"] * (s.get("atr") or 0)) \
            + C.round_trip(sym, market, qty, fill, s["stop"], long)
        if ml > (sz.get("budget") or 0) * 1.1:
            why = f"max loss at the real fill {ml:,.2f} exceeds the approved budget {sz.get('budget'):,.2f}"
    if why:
        _skip(decision_id, why)
        return {"ok": False, "why": why}
    state = {"left": 1.0, "realised": 0.0, "stop_now": s["stop"], "hit": [], "exits": [], "last_t": bars[-1].time + 60,
             "mfe_r": 0.0, "mae_r": 0.0, "expected_hold": s.get("expected_hold"), "atr": s.get("atr"), "score": s.get("score"),
             "evidence": s.get("evidence"), "quality_at_entry": chk.get("quality"), "fill_source": q.get("source"),
             "planned_entry": s["entry"], "entry_slippage": (fill - s["entry"]) * d, "bar_time": s.get("bar_time"),
             "tp1": sz.get("tp1"), "trail_atr": CFG.risk()["trail_atr"], "best": fill, "lots": sz.get("lots"),
             "book_frac": (sz.get("book_qty", qty / 2) / qty) if qty else 0.5,
             "events": [{"kind": "entry", "t": int(now), "price": fill, "stop": s["stop"], "tp1": sz.get("tp1")}]}
    if sz.get("tp1") is not None and (fill - sz["tp1"]) * d >= 0:
        _skip(decision_id, f"price {fill:,.4g} is already beyond the 1:2 target {sz['tp1']:,.4g}")
        return {"ok": False, "why": "price already beyond the 1:2 target"}
    wd = dt.datetime.fromtimestamp(now, IST)
    session = "asia" if wd.hour < 12 else "europe" if wd.hour < 18 else "us"
    if market in ("NSE", "FNO", "MCX"):
        m = wd.hour * 60 + wd.minute
        session = "india_open" if m < 660 else "india_mid" if m < 840 else "india_close"
    try:
        tid = db.execute("INSERT INTO agent_trades(decision_id, symbol, market, tf, strategy, side, qty, planned_entry, entry, stop, targets, opened_at,"
                         " status, currency, regime, session, mode, risk_amount, payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (decision_id, sym, market, s["tf"], s["strategy"], s["direction"], qty, s["entry"], fill, s["stop"], json.dumps(s["targets"]),
                          int(now), "open", G.currency(market), s.get("regime"), session, "paper", sz.get("max_loss"), json.dumps(state)))
    except sqlite3.IntegrityError:
        return {"ok": False, "why": "duplicate: this approval already has a position"}
    db.execute("UPDATE agent_decisions SET trade_id=?, stage='executed' WHERE id=?", (tid, decision_id))
    db.log("execution", f"PAPER {s['direction']} {qty:g} {sym} @ {fill:,.4g} (planned {s['entry']:,.4g}), stop {s['stop']:,.4g}, "
           f"T1/T2/T3 {' / '.join(f'{x:,.4g}' for x in s['targets'])}", sym, {"trade_id": tid, "decision_id": decision_id})
    return {"ok": True, "trade_id": tid, "fill": fill, "qty": qty}


def _skip(decision_id: int, why: str) -> None:
    db.execute("UPDATE agent_decisions SET stage='execution_skipped' WHERE id=?", (decision_id,))
    d = db.q("SELECT symbol FROM agent_decisions WHERE id=?", (decision_id,), one=True) or {}
    db.log("execution", f"Order skipped: {why}", d.get("symbol"), {"decision_id": decision_id}, level="warn")


def _hold_candles(s: str | None) -> int:
    m = re.search(r"about (\d+)", s or "")
    return max(10, int(m.group(1))) if m else 10


def _finish(t: dict, st: dict, reason: str, at: int) -> None:
    entry, qty, d = t["entry"], t["qty"], 1 if t["side"] == "LONG" else -1
    tot = sum(p for p, _ in st["exits"]) or 1
    avg = sum(p * x for p, x in st["exits"]) / tot
    fees = C.round_trip(t["symbol"], t["market"], qty, entry, avg, d > 0)
    gross = st["realised"] * qty
    risk = abs(entry - t["stop"]) * qty
    db.execute("UPDATE agent_trades SET status='closed', closed_at=?, exit=?, exit_reason=?, gross=?, costs=?, pnl=?, r_result=?, payload=? WHERE id=?",
               (at, avg, reason, gross, fees, gross - fees, (gross - fees) / risk if risk else 0, json.dumps(st), t["id"]))
    db.log("execution", f"Closed #{t['id']} {t['symbol']} ({reason}): P&L {gross - fees:,.2f} {t['currency']} after {fees:,.2f} costs",
           t["symbol"], {"trade_id": t["id"]})


def _session_end(market: str, t: int) -> bool:
    if market not in ("NSE", "FNO", "MCX"):
        return False
    x = dt.datetime.fromtimestamp(t, IST)
    m = x.hour * 60 + x.minute
    return m >= (915 if market != "MCX" else 1395)


def step_bars(t: dict, st: dict, bars) -> str | None:
    """Advance one open position over new 1-minute candles. Returns the exit reason if it closed (state updated)."""
    d = 1 if t["side"] == "LONG" else -1
    entry, (t1, t2, t3) = t["entry"], json.loads(t["targets"]) if isinstance(t["targets"], str) else t["targets"]
    risk_u = abs(entry - t["stop"]) or 1e-9
    tf_min = TF_MIN.get(t["tf"], 15)
    limit_t = t["opened_at"] + int(CFG.risk()["time_stop_x"] * _hold_candles(st.get("expected_hold")) * tf_min * 60)
    if st.get("tp1") is not None:
        return _step_two_lots(t, st, bars, limit_t, risk_u)
    for b in bars:
        if b.time < st["last_t"]:
            continue
        st["last_t"] = b.time + 60
        fav = (b.high - entry) * d if d > 0 else (entry - b.low)
        adv = (entry - b.low) if d > 0 else (b.high - entry)
        st["mfe_r"] = max(st["mfe_r"], fav / risk_u)
        st["mae_r"] = max(st["mae_r"], adv / risk_u)
        hit_stop = (b.low <= st["stop_now"]) if d > 0 else (b.high >= st["stop_now"])
        if hit_stop:
            raw = min(b.open, st["stop_now"]) if d > 0 else max(b.open, st["stop_now"])
            px = C.slip_price(raw, -d, t["market"])
            st["realised"] += st["left"] * (px - entry) * d
            st["exits"].append((st["left"], px))
            st["left"] = 0
            return "stop" if not st["hit"] else ("breakeven stop" if len(st["hit"]) == 1 else "trailing stop")
        for k, tp in enumerate((t1, t2, t3)):
            if k < len(st["hit"]):
                continue
            if (b.high >= tp) if d > 0 else (b.low <= tp):
                part = min(1 / 3 if k < 2 else st["left"], st["left"])
                px = C.slip_price(tp, -d, t["market"])
                st["realised"] += part * (px - entry) * d
                st["exits"].append((part, px))
                st["left"] -= part
                st["hit"].append(f"T{k + 1}")
                st["stop_now"] = entry if k == 0 else t1 if k == 1 else st["stop_now"]
            else:
                break
        if st["left"] <= 1e-9:
            return "T3"
        if b.time + 60 >= limit_t or _session_end(t["market"], b.time):
            px = C.slip_price(b.close, -d, t["market"])
            st["realised"] += st["left"] * (px - entry) * d
            st["exits"].append((st["left"], px))
            st["left"] = 0
            return "time stop" if b.time + 60 >= limit_t else "session close"
    return None


def _event(st: dict, kind: str, at: int, **kw) -> None:
    """Profit-booking journal of a position (entry, lot 1 booked, stop to breakeven, trail steps, exit). Record only:
    it never changes how the position is managed."""
    ev = st.setdefault("events", [])
    if kind == "trail" and ev and ev[-1]["kind"] == "trail" and abs(kw.get("stop", 0) - ev[-1].get("stop", 0)) < kw.pop("min_step", 0):
        return                                            # trail moved less than 0.25R since the last entry: not worth a line
    kw.pop("min_step", None)
    ev.append({"kind": kind, "t": int(at), **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in kw.items()}})
    if len(ev) > 60:
        del ev[1:len(ev) - 59]


def _step_two_lots(t: dict, st: dict, bars, limit_t: int, risk_u: float) -> str | None:
    """Your 2-lot rule. Lot 1 (half) books at tp1 = 1:2 after costs; then lot 2's stop goes to breakeven and trails
    trail_atr x ATR behind the best price. Stop first if one candle touches both."""
    d = 1 if t["side"] == "LONG" else -1
    entry, atr = t["entry"], st.get("atr") or 0
    for b in bars:
        if b.time < st["last_t"]:
            continue
        st["last_t"] = b.time + 60
        st["mfe_r"] = max(st["mfe_r"], ((b.high - entry) if d > 0 else (entry - b.low)) / risk_u)
        st["mae_r"] = max(st["mae_r"], ((entry - b.low) if d > 0 else (b.high - entry)) / risk_u)
        if (b.low <= st["stop_now"]) if d > 0 else (b.high >= st["stop_now"]):
            raw = min(b.open, st["stop_now"]) if d > 0 else max(b.open, st["stop_now"])
            px = C.slip_price(raw, -d, t["market"])
            st["realised"] += st["left"] * (px - entry) * d
            st["exits"].append((st["left"], px))
            lots = "rest" if st["hit"] else "all lots"
            st["left"] = 0
            why = "stop" if not st["hit"] else ("breakeven stop" if (px - entry) * d <= 0 else "trailing stop")
            _event(st, "exit", b.time + 60, price=px, r=(px - entry) * d / risk_u, lots=lots, reason=why)
            return why
        if not st["hit"] and ((b.high >= st["tp1"]) if d > 0 else (b.low <= st["tp1"])):
            px = C.slip_price(st["tp1"], -d, t["market"])
            bf = float(st.get("book_frac", 0.5))
            if bf > 0:                                     # book half the lots
                st["realised"] += bf * (px - entry) * d
                st["exits"].append((bf, px))
                st["left"] = 1.0 - bf
                st["hit"].append("lot 1 at 1:2")
                _event(st, "lot1", b.time + 60, price=px, r=(px - entry) * d / risk_u)
            else:                                          # single lot: nothing to book, protect and trail it whole
                st["hit"].append("1:2 reached (single lot)")
            st["stop_now"] = entry                         # remaining position: breakeven
            st["best"] = b.high if d > 0 else b.low
            _event(st, "breakeven", b.time + 60, stop=entry)
        if st["hit"]:
            st["best"] = max(st["best"], b.high) if d > 0 else min(st["best"], b.low)
            trail = st["best"] - d * st["trail_atr"] * atr
            old = st["stop_now"]
            st["stop_now"] = max(st["stop_now"], trail) if d > 0 else min(st["stop_now"], trail)
            if st["stop_now"] != old:
                _event(st, "trail", b.time + 60, stop=st["stop_now"], locked_r=(st["stop_now"] - entry) * d / risk_u, best=st["best"],
                       min_step=0.25 * risk_u)
        if b.time + 60 >= limit_t or _session_end(t["market"], b.time):
            px = C.slip_price(b.close, -d, t["market"])
            st["realised"] += st["left"] * (px - entry) * d
            st["exits"].append((st["left"], px))
            lots = "rest" if st["hit"] else "all lots"
            st["left"] = 0
            why = "time stop" if b.time + 60 >= limit_t else "session close"
            _event(st, "exit", b.time + 60, price=px, r=(px - entry) * d / risk_u, lots=lots, reason=why)
            return why
    return None


async def update_positions() -> list[int]:
    rows = db.q("SELECT * FROM agent_trades WHERE status='open'")
    opts = [t for t in rows if (db.jload(t["payload"], {}) or {}).get("kind") == "options"]
    closed = await update_options(opts)
    for t in rows:
        if t in opts:
            continue
        st = db.jload(t["payload"], {})
        try:
            bars, q = await _price(t["symbol"], t["market"], 500)
            MONITOR.pop(t["id"], None)
        except Exception as exc:                          # noqa: BLE001 - broker/data disconnect: keep the position, report it
            m = MONITOR.setdefault(t["id"], {"since": int(time.time())})
            m["error"] = str(exc)[:160]
            db.log("monitor", f"Monitoring data unavailable for #{t['id']} {t['symbol']}: {m['error']}", t["symbol"], level="warn")
            continue
        why = step_bars(t, st, bars)
        if why:
            _finish(t, st, why, st["last_t"])
            closed.append(t["id"])
        else:
            db.execute("UPDATE agent_trades SET payload=? WHERE id=?", (json.dumps(st), t["id"]))
    return closed


async def close_position(tid: int, reason: str = "manual close") -> dict:
    t = db.q("SELECT * FROM agent_trades WHERE id=? AND status='open'", (tid,), one=True)
    if not t:
        return {"ok": False, "why": "no such open position"}
    st = db.jload(t["payload"], {})
    if st.get("kind") == "options":
        from . import options as OP
        try:
            q = await OP.quotes([lg["symbol"] for lg in st["legs"]])
        except Exception as exc:                          # noqa: BLE001
            return {"ok": False, "why": f"no option quotes to close at: {str(getattr(exc, 'detail', exc))[:120]} (position kept; it keeps its stop)"}
        cl = _close_legs(st, q)
        if cl is None:
            return {"ok": False, "why": "an option leg has no two-sided quote (position kept; it keeps its stop)"}
        _finish_options(t, st, cl[1], reason, int(time.time()))
        return {"ok": True, "exit": cl[0]}
    d = 1 if t["side"] == "LONG" else -1
    try:
        bars, _ = await _price(t["symbol"], t["market"], 5)
        px = C.slip_price(bars[-1].close, -d, t["market"])
    except Exception as exc:                              # noqa: BLE001
        return {"ok": False, "why": f"no price to close at: {str(exc)[:120]} (position kept open; it still has its stop)"}
    st["realised"] += st["left"] * (px - t["entry"]) * d
    st["exits"].append((st["left"], px))
    _event(st, "exit", time.time(), price=px, r=(px - t["entry"]) * d / (abs(t["entry"] - t["stop"]) or 1e-9),
           lots="lot 2" if st.get("hit") else "both lots", reason=reason)
    st["left"] = 0
    _finish(t, st, reason, int(time.time()))
    return {"ok": True, "exit": px}


async def emergency_exit_all(reason: str) -> list[dict]:
    out = []
    for t in db.q("SELECT id FROM agent_trades WHERE status='open'"):
        out.append({"id": t["id"], **(await close_position(t["id"], f"emergency: {reason}"))})
    return out


def reconcile() -> dict:
    """Run at start-up: the database is the paper broker's book of record. Finds approvals marked executed without a
    position, positions without an approval, and positions with unreadable state."""
    issues = []
    for d in db.q("SELECT id, trade_id FROM agent_decisions WHERE stage='executed'"):
        if not d["trade_id"] or not db.q("SELECT id FROM agent_trades WHERE id=?", (d["trade_id"],), one=True):
            issues.append(f"decision #{d['id']} marked executed but has no position")
            db.execute("UPDATE agent_decisions SET stage='execution_unknown' WHERE id=?", (d["id"],))
    for t in db.q("SELECT id, decision_id, payload FROM agent_trades WHERE status='open'"):
        if not db.q("SELECT id FROM agent_decisions WHERE id=?", (t["decision_id"],), one=True):
            issues.append(f"open position #{t['id']} has no approval record")
        st = db.jload(t["payload"])
        if not st or "stop_now" not in st:
            issues.append(f"open position #{t['id']} has unreadable state")
    n = len(db.q("SELECT id FROM agent_trades WHERE status='open'"))
    db.log("reconcile", f"Start-up reconciliation: {n} open paper position(s); {len(issues)} issue(s)", data={"issues": issues},
           level="warn" if issues else "info")
    return {"open_positions": n, "issues": issues}


# ---------------------------------------------------------------- options (index + stock options, paper)
def _approval_problem(dec: dict | None, now: float) -> str | None:
    if not dec:
        return "no such decision"
    if dec["verdict"] != "APPROVED":
        return f"Risk Guardian verdict is {dec['verdict']}: execution refused"
    if dec["trade_id"]:
        return f"approval already used by trade #{dec['trade_id']}"
    if now - dec["t"] > APPROVAL_TTL:
        return "approval expired"
    if CFG.emergency().get("on") or G.halt_state().get("on"):
        return "emergency stop / halt is on"
    if CFG.agent()["mode"] != "paper":
        return f"mode is {CFG.agent()['mode']}: no orders"
    return None


def _fresh_legs(legs: list[dict], q: dict) -> list[dict] | None:
    out = []
    for lg in legs:
        x = q.get(lg["symbol"].upper())
        if not x or not x.get("bid") or not x.get("ask"):
            return None
        out.append({**lg, "bid": x["bid"], "ask": x["ask"], "ltp": x.get("lp")})
    return out


async def open_options(decision_id: int, now: float | None = None) -> dict:
    from . import options as OP
    now = now or time.time()
    dec = db.q("SELECT * FROM agent_decisions WHERE id=?", (decision_id,), one=True)
    why = _approval_problem(dec, now)
    if why:
        return {"ok": False, "why": why}
    s = db.jload(dec["setup"], {})
    chk = db.jload(dec["checks"], {})
    sz = chk.get("sizing") or {}
    op = s.get("options") or {}
    st, pl = op.get("structure") or {}, op.get("plan") or {}
    qty = sz.get("qty") or 0
    if qty <= 0 or not st.get("legs"):
        return {"ok": False, "why": "approved size is zero"}
    r = CFG.risk()
    und = s["symbol"]
    try:
        q = await OP.quotes([lg["symbol"] for lg in st["legs"]] + [und])
    except Exception as exc:                              # noqa: BLE001 - no quotes, no order
        _skip(decision_id, f"no option quotes: {str(getattr(exc, 'detail', exc))[:120]}")
        return {"ok": False, "why": "no option quotes"}
    legs = _fresh_legs(st["legs"], q)
    if not legs:
        _skip(decision_id, "a leg has no two-sided quote at execution")
        return {"ok": False, "why": "a leg has no two-sided quote"}
    bad = OP.leg_liquidity(legs, r, OP.is_index(st["underlying"]))
    p2 = OP.plan({**st, "legs": legs}, r)
    why = None
    if bad:
        why = "liquidity at execution: " + "; ".join(bad)
    elif not p2:
        why = "no executable prices"
    elif p2["v0"] - pl["v0"] > 0.10 * max(pl["planned_loss_unit"], 1e-9):
        why = f"net price {p2['v0']:,.2f} is worse than the approved {pl['v0']:,.2f} by more than 10% of the planned risk"
    else:
        f = OP.fees(legs, qty)
        if qty * p2["planned_loss_unit"] + f > (sz.get("budget") or 0) * 1.1 or \
           qty * p2["max_loss_unit"] + f > (sz.get("budget") or 0) * r["option_premium_risk_x"] * 1.1:
            why = "risk at the real fill exceeds the approved budget"
    if why:
        _skip(decision_id, why)
        return {"ok": False, "why": why}
    fills = [{"symbol": lg["symbol"], "type": lg["type"], "strike": lg["strike"], "side": lg["side"], "expiry": lg.get("expiry"),
              "entry_px": OP.leg_price(lg, True)} for lg in legs]
    shorts = {lg["type"]: lg["strike"] for lg in legs if lg["side"] == "SELL"}
    state = {"kind": "options", "structure": st["kind"], "label": OP.describe({**st, "legs": legs}), "legs": fills, "v0": p2["v0"],
             "sl": p2["sl_value"], "tp": p2["tp_value"], "planned_unit": p2["planned_loss_unit"], "max_unit": p2["max_loss_unit"],
             "tp1": p2["tp1_value"], "trail_unit": p2["trail_unit"], "lots_left": 2, "parts": [], "best": p2["v0"],
             "lots": int(qty // (st.get("lot") or qty or 1)) if qty else None, "book_qty": sz.get("book_qty", qty / 2), "frac_left": 1.0,
             "be_moved": False, "mfe_r": 0.0, "mae_r": 0.0, "und": {"symbol": und, "direction": s["direction"], "stop": s.get("stop"),
             "targets": s.get("targets"), "entry": s.get("entry"), "shorts": shorts}, "expected_hold": s.get("expected_hold"),
             "evidence": s.get("evidence"), "quality_at_entry": chk.get("quality"), "fill_source": "FYERS quotes",
             "planned_entry": pl["v0"], "entry_slippage": p2["v0"] - pl["v0"], "bar_time": s.get("bar_time"), "expiry": st.get("expiry"),
             "events": [{"kind": "entry", "t": int(now), "value": p2["v0"], "stop": p2["sl_value"], "tp1": p2["tp1_value"]}]}
    m = dt.datetime.fromtimestamp(now, IST)
    mins = m.hour * 60 + m.minute
    session = "india_open" if mins < 660 else "india_mid" if mins < 840 else "india_close"
    try:
        tid = db.execute("INSERT INTO agent_trades(decision_id, symbol, market, tf, strategy, side, qty, planned_entry, entry, stop, targets, opened_at,"
                         " status, currency, regime, session, mode, risk_amount, payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (decision_id, und, "FNO", s["tf"], s["strategy"] if "+" in s["strategy"] else f"{s['strategy']}+{st['kind']}", s["direction"], qty, pl["v0"], p2["v0"], p2["sl_value"],
                          json.dumps([p2["tp_value"]] * 3), int(now), "open", "INR", s.get("regime"), session, "paper",
                          round(qty * p2["planned_loss_unit"] + OP.fees(legs, qty), 2), json.dumps(state)))
    except sqlite3.IntegrityError:
        return {"ok": False, "why": "duplicate: this approval already has a position"}
    db.execute("UPDATE agent_decisions SET trade_id=?, stage='executed' WHERE id=?", (tid, decision_id))
    db.log("execution", f"PAPER {state['label']}: {qty:g} units ({qty // st['lot']:g} lot(s)) at net {p2['v0']:+,.2f}/unit "
           f"({'debit' if p2['v0'] > 0 else 'credit'}); legs " + ", ".join(f"{x['side']} {x['strike']:g}{x['type']} @ {x['entry_px']:,.2f}" for x in fills)
           + f"; stop value {p2['sl_value']:,.2f}, take profit {p2['tp_value']:,.2f}", und, {"trade_id": tid, "decision_id": decision_id})
    return {"ok": True, "trade_id": tid, "fill": p2["v0"], "qty": qty}


def _close_legs(st: dict, q: dict) -> tuple[float, dict] | None:
    """Liquidation value per unit (sell longs at bid, buy back shorts at ask) and the per-leg exit prices."""
    tot, px = 0.0, {}
    for lg in st["legs"]:
        x = q.get(lg["symbol"].upper()) or {}
        p = x.get("bid") if lg["side"] == "BUY" else x.get("ask")
        if not p or p <= 0:
            return None
        px[lg["symbol"]] = p
        tot += p if lg["side"] == "BUY" else -p
    return tot, px


def step_options(t: dict, st: dict, q: dict, now: float) -> tuple[str | None, dict | None]:
    """One monitoring step for an options position (2 lots). Returns (reason, per-leg prices): reason None = hold,
    "lot 1 at 1:2" = half booked (position stays open), anything else = close the remaining lot(s)."""
    cl = _close_legs(st, q)
    if cl is None:
        raise D.DataUnavailable("an option leg has no two-sided quote")
    v, px = cl
    pu = st["planned_unit"] or 1e-9
    st["mfe_r"] = max(st["mfe_r"], (v - st["v0"]) / pu)
    st["mae_r"] = max(st["mae_r"], (st["v0"] - v) / pu)
    st["value_now"] = v
    u = st["und"]
    lp = (q.get(u["symbol"].upper()) or {}).get("lp")
    st["und_now"] = lp
    k = st["structure"]
    two = "tp1" in st                                      # older single-exit positions keep their original plan
    if v <= st["sl"]:
        return ("stop" if not (st.get("parts") or st.get("lots_left") == 1) else "breakeven/trailing stop"), px
    if two and st.get("lots_left", 2) == 2 and v >= st["tp1"]:
        return "lot 1 at 1:2", px
    if two and st.get("lots_left") == 1:
        st["best"] = max(st.get("best", v), v)
        old = st["sl"]
        st["sl"] = max(st["sl"], st["v0"], st["best"] - st["trail_unit"])
        if st["sl"] != old:
            _event(st, "trail", now, stop=st["sl"], locked_r=(st["sl"] - st["v0"]) / pu, best=st["best"], min_step=0.25 * pu)
    if not two and v >= st["tp"]:
        return "take profit", px
    if lp:
        dr = 1 if u["direction"] == "LONG" else -1
        if k in ("long_option", "debit_spread", "credit_spread") and u.get("stop") is not None and (lp - u["stop"]) * dr <= 0:
            return "underlying stop", px
        sh = u.get("shorts") or {}
        if ("CE" in sh and lp > sh["CE"]) or ("PE" in sh and lp < sh["PE"]):
            return "short strike breached", px
    x = dt.datetime.fromtimestamp(now, IST)
    if x.hour * 60 + x.minute >= 915:
        return "session close", px
    tf_min = TF_MIN.get(t["tf"], 15)
    if now >= t["opened_at"] + CFG.risk()["time_stop_x"] * _hold_candles(st.get("expected_hold")) * tf_min * 60:
        return "time stop", px
    return None, px


def _book_lot1(t: dict, st: dict, px: dict) -> None:
    v = sum(px[lg["symbol"]] if lg["side"] == "BUY" else -px[lg["symbol"]] for lg in st["legs"])
    bq = st.get("book_qty", t["qty"] / 2)
    if bq:
        st["parts"].append({"qty": bq, "px": px, "value": v, "t": int(time.time())})
    st["frac_left"] = 1.0 - (bq / t["qty"] if t["qty"] else 0)
    st["lots_left"] = 1
    st["sl"] = max(st["sl"], st["v0"])                    # lot 2: breakeven
    st["best"] = v
    st["be_moved"] = True
    _event(st, "lot1", time.time(), value=v, r=(v - st["v0"]) / (st["planned_unit"] or 1e-9))
    _event(st, "breakeven", time.time(), stop=st["sl"])
    db.log("execution", (f"#{t['id']} {st['label']}: {bq:g} units booked at {v:+,.2f}/unit (1:2); rest stop to breakeven, trailing {st['trail_unit']:,.2f}"
                         if bq else f"#{t['id']} {st['label']}: 1:2 reached at {v:+,.2f}/unit (single lot): stop to breakeven, trailing {st['trail_unit']:,.2f}"),
           t["symbol"], {"trade_id": t["id"]})


def _finish_options(t: dict, st: dict, px: dict, reason: str, at: int) -> None:
    from . import options as OP
    v = sum(px[lg["symbol"]] if lg["side"] == "BUY" else -px[lg["symbol"]] for lg in st["legs"])
    legs = [{**lg, "bid": lg["entry_px"], "ask": lg["entry_px"]} for lg in st["legs"]]
    parts = list(st.get("parts") or [])
    rem = t["qty"] - sum(p["qty"] for p in parts)
    parts_all = parts + [{"qty": rem, "px": px, "value": v}]
    fees = sum(OP.fees(legs, p["qty"], p["px"]) for p in parts_all if p["qty"] > 0)
    gross = sum((p["value"] - st["v0"]) * p["qty"] for p in parts_all)
    risk = st["planned_unit"] * t["qty"]
    avg = sum(p["value"] * p["qty"] for p in parts_all) / t["qty"]
    st["exits"] = [(p["qty"] / t["qty"], p["value"]) for p in parts_all]
    st["exit_legs"] = px
    _event(st, "exit", at, value=v, r=(v - st["v0"]) / (st["planned_unit"] or 1e-9), lots="rest" if parts else "all lots", reason=reason)
    st["lots_left"] = 0
    db.execute("UPDATE agent_trades SET status='closed', closed_at=?, exit=?, exit_reason=?, gross=?, costs=?, pnl=?, r_result=?, payload=? WHERE id=?",
               (at, avg, reason, gross, fees, gross - fees, (gross - fees) / risk if risk else 0, json.dumps(st), t["id"]))
    db.log("execution", f"Closed #{t['id']} {st['label']} ({reason}) at net {v:+,.2f}/unit: P&L {gross - fees:,.2f} INR after {fees:,.2f} fees",
           t["symbol"], {"trade_id": t["id"]})


async def update_options(rows: list[dict]) -> list[int]:
    from . import options as OP
    if not rows:
        return []
    closed = []
    syms = [lg["symbol"] for t in rows for lg in db.jload(t["payload"], {}).get("legs", [])] + [t["symbol"] for t in rows]
    try:
        q = await OP.quotes(syms)
    except Exception as exc:                              # noqa: BLE001 - quotes down: keep positions, report the gap
        for t in rows:
            m = MONITOR.setdefault(t["id"], {"since": int(time.time())})
            m["error"] = f"option quotes unavailable: {str(getattr(exc, 'detail', exc))[:140]}"
        db.log("monitor", f"Option quotes unavailable for {len(rows)} position(s): {str(getattr(exc, 'detail', exc))[:140]}", level="warn")
        return []
    now = time.time()
    for t in rows:
        st = db.jload(t["payload"], {})
        try:
            why, px = step_options(t, st, q, now)
            MONITOR.pop(t["id"], None)
        except D.DataUnavailable as exc:
            MONITOR.setdefault(t["id"], {"since": int(now)})["error"] = str(exc)
            continue
        if why == "lot 1 at 1:2":
            _book_lot1(t, st, px)
            db.execute("UPDATE agent_trades SET payload=? WHERE id=?", (json.dumps(st), t["id"]))
        elif why:
            _finish_options(t, st, px, why, int(now))
            closed.append(t["id"])
        else:
            db.execute("UPDATE agent_trades SET payload=? WHERE id=?", (json.dumps(st), t["id"]))
    return closed
