"""Paper P&L tracking: how the Auto bot (and, to compare, manual Signal Lab paper trades and the AI agent's book) is doing.
PAPER ONLY: reads the ledgers, never places an order.

Sources
  bot     Signal Lab ledger trades opened by the PC Auto bot (ids kept in .lab/bot_state.json)
  manual  Signal Lab ledger trades you opened yourself ("Paper trade" button)
  agent   the AI agent's own paper book (agent_trades, mode = paper)
  all     everything above

Money is per currency (INR for India / MCX, USD for crypto / US / forex) and never added across currencies; R
(P&L after costs / the initial stop risk) is the cross-currency yardstick. Closed P&L is after costs. Open P&L =
what is already booked at T1/T2 plus the rest marked at the latest price, BEFORE exit costs (an estimate).
Days and weeks are Indian time (IST); a week starts Monday.

build() is pure (rows in, report out) so it can be tested without a market feed; report() gathers the rows.
"""
from __future__ import annotations

import datetime as dt
import json
import time

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SOURCES = ("bot", "manual", "agent", "all")
_MARKS: dict[str, tuple[float, float]] = {}          # symbol -> (time, last price): one candle call per symbol per 20 s
MARK_TTL = 20


def _j(s, default):
    try:
        return json.loads(s) if isinstance(s, str) else (s if s is not None else default)
    except ValueError:
        return default


# ---------------------------------------------------------------- rows from the ledgers
def norm_lab(r: dict, bot_ids: dict) -> dict:
    p = _j(r.get("payload"), {})
    b = bot_ids.get(str(r["id"]))
    return {"src": "bot" if b else "manual", "id": r["id"], "key": f"L{r['id']}", "symbol": r["symbol"], "market": r["market"],
            "side": r["side"], "qty": r["qty"] or 0, "entry": r["entry"], "stop": r["stop"], "targets": _j(r.get("targets"), []),
            "opened_at": r["opened_at"], "closed_at": r["closed_at"], "exit": r.get("exit"), "exit_reason": r.get("exit_reason"),
            "status": r["status"], "currency": r["currency"] or "USD", "gross": r.get("gross"), "costs": r.get("costs"),
            "pnl": r.get("pnl"), "r": r.get("r_result"), "strategy": (b or {}).get("name") or p.get("strategy") or "",
            "strategy_id": p.get("strategy") or "", "tf": p.get("tf") or (b or {}).get("tf") or "", "hit": p.get("hit") or [],
            "left": p.get("left", 1.0), "booked": p.get("realised", 0.0), "stop_now": p.get("stop_now", r["stop"])}


def norm_agent(r: dict) -> dict:
    p = _j(r.get("payload"), {})
    return {"src": "agent", "id": r["id"], "key": f"A{r['id']}", "symbol": r["symbol"], "market": r["market"], "side": r["side"],
            "qty": r["qty"] or 0, "entry": r["entry"], "stop": r["stop"], "targets": _j(r.get("targets"), []),
            "opened_at": r["opened_at"], "closed_at": r["closed_at"], "exit": r.get("exit"), "exit_reason": r.get("exit_reason"),
            "status": r["status"], "currency": r["currency"] or "USD", "gross": r.get("gross"), "costs": r.get("costs"),
            "pnl": r.get("pnl"), "r": r.get("r_result"), "strategy": r.get("strategy") or "", "strategy_id": r.get("strategy") or "",
            "tf": r.get("tf") or "", "hit": p.get("hit") or [], "left": p.get("left", 1.0), "booked": p.get("realised", 0.0),
            "stop_now": p.get("stop_now", r["stop"])}


# ---------------------------------------------------------------- the maths
def _day(t: int) -> str:
    return dt.datetime.fromtimestamp(t, IST).strftime("%Y-%m-%d")


def _starts(now: float) -> dict:
    d = dt.datetime.fromtimestamp(now, IST).replace(hour=0, minute=0, second=0, microsecond=0)
    return {"today": d.timestamp(), "week": (d - dt.timedelta(days=d.weekday())).timestamp(),
            "month": d.replace(day=1).timestamp(), "all": 0}


