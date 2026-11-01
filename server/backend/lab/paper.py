"""Paper ledger. PAPER ONLY: nothing here talks to a broker's order API.

Fill: the latest closed candle's close plus slippage and half-spread (costs.slip_price). Management uses the same
rules as the backtester (thirds at T1/T2/T3, breakeven after T1, trail after T2, stop first inside a candle). Open
trades are updated from 1-minute candles (5-minute for Yahoo symbols older than 7 days) each time the ledger is read
and by the recorder.
"""
from __future__ import annotations

import json

from . import data as D
from . import costs as C
from . import protect as P
from . import store


async def open_trade(sig: dict) -> dict:
    symbol, tf = sig["symbol"], sig.get("tf", "5m")
    market = D.market_of(symbol)
    direction = sig["direction"]
    chk = P.check(symbol, market, direction, float(sig.get("score") or 0))
    if not chk["ok"]:
        return {"ok": False, "blocks": chk["blocks"]}
    bars, q = await D.candles(symbol, "1m" if market != "YF" else "5m", 30)
    d = 1 if direction == "LONG" else -1
    px = C.slip_price(bars[-1].close, d, market)
    stop = float(sig["stop"])
    targets = [float(x) for x in sig["targets"]]
    if (px - stop) * d <= 0:
        return {"ok": False, "blocks": [f"Price {px:,.2f} is already through the stop {stop:,.2f}"]}
    if (px - targets[0]) * d >= 0:
        return {"ok": False, "blocks": [f"Price {px:,.2f} is already beyond T1 {targets[0]:,.2f}: the move has gone"]}
    sz = P.size(symbol, market, px, stop)
    if not sz.get("qty"):
        return {"ok": False, "blocks": [sz.get("why", "size is zero")]}
    state = {"left": 1.0, "realised": 0.0, "stop_now": stop, "hit": [], "exits": [], "tf": tf, "strategy": sig.get("strategy"),
             "score": sig.get("score"), "last_t": bars[-1].time + 60, "source": q.get("source")}
    tid = store.execute("INSERT INTO trades(signal_id,symbol,market,side,qty,entry,stop,targets,opened_at,status,currency,payload)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sig.get("signal_id"), symbol, market, direction, sz["qty"], px, stop, json.dumps(targets), store.now(), "open",
                         sz["currency"], json.dumps(state)))
    return {"ok": True, "id": tid, "fill": px, "qty": sz["qty"], "risk": sz["risk_amount"], "currency": sz["currency"]}


def _finish(t: dict, st: dict, reason: str, now: int) -> None:
    entry, qty, d = t["entry"], t["qty"], 1 if t["side"] == "LONG" else -1
    avg = sum(p * x for p, x in st["exits"]) / sum(p for p, _ in st["exits"])
    fees = C.round_trip(t["symbol"], t["market"], qty, entry, avg, d > 0)
    gross = st["realised"] * qty
    risk = abs(entry - t["stop"]) * qty
    store.execute("UPDATE trades SET status='closed', closed_at=?, exit=?, exit_reason=?, gross=?, costs=?, pnl=?, r_result=?, payload=? WHERE id=?",
                  (now, avg, reason, gross, fees, gross - fees, (gross - fees) / risk if risk else 0, json.dumps(st), t["id"]))


async def update_open() -> int:
    n = 0
    for t in store.q("SELECT * FROM trades WHERE status='open'"):
        st = json.loads(t["payload"] or "{}")
        try:
            bars, _ = await D.candles(t["symbol"], "1m" if t["market"] != "YF" else "5m", 500)
        except Exception:                                 # noqa: BLE001 - data gap: try again next time
            continue
        d = 1 if t["side"] == "LONG" else -1
        entry, (t1, t2, t3) = t["entry"], json.loads(t["targets"])
        for b in bars:
            if b.time < st["last_t"]:
                continue
            st["last_t"] = b.time + 60
            if (b.low <= st["stop_now"]) if d > 0 else (b.high >= st["stop_now"]):
                px = C.slip_price(min(b.open, st["stop_now"]) if d > 0 else max(b.open, st["stop_now"]), -d, t["market"])
                st["realised"] += st["left"] * (px - entry) * d
                st["exits"].append((st["left"], px))
                st["left"] = 0
                _finish(t, st, "stop" if not st["hit"] else "breakeven/trailing stop", b.time + 60)
                n += 1
                break
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
                    if k == 0:
                        st["stop_now"] = entry
                else:
                    break
            if st["left"] <= 1e-9:
                _finish(t, st, "T3", b.time + 60)
                n += 1
                break
        else:
            store.execute("UPDATE trades SET payload=? WHERE id=?", (json.dumps(st), t["id"]))
    return n


async def close_trade(tid: int) -> dict:
    t = store.q("SELECT * FROM trades WHERE id=? AND status='open'", (tid,), one=True)
    if not t:
        return {"ok": False, "error": "no such open trade"}
    st = json.loads(t["payload"] or "{}")
    bars, _ = await D.candles(t["symbol"], "1m" if t["market"] != "YF" else "5m", 5)
    d = 1 if t["side"] == "LONG" else -1
    px = C.slip_price(bars[-1].close, -d, t["market"])
    st["realised"] += st["left"] * (px - t["entry"]) * d
    st["exits"].append((st["left"], px))
    st["left"] = 0
    _finish(t, st, "manual close", store.now())
    return {"ok": True, "exit": px}


def ledger() -> dict:
    rows = store.q("SELECT * FROM trades ORDER BY id DESC LIMIT 300")
    for r in rows:
        r["targets"] = json.loads(r["targets"] or "[]")
        p = json.loads(r.pop("payload") or "{}")
        r.update(hit=p.get("hit"), stop_now=p.get("stop_now"), left=p.get("left"), strategy=p.get("strategy"), tf=p.get("tf"))
    closed = [r for r in rows if r["status"] == "closed"]
    summ = {}
    for cur in ("INR", "USD"):
        c = [r for r in closed if r["currency"] == cur]
        if c:
            w = [r for r in c if (r["pnl"] or 0) > 0]
            summ[cur] = {"trades": len(c), "win_rate": round(100 * len(w) / len(c), 1), "pnl": round(sum(r["pnl"] or 0 for r in c), 2),
                         "avg_r": round(sum(r["r_result"] or 0 for r in c) / len(c), 3), "costs": round(sum(r["costs"] or 0 for r in c), 2)}
    return {"trades": rows, "summary": summ, "protection": P.settings()}
