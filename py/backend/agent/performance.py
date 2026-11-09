"""Performance intelligence for the agent's own book (never mixed with the Lab ledger, the Telegram bot or the Day
Desk). Cross-currency figures are in R (P&L / initial stop risk, after costs); money figures are per currency.

Metrics: trades, wins, losses, win rate, average win/loss (R and money), profit factor, expectancy with a 95%
confidence interval, max drawdown (R and % of equity), Sharpe and Sortino per trade, longest win/loss streaks,
costs, entry slippage, capital utilisation (risk and notional as % of equity), breakdowns by strategy, instrument,
timeframe, regime and session. Win rate is never used on its own.

Degradation (degradation_check): a strategy with >= 15 closed agent trades whose LAST 15 trades have expectancy
< -0.15R and profit factor < 0.8 is SUSPENDED from new agent paper trades (reason logged). If the strategy is
validated by backtest for a symbol/timeframe and its last 15 live-paper trades are below zero, a degradation alert is
raised even before suspension. Only the user can resume a suspended strategy.
"""
from __future__ import annotations

import math
import statistics

from . import config as CFG
from . import db
from . import guardian as G


def _streaks(rs: list[float]) -> tuple[int, int]:
    w = l = bw = bl = 0
    for r in rs:
        if r > 0:
            w, l = w + 1, 0
        else:
            l, w = l + 1, 0
        bw, bl = max(bw, w), max(bl, l)
    return bw, bl


def stats(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"trades": 0}
    rs = [r["r_result"] or 0 for r in rows]
    w = [x for x in rs if x > 0]
    lo = [x for x in rs if x <= 0]
    mean = sum(rs) / n
    sd = statistics.stdev(rs) if n > 1 else 0
    down = [min(0, x) for x in rs]
    dd_sd = math.sqrt(sum(x * x for x in down) / n) if n else 0
    eq = peak = mdd = 0.0
    for x in rs:
        eq += x
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    bw, bl = _streaks(rs)
    money: dict = {}
    for cur in ("INR", "USD"):
        c = [r for r in rows if r["currency"] == cur]
        if c:
            pw = [r["pnl"] for r in c if (r["pnl"] or 0) > 0]
            pl = [r["pnl"] for r in c if (r["pnl"] or 0) <= 0]
            money[cur] = {"trades": len(c), "pnl": round(sum(r["pnl"] or 0 for r in c), 2), "avg_profit": round(sum(pw) / len(pw), 2) if pw else 0,
                          "avg_loss": round(sum(pl) / len(pl), 2) if pl else 0, "costs": round(sum(r["costs"] or 0 for r in c), 2)}
    slip = [db.jload(r.get("payload"), {}).get("entry_slippage", 0) / (abs(r["entry"] - r["stop"]) or 1) for r in rows if r.get("payload")]
    half = 1.96 * sd / math.sqrt(n) if n > 1 else None
    return {"trades": n, "wins": len(w), "losses": len(lo), "win_rate": round(100 * len(w) / n, 1),
            "avg_win_r": round(sum(w) / len(w), 3) if w else 0, "avg_loss_r": round(sum(lo) / len(lo), 3) if lo else 0,
            "profit_factor": round(sum(w) / -sum(lo), 2) if lo and sum(lo) < 0 else None,
            "expectancy_r": round(mean, 3), "expectancy_r_ci95": [round(mean - half, 3), round(mean + half, 3)] if half is not None else [None, None],
            "total_r": round(sum(rs), 2), "max_drawdown_r": round(mdd, 2),
            "sharpe_per_trade": round(mean / sd, 3) if sd else None, "sortino_per_trade": round(mean / dd_sd, 3) if dd_sd else None,
            "max_win_streak": bw, "max_loss_streak": bl, "money": money,
            "avg_entry_slippage_r": round(sum(slip) / len(slip), 3) if slip else None}


def _group(rows, key):
    g: dict = {}
    for r in rows:
        g.setdefault(str(r.get(key)), []).append(r)
    return {k: stats(v) for k, v in sorted(g.items())}


