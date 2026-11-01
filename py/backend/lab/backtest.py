"""Event-driven backtester on the same engine as the live screen.

Rules
- Signals are evaluated on the CLOSE of candle i using only data known then (features.py). The entry is the OPEN of
  candle i+1 plus slippage and half the spread. If that open is already through the stop or past T1 the trade is
  skipped (logged as skipped, not counted).
- Exits: one third at T1 (stop moves to breakeven), one third at T2 (stop then trails at the higher of T1 and
  close - 2 x ATR for longs, mirror for shorts), the rest at T3. If the stop and a target are both inside one candle the
  STOP is assumed first (conservative). Gaps through the stop fill at the open. Time stop: 3 x the expected holding
  period (minimum 10 candles), exit at that close. Indian intraday trades are closed at the last candle of the session.
- One open trade per strategy at a time; strategies are simulated independently and then pooled.
- Costs: costs.py (Indian statutory charges, crypto taker fees, slippage, spread). Position size: protect.size with the
  protection settings, so R and currency P&L are both after costs. Crypto funding is not modelled.
- options_momentum needs a live option chain, so it is skipped in backtests (said so in the result). Option premiums
  are backtested only when the symbol IS an option with its own candles (DELTA:..., NSE:...CE/PE).

Validation
- Data is split by time: first 60% train, next 20% validation, last 20% an untouched test set used once.
- Parameters searched on train only: stop buffer {0.05, 0.1, 0.2} x ATR and minimum reward:risk {1.5, 2.0, 2.5}.
  The choice is the best train expectancy with at least 10 trades (else the defaults). The full grid is reported for
  validation as a sensitivity table, so a lucky single cell is visible.
- Rolling walk-forward: train+validation in 4 consecutive folds; for k = 1..3 parameters are picked on folds < k and
  measured on fold k.
- Monte Carlo: 1,000 bootstrap resamples of the full-period trade R sequence (fixed seed) -> 5th/50th/95th percentile
  of total R and of maximum drawdown, and the share of runs ending below zero.
- A strategy is marked VALIDATED for this symbol/timeframe only if validation AND test expectancy are > 0 with at least
  20 test+validation trades. Nothing is deployed automatically; the badge is information for the paper trader.
"""
from __future__ import annotations

import datetime as dt
import math
import random
import statistics

from .candle import TF_SECONDS
from .engine import Engine
from . import strategies as ST
from . import scoring as SC
from . import costs as C
from . import protect as P

GRID = [(sb, rr) for sb in (0.05, 0.1, 0.2) for rr in (1.5, 2.0, 2.5)]
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
WARMUP = 210


def _ideas(E: Engine, start: int, end: int, settings: dict | None) -> list[tuple[int, dict, dict, list]]:
    """Raw strategy ideas per candle (parameter-independent), with context and directional score."""
    out = []
    F = E.F
    st = settings or {}
    for i in range(start, end):
        if not ST.in_session(F, i, E.market):
            continue
        ctx = E.ctx(i)
        raws = []
        for key, fn in ST.MODULES.items():
            if key == "options_momentum" or not {**ST.PARAMS[key], **(st.get(key) or {})}.get("enabled", True):
                continue
            for d in (1, -1):
                try:
                    r = fn(F, i, d, ctx)
                except (IndexError, ZeroDivisionError, TypeError):
                    r = None
                if r:
                    raws.append((key, d, r))
        if raws:
            out.append((i, ctx, SC.directional(F, i, ctx), raws))
    return out


def _session_last(E: Engine, j: int) -> bool:
    F = E.F
    if E.market not in ("NSE", "FNO", "MCX") or F.step >= 86400:
        return False
    if j + 1 >= len(F.bars):
        return False
    return E.session_of(F.t[j + 1]) != E.session_of(F.t[j])


