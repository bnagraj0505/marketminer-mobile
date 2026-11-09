"""Risk Guardian: the only authority that can approve a trade. Deterministic, independent of the strategy and
scoring code, and impossible to bypass: execution.open_position() refuses anything without a fresh APPROVED decision
row written here, and the approval is single-use.

Position size = maximum permissible monetary risk / estimated loss per unit, where the loss per unit is
    |entry - stop|  + entry slippage + exit slippage at the stop + gap allowance (gap_allowance_atr x ATR)
and the round-trip fees at the stop are then added; the size is reduced until the whole maximum loss fits the
budget. Sizes are rounded DOWN to the exchange lot. Risk per trade is halved (never increased) after
`derisk_after_losses` consecutive losses - so a losing streak can only shrink size.

INR book (user rule 2026-11-04): capital in open positions <= inr_day_capital (2L) is the one fixed limit. Per-trade
risk, the day stop and the lot count are decided by the AI risk engine (airisk.py) for each trade; today's realised
loss + open risk + the new trade's risk must stay inside the AI's day stop.
USD book: equity / capital_parts, part_risk_pct budget split across risk_slices trades.

Every check is recorded with its result and numbers, whether the trade is approved or not.
"""
from __future__ import annotations

import datetime as dt
import math
import time

from backend.lab import costs as C
from backend.lab import protect as LP
from backend.lab import store
from . import config as CFG
from . import db

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
CRYPTO = ("SHARK", "DELTAF", "DELTA", "BINANCE")


def currency(market: str) -> str:
    return "INR" if market in ("NSE", "FNO", "MCX") else "USD"


def is_option(symbol: str) -> bool:
    s = symbol.upper()
    return s.endswith(("CE", "PE")) or s.startswith("DELTA:") or "-ATM-" in s


def day_start(now: float) -> int:
    d = dt.datetime.fromtimestamp(now, IST)
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=IST).timestamp())


def week_start(now: float) -> int:
    return day_start(now) - dt.datetime.fromtimestamp(now, IST).weekday() * 86400


def book(cur: str, now: float | None = None, mode: str = "paper") -> dict:
    """Equity, peak, drawdown and P&L windows for one currency of the agent's book."""
    now = now or time.time()
    r = CFG.risk()
    cap = r["capital_inr"] if cur == "INR" else r["capital_usd"]
    closed = db.q("SELECT pnl, closed_at, symbol FROM agent_trades WHERE status='closed' AND currency=? AND mode=? ORDER BY closed_at, id",
                  (cur, mode))
    eq = peak = cap
    for t in closed:
        eq += t["pnl"] or 0
        peak = max(peak, eq)
    d0, w0 = day_start(now), week_start(now)
    day = sum(t["pnl"] or 0 for t in closed if (t["closed_at"] or 0) >= d0)
    week = sum(t["pnl"] or 0 for t in closed if (t["closed_at"] or 0) >= w0)
    allc = db.q("SELECT pnl, closed_at, symbol FROM agent_trades WHERE status='closed' AND mode=? ORDER BY closed_at DESC, id DESC LIMIT 50", (mode,))
    streak = 0
    for t in allc:
        if (t["pnl"] or 0) < 0:
            streak += 1
        else:
            break
    streak_today = streak if allc and (allc[0]["closed_at"] or 0) >= d0 else 0
    losses_today = sum(1 for t in closed if (t["closed_at"] or 0) >= d0 and (t["pnl"] or 0) < 0)
    open_ = db.q("SELECT * FROM agent_trades WHERE status='open' AND mode=?", (mode,))
    return {"currency": cur, "capital": cap, "equity": eq, "peak": peak, "drawdown_pct": (peak - eq) / peak * 100 if peak else 0,
            "day_pnl": day, "week_pnl": week, "loss_streak": streak, "loss_streak_today": streak_today,
            "losses_today": losses_today, "open": open_,
            "recent": allc}


def open_risk(cur: str, mode: str = "paper") -> float:
    """Money still at risk in open positions of one currency: remaining quantity x distance to the CURRENT stop. A
    runner whose stop is at breakeven (after lot 1 booked) carries no risk."""
    tot = 0.0
    for t in db.q("SELECT * FROM agent_trades WHERE status='open' AND mode=? AND currency=?", (mode, cur)):
        st = db.jload(t["payload"], {}) or {}
        if st.get("kind") == "options":
            tot += t["qty"] * _opt_frac(st) * max(0.0, st.get("v0", 0) - st.get("sl", 0))
        else:
            dr = 1 if t["side"] == "LONG" else -1
            tot += t["qty"] * st.get("left", 1.0) * max(0.0, (t["entry"] - st.get("stop_now", t["stop"])) * dr)
    return tot