def summary(mode: str = "paper") -> dict:
    rows = db.q("SELECT * FROM agent_trades WHERE status='closed' AND mode=? ORDER BY closed_at, id", (mode,))
    out = stats(rows)
    out.update(by_strategy=_group(rows, "strategy"), by_symbol=_group(rows, "symbol"), by_tf=_group(rows, "tf"),
               by_regime=_group(rows, "regime"), by_session=_group(rows, "session"))
    eqs, util = {}, {}
    dd_pct = 0.0
    for cur in ("INR", "USD"):
        bk = G.book(cur, mode=mode)
        c = [r for r in rows if r["currency"] == cur]
        curve, e, pk, worst = [], bk["capital"], bk["capital"], 0.0
        for r in c:
            e += r["pnl"] or 0
            pk = max(pk, e)
            worst = max(worst, (pk - e) / pk * 100 if pk else 0)
            curve.append({"t": r["closed_at"], "equity": round(e, 2)})
        eqs[cur] = {"capital": bk["capital"], "equity": round(bk["equity"], 2), "peak": round(bk["peak"], 2),
                    "drawdown_pct": round(bk["drawdown_pct"], 2), "max_drawdown_pct": round(worst, 2),
                    "day_pnl": round(bk["day_pnl"], 2), "week_pnl": round(bk["week_pnl"], 2), "curve": curve[-300:]}
        dd_pct = max(dd_pct, worst)                       # worst historical peak-to-trough, not just today's
        if c:
            util[cur] = {"avg_risk_pct_of_equity": round(100 * sum((r["risk_amount"] or 0) for r in c) / len(c) / bk["capital"], 3),
                         "avg_notional_x": round(sum(abs(r["qty"] * r["entry"]) for r in c) / len(c) / bk["capital"], 3)}
    out.update(equity=eqs, capital_utilisation=util, max_drawdown_pct=round(dd_pct, 2),
               note="Paper results from MarketMiner's own simulated fills (real candles, modelled costs). Small samples are noise; see the 95% interval.")
    return out


def degradation_check() -> list[str]:
    msgs = []
    for row in db.q("SELECT DISTINCT strategy FROM agent_trades WHERE status='closed'"):
        k = row["strategy"]
        rs = db.q("SELECT r_result, symbol, tf FROM agent_trades WHERE status='closed' AND strategy=? ORDER BY closed_at DESC, id DESC LIMIT 15", (k,))
        if len(rs) < 15:
            continue
        s = stats([{**r, "currency": "", "pnl": 0, "costs": 0, "entry": 1, "stop": 0} for r in rs])
        exp, pf = s["expectancy_r"], s["profit_factor"]
        st = db.q("SELECT status FROM agent_strategy_status WHERE strategy=?", (k,), one=True)
        if exp < -0.15 and (pf is None or pf < 0.8) and (st or {}).get("status") != "suspended":
            reason = f"last 15 agent trades: expectancy {exp:+.2f}R, profit factor {pf}"
            db.execute("INSERT INTO agent_strategy_status(strategy, status, reason, t) VALUES(?,?,?,?) ON CONFLICT(strategy) DO UPDATE SET "
                       "status=excluded.status, reason=excluded.reason, t=excluded.t", (k, "suspended", reason, db.now()))
            db.log("learning", f"Strategy {k} SUSPENDED from new paper trades: {reason}", level="warn")
            msgs.append(f"{k} suspended")
        elif exp < 0:
            from backend.lab import store
            if any((store.get_setting(f"validation:{r['symbol']}:{r['tf']}") or {}).get("status", {}).get(k, {}).get("status") == "validated" for r in rs):
                db.log("learning", f"Degradation alert: {k} is backtest-validated but its last 15 agent trades average {exp:+.2f}R", level="warn")
                msgs.append(f"{k} degradation alert")
    return msgs


def set_strategy_status(strategy: str, status: str, reason: str, who: str = "user") -> dict:
    if status not in ("active", "suspended"):
        raise ValueError("status must be active or suspended")
    before = db.q("SELECT * FROM agent_strategy_status WHERE strategy=?", (strategy,), one=True)
    db.execute("INSERT INTO agent_strategy_status(strategy, status, reason, t) VALUES(?,?,?,?) ON CONFLICT(strategy) DO UPDATE SET "
               "status=excluded.status, reason=excluded.reason, t=excluded.t", (strategy, status, reason, db.now()))
    db.config_change(who, f"strategy_status:{strategy}", before, {"status": status, "reason": reason})
    db.log("learning", f"Strategy {strategy} set {status} by {who}: {reason}")
    return {"strategy": strategy, "status": status}
