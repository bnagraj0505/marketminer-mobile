"""Daily P&L and agent performance report (PAPER ONLY: reads the ledgers, never places an order).

Every IST day the report covers:
  - P&L per book (AI agent, PC Auto bot, manual Signal Lab trades) and currency: closed trades, wins / losses, win
    rate, P&L after costs, costs, R, profit factor, best and worst trade; week-to-date and month-to-date P&L
  - open positions at report time (unrealised P&L at the latest price, before exit costs)
  - the agent: equity at the start and end of the day, drawdown, today's results by strategy and symbol, decisions
    (setups seen, approved, rejected) and the checks that rejected most often, strategies suspended, and all-time
    statistics (expectancy with its 95% interval, profit factor, max drawdown)
Money is never added across currencies (INR for India / MCX, USD for crypto); R is the cross-currency yardstick.

Schedule: generated automatically at report_time IST (default 23:55, covering the whole IST day, after the Indian
close and with crypto up to the end of the day), saved to .lab/reports/daily/YYYY-MM-DD.json and sent on Telegram
when the Telegram bot is set up. If MarketMiner was off at that time, the missed day is generated at the next start.
build() is pure (rows in, report out) so it is testable without a market feed.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("marketminer.dailyreport")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / ".lab" / "reports" / "daily"
SOURCES = {"agent": "AI agent", "bot": "Auto bot", "manual": "Manual (Signal Lab)"}
DEFAULTS = {"enabled": True, "time": "23:55", "telegram": True}


def settings() -> dict:
    from backend.lab import store
    return {**DEFAULTS, **(store.get_setting("daily_report") or {})}


def save_settings(v: dict) -> dict:
    from backend.lab import store
    cur = settings()
    if "enabled" in v:
        cur["enabled"] = bool(v["enabled"])
    if "telegram" in v:
        cur["telegram"] = bool(v["telegram"])
    if "time" in v:
        hh, mm = str(v["time"]).split(":")
        cur["time"] = f"{min(23, max(0, int(hh))):02d}:{min(59, max(0, int(mm))):02d}"
    store.set_setting("daily_report", cur)
    return cur


def day_bounds(day: str) -> tuple[int, int]:
    d = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=IST)
    return int(d.timestamp()), int((d + dt.timedelta(days=1)).timestamp())


def today(now: float | None = None) -> str:
    return dt.datetime.fromtimestamp(now or time.time(), IST).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- the maths (pure)
def _stats(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": None, "pnl": 0.0, "costs": 0.0, "total_r": 0.0, "profit_factor": None,
                "avg_r": None, "best": None, "worst": None}
    pnl = [r.get("pnl") or 0 for r in rows]
    w = [x for x in pnl if x > 0]
    lo = [x for x in pnl if x <= 0]
    best = max(rows, key=lambda r: r.get("pnl") or 0)
    worst = min(rows, key=lambda r: r.get("pnl") or 0)
    pick = lambda r: {"symbol": r["symbol"], "side": r.get("side"), "strategy": r.get("strategy") or "", "pnl": round(r.get("pnl") or 0, 2),
                      "r": round(r.get("r") or 0, 2), "exit": r.get("exit_reason") or ""}
    rs = [r.get("r") or 0 for r in rows]
    return {"trades": n, "wins": len(w), "losses": len(lo), "win_rate": round(100 * len(w) / n, 1), "pnl": round(sum(pnl), 2),
            "costs": round(sum(r.get("costs") or 0 for r in rows), 2), "total_r": round(sum(rs), 2), "avg_r": round(sum(rs) / n, 3),
            "profit_factor": round(sum(w) / -sum(lo), 2) if lo and sum(lo) < 0 else None, "best": pick(best), "worst": pick(worst)}


def _by(rows: list[dict], key) -> list[dict]:
    g: dict = {}
    for r in rows:
        g.setdefault(key(r), []).append(r)
    out = []
    for k, v in g.items():
        pnl = {}
        for r in v:
            pnl[r["currency"]] = round(pnl.get(r["currency"], 0) + (r.get("pnl") or 0), 2)
        w = sum(1 for r in v if (r.get("pnl") or 0) > 0)
        out.append({"key": k, "trades": len(v), "wins": w, "win_rate": round(100 * w / len(v), 1), "pnl": pnl,
                    "total_r": round(sum(r.get("r") or 0 for r in v), 2)})
    return sorted(out, key=lambda x: -x["total_r"])


def _failed_checks(dec: dict) -> list[str]:
    try:
        ch = json.loads(dec.get("checks") or "[]")
    except ValueError:
        return []
    return [c.get("check", "?") for c in ch if isinstance(c, dict) and not c.get("ok") and c.get("hard", True)]


def build(day: str, rows: list[dict], decisions: list[dict] | None = None, agent_all: dict | None = None,
          agent_equity: dict | None = None, open_rows: list[dict] | None = None, suspended: list[dict] | None = None,
          now: float | None = None) -> dict:
    """rows: normalised trades (backend.bot.perf.norm_lab / norm_agent). decisions: agent_decisions rows of the day."""
    d0, d1 = day_bounds(day)
    dd = dt.datetime.strptime(day, "%Y-%m-%d")
    w0 = int((dt.datetime(dd.year, dd.month, dd.day, tzinfo=IST) - dt.timedelta(days=dd.weekday())).timestamp())
    m0 = int(dt.datetime(dd.year, dd.month, 1, tzinfo=IST).timestamp())
    closed = [r for r in rows if r.get("status") == "closed" and r.get("closed_at")]
    day_rows = [r for r in closed if d0 <= r["closed_at"] < d1]
    books = {}
    for src, label in SOURCES.items():
        mine = [r for r in closed if r["src"] == src]
        curs = sorted({r["currency"] for r in mine if r["closed_at"] < d1} | {r["currency"] for r in (open_rows or []) if r["src"] == src})
        per = {}
        for c in curs:
            cc = [r for r in mine if r["currency"] == c]
            per[c] = {"today": _stats([r for r in cc if d0 <= r["closed_at"] < d1]),
                      "week_pnl": round(sum(r.get("pnl") or 0 for r in cc if w0 <= r["closed_at"] < d1), 2),
                      "month_pnl": round(sum(r.get("pnl") or 0 for r in cc if m0 <= r["closed_at"] < d1), 2),
                      "all_pnl": round(sum(r.get("pnl") or 0 for r in cc if r["closed_at"] < d1), 2)}
            op = [r for r in (open_rows or []) if r["src"] == src and r["currency"] == c]
            if op:
                up = [r["upnl"] for r in op if r.get("upnl") is not None]
                per[c]["open"] = {"trades": len(op), "upnl": round(sum(up), 2), "unpriced": len(op) - len(up)}
        if per:
            books[src] = {"label": label, "by_currency": per}
    totals = {}
    for r in day_rows:
        t = totals.setdefault(r["currency"], {"pnl": 0.0, "trades": 0})
        t["pnl"] = round(t["pnl"] + (r.get("pnl") or 0), 2)
        t["trades"] += 1
    ag = [r for r in day_rows if r["src"] == "agent"]
    decs = decisions or []
    fails: dict = {}
    for x in decs:
        if (x.get("verdict") or "") == "REJECTED":
            for c in _failed_checks(x):
                fails[c] = fails.get(c, 0) + 1
    agent = {"today_by_strategy": _by(ag, lambda r: r.get("strategy") or "—"),
             "today_by_symbol": _by(ag, lambda r: r["symbol"]),
             "today_by_exit": _by(ag, lambda r: r.get("exit_reason") or "—"),
             "decisions": {"setups": len(decs), "approved": sum(1 for x in decs if x.get("verdict") == "APPROVED"),
                           "rejected": sum(1 for x in decs if x.get("verdict") == "REJECTED"),
                           "observed": sum(1 for x in decs if x.get("verdict") == "OBSERVE"),
                           "opened": sum(1 for x in decs if x.get("trade_id")),
                           "top_rejections": sorted(({"check": k, "count": v} for k, v in fails.items()), key=lambda x: -x["count"])[:6]},
             "equity": agent_equity or {}, "suspended": suspended or []}
    if agent_all and agent_all.get("trades"):
        agent["all_time"] = {k: agent_all.get(k) for k in ("trades", "win_rate", "profit_factor", "expectancy_r", "expectancy_r_ci95",
                                                            "total_r", "max_drawdown_r", "max_drawdown_pct", "max_win_streak", "max_loss_streak")}
    return {"day": day, "generated": int(now or time.time()), "totals": totals, "books": books, "agent": agent,
            "note": "Paper results (simulated fills on real prices, modelled costs). Money is per currency; R = P&L / initial stop risk. "
                    "A single day is noise; judge the strategy on the all-time figures and their 95% interval."}


# ---------------------------------------------------------------- text for Telegram / the page
def _m(x: float, c: str) -> str:
    sign = "-" if x < 0 else "+"
    return f"{sign}₹{abs(x):,.0f}" if c == "INR" else f"{sign}${abs(x):,.2f}"


def text(rep: dict) -> str:
    E = html.escape
    d = dt.datetime.strptime(rep["day"], "%Y-%m-%d").strftime("%a %d %b %Y")
    out = [f"📒 <b>Daily paper report · {d}</b>"]
    if rep["totals"]:
        out.append("Closed today: " + " · ".join(f"{_m(v['pnl'], c)} ({v['trades']} trades)" for c, v in rep["totals"].items()))
    else:
        out.append("No trades closed today.")
    for src, b in rep["books"].items():
        lines = []
        for c, v in b["by_currency"].items():
            t = v["today"]
            s = (f"{c}: {_m(t['pnl'], c)} · {t['trades']} trades" + (f" · {t['wins']}W/{t['losses']}L ({t['win_rate']}%)" if t["trades"] else "")
                 + (f" · PF {t['profit_factor']}" if t.get("profit_factor") is not None else "") + (f" · {t['total_r']:+.2f}R" if t["trades"] else ""))
            s += f"\n   week {_m(v['week_pnl'], c)} · month {_m(v['month_pnl'], c)} · all {_m(v['all_pnl'], c)}"
            if v.get("open"):
                s += f"\n   open {v['open']['trades']} · {_m(v['open']['upnl'], c)} unrealised"
            if t["trades"]:
                s += f"\n   best {E(t['best']['symbol'])} {_m(t['best']['pnl'], c)} · worst {E(t['worst']['symbol'])} {_m(t['worst']['pnl'], c)}"
            lines.append(s)
        out.append(f"\n<b>{E(b['label'])}</b>\n" + "\n".join(lines))
    a = rep["agent"]
    dc = a["decisions"]
    out.append(f"\n<b>Agent performance</b>\nSetups {dc['setups']} · approved {dc['approved']} · rejected {dc['rejected']} · opened {dc['opened']}")
    if dc["top_rejections"]:
        out.append("Top rejections: " + ", ".join(f"{E(x['check'])} ×{x['count']}" for x in dc["top_rejections"][:4]))
    for c, e in (a.get("equity") or {}).items():
        out.append(f"{c} equity {e['start']:,.2f} → {e['end']:,.2f} ({_m(e['end'] - e['start'], c)}) · drawdown {e['drawdown_pct']:.2f}%")
    if a["today_by_strategy"]:
        out.append("By strategy: " + "; ".join(f"{E(x['key'])} {x['trades']}t {x['total_r']:+.2f}R" for x in a["today_by_strategy"][:5]))
    if a.get("all_time"):
        t = a["all_time"]
        ci = t.get("expectancy_r_ci95") or [None, None]
        out.append(f"All time: {t['trades']} trades · win {t['win_rate']}% · PF {t['profit_factor'] if t['profit_factor'] is not None else '—'} · "
                   f"expectancy {t['expectancy_r']:+.3f}R" + (f" (95%: {ci[0]:+.2f} to {ci[1]:+.2f})" if ci[0] is not None else "")
                   + f" · max DD {t.get('max_drawdown_pct') or 0:.2f}%")
    if a.get("suspended"):
        out.append("Suspended: " + ", ".join(E(s["strategy"]) for s in a["suspended"]))
    out.append("\nPaper trading only. Full report: MarketMiner → 📊 P&amp;L → Daily report")
    return "\n".join(out)


# ---------------------------------------------------------------- gathering (needs the running app)
def _agent_equity(d0: int, d1: int) -> dict:
    from . import config as CFG, db
    r = CFG.risk()
    out = {}
    for cur in ("INR", "USD"):
        cap = r["capital_inr"] if cur == "INR" else r["capital_usd"]
        rows = db.q("SELECT pnl, closed_at FROM agent_trades WHERE status='closed' AND currency=? AND mode='paper' AND closed_at<? ORDER BY closed_at, id",
                    (cur, d1))
        if not rows:
            continue
        start = cap + sum(x["pnl"] or 0 for x in rows if x["closed_at"] < d0)
        e = peak = cap
        for x in rows:
            e += x["pnl"] or 0
            peak = max(peak, e)
        out[cur] = {"start": round(start, 2), "end": round(e, 2), "peak": round(peak, 2), "drawdown_pct": round((peak - e) / peak * 100, 2) if peak else 0}
    return out


async def make(day: str | None = None, save: bool = True) -> dict:
    from backend.bot import perf as PF
    from . import db, performance as P
    try:
        from backend.bot import service as B              # PC: the Auto bot's trade ids (the phone version has no bot service)
        bot_ids = B.ST.get("trades") or {}
    except Exception:                                     # noqa: BLE001
        bot_ids = {}
    day = day or today()
    d0, d1 = day_bounds(day)
    rows = PF.rows_for("all", bot_ids)
    open_rows = None
    if today() == day:                                    # open positions only make sense for today
        marks = {}
        for r in rows:
            if r["status"] != "closed" and r["symbol"] not in marks:
                marks[r["symbol"]] = await PF._price(r["symbol"], r["market"])
        open_rows = [PF.mark_open(r, marks.get(r["symbol"])) for r in rows if r["status"] != "closed"]
    try:
        decs = db.q("SELECT verdict, checks, trade_id, strategy, symbol FROM agent_decisions WHERE t>=? AND t<?", (d0, d1))
        susp = db.q("SELECT strategy, reason FROM agent_strategy_status WHERE status='suspended'")
        allt = P.stats(db.q("SELECT * FROM agent_trades WHERE status='closed' AND mode='paper' AND closed_at<? ORDER BY closed_at, id", (d1,)))
        eq = _agent_equity(d0, d1)
        if allt.get("trades"):
            allt["max_drawdown_pct"] = max((v["drawdown_pct"] for v in eq.values()), default=0)
    except Exception:                                     # noqa: BLE001 - agent tables not created yet
        decs, susp, allt, eq = [], [], {}, {}
    rep = build(day, rows, decs, allt, eq, open_rows, susp)
    if save:
        DIR.mkdir(parents=True, exist_ok=True)
        (DIR / f"{day}.json").write_text(json.dumps(rep, indent=1), encoding="utf-8")
    return rep


def saved(day: str) -> dict | None:
    try:
        return json.loads((DIR / f"{day}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def listing(n: int = 60) -> list[dict]:
    out = []
    for p in sorted(DIR.glob("*.json"), reverse=True)[:n]:
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            out.append({"day": r["day"], "totals": r.get("totals", {}), "sent": r.get("sent"), "generated": r.get("generated")})
        except (OSError, ValueError, KeyError):
            continue
    return out


async def send(rep: dict) -> dict:
    try:
        from backend.bot import service as B
    except Exception:                                     # noqa: BLE001
        return {"ok": False, "why": "Telegram sending runs on the PC version"}
    res = await B.tg(text(rep))
    if res.get("ok"):
        rep["sent"] = int(time.time())
        DIR.mkdir(parents=True, exist_ok=True)
        (DIR / f"{rep['day']}.json").write_text(json.dumps(rep, indent=1), encoding="utf-8")
    return res


def _due(now: float, cfg: dict) -> str | None:
    """The day whose final report should be produced now, or None: today once report_time has passed, otherwise
    yesterday if its final report is missing (MarketMiner was off at report time)."""
    t = dt.datetime.fromtimestamp(now, IST)
    hh, mm = map(int, cfg["time"].split(":"))
    day = t.strftime("%Y-%m-%d") if (t.hour, t.minute) >= (hh, mm) else (t - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    return None if (saved(day) or {}).get("final") else day


async def loop() -> None:
    await asyncio.sleep(60)                               # let the feeds start
    while True:
        try:
            cfg = settings()
            if cfg["enabled"]:
                day = _due(time.time(), cfg)
                if day:
                    rep = await make(day)
                    rep["final"] = True
                    (DIR / f"{day}.json").write_text(json.dumps(rep, indent=1), encoding="utf-8")
                    if cfg["telegram"]:
                        await send(rep)
                    log.info("daily report %s written", day)
        except asyncio.CancelledError:
            raise
        except Exception as e:                            # noqa: BLE001 - a report problem never stops the app
            log.warning("daily report: %s", e)
        await asyncio.sleep(120)


def start() -> None:
    asyncio.get_event_loop().create_task(loop())