def capital_in_use(cur: str, mode: str = "paper") -> float:
    """Capital tied up in open positions of one currency: premium paid for long options / debit spreads, the
    worst-case loss (margin proxy) for hedged credit structures, notional for everything else."""
    tot = 0.0
    for t in db.q("SELECT * FROM agent_trades WHERE status='open' AND mode=? AND currency=?", (mode, cur)):
        st = db.jload(t["payload"], {}) or {}
        if st.get("kind") == "options":
            rem = t["qty"] * _opt_frac(st)
            v0 = st.get("v0", 0) or 0
            tot += rem * (v0 if v0 > 0 else (st.get("max_unit") or abs(v0)))
        else:
            tot += t["qty"] * st.get("left", 1.0) * abs(t["entry"] or 0)
    return tot


def _opt_frac(st: dict) -> float:
    """Share of an options position still open (older positions: 2 equal lots)."""
    if "frac_left" in st:
        return float(st["frac_left"])
    return st.get("lots_left", 2) / 2


def book_qty(q: float, lot: float, fractional: bool) -> float:
    """How much books at 1:2: half the lots (rounded down) with 2+ lots; nothing with a single lot (that position
    moves its stop to breakeven at 1:2 and trails whole)."""
    if fractional:
        return q / 2
    lots = int(round(q / lot)) if lot else 0
    return (lots // 2) * lot


def new_trade_capital(sz: dict, is_opt: bool) -> float:
    if is_opt:
        return float(sz.get("premium") or 0) if sz.get("kind") not in _credit_kinds() else float(sz.get("structural_max_loss") or 0)
    return float(sz.get("notional") or 0)


def _credit_kinds() -> tuple:
    from . import options as OP
    return tuple(OP.CREDIT)


def budgets(equity: float, r: dict, loss_streak: int = 0, cur: str = "USD", bk: dict | None = None,
            setup: dict | None = None, ctx: dict | None = None) -> dict:
    derisk = loss_streak >= r["derisk_after_losses"]
    if cur == "INR":                                      # AI risk engine (no setup: neutral confidence)
        from . import airisk as AI
        a = AI.decide(bk or {}, r, setup, ctx)
        return {"part": r["inr_day_capital"], "part_budget": a["day_limit"], "per_trade": a["per_trade"], "derisked": False,
                "rule": "ai", "ai": a}
    part = equity / r["capital_parts"]
    part_budget = part * r["part_risk_pct"] / 100
    per_trade = part_budget / r["risk_slices"]
    return {"part": part, "part_budget": part_budget, "per_trade": per_trade * (0.5 if derisk else 1.0), "derisked": derisk, "rule": "parts"}


def day_closed(cur: str, bk: dict, r: dict) -> str | None:
    """Reason the day is closed for new entries in this book, or None."""
    if cur == "INR":
        from . import airisk as AI
        return AI.day_closed(bk, r, day_start(time.time()))
    lim = bk["capital"] * r["daily_loss_pct"] / 100
    if bk["day_pnl"] <= -lim:
        return f"today's loss {-bk['day_pnl']:,.0f} {cur} reached the {lim:,.0f} day limit"
    return None


def halt_state() -> dict:
    return store.get_setting("agent_halt") or {"on": False}


def set_halt(on: bool, reason: str = "") -> dict:
    v = {"on": bool(on), "reason": reason, "t": int(time.time())}
    store.set_setting("agent_halt", v)
    return v


def size(symbol: str, market: str, entry: float, stop: float, targets: list[float], atr: float, equity: float,
         risk_pct: float, r: dict | None = None, max_notional: float | None = None) -> dict:
    r = r or CFG.risk()
    d = 1 if entry > stop else -1
    long = d > 0
    slip_in = abs(C.slip_price(entry, d, market) - entry)
    slip_out = abs(C.slip_price(stop, -d, market) - stop)
    gap = r["gap_allowance_atr"] * (atr or 0)
    per_unit = abs(entry - stop) + slip_in + slip_out + gap
    budget = equity * risk_pct / 100
    lot = LP.lot_of(symbol)
    fractional = market in CRYPTO and lot == 1
    if per_unit <= 0:
        return {"qty": 0, "why": "zero risk per unit"}

    def floor_q(x: float) -> float:
        return math.floor(x * 1e4) / 1e4 if fractional else math.floor(x / lot) * lot

    def max_loss(q: float) -> float:
        return q * per_unit + C.round_trip(symbol, market, q, entry, stop, long)

    q = floor_q(budget / per_unit)
    step = (q * 0.01 if fractional else lot)
    guard = 0
    while q > 0 and max_loss(q) > budget and guard < 500:
        q = floor_q(q - max(step, 1e-4 if fractional else lot))
        guard += 1
    premium_cap = None
    if is_option(symbol) and long:
        premium_cap = budget * r["option_premium_risk_x"]
        while q > 0 and q * entry > premium_cap:
            q = floor_q(q - (lot if not fractional else max(q * 0.01, 1e-4)))
    if max_notional is not None:                          # INR day capital: cash quantity shrinks to fit what is left of it
        while q > 0 and q * entry > max_notional + 1e-6:
            q = floor_q(q - (lot if not fractional else max(q * 0.01, 1e-4)))
    if q <= 0:
        one = lot if not fractional else 1e-4
        return {"qty": 0, "lot": lot, "budget": budget, "per_unit_loss": per_unit,
                "why": f"one lot ({lot:g}) would risk {max_loss(one):,.2f} {currency(market)}, more than the {budget:,.2f} budget"}
    t2 = targets[1] if len(targets) > 1 else targets[0]
    fees_target = C.round_trip(symbol, market, q, entry, t2, long)
    reward_net = q * (abs(t2 - entry) - slip_in - abs(C.slip_price(t2, -d, market) - t2)) - fees_target
    ml = max_loss(q)
    fees_stop = C.round_trip(symbol, market, q, entry, stop, long)
    fee_u = fees_stop / q
    k_r = r.get("first_target_r", 2.0)
    dist1 = k_r * (per_unit + fee_u) + slip_in + abs(C.slip_price(entry, -d, market) - entry) + fee_u
    tp1 = entry + d * dist1                               # lot 1: books at 1:2 AFTER costs
    bq = book_qty(q, lot, fractional)
    h = bq if bq > 0 else q                               # single lot: the 1:2 point is where the stop goes to breakeven
    loss1 = h * per_unit + C.round_trip(symbol, market, h, entry, stop, long)
    for _ in range(200):                                  # widen until lot 1 nets at least k_r x its loss after every cost
        gain1 = h * (dist1 - slip_in - abs(C.slip_price(tp1, -d, market) - tp1)) - C.round_trip(symbol, market, h, entry, tp1, long)
        if gain1 >= k_r * loss1:
            break
        dist1 *= 1.002
        tp1 = entry + d * dist1
    return {"qty": q, "lot": lot, "lots": (q / lot) if lot > 1 else None, "book_qty": bq, "budget": round(budget, 2), "per_unit_loss": per_unit, "max_loss": round(ml, 2),
            "tp1": tp1, "rr_lot1": round(gain1 / loss1, 2) if loss1 else 0,
            "fees_at_stop": round(fees_stop, 2), "slippage_cost": round(q * (slip_in + slip_out), 2), "gap_allowance": round(q * gap, 2),
            "reward_net_t2": round(reward_net, 2), "rr_net": round(reward_net / ml, 2) if ml else 0,
            "cost_share": round((fees_stop + q * (slip_in + slip_out)) / ml, 3) if ml else 1,
            "notional": round(q * entry, 2), "premium_cap": premium_cap, "currency": currency(market)}


def _ist_minutes(now: float) -> tuple[int, int]:
    t = dt.datetime.fromtimestamp(now, IST)
    return t.weekday(), t.hour * 60 + t.minute


def evaluate(setup: dict, ctx: dict, now: float | None = None) -> dict:
    """setup: from agents.build_setup. ctx: {quality, picture, evidence, avg_volume, mode, strategy_status}.
    Returns {verdict: APPROVED | REJECTED | OBSERVE, checks: [...], sizing}."""
    now = now or time.time()
    r = CFG.risk()
    a = CFG.agent()
    mode = ctx.get("mode") or a["mode"]
    sym, market, d = setup["symbol"], setup["market"], setup["direction"]
    cur = currency(market)
    bk = book(cur, now)
    checks: list[dict] = []

    def chk(name: str, ok: bool, detail: str, hard: bool = True):
        checks.append({"check": name, "ok": bool(ok), "detail": detail, "hard": hard})

    em = CFG.emergency()
    chk("Emergency stop", not em.get("on"), "off" if not em.get("on") else f"ON: {em.get('reason') or 'manual'}")
    h = halt_state()
    chk("Drawdown halt", not h.get("on"), "not halted" if not h.get("on") else f"HALTED: {h.get('reason')}")
    op = ctx.get("options_plan")                       # {"structure": ..., "plan": ...} when this is an options trade
    st_ = (op or {}).get("structure") or {}
    if op:
        from . import options as OP
        chk("Tradable market", True, f"{st_.get('underlying')} options (NSE/BSE F&O via FYERS quotes)")
        cov, why_cov = OP.covered(st_.get("legs") or [])
        pl = op.get("plan") or {}
        unb = (pl.get("payoff") or {}).get("unbounded", True)
        chk("No unhedged option selling", cov and not unb, why_cov + ("; payoff verified: loss is capped" if cov and not unb else
                                                                     "; payoff UNBOUNDED" if unb else ""))
    elif CFG.index_of(sym):
        chk("Tradable market", False, f"{sym} is an index: indices are traded only through options (options desk), never futures or spot")
    else:
        chk("Tradable market", market in CFG.TRADABLE_MARKETS, f"{market} {'is' if market in CFG.TRADABLE_MARKETS else 'is not'} paper-tradable by the agent")
        short_opt = is_option(sym) and d == "SHORT"
        chk("No unhedged option selling", not short_opt,
            "short option = undefined risk: options are traded through the options desk as hedged structures" if short_opt else "not a short option")
    q = ctx.get("quality") or {"ok": False, "blocks": ["no quality report"]}
    chk("Data quality", q["ok"], "; ".join(q["blocks"]) or f"closed candles, fresh, no gaps ({q.get('source')})")
    if d == "NEUTRAL":
        stop_ok = bool(op and (op.get("plan") or {}).get("sl_value") is not None)
        chk("Stop-loss defined", stop_ok, f"position stop at value {(op.get('plan') or {}).get('sl_value', 0):,.2f}/unit; invalidation: {setup.get('invalidation')}"
            if stop_ok else "no position stop")
    else:
        stop_ok = setup.get("stop") is not None and ((setup["entry"] - setup["stop"]) * (1 if d == "LONG" else -1) > 0)
        chk("Stop-loss defined", stop_ok and bool(setup.get("invalidation")),
            f"stop {setup.get('stop')} / invalidation: {setup.get('invalidation')}" if stop_ok else "stop missing or on the wrong side")
    pic = ctx.get("picture") or {}
    chk("Predictable regime", not pic.get("unpredictable"), f"{pic.get('regime_label')} · volatility {pic.get('volatility')}")
    ss = ctx.get("strategy_status") or "active"
    chk("Strategy active", ss != "suspended", f"strategy {setup['strategy']} is {ss}")
    try:                                                # learned from the agent's own results: can only block, never allow
        from . import diagnose as DG
        lf = DG.filter_hit(setup, ctx, now)
    except Exception:                                   # noqa: BLE001 - a diagnosis problem must never stop risk checks
        lf = None
    chk("Learned filter", not lf, f"blocked by learned filter {lf['label']}: {lf['why']}" if lf else "no learned filter matches")
    val = (setup.get("validation") or {}).get("status", "not backtested")
    chk("Strategy validated", val == "validated" or not r["require_validated"],
        f"backtest status: {val}" + ("" if val == "validated" else " (experimental - allowed in paper)" if not r["require_validated"] else ""),
        hard=bool(r["require_validated"]))
    ms = max(r["min_score"], float(ctx.get("min_score") or 0))      # research thresholds can only RAISE the bar
    ev = ctx.get("evidence") or {}
    if d == "NEUTRAL":
        b_, br_ = pic.get("bull") or 0, pic.get("bear") or 0
        rng = pic.get("regime") in ("range", "choppy") and pic.get("volatility") != "high" and abs(b_ - br_) < 20
        chk("Range confirmation", rng, f"regime {pic.get('regime_label')}, volatility {pic.get('volatility')}, bull {b_:.0f} vs bear {br_:.0f} (need a range, not high volatility, |bull-bear| < 20)")
    else:
        chk("Score", setup["score"] >= ms, f"{setup['score']:.0f} vs minimum {ms:.0f} (a rank, not a probability)")
        chk("Independent evidence", ev.get("independent_confirmations", 0) >= r["min_independent_evidence"]
            and ev.get("independent_conflicts", 0) <= r["max_opposing_evidence"],
            f"{ev.get('independent_confirmations', 0)} families agree ({', '.join(ev.get('agree', [])) or '-'}), "
            f"{ev.get('independent_conflicts', 0)} against ({', '.join(ev.get('oppose', [])) or '-'})")
    if market in ("NSE", "FNO", "MCX"):
        wd, m = _ist_minutes(now)
        close = 930 if market != "MCX" else 1410
        ok = wd < 5 and 570 <= m <= close - r["india_no_entry_last_min"]
        last = min(close - r["india_no_entry_last_min"], r["opt_last_entry_min"]) if op else close - r["india_no_entry_last_min"]
        ok = wd < 5 and 570 <= m <= last
        chk("Session timing", ok, f"IST {m // 60:02d}:{m % 60:02d}; entries 09:30 to {last // 60:02d}:{last % 60:02d}")
    cap = bk["equity"]
    if cur == "INR":
        from . import airisk as AI
        why_closed = AI.day_closed(bk, r, day_start(now))
        chk("AI day stop", not why_closed, why_closed or f"today {bk['day_pnl']:,.0f} INR vs AI day stop -{AI.day_limit(r):,.0f}; "
            f"{bk['losses_today']} losing trade(s) today")
    else:
        dlim = bk["capital"] * r["daily_loss_pct"] / 100
        chk("Daily loss limit", bk["day_pnl"] > -dlim, f"today {bk['day_pnl']:,.2f} {cur} vs limit -{dlim:,.2f}")
    chk("Weekly loss limit", bk["week_pnl"] > -bk["capital"] * r["weekly_loss_pct"] / 100,
        f"this week {bk['week_pnl']:,.2f} {cur} vs limit -{bk['capital'] * r['weekly_loss_pct'] / 100:,.2f}")
    chk("Max drawdown", bk["drawdown_pct"] < r["max_drawdown_pct"], f"{bk['drawdown_pct']:.2f}% below peak vs limit {r['max_drawdown_pct']}%")
    chk("Loss-streak lockout", bk["loss_streak_today"] < r["loss_streak_lockout"],
        f"{bk['loss_streak_today']} consecutive losses today (lockout at {r['loss_streak_lockout']})")
    last_loss = next((t for t in bk["recent"] if t["symbol"] == sym and (t["pnl"] or 0) < 0), None)
    cool_ok = not last_loss or now - (last_loss["closed_at"] or 0) >= r["cooldown_min"] * 60
    chk("Revenge-trade cooldown", cool_ok, "no recent loss on this symbol" if not last_loss else
        f"last {sym} loss {int((now - (last_loss['closed_at'] or 0)) / 60)} min ago (cooldown {r['cooldown_min']} min)")
    all_open = db.q("SELECT * FROM agent_trades WHERE status='open' AND mode=?", (mode if mode != "intelligence" else "paper",))
    chk("Concurrent positions", len(all_open) < r["max_open"], f"{len(all_open)} open (max {r['max_open']})")
    g = LP.group_of(sym)
    same_g = [t for t in all_open if LP.group_of(t["symbol"]) == g]
    chk("Correlated exposure", g.startswith("single:") or len(same_g) < r["max_per_group"],
        f"group {g}: {len(same_g)} open (max {r['max_per_group']})")
    same = [t for t in all_open if t["symbol"] == sym]
    chk("No averaging / stacking", not same, "no open position in this symbol" if not same else f"already {same[0]['side']} {sym} (#{same[0]['id']})")
    bud = budgets(cap, r, bk["loss_streak"], cur, bk, setup, ctx)
    ai = bud.pop("ai", None)
    if ai:
        chk("AI risk decision", True, f"risk {ai['per_trade']:,.0f} INR this trade, day stop {ai['day_limit']:,.0f}: " + "; ".join(ai["factors"]), hard=False)
    risk_pct = bud["per_trade"] / cap * 100
    orisk = open_risk(cur)
    used_loss = max(0.0, -bk["day_pnl"])

    ml_mult, ml_note = _ml_checks(chk, ctx, bud, orisk, used_loss, sym, bool(op))

    def part_check(new_risk: float, sz: dict | None = None):
        tot = orisk + new_risk + used_loss
        if cur == "INR":
            chk("Part risk budget", tot <= bud["part_budget"] + 1e-6,
                f"AI day stop {bud['part_budget']:,.0f} INR: open risk {orisk:,.0f} + this trade {new_risk:,.0f} + today's realised loss "
                f"{used_loss:,.0f} = {tot:,.0f} (even if every open trade hits its stop, the day cannot lose more than {bud['part_budget']:,.0f})")
            used_cap = capital_in_use(cur)
            new_cap = new_trade_capital(sz or {}, bool(op))
            chk("Day capital", used_cap + new_cap <= r["inr_day_capital"] + 1e-6,
                f"capital in open positions {used_cap:,.0f} + this trade {new_cap:,.0f} = {used_cap + new_cap:,.0f} INR vs {r['inr_day_capital']:,.0f} per day")
            return
        chk("Part risk budget", tot <= bud["part_budget"] + 1e-6,
            f"trading part {bud['part']:,.0f} {cur} (equity / {r['capital_parts']}), budget {r['part_risk_pct']}% = {bud['part_budget']:,.0f}: "
            f"open risk {orisk:,.0f} + this trade {new_risk:,.0f} + today's realised loss {used_loss:,.0f} = {tot:,.0f}")
    if op:
        sz = _options_checks(chk, op, setup, ctx, cap, risk_pct, r, now,
                             max_capital=max(0.0, r["inr_day_capital"] - capital_in_use(cur)) if cur == "INR" else None)
        sz["risk_pct_used"], sz["derisked"] = risk_pct, bud["derisked"]
        sz["ml"] = ml_note
        sz["ai_risk"] = ai
        sz["budgets"] = {k: round(v, 2) if isinstance(v, float) else v for k, v in bud.items()}
        part_check(sz.get("max_loss") or 0, sz)
        hard_fail = [c for c in checks if not c["ok"] and c["hard"]]
        verdict = "REJECTED" if hard_fail else ("APPROVED" if mode == "paper" else "OBSERVE")
        return {"verdict": verdict, "checks": checks, "sizing": sz, "failed": [c["check"] for c in hard_fail], "book": {
            k: (round(v, 2) if isinstance(v, float) else v) for k, v in bk.items() if k not in ("open", "recent")}}
    risk_used = risk_pct * min(1.0, ml_mult)               # the model can only LOWER the risk, never raise it
    cap_left = max(0.0, r["inr_day_capital"] - capital_in_use(cur)) if cur == "INR" else None
    sz = size(sym, market, setup["entry"], setup["stop"], setup["targets"], setup.get("atr") or 0, cap, risk_used, r,
              max_notional=cap_left) if stop_ok else {"qty": 0, "why": "no valid stop"}
    sz["risk_pct_used"] = risk_used
    sz["ml"] = ml_note
    sz["derisked"] = bud["derisked"]
    sz["ai_risk"] = ai
    sz["budgets"] = {k: round(v, 2) if isinstance(v, float) else v for k, v in bud.items()}
    chk("Position size", (sz.get("qty") or 0) > 0, sz.get("why") or
        f"{sz['qty']:g} units (lot {sz['lot']:g}) risking {sz['max_loss']:,.2f} of {sz['budget']:,.2f} {cur} budget ({risk_used:g}% of equity{', halved after losses' if sz['derisked'] else ''}"
        f"{', halved by the ML model (low confidence)' if ml_mult < 1 else ''})")
    if sz.get("qty"):
        chk("Maximum loss within budget", sz["max_loss"] <= sz["budget"] + 1e-6,
            f"max loss {sz['max_loss']:,.2f} incl. fees {sz['fees_at_stop']:,.2f}, slippage {sz['slippage_cost']:,.2f}, gap allowance {sz['gap_allowance']:,.2f}")
        part_check(sz["max_loss"], sz)
        lab_t2 = setup["targets"][1] if len(setup.get("targets") or []) > 1 else None
        room = lab_t2 is not None and (lab_t2 - sz["tp1"]) * (1 if d == "LONG" else -1) >= 0
        chk("Room to 1:2 target", room, f"lot 1 books at {sz['tp1']:,.4g} ({sz['rr_lot1']:.2f}R after costs); the setup's T2 liquidity level is "
            f"{lab_t2:,.4g}" + ("" if room else " - an opposing level sits before the 1:2 target") if lab_t2 is not None else "no T2 level")
        chk("Costs vs risk", sz["cost_share"] <= r["max_cost_share"], f"fees + slippage = {sz['cost_share'] * 100:.0f}% of max loss (max {r['max_cost_share'] * 100:.0f}%)")
        lev_base = sz["notional"]
        chk("Leverage", lev_base <= r["max_notional_x"] * cap, f"notional {lev_base:,.0f} = {lev_base / cap:.2f}x equity (max {r['max_notional_x']}x)")
        av = ctx.get("avg_volume")
        if av:
            chk("Liquidity", sz["qty"] <= av * r["max_volume_share"], f"qty {sz['qty']:g} vs {r['max_volume_share'] * 100:g}% of avg candle volume {av:,.0f}")
        else:
            chk("Liquidity", True, "no volume data for this instrument (index/future proxy): not checked", hard=False)
        cfg_c = {**C.DEFAULTS, **(store.get_setting("costs") or {})}
        spread = setup["entry"] * cfg_c["spread_bps"].get(market, 2.0) / 10000
        chk("Spread", spread <= (setup.get("max_spread") or spread), f"estimated spread {spread:,.4g} vs max {setup.get('max_spread') or 0:,.4g} (model spread; no live depth feed)")
    hard_fail = [c for c in checks if not c["ok"] and c["hard"]]
    verdict = "REJECTED" if hard_fail else ("APPROVED" if mode == "paper" else "OBSERVE")
    return {"verdict": verdict, "checks": checks, "sizing": sz, "failed": [c["check"] for c in hard_fail], "book": {
        k: (round(v, 2) if isinstance(v, float) else v) for k, v in bk.items() if k not in ("open", "recent")}}


def _ml_checks(chk, ctx: dict, bud: dict, orisk: float, used_loss: float, sym: str, is_options: bool) -> tuple[float, dict | None]:
    """ML layer (ml.py), ACTIVE mode only. It can add a failing check or halve the risk - never anything else."""
    mlp = ctx.get("ml")
    if not mlp or mlp.get("mode") != "active":
        return 1.0, ({"mode": mlp.get("mode"), "p": mlp.get("p"), "action": mlp.get("action"), "effect": "none (shadow)"} if mlp else None)
    p, a, th = mlp["p"], mlp["action"], mlp.get("thresholds") or {}
    chk("ML filter", a != "skip", f"model #{mlp.get('model_id')}: {p * 100:.0f}% chance of 1:2 before the stop (base rate {mlp.get('base_rate', 0) * 100:.0f}%); "
        f"skip below {th.get('skip_below', 0) * 100:.0f}%, full size from {th.get('full_from', 0) * 100:.0f}% -> {a.upper()}")
    room = bud["part_budget"] - orisk - used_loss
    last = room < 2 * bud["per_trade"]
    chk("ML budget priority", not (last and a != "full"),
        (f"room for one more trade ({room:,.0f}): kept for a FULL-confidence setup, this one is {a}" if last and a != "full" else
         f"room {room:,.0f} = {room / (bud['per_trade'] or 1):.1f} trades: no rationing needed" if not last else "full-confidence setup: may use the last slot"))
    mult = 0.5 if a == "half" else 1.0                   # no lot rule any more: half confidence = half risk for every market
    eff = "half risk" if mult < 1 else "full risk"
    return mult, {"mode": "active", "p": p, "action": a, "effect": eff, "model_id": mlp.get("model_id")}


def _options_checks(chk, op: dict, setup: dict, ctx: dict, cap: float, risk_pct: float, r: dict, now: float,
                    max_capital: float | None = None) -> dict:
    from . import options as OP
    st, pl = op["structure"], op.get("plan")
    idx = OP.is_index(st["underlying"])
    age = now - (st.get("chain_as_of") or 0)
    chk("Option prices fresh", age <= r["opt_max_chain_age_s"], f"chain/quotes {int(age)} s old (max {r['opt_max_chain_age_s']} s)")
    bad = OP.leg_liquidity(st["legs"], r, idx)
    chk("Option leg liquidity", not bad, "; ".join(bad) or f"all {len(st['legs'])} leg(s) two-sided, spreads within {r['opt_max_leg_spread_pct' if idx else 'opt_max_leg_spread_pct_stock']}%, open interest present")
    sells = any(lg["side"] == "SELL" for lg in st["legs"])
    if sells:
        chk("Days to expiry (selling)", st["days"] >= r["opt_min_dte_sell"], f"{st['days']:.1f} days to {st['expiry']} (min {r['opt_min_dte_sell']} for any short leg)")
    if not pl:
        chk("Position size", False, "no executable prices for every leg")
        return {"qty": 0, "why": "no executable prices"}
    budget = cap * risk_pct / 100
    sz = OP.size(st, pl, budget, r, max_capital)
    sz.update(structure=OP.describe(st), kind=st["kind"], v0=pl["v0"], sl_value=pl["sl_value"], tp_value=pl["tp_value"], tp1_value=pl["tp1_value"],
              trail_unit=pl["trail_unit"],
              breakevens=pl["payoff"]["breakevens"])
    chk("Position size", (sz.get("qty") or 0) > 0, sz.get("why") or
        f"{sz['lots']} lots x {sz['lot']} = {sz['qty']} units of {OP.describe(st)}; risking {sz['max_loss']:,.0f} of {sz['budget']:,.0f} INR per-trade risk ({risk_pct:.3g}% of equity)")
    if not sz.get("qty"):
        return sz
    chk("Maximum loss within budget", sz["max_loss"] <= sz["budget"] + 1e-6, f"planned loss at the position stop {sz['max_loss']:,.0f} incl. fees {sz['fees']:,.0f}")
    chk("Structural maximum loss", sz["structural_max_loss"] <= sz["budget"] * r["option_premium_risk_x"] + 1e-6,
        f"worst case at expiry (payoff verified) {sz['structural_max_loss']:,.0f} vs {r['option_premium_risk_x']:g} x budget {sz['budget'] * r['option_premium_risk_x']:,.0f}")
    lens = st.get("lens") or {}
    dr = st.get("direction")
    against = (dr == "LONG" and lens.get("view") == "bearish") or (dr == "SHORT" and lens.get("view") == "bullish")
    chk("Seller's view", not against and (dr != "NEUTRAL" or lens.get("view") == "range"),
        f"writers: {lens.get('view')} ({lens.get('basis')}); " + "; ".join(lens.get("reads") or []))
    chk("1:2 achievable", bool(pl.get("rr_ok")), pl.get("rr_why") or
        f"lot 1 books at value {pl['tp1_value']:,.2f} (1:2 after costs: {sz['rr_net']:.2f}R on lot 1); lot 2 then trails {pl['trail_unit']:,.2f}/unit")
    if st["kind"] in OP.CREDIT:
        cr = pl.get("credit_ratio") or 0
        chk("Credit vs width", cr >= r["opt_min_credit_ratio"], f"credit {-pl['v0']:,.2f} = {cr * 100:.0f}% of the {pl['width']:g}-point width (min {r['opt_min_credit_ratio'] * 100:.0f}%)")
    else:
        u = st.get("und") or {}
        wall, tgt = u.get("wall"), u.get("und_target_1")
        ok = tgt is not None and (wall is None or ((wall > tgt) if dr == "LONG" else (wall < tgt)))
        chk("Room to 1:2 target", ok, f"underlying 1:2 point {tgt:,.0f}; writers' wall {wall if wall is not None else 'none'}"
            if tgt is not None else "no underlying plan")
    chk("Costs vs risk", sz["fees"] <= r["max_cost_share"] * sz["max_loss"], f"fees {sz['fees']:,.0f} = {sz['fees'] / sz['max_loss'] * 100:.0f}% of the planned loss (max {r['max_cost_share'] * 100:.0f}%); spreads already paid by buying at ask / selling at bid")
    chk("Leverage", sz["premium"] <= r["max_notional_x"] * cap, f"premium {sz['premium']:,.0f} = {sz['premium'] / cap:.2f}x equity (max {r['max_notional_x']}x)")
    return sz


def post_trade_limits(now: float | None = None) -> list[str]:
    """After every close: halt the agent when the drawdown limit is breached (stays halted until the user resets)."""
    r = CFG.risk()
    out = []
    for cur in ("INR", "USD"):
        bk = book(cur, now)
        if bk["drawdown_pct"] >= r["max_drawdown_pct"] and not halt_state().get("on"):
            set_halt(True, f"{cur} drawdown {bk['drawdown_pct']:.2f}% >= {r['max_drawdown_pct']}%")
            out.append(f"HALT: {cur} drawdown {bk['drawdown_pct']:.2f}%")
    return out