def _simulate(E: Engine, symbol: str, i: int, setup: dict, cost_cfg: dict, prot: dict) -> dict | None:
    F = E.F
    if i + 1 >= len(F.bars):
        return None
    d = 1 if setup["direction"] == "LONG" else -1
    entry = C.slip_price(F.bars[i + 1].open, d, E.market, cost_cfg)
    stop, (t1, t2, t3) = setup["stop"], setup["targets"]
    if (entry - stop) * d <= 0:
        return {"skipped": "opened through the stop"}
    if (entry - t1) * d >= 0:
        return {"skipped": "opened beyond T1"}
    risk = (entry - stop) * d
    hold = max(10, 3 * max(1, round(abs(t2 - setup["entry"]) / (setup["atr"] or 1) * 1.5)))
    left, realised, stop_now, exits, hit = 1.0, 0.0, stop, [], []
    j = i + 1
    exit_reason = None
    while j < len(F.bars):
        b = F.bars[j]
        # stop first
        if (b.low <= stop_now) if d > 0 else (b.high >= stop_now):
            px = min(b.open, stop_now) if d > 0 else max(b.open, stop_now)   # gap through the stop fills at the open
            px = C.slip_price(px, -d, E.market, cost_cfg)
            realised += left * (px - entry) * d
            exits.append((left, px))
            left = 0
            exit_reason = "stop" if not hit else "breakeven/trailing stop"
            break
        for k, tp in enumerate((t1, t2, t3)):
            if k < len(hit):
                continue
            if (b.high >= tp) if d > 0 else (b.low <= tp):
                part = 1 / 3 if k < 2 else left
                part = min(part, left)
                px = C.slip_price(tp, -d, E.market, cost_cfg)
                realised += part * (px - entry) * d
                exits.append((part, px))
                left -= part
                hit.append(f"T{k + 1}")
                if k == 0:
                    stop_now = entry
            else:
                break
        if left <= 1e-9:
            exit_reason = "T3"
            break
        if len(hit) >= 2 and F.atr[j]:
            trail = b.close - 2 * F.atr[j] if d > 0 else b.close + 2 * F.atr[j]
            stop_now = max(stop_now, t1, trail) if d > 0 else min(stop_now, t1, trail)
        if j - (i + 1) + 1 >= hold or _session_last(E, j) or j == len(F.bars) - 1:
            px = C.slip_price(b.close, -d, E.market, cost_cfg)
            realised += left * (px - entry) * d
            exits.append((left, px))
            left = 0
            exit_reason = "session end" if _session_last(E, j) else "time stop" if j < len(F.bars) - 1 else "end of data"
            break
        j += 1
    sz = P.size(symbol, E.market, entry, stop, prot)
    qty = sz.get("qty") or P.lot_of(symbol)
    avg_exit = sum(p * x for p, x in exits) / sum(p for p, _ in exits)
    fees = C.round_trip(symbol, E.market, qty, entry, avg_exit, d > 0, cost_cfg)
    if E.market in ("NSE", "FNO", "MCX") and len(exits) > 1:
        fees += (len(exits) - 1) * cost_cfg["brokerage_inr"] * 1.18
    gross = realised * qty
    pnl = gross - fees
    return {"strategy": setup["strategy"], "side": setup["direction"], "signal_time": F.t[i], "entry_time": F.t[i + 1],
            "exit_time": F.t[j] + F.step, "entry": entry, "stop": stop, "targets": [t1, t2, t3], "exit": avg_exit,
            "exit_reason": exit_reason, "targets_hit": hit, "bars_held": j - i, "qty": qty, "gross": gross, "costs": fees,
            "pnl": pnl, "r": pnl / (risk * qty) if risk and qty else 0.0, "r_gross": realised / risk, "score": setup["score"],
            "regime": setup.get("regime"), "hour": _hour(E, F.t[i])}


def _hour(E: Engine, t: int) -> int:
    tz = IST if E.market in ("NSE", "FNO", "MCX") else dt.timezone.utc
    return dt.datetime.fromtimestamp(t, tz).hour


