"""Profit-booking tracker for your 2-lot rule: lot 1 books at 1:2, lot 2's stop goes to breakeven and then trails.

Per position: was lot 1 booked (when, at what R), where lot 2's stop is now (R locked in), the best price since, how lot
2 ended, how much lot 2 gave back from its peak, and what trailing added compared with booking lot 2 at 1:2 too.
R here is per unit before fees (the trade's after-fee R is r_result). Read-only: nothing here changes a position.
"""
from __future__ import annotations

from . import db


def _r(x, entry, d, risk):
    return None if x is None or not risk else (x - entry) * d / risk


def trade_view(t: dict) -> dict | None:
    st = db.jload(t.get("payload"), {}) or {}
    ev = st.get("events") or []
    opt = st.get("kind") == "options"
    if opt:
        if "tp1" not in st:
            return None                                   # older single-exit option position
        d, entry, risk = 1, st["v0"], st.get("planned_unit") or 0
        stop_now, best, now_px = st.get("sl"), st.get("best"), st.get("value_now")
        parts = st.get("parts") or []
        lot1 = ({"r": _r(parts[0]["value"], entry, d, risk), "at": parts[0].get("t"), "price": parts[0]["value"]} if parts else None)
        target = st.get("tp1")
        label = st.get("label") or t["strategy"]
    else:
        if st.get("tp1") is None:
            return None                                   # thirds-plan position from before the 2-lot rule
        d = 1 if t["side"] == "LONG" else -1
        entry, risk = t["entry"], abs(t["entry"] - t["stop"])
        stop_now, best = st.get("stop_now"), st.get("best")
        now_px = None
        e1 = next((e for e in ev if e["kind"] == "lot1"), None)
        if e1:
            lot1 = {"r": e1.get("r"), "at": e1["t"], "price": e1.get("price")}
        elif st.get("hit") and len(st.get("exits") or []) >= 1:
            px = st["exits"][0][1]
            lot1 = {"r": _r(px, entry, d, risk), "at": None, "price": px}
        else:
            lot1 = None
        target = st.get("tp1")
        label = f"{t['side']} {t['symbol']}"
    closed = t["status"] == "closed"
    ex = next((e for e in reversed(ev) if e["kind"] == "exit"), None)
    last_px = (ex or {}).get("value" if opt else "price")
    if closed and last_px is None and st.get("exits"):
        last_px = st["exits"][-1][1]
    lot2_r = _r(last_px, entry, d, risk) if closed else None
    best_r = _r(best, entry, d, risk) if lot1 else None
    locked = _r(stop_now, entry, d, risk) if lot1 and not closed else None
    if closed:
        if not lot1:
            outcome = "stopped before 1:2 (both lots)" if (lot2_r or 0) <= -0.5 else f"closed before 1:2 ({t.get('exit_reason')})"
        elif lot2_r is not None and abs(lot2_r) < 0.05:
            outcome = "lot 1 booked · lot 2 out at breakeven"
        elif lot2_r is not None and lot2_r > (lot1 or {}).get("r", 2):
            outcome = f"lot 1 booked · lot 2 trailed further ({lot2_r:+.2f}R)"
        else:
            outcome = f"lot 1 booked · lot 2 {t.get('exit_reason')} ({lot2_r:+.2f}R)" if lot2_r is not None else "lot 1 booked"
    else:
        outcome = (f"lot 1 booked · lot 2 running, stop locks {locked:+.2f}R" if lot1 else
                   f"both lots open · lot 1 books at {target:,.4g}" if target is not None else "both lots open")
    tot = None
    if closed:
        tot = ((lot1["r"] + lot2_r) / 2) if lot1 and lot2_r is not None else lot2_r
    return {"id": t["id"], "label": label, "symbol": t["symbol"], "strategy": t["strategy"], "side": t["side"], "kind": "options" if opt else "direct",
            "status": t["status"], "opened_at": t["opened_at"], "closed_at": t.get("closed_at"), "entry": entry, "lot1_target": target,
            "lot1": lot1, "stop_now": None if closed else stop_now, "locked_r": locked, "best": best if lot1 else None, "best_r": best_r,
            "now": now_px, "lot2_exit": last_px if closed else None, "lot2_r": lot2_r, "exit_reason": t.get("exit_reason"),
            "giveback_r": (best_r - lot2_r) if closed and best_r is not None and lot2_r is not None else None,
            "trail_extra_r": (lot2_r - lot1["r"]) if closed and lot1 and lot2_r is not None else None,
            "trade_r_before_fees": tot, "trade_r": t.get("r_result"), "pnl": t.get("pnl"), "currency": t.get("currency"),
            "outcome": outcome, "events": ev[-25:]}


def report(limit: int = 100) -> dict:
    rows = db.q("SELECT * FROM agent_trades WHERE status='open' ORDER BY id DESC")
    rows += db.q("SELECT * FROM agent_trades WHERE status='closed' ORDER BY closed_at DESC LIMIT ?", (limit,))
    views = [v for v in (trade_view(t) for t in rows) if v]
    done = [v for v in views if v["status"] == "closed"]
    booked = [v for v in done if v["lot1"]]
    mean = lambda xs: round(sum(xs) / len(xs), 3) if xs else None
    summ = {
        "closed": len(done), "open": len(views) - len(done),
        "lot1_booked": len(booked), "lot1_booked_pct": round(len(booked) / len(done) * 100, 1) if done else None,
        "both_stopped": len([v for v in done if not v["lot1"]]),
        "lot2_breakeven": len([v for v in booked if v["lot2_r"] is not None and abs(v["lot2_r"]) < 0.05]),
        "lot2_beyond_2r": len([v for v in booked if (v["lot2_r"] or 0) > (v["lot1"]["r"] or 2)]),
        "avg_lot2_r": mean([v["lot2_r"] for v in booked if v["lot2_r"] is not None]),
        "avg_trail_extra_r": mean([v["trail_extra_r"] for v in booked if v["trail_extra_r"] is not None]),
        "avg_giveback_r": mean([v["giveback_r"] for v in booked if v["giveback_r"] is not None]),
        "best_runner_r": max([v["lot2_r"] for v in booked if v["lot2_r"] is not None], default=None),
        "avg_trade_r_after_fees": mean([v["trade_r"] for v in done if v["trade_r"] is not None]),
    }
    if booked and summ["avg_trail_extra_r"] is not None:
        x = summ["avg_trail_extra_r"]
        summ["verdict"] = (f"Trailing lot 2 has added {x:+.2f}R per booked trade versus booking it at 1:2 as well" if x > 0 else
                           f"Trailing lot 2 has given back {abs(x):.2f}R per booked trade versus booking it at 1:2 as well (small sample? check before changing)")
    return {"summary": summ, "trades": views,
            "note": "R per unit before fees; trade R after fees is in the last column. Lot 1 books at 1:2 after costs; lot 2: breakeven, then trails."}
