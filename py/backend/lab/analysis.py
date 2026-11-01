"""Live analysis for the Signal Lab screen: one call returns chart data, overlays, intelligence and signals."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict

from .candle import TF_SECONDS
from . import data as D
from . import ind as I
from . import ict as ICT
from . import pa as PA
from . import options as O
from . import store
from . import strategies as ST
from . import protect as P
from .engine import Engine, MTF

CRYPTO_OPT = ("BTC", "ETH")
_chain_cache: dict[str, tuple[float, dict]] = {}


def underlying_of(symbol: str) -> tuple[str | None, str]:
    """Option-chain underlying for a symbol, and its kind (india | crypto | none)."""
    s = symbol.upper()
    m = D.market_of(s)
    if m in ("NSE", "FNO"):
        body = s.split(":", 1)[1]
        for k, v in (("NIFTYBANK", "BANKNIFTY"), ("BANKNIFTY", "BANKNIFTY"), ("FINNIFTY", "FINNIFTY"), ("MIDCPNIFTY", "MIDCPNIFTY"),
                     ("NIFTY", "NIFTY"), ("SENSEX", "SENSEX"), ("BANKEX", "BANKEX")):
            if body.startswith(k):
                return v, "india"
        return None, "none"
    if m in ("SHARK", "DELTAF", "BINANCE", "DELTA"):
        base = s.split(":", 1)[1].split("-")[0].replace("USDT", "").replace("USD", "")
        if base.startswith("C") and "-" in s:
            base = s.split(":", 1)[1].split("-")[1]
        return (base, "crypto") if base in CRYPTO_OPT else (None, "none")
    return None, "none"


async def derivatives(symbol: str, save: bool = True) -> dict:
    und, kind = underlying_of(symbol)
    out: dict = {"symbol": symbol, "underlying": und, "kind": kind}
    if kind == "none":
        out["note"] = "No listed options for this instrument in the connected sources."
    else:
        hit = _chain_cache.get(und)
        if hit and time.time() - hit[0] < 60:
            ch = hit[1]
        else:
            try:
                ch = await (O.india_chain(und) if kind == "india" else O.crypto_chain(und))
            except Exception as exc:                      # noqa: BLE001
                ch = {"error": f"DATA UNAVAILABLE: {str(getattr(exc, 'detail', exc))[:160]}"}
            if not ch.get("error"):
                _chain_cache[und] = (time.time(), ch)
                if save:
                    O.save_snapshot(ch)
        out["chain"] = ch
        if not ch.get("error"):
            ch["trends"] = O.history_trends(und)
    m = D.market_of(symbol)
    if m in ("SHARK", "DELTAF", "BINANCE"):
        pair = symbol.split(":", 1)[1]
        out["perp"] = await O.crypto_derivs(pair)
    return out


def _quality_recent(bars, step) -> int:
    tail = bars[-21:]
    return sum(1 for a, b in zip(tail, tail[1:]) if b.time - a.time > step and (b.time - a.time) < 3 * 3600)


def _overlays(E: Engine, n: int) -> dict:
    F = E.F
    lo = max(0, len(F.bars) - n)
    T = F.t[lo:]
    line = lambda xs: [{"time": t, "value": round(v, 6)} for t, v in zip(T, xs[lo:]) if v is not None]
    t_last = F.t[-1]
    obs = [{"direction": o.direction, "top": o.top, "bottom": o.bottom, "time": o.time, "state": o.state, "break_type": o.break_type,
            "volume_confirmed": o.volume_confirmed} for o in F.obs_at(t_last) if o.time >= F.t[lo]][-12:]
    fvgs = [{k: g[k] for k in ("direction", "top", "bottom", "ce", "time", "quality")} for g in F.open_fvgs_at(t_last) if g["time"] >= F.t[lo]][-12:]
    marks = []
    for s in F.ext.structure:
        if s.time >= F.t[lo]:
            marks.append({"time": s.time, "kind": s.type, "direction": s.direction, "level": s.level})
    for s in F.ext.sweeps + F.intl.sweeps:
        if s.time >= F.t[lo]:
            marks.append({"time": s.time, "kind": "sweep", "direction": "bullish" if s.side == "sell" else "bearish", "level": s.level})
    above, below = F.liquidity_at(t_last, F.c[-1])
    return {"ema20": line(F.ema[20]), "ema50": line(F.ema[50]), "ema200": line(F.ema[200]), "vwap": line(F.vwap) if F.step < 86400 else [],
            "bb_up": line(F.bb_up), "bb_lo": line(F.bb_lo), "supertrend": line(F.st_line), "order_blocks": obs, "fvgs": fvgs,
            "markers": sorted(marks, key=lambda m: m["time"])[-80:], "liquidity_above": above[:6], "liquidity_below": below[:6]}


def _indicators(E: Engine) -> dict:
    F, i = E.F, len(E.F.bars) - 1
    bars = F.bars
    L = lambda xs: xs[i] if xs and xs[i] is not None else None
    india = E.market in ("NSE", "FNO", "MCX")
    ppy = (252 if india or E.market == "YF" else 365) * (1 if F.step >= 86400 else (375 * 60 if india else 86400) / F.step)
    hv = I.hist_vol(F.c, 20, ppy)
    vp = I.volume_profile(bars[-200:]) if F.has_volume else None
    hs, ls = F.swings_at(F.t[i], "high"), F.swings_at(F.t[i], "low")
    fib = None
    if hs and ls:
        up = ls[-1].time < hs[-1].time
        fib = {"from": ls[-1].price if up else hs[-1].price, "to": hs[-1].price if up else ls[-1].price, "direction": "up" if up else "down",
               "levels": I.fib_levels(ls[-1].price, hs[-1].price, up)}
    div = I.divergences(bars, F.rsi, "RSI")[-4:] + I.divergences(bars, F.macd_hist, "MACD")[-4:]
    obv_slope = (F.obv[i] - F.obv[i - 10]) if i >= 10 else None
    return {"close": F.c[i], "ema": {n: L(F.ema[n]) for n in F.ema}, "sma": {n: L(F.sma[n]) for n in F.sma}, "rsi": L(F.rsi),
            "stoch_rsi": {"k": L(F.srsi_k), "d": L(F.srsi_d)}, "macd": {"macd": L(F.macd), "signal": L(F.macd_sig), "hist": L(F.macd_hist)},
            "adx": {"adx": L(F.adx), "plus_di": L(F.pdi), "minus_di": L(F.mdi)},
            "bollinger": {"mid": L(F.bb_mid), "up": L(F.bb_up), "lo": L(F.bb_lo), "bandwidth_pct": L(F.bb_bw)},
            "keltner": {"mid": L(F.kc_mid), "up": L(F.kc_up), "lo": L(F.kc_lo)},
            "squeeze": bool(F.bb_up[i] and F.kc_up[i] and F.bb_up[i] < F.kc_up[i] and F.bb_lo[i] > F.kc_lo[i]),
            "supertrend": {"line": L(F.st_line), "dir": L(F.st_dir)}, "atr": L(F.atr), "atr_pct": (L(F.atr) / F.c[i] * 100) if L(F.atr) else None,
            "vwap": L(F.vwap) if F.step < 86400 else None, "mfi": L(F.mfi) if F.has_volume else None, "obv_slope_10": obv_slope if F.has_volume else None,
            "rvol": L(F.rvol) if F.has_volume else None, "roc": L(F.roc), "hist_vol_pct": L(hv), "parkinson_vol_pct": I.last(I.parkinson_vol(bars)),
            "volume_profile": vp, "fib": fib, "divergences": div, "has_volume": F.has_volume}


async def analyze(symbol: str, tf: str, bars: int = 600, fresh: bool = False, live: bool = False) -> dict:
    t0 = time.time()
    itf, htf = MTF[tf]
    sess, anchor, market = D.session_key(symbol), D.anchor_minutes(symbol), D.market_of(symbol)
    main, q = await D.candles(symbol, tf, bars, fresh, forming=live)  # DataUnavailable propagates -> DATA UNAVAILABLE on screen
    q["recent_gaps"] = _quality_recent(main, TF_SECONDS[tf])

    async def opt(sym, t, n):
        if not t:
            return None, None
        try:
            return await D.candles(sym, t, n)
        except Exception as exc:                          # noqa: BLE001
            return None, {"error": str(exc)[:160]}
    (ib, iq), (hb, hq), (db, dq) = await asyncio.gather(opt(symbol, itf, 400), opt(symbol, htf, 400),
                                                          opt(symbol, "1d", 120) if tf != "1d" else asyncio.sleep(0, (main, q)))
    E = Engine(main, tf, market, sess, anchor, ib, hb)
    der = await derivatives(symbol) if TF_SECONDS[tf] < 86400 or underlying_of(symbol)[1] != "none" else None
    ch = (der or {}).get("chain") if der else None
    opt_ctx = None
    if ch and not ch.get("error"):
        opt_ctx = {"oi_flow": ch.get("oi_flow"), "trends": ch.get("trends")}
    settings = store.get_setting("strategies") or {}
    i = len(main) - 1
    res = E.signals_at(i, opt_ctx, settings, q)
    val = store.get_setting(f"validation:{symbol}:{tf}") or {}
    prot = P.settings()
    for s in res["setups"]:
        s["validation"] = (val.get("status") or {}).get(s["strategy"], {"status": "not backtested"})
        s["size"] = P.size(symbol, market, s["entry"], s["stop"], prot)
        s["signal"] = s["score"] >= prot["min_score"]
        s["provisional"] = bool(q.get("forming"))
    if not q.get("forming"):                          # journal / alerts only from closed candles (a live candle can still change)
        _journal(symbol, tf, res["setups"], prot["min_score"])
    ictr = ICT.analyze(main, market, db or None, main[-1].time)
    ext, intl, obs = ictr.pop("_reports")
    traps = [asdict(t) for t in ext.traps]
    par = PA.analyze(main, E.F.disp, traps)
    ctx = res["ctx"]
    ctx.pop("options", None)
    reg = ctx.pop("regime_full")
    mtf = {"execution": tf, "intermediate": {"tf": itf, "bias": ctx["itf_bias"], "available": ctx["itf_ok"], "error": (iq or {}).get("error")},
           "higher": {"tf": htf, "bias": ctx["htf_bias"], "available": ctx["htf_ok"], "error": (hq or {}).get("error")},
           "execution_bias": E.F.bias_at(i)}
    liq = {}
    if ch and not ch.get("error"):
        liq["oi_support"], liq["oi_resistance"], liq["max_pain"] = ch.get("oi_support"), ch.get("oi_resistance"), ch.get("max_pain")
    if (der or {}).get("perp") and not der["perp"].get("error"):
        hs, ls = E.F.swings_at(E.F.t[i], "high"), E.F.swings_at(E.F.t[i], "low")
        der["liquidations"] = O.liquidation_estimates(main[-1].close, ls[-1].price if ls else None, hs[-1].price if hs else None)
    candles_out = [{"time": b.time, "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume} for b in main[-400:]]
    if not q.get("forming"):
        _alerts(symbol, tf, E, res, prot)
    return {"symbol": symbol, "tf": tf, "market": market, "quality": q, "candles": candles_out, "overlays": _overlays(E, 400),
            "indicators": _indicators(E), "regime": reg, "score": res["score"], "setups": res["setups"][:8],
            "rejected": res["rejected"][:20], "context": ctx, "mtf": mtf, "ict": ictr, "price_action": par, "derivatives": der,
            "levels": liq, "validation": val.get("summary"), "elapsed_ms": int((time.time() - t0) * 1000),
            "disclaimer": "Analysis and paper trading only. Scores rank setups; they are not probabilities of profit."}


def _journal(symbol: str, tf: str, setups: list[dict], min_score: float) -> None:
    for s in setups:
        if s["score"] < min_score:
            continue
        try:
            store.execute("INSERT OR IGNORE INTO signals(created,symbol,tf,strategy,direction,score,bull,bear,neutral,entry,stop,t1,t2,t3,rr,status,bar_time,payload)"
                          " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (store.now(), symbol, tf, s["strategy"], s["direction"], s["score"], None, None, None, s["entry"], s["stop"],
                           s["targets"][0], s["targets"][1], s["targets"][2], s["rr"], "open", s["time"],
                           json.dumps({k: s[k] for k in ("reasons_for", "reasons_against", "grade", "atr", "regime", "expected_hold")})))
        except Exception:                                 # noqa: BLE001 - the journal must never break analysis
            pass


def _alerts(symbol: str, tf: str, E: Engine, res: dict, prot: dict) -> None:
    F = E.F
    i = len(F.bars) - 1
    t = F.t[i]
    for s in res["setups"]:
        if s["score"] >= prot["min_score"]:
            store.add_alert(f"sig:{symbol}:{tf}:{s['strategy']}:{s['direction']}:{t}", t, symbol, tf, "signal",
                            f"{s['direction']} {s['name']} score {s['score']:.0f} ({s['grade']}) entry {s['entry']:,.2f} stop {s['stop']:,.2f}")
    for sw in F.sweeps_between(F.t[max(0, i - 1)], t):
        store.add_alert(f"sweep:{symbol}:{tf}:{sw.time}:{sw.level}", sw.time, symbol, tf, "sweep",
                        f"{'Buy' if sw.side == 'buy' else 'Sell'}-side liquidity swept at {sw.level:,.2f}")
    for m in F.mss_between(F.t[max(0, i - 1)], t):
        store.add_alert(f"mss:{symbol}:{tf}:{m['time']}", m["time"], symbol, tf, "structure", f"{m['direction'].title()} market-structure shift at {m['level']:,.2f}")


async def mtf_matrix(symbol: str) -> dict:
    """Bias, trend, RSI and regime on every timeframe (fetched sequentially to respect rate limits)."""
    from . import regime as R
    sess, anchor, market = D.session_key(symbol), D.anchor_minutes(symbol), D.market_of(symbol)
    rows = []
    for tf in ("1w", "1d", "4h", "1h", "30m", "15m", "5m", "3m", "1m"):
        try:
            b, q = await D.candles(symbol, tf, 300)
            if len(b) < 60:
                rows.append({"tf": tf, "error": f"only {len(b)} candles"})
                continue
            E = Engine(b, tf, market, sess, anchor)
            F, i = E.F, len(b) - 1
            reg = R.classify_at(F, i)
            rows.append({"tf": tf, "bias": F.bias_at(i), "structure": F.trend_at(F.t[i]), "rsi": F.rsi[i], "adx": F.adx[i],
                         "regime": reg["label"], "close": F.c[i], "ema20": F.ema[20][i], "ema50": F.ema[50][i], "as_of": q["as_of"], "source": q["source"]})
        except Exception as exc:                          # noqa: BLE001
            rows.append({"tf": tf, "error": f"DATA UNAVAILABLE: {str(exc)[:120]}"})
    vals = [r["bias"] for r in rows if "bias" in r]
    groups = {"higher (1w/1d/4h)": ["1w", "1d", "4h"], "intermediate (1h/30m/15m)": ["1h", "30m", "15m"], "execution (5m/3m/1m)": ["5m", "3m", "1m"]}
    g_out = {}
    for name, tfs in groups.items():
        v = [r["bias"] for r in rows if r["tf"] in tfs and "bias" in r]
        g_out[name] = (1 if sum(v) >= 2 else -1 if sum(v) <= -2 else 0) if v else None
    agree = [x for x in g_out.values() if x is not None]
    return {"symbol": symbol, "rows": rows, "groups": g_out,
            "alignment": "aligned bullish" if agree and all(x == 1 for x in agree) else "aligned bearish" if agree and all(x == -1 for x in agree) else "mixed",
            "net": sum(vals)}