def mark_open(t: dict, px: float | None) -> dict:
    """Open trade: booked part + the rest at `px`, in money and R (before exit costs)."""
    d = 1 if t["side"] == "LONG" else -1
    risk = abs((t["entry"] or 0) - (t["stop"] or 0)) * t["qty"]
    if px is None:
        return {**t, "price": None, "upnl": None, "ur": None, "risk_money": round(risk, 2)}
    money = (t["booked"] + t["left"] * (px - t["entry"]) * d) * t["qty"]
    at_risk = max(0.0, (t["entry"] - t["stop_now"]) * d * t["left"] * t["qty"]) if t.get("stop_now") is not None else risk
    return {**t, "price": px, "upnl": round(money, 2), "ur": round(money / risk, 3) if risk else 0,
            "risk_money": round(risk, 2), "open_risk": round(at_risk, 2)}


def stats(rows: list[dict]) -> dict:
    """Closed trades, one currency (money) or mixed (R only)."""
    n = len(rows)
    if not n:
        return {"trades": 0}
    pn = [r["pnl"] or 0 for r in rows]
    rs = [r["r"] or 0 for r in rows]
    w = [x for x in pn if x > 0]
    lo = [x for x in pn if x <= 0]
    eq = peak = mdd = 0.0
    for x in pn:
        eq += x
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    best_w = best_l = run = 0                               # streaks in time order: +n wins in a row, -n losses
    for x in pn:
        if x > 0:
            run = run + 1 if run > 0 else 1
        else:
            run = run - 1 if run < 0 else -1
        best_w, best_l = max(best_w, run), min(best_l, run)
    cur = run
    gw, gl = sum(w), -sum(lo)
    return {"trades": n, "wins": len(w), "losses": len(lo), "win_rate": round(100 * len(w) / n, 1),
            "pnl": round(sum(pn), 2), "gross": round(sum(r["gross"] or 0 for r in rows), 2), "costs": round(sum(r["costs"] or 0 for r in rows), 2),
            "avg_win": round(gw / len(w), 2) if w else 0, "avg_loss": round(-gl / len(lo), 2) if lo else 0,
            "profit_factor": round(gw / gl, 2) if gl else (None if not gw else 999),
            "avg_r": round(sum(rs) / n, 3), "total_r": round(sum(rs), 2),
            "best": round(max(pn), 2), "worst": round(min(pn), 2), "max_dd": round(mdd, 2),
            "streak": cur, "best_streak": best_w, "worst_streak": -best_l}


def _group(rows: list[dict], key) -> list[dict]:
    g: dict = {}
    for r in rows:
        g.setdefault(key(r), []).append(r)
    out = []
    for k, v in g.items():
        by_cur = {c: round(sum(x["pnl"] or 0 for x in v if x["currency"] == c), 2) for c in sorted({x["currency"] for x in v})}
        w = sum(1 for x in v if (x["pnl"] or 0) > 0)
        out.append({"key": k, "trades": len(v), "win_rate": round(100 * w / len(v), 1), "pnl": by_cur,
                    "avg_r": round(sum(x["r"] or 0 for x in v) / len(v), 3), "total_r": round(sum(x["r"] or 0 for x in v), 2)})
    return sorted(out, key=lambda x: -x["total_r"])