def _trades_for(E: Engine, symbol: str, ideas, params: dict, min_score: float, cost_cfg: dict, prot: dict, skipped: list | None = None) -> list[dict]:
    trades, busy_until = [], {}
    for i, ctx, dscore, raws in ideas:
        for key, d, raw in raws:
            if busy_until.get(key, -1) >= i:
                continue
            setup, why = ST.build_setup(key, raw, E.F, i, d, {**ST.PARAMS[key], **params}, E.market)
            if not setup:
                continue
            setup["regime_ok"] = key in ctx["suits"]
            setup.update(SC.setup_score(setup, dscore, ctx))
            setup["regime"] = ctx["regime"]
            if setup["score"] < min_score:
                continue
            tr = _simulate(E, symbol, i, setup, cost_cfg, prot)
            if not tr:
                continue
            if tr.get("skipped"):
                if skipped is not None:
                    skipped.append({"strategy": key, "time": E.F.t[i], "why": tr["skipped"]})
                continue
            trades.append(tr)
            busy_until[key] = i + tr["bars_held"]
    trades.sort(key=lambda x: x["entry_time"])
    return trades


def stats(trades: list[dict]) -> dict:
    n = len(trades)
    if not n:
        return {"trades": 0}
    rs = [t["r"] for t in trades]
    w = [r for r in rs if r > 0]
    l = [r for r in rs if r <= 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    streak = best = 0
    for r in rs:
        streak = streak + 1 if r <= 0 else 0
        best = max(best, streak)
    sd = statistics.pstdev(rs) if n > 1 else 0
    return {"trades": n, "win_rate": round(100 * len(w) / n, 1), "avg_win_r": round(sum(w) / len(w), 2) if w else 0,
            "avg_loss_r": round(sum(l) / len(l), 2) if l else 0, "expectancy_r": round(sum(rs) / n, 3),
            "profit_factor": round(sum(w) / -sum(l), 2) if l and sum(l) < 0 else None, "total_r": round(sum(rs), 2),
            "max_drawdown_r": round(dd, 2), "sharpe_per_trade": round((sum(rs) / n) / sd, 3) if sd else None,
            "max_losing_streak": best, "pnl": round(sum(t["pnl"] for t in trades), 2), "costs": round(sum(t["costs"] for t in trades), 2),
            "avg_bars_held": round(sum(t["bars_held"] for t in trades) / n, 1)}


def breakdown(trades: list[dict], key) -> dict:
    g: dict = {}
    for t in trades:
        g.setdefault(key(t), []).append(t)
    return {str(k): stats(v) for k, v in sorted(g.items(), key=lambda kv: str(kv[0]))}


def _bucket(s: float) -> str:
    return "75+" if s >= 75 else "60-74" if s >= 60 else "45-59"


def monte_carlo(trades: list[dict], runs: int = 1000, seed: int = 7) -> dict:
    rs = [t["r"] for t in trades]
    if len(rs) < 10:
        return {"available": False, "note": "needs at least 10 trades"}
    rnd = random.Random(seed)
    tot, dds = [], []
    for _ in range(runs):
        eq = peak = dd = 0.0
        for _ in rs:
            eq += rs[rnd.randrange(len(rs))]
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
        tot.append(eq)
        dds.append(dd)
    tot.sort()
    dds.sort()
    p = lambda xs, q: round(xs[min(len(xs) - 1, int(q * len(xs)))], 2)
    return {"available": True, "runs": runs, "trades_per_run": len(rs), "total_r_p5": p(tot, 0.05), "total_r_p50": p(tot, 0.5),
            "total_r_p95": p(tot, 0.95), "max_dd_r_p5": p(dds, 0.05), "max_dd_r_p50": p(dds, 0.5),
            "prob_negative": round(100 * sum(1 for x in tot if x < 0) / runs, 1)}


def _within(trades, t0, t1):
    return [t for t in trades if t0 <= t["signal_time"] < t1]


def _pick(by_combo: dict, t0: int, t1: int) -> tuple:
    best, best_e = (0.1, 1.5), None
    for combo, trs in by_combo.items():
        s = stats(_within(trs, t0, t1))
        if s["trades"] >= 10 and (best_e is None or s["expectancy_r"] > best_e):
            best, best_e = combo, s["expectancy_r"]
    return best


def run(E: Engine, symbol: str, settings: dict | None = None, min_score: float | None = None, cost_cfg: dict | None = None,
        strategy: str | None = None) -> dict:
    F = E.F
    n = len(F.bars)
    if n < WARMUP + 100:
        return {"error": f"Not enough history: {n} candles (need {WARMUP + 100}+)."}
    cost_cfg = cost_cfg or C.DEFAULTS
    prot = P.settings()
    min_score = prot["min_score"] if min_score is None else min_score
    st = dict(settings or {})
    if strategy:
        st = {k: {**(st.get(k) or {}), "enabled": k == strategy} for k in ST.MODULES}
    ideas = _ideas(E, WARMUP, n - 1, st)
    t_start, t_end = F.t[WARMUP], F.t[-1] + 1
    span = t_end - t_start
    t_train, t_val = t_start + int(span * 0.6), t_start + int(span * 0.8)
    by_combo, skipped = {}, []
    for sb, rr in GRID:
        by_combo[(sb, rr)] = _trades_for(E, symbol, ideas, {"stop_buffer_atr": sb, "min_rr": rr}, 45, cost_cfg, prot,
                                         skipped if (sb, rr) == (0.1, 1.5) else None)
    chosen = _pick({k: [t for t in v if t["score"] >= min_score] for k, v in by_combo.items()}, t_start, t_train)
    all_trades = by_combo[chosen]
    main = [t for t in all_trades if t["score"] >= min_score]
    train, val, test = _within(main, t_start, t_train), _within(main, t_train, t_val), _within(main, t_val, t_end)
    # rolling walk-forward over train+val
    folds = [t_start + int((t_val - t_start) * k / 4) for k in range(5)]
    wf = []
    for k in range(1, 4):
        p = _pick({c: [t for t in v if t["score"] >= min_score] for c, v in by_combo.items()}, folds[0], folds[k])
        oos = _within([t for t in by_combo[p] if t["score"] >= min_score], folds[k], folds[k + 1])
        wf.append({"fold": k, "params": {"stop_buffer_atr": p[0], "min_rr": p[1]}, "from": folds[k], "to": folds[k + 1], **stats(oos)})
    sens = [{"stop_buffer_atr": c[0], "min_rr": c[1],
             "train": stats(_within([t for t in v if t["score"] >= min_score], t_start, t_train)),
             "validation": stats(_within([t for t in v if t["score"] >= min_score], t_train, t_val))} for c, v in by_combo.items()]
    validation = {}
    for key in ST.MODULES:
        v_ = [t for t in val if t["strategy"] == key]
        te = [t for t in test if t["strategy"] == key]
        sv, stt = stats(v_), stats(te)
        ok = len(v_) + len(te) >= 20 and sv.get("expectancy_r", 0) > 0 and stt.get("expectancy_r", 0) > 0
        validation[key] = {"status": "validated" if ok else "not validated", "val_trades": len(v_), "test_trades": len(te),
                           "val_expectancy": sv.get("expectancy_r"), "test_expectancy": stt.get("expectancy_r")}
    notes = ["Entries at the next candle's open after the signal candle closes; stop assumed before target inside one candle.",
             "options_momentum needs a live option chain and is not backtested."]
    kind, proxy = C.instrument_kind(symbol, E.market)
    if proxy:
        notes.append(proxy)
    if E.market in ("SHARK", "DELTAF", "DELTA", "BINANCE"):
        notes.append("Crypto funding payments are not modelled.")
    if not F.has_volume:
        notes.append("No volume data: volume filters replaced by range expansion.")
    return {"symbol": symbol, "tf": E.tf, "candles": n, "from": t_start, "to": t_end, "min_score": min_score,
            "split": {"train_end": t_train, "validation_end": t_val},
            "params": {"stop_buffer_atr": chosen[0], "min_rr": chosen[1]},
            "overall": stats(main), "train": stats(train), "validation": stats(val), "test": stats(test),
            "by_strategy": breakdown(main, lambda t: t["strategy"]), "by_regime": breakdown(main, lambda t: t["regime"]),
            "by_side": breakdown(main, lambda t: t["side"]), "by_hour": breakdown(main, lambda t: t["hour"]),
            "by_score": breakdown(all_trades, lambda t: _bucket(t["score"])),
            "walk_forward": wf, "sensitivity": sens, "monte_carlo": monte_carlo(main), "validation_status": validation,
            "skipped": skipped[-50:], "trades": main[-400:], "notes": notes}