def build(rows: list[dict], marks: dict[str, float | None], now: float | None = None, capital: dict | None = None, source: str = "bot") -> dict:
    now = now or time.time()
    capital = capital or {}
    closed = sorted([r for r in rows if r["status"] == "closed" and r.get("closed_at")], key=lambda r: r["closed_at"])
    opened = [mark_open(r, marks.get(r["symbol"])) for r in rows if r["status"] != "closed"]
    starts = _starts(now)
    curs = sorted({r["currency"] for r in rows}) or ["INR"]
    by_cur = {}
    for c in curs:
        cc = [r for r in closed if r["currency"] == c]
        oc = [r for r in opened if r["currency"] == c]
        periods = {}
        for p, t0 in starts.items():
            s = stats([r for r in cc if r["closed_at"] >= t0])
            periods[p] = {k: s.get(k, 0) for k in ("trades", "wins", "losses", "win_rate", "pnl", "costs", "total_r")}
        days: dict = {}
        for r in cc:
            d = days.setdefault(_day(r["closed_at"]), {"day": _day(r["closed_at"]), "pnl": 0.0, "trades": 0, "wins": 0, "r": 0.0})
            d["pnl"] += r["pnl"] or 0
            d["trades"] += 1
            d["wins"] += (r["pnl"] or 0) > 0
            d["r"] += r["r"] or 0
        cum, daily = 0.0, []
        for d in sorted(days.values(), key=lambda x: x["day"]):
            cum += d["pnl"]
            daily.append({**d, "pnl": round(d["pnl"], 2), "r": round(d["r"], 2), "cum": round(cum, 2)})
        cum, curve = 0.0, []
        for r in cc:
            cum += r["pnl"] or 0
            curve.append({"t": r["closed_at"], "cum": round(cum, 2), "key": r["key"]})
        upnl = [r["upnl"] for r in oc if r["upnl"] is not None]
        cap = capital.get(c)
        s = stats(cc)
        by_cur[c] = {"closed": s, "periods": periods, "daily": daily, "curve": curve, "capital": cap,
                     "return_pct": round(100 * s.get("pnl", 0) / cap, 2) if cap else None,
                     "open": {"trades": len(oc), "upnl": round(sum(upnl), 2) if upnl else 0, "unpriced": len(oc) - len(upnl),
                              "risk": round(sum(r.get("open_risk", r["risk_money"]) for r in oc), 2)},
                     "today_total": round(periods["today"]["pnl"] + (sum(upnl) if upnl else 0), 2)}
    return {"source": source, "generated": int(now), "currencies": curs, "by_currency": by_cur,
            "all_r": {k: v for k, v in stats(closed).items() if k in ("trades", "win_rate", "avg_r", "total_r")},
            "by_symbol": _group(closed, lambda r: r["symbol"]), "by_strategy": _group(closed, lambda r: r["strategy"] or "—"),
            "by_exit": _group(closed, lambda r: r["exit_reason"] or "—"), "by_side": _group(closed, lambda r: r["side"]),
            "by_source": _group(closed, lambda r: r["src"]) if source == "all" else [],
            "open": sorted(opened, key=lambda r: -r["opened_at"]),
            "trades": sorted(closed, key=lambda r: -r["closed_at"])[:500]}


# ---------------------------------------------------------------- gathering (needs the running app)
async def _price(symbol: str, market: str) -> float | None:
    from backend.lab import data as D
    hit = _MARKS.get(symbol)
    if hit and time.time() - hit[0] < MARK_TTL:
        return hit[1]
    try:
        bars, _ = await D.candles(symbol, "1m" if market != "YF" else "5m", 3)
        px = float(bars[-1].close)
    except Exception:                                     # noqa: BLE001 - no feed (e.g. FYERS logged out): leave it unpriced
        return hit[1] if hit else None
    _MARKS[symbol] = (time.time(), px)
    return px


def rows_for(source: str, bot_ids: dict) -> list[dict]:
    from backend.lab import store
    rows: list[dict] = []
    if source in ("bot", "manual", "all"):
        for r in store.q("SELECT * FROM trades ORDER BY id"):
            n = norm_lab(r, bot_ids)
            if source == "all" or n["src"] == source:
                rows.append(n)
    if source in ("agent", "all"):
        try:
            rows += [norm_agent(r) for r in store.q("SELECT * FROM agent_trades WHERE mode='paper' OR mode IS NULL ORDER BY id")]
        except Exception:                                 # noqa: BLE001 - agent tables not created yet
            pass
    return rows


async def report(source: str = "bot", bot_ids: dict | None = None) -> dict:
    from backend.lab import service as SV, protect as P
    if source not in SOURCES:
        source = "bot"
    try:
        await SV.paper_list()                              # walks open Lab trades forward first (T1/T2/stop)
    except Exception:                                     # noqa: BLE001
        pass
    rows = rows_for(source, bot_ids or {})
    marks = {}
    for r in rows:
        if r["status"] != "closed" and r["symbol"] not in marks:
            marks[r["symbol"]] = await _price(r["symbol"], r["market"])
    ps = P.settings()
    return build(rows, marks, capital={"INR": ps.get("capital_inr"), "USD": ps.get("capital_usd")}, source=source)
