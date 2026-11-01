"""Options and derivatives intelligence.

Indian options come from the FYERS option chain (greeks=1); crypto options from Delta Exchange India; crypto
perpetuals (funding, open interest, basis) from Delta Exchange and Shark where Shark publishes it.

Interpretations are probabilistic, not statements about who holds what:
- Build-up per strike: OI up + premium up = long build-up; OI up + premium down = writing; OI down + premium up =
  short covering; OI down + premium down = long unwinding.
- OI support / resistance: the strikes with the most fresh put (call) writing within ATM +/-6 strikes, else the
  largest put (call) OI there.
- Max pain: the expiry price that minimises the total payout to option holders. Limitation: assumes positions are
  held to expiry and ignores hedges; it is a magnet only in quiet markets.
- GEX (gamma exposure, estimate): sum over strikes of gamma x OI x lot x spot^2 x 1%, calls positive and puts
  negative, i.e. the textbook assumption that dealers are long calls / short puts. Real dealer positioning is
  unknown; treat the sign as a rough guide (positive: moves tend to be dampened, negative: amplified).
- DEX (delta exposure): sum of delta x OI x lot.
- Expected move to expiry: spot x ATM IV x sqrt(days / 365), and the ATM straddle x 0.85 as a market-priced check.
- Skew: OTM put IV minus OTM call IV at about 3% from spot.
- IV rank / percentile: ATM IV against the snapshots this app has recorded (needs at least 20; until then shown as
  "not enough history").
- Rho: FYERS does not send it; it is computed with Black-Scholes from the strike, spot, IV, time and a 6.5% (INR)
  or 0% (crypto) rate.
"""
from __future__ import annotations

import json
import math
import statistics
import time
from typing import Any

from . import store

INDEX_SYMBOLS = {"NIFTY": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX", "FINNIFTY": "NSE:FINNIFTY-INDEX",
                 "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX", "SENSEX": "BSE:SENSEX-INDEX", "BANKEX": "BSE:BANKEX-INDEX"}
LOTS = {"NIFTY": 65, "BANKNIFTY": 30, "FINNIFTY": 65, "MIDCPNIFTY": 120, "SENSEX": 20, "BANKEX": 30}


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_rho(kind: str, s: float, k: float, iv_pct: float, days: float, r: float) -> float | None:
    """Rho per 1 percentage point change in the rate."""
    t = max(days, 1e-4) / 365
    v = (iv_pct or 0) / 100
    if s <= 0 or k <= 0 or v <= 0:
        return None
    d1 = (math.log(s / k) + (r + v * v / 2) * t) / (v * math.sqrt(t))
    d2 = d1 - v * math.sqrt(t)
    rho = k * t * math.exp(-r * t) * (_ncdf(d2) if kind == "CE" else -_ncdf(-d2))
    return rho / 100


def _build(side: str, d_oi: float, d_px: float) -> tuple[str, int] | None:
    if not d_oi:
        return None
    up = d_px > 0
    if d_oi > 0 and up:
        return "Long build-up", 1 if side == "CE" else -1
    if d_oi > 0:
        return ("Call writing", -1) if side == "CE" else ("Put writing", 1)
    if up:
        return "Short covering", 1 if side == "CE" else -1
    return ("Call unwinding", 1) if side == "CE" else ("Put unwinding", -1)


def analyse_chain(rows: list[dict], spot: float, lot: float, days: float, rate: float, coin: bool = False) -> dict:
    """rows: one dict per strike {strike, ce:{...}, pe:{...}} with oi, oi_chg, ltp, ltp_chg, vol, bid, ask, iv, delta,
    gamma, theta, vega."""
    rows = sorted(rows, key=lambda r: r["strike"])
    if not rows or not spot:
        return {"error": "empty chain"}
    ks = [r["strike"] for r in rows]
    ai = min(range(len(ks)), key=lambda i: abs(ks[i] - spot))
    leg = lambda r, s: r.get(s) or {}
    tot = {s: sum(leg(r, s).get("oi", 0) or 0 for r in rows) for s in ("ce", "pe")}
    vol = {s: sum(leg(r, s).get("vol", 0) or 0 for r in rows) for s in ("ce", "pe")}
    pain = min(ks, key=lambda x: sum((leg(r, "ce").get("oi", 0) or 0) * max(0, x - r["strike"]) +
                                     (leg(r, "pe").get("oi", 0) or 0) * max(0, r["strike"] - x) for r in rows))
    band = rows[max(0, ai - 6): ai + 7]
    below = [r for r in band if r["strike"] <= spot] or band
    above = [r for r in band if r["strike"] >= spot] or band
    pick = lambda arr, s: (max((r for r in arr if (leg(r, s).get("oi_chg") or 0) > 0), key=lambda r: leg(r, s)["oi_chg"], default=None)
                           or max(arr, key=lambda r: leg(r, s).get("oi", 0) or 0))
    sup, res = pick(below, "pe"), pick(above, "ce")
    chg_all = [abs(leg(r, s).get("oi_chg") or 0) for r in rows for s in ("ce", "pe") if leg(r, s)]
    med = statistics.median(chg_all) if chg_all else 0
    unusual, strikes_out, gex, dex, bull_w, bear_w = [], [], 0.0, 0.0, 0.0, 0.0
    for r in rows:
        out = {"strike": r["strike"]}
        for s, sign in (("ce", 1), ("pe", -1)):
            o = leg(r, s)
            if not o:
                continue
            g, dlt = o.get("gamma") or 0, o.get("delta") or 0
            gex += sign * g * (o.get("oi") or 0) * lot * spot * spot * 0.01
            dex += dlt * (o.get("oi") or 0) * lot
            b = _build(s.upper(), o.get("oi_chg") or 0, o.get("ltp_chg") or 0)
            if b and abs(r["strike"] - spot) <= 8 * (ks[1] - ks[0] if len(ks) > 1 else 1):
                if b[1] > 0:
                    bull_w += abs(o.get("oi_chg") or 0)
                else:
                    bear_w += abs(o.get("oi_chg") or 0)
            spread = (o.get("ask") or 0) - (o.get("bid") or 0) if o.get("ask") and o.get("bid") else None
            rho = bs_rho(s.upper(), spot, r["strike"], o.get("iv") or 0, days, rate)
            out[s] = {**{k: o.get(k) for k in ("oi", "oi_chg", "ltp", "ltp_chg", "vol", "iv", "delta", "gamma", "theta", "vega", "symbol")},
                      "rho": round(rho, 4) if rho is not None else None, "spread": spread, "build_up": b[0] if b else None}
            if med and abs(o.get("oi_chg") or 0) >= 3 * med and abs(o.get("oi_chg") or 0) > 0:
                unusual.append({"strike": r["strike"], "side": s.upper(), "oi_chg": o.get("oi_chg"), "times_median": round(abs(o["oi_chg"]) / med, 1)})
        strikes_out.append(out)
    atm = rows[ai]
    ivs = [x for x in (leg(atm, "ce").get("iv"), leg(atm, "pe").get("iv")) if x]
    atm_iv = sum(ivs) / len(ivs) if ivs else None
    straddle = (leg(atm, "ce").get("ltp") or 0) + (leg(atm, "pe").get("ltp") or 0)
    em = spot * atm_iv / 100 * math.sqrt(max(days, 1 / 24) / 365) if atm_iv else None
    put_k = min(ks, key=lambda k: abs(k - spot * 0.97))
    call_k = min(ks, key=lambda k: abs(k - spot * 1.03))
    piv = leg(next(r for r in rows if r["strike"] == put_k), "pe").get("iv")
    civ = leg(next(r for r in rows if r["strike"] == call_k), "ce").get("iv")
    flow = round(100 * (bull_w - bear_w) / (bull_w + bear_w)) if bull_w + bear_w else 0
    return {"spot": spot, "atm": atm["strike"], "days": round(days, 2), "pcr_oi": tot["pe"] / tot["ce"] if tot["ce"] else None,
            "pcr_vol": vol["pe"] / vol["ce"] if vol["ce"] else None, "oi_ce": tot["ce"], "oi_pe": tot["pe"], "max_pain": pain,
            "max_pain_note": "Assumes positions are held to expiry; a magnet mainly in quiet markets.",
            "oi_support": sup["strike"], "oi_resistance": res["strike"], "oi_flow": flow,
            "gex_est": gex, "gex_note": "Estimate: assumes dealers long calls / short puts. Positive = moves damped, negative = amplified.",
            "dex": dex, "atm_iv": atm_iv, "straddle": straddle, "expected_move_iv": em,
            "expected_move_straddle": straddle * 0.85 if straddle else None,
            "skew_put_minus_call": (piv - civ) if piv and civ else None, "unusual_oi": sorted(unusual, key=lambda u: -abs(u["oi_chg"]))[:8],
            "strikes": strikes_out}


async def india_chain(underlying: str, expiry: str = "wk", strikes: int = 15) -> dict:
    from backend.brokers.fyers.client import data_get
    sym = INDEX_SYMBOLS.get(underlying.upper(), underlying)
    first = await data_get("/options-chain-v3", {"symbol": sym, "strikecount": strikes, "timestamp": "", "greeks": "1"})
    d = first.get("data") or first
    exps = sorted(d.get("expiryData") or [], key=lambda e: int(e.get("expiry") or 0))
    if not exps:
        return {"error": f"FYERS returned no expiries for {underlying}"}
    pick = exps[0] if expiry == "wk" else next((e for e in exps if e.get("expiry_flag") == "M"), exps[0])
    if pick is not exps[0]:
        d = (await data_get("/options-chain-v3", {"symbol": sym, "strikecount": strikes, "timestamp": str(pick["expiry"]), "greeks": "1"})).get("data") or {}
    chain = d.get("optionsChain") or []
    spot = next((float(r.get("ltp") or 0) for r in chain if float(r.get("strike_price") or 0) <= 0), 0.0)
    by: dict[float, dict] = {}
    for r in chain:
        if r.get("option_type") not in ("CE", "PE"):
            continue
        g = r.get("greeks") or {}
        by.setdefault(float(r["strike_price"]), {"strike": float(r["strike_price"])})[r["option_type"].lower()] = {
            "oi": float(r.get("oi") or 0), "oi_chg": float(r.get("oich") or 0), "ltp": float(r.get("ltp") or 0),
            "ltp_chg": float(r.get("ltpch") or 0), "vol": float(r.get("volume") or 0), "bid": float(r.get("bid") or 0),
            "ask": float(r.get("ask") or 0), "iv": float(g.get("iv") or 0), "delta": float(g.get("delta") or 0),
            "gamma": float(g.get("gamma") or 0), "theta": float(g.get("theta") or 0), "vega": float(g.get("vega") or 0),
            "symbol": r.get("symbol")}
    days = max(0.0, (int(pick["expiry"]) + 10 * 3600 - time.time()) / 86400)
    out = analyse_chain(list(by.values()), spot, LOTS.get(underlying.upper(), 1), days, 0.065)
    out.update(underlying=underlying.upper(), expiry=pick.get("date"), source="FYERS option chain", as_of=int(time.time()),
               vix=float((d.get("indiavixData") or {}).get("ltp") or 0) or None)
    out.update(iv_rank=iv_rank(underlying.upper(), out.get("atm_iv")))
    return out


async def crypto_chain(coin: str, expiry: str | None = None) -> dict:
    from backend.markets.crypto_dash import options as delta_options
    ch = await delta_options(coin.upper(), expiry, 25)
    rows = []
    for s in ch["strikes"]:
        r = {"strike": s["strike"]}
        for side in ("ce", "pe"):
            o = s.get(side)
            if o:
                oi_coins = o.get("oi") or 0
                per = (o.get("oi_usd") / oi_coins) if oi_coins and o.get("oi_usd") else ch["spot"]
                r[side] = {"oi": oi_coins, "oi_chg": (o.get("oi_chg6h_usd") or 0) / per if per else 0, "ltp": o.get("mark") or 0,
                           "ltp_chg": o.get("mark_chg24h") or 0, "vol": 0, "iv": o.get("iv") or 0, "delta": o.get("delta") or 0,
                           "gamma": o.get("gamma") or 0, "theta": o.get("theta") or 0, "vega": 0, "symbol": o.get("symbol")}
        rows.append(r)
    out = analyse_chain(rows, ch["spot"], 1.0, ch["days"], 0.0, True)
    out.update(underlying=coin.upper(), expiry=ch["expiry"], source="Delta Exchange India options (OI change = 6 hours)",
               as_of=int(time.time()), iv_rank=iv_rank(coin.upper(), out.get("atm_iv")))
    return out


async def crypto_derivs(pair: str) -> dict:
    """Funding, open interest and basis for a perpetual. Shark first for its own price; Delta for funding/OI/basis
    (Shark does not publish them on its public API)."""
    from backend.markets.crypto import DELTA
    base = pair.upper().replace("USDT", "").replace("INR", "").replace("USD", "")
    out: dict[str, Any] = {"pair": pair, "source": "Delta Exchange India"}
    try:
        r = (await DELTA._get(f"/v2/tickers/{base}USD")).get("result") or {}
        mark, spot = float(r.get("mark_price") or 0), float(r.get("spot_price") or 0)
        out.update(mark=mark, spot=spot, basis_pct=(mark / spot - 1) * 100 if mark and spot else None,
                   funding_rate_pct=float(r.get("funding_rate") or 0) if r.get("funding_rate") is not None else None,
                   oi_contracts=float(r.get("oi_contracts") or r.get("oi") or 0), oi_usd=float(r.get("oi_value_usd") or 0) or None,
                   oi_change_usd_6h=float(r.get("oi_change_usd_6h") or 0) if r.get("oi_change_usd_6h") is not None else None)
    except Exception as exc:                          # noqa: BLE001
        out["error"] = f"Delta derivatives data unavailable: {str(getattr(exc, 'detail', exc))[:120]}"
    return out


def liquidation_estimates(price: float, swing_low: float | None, swing_high: float | None) -> dict:
    """Where leveraged positions opened near recent swings would be liquidated (isolated margin, ~0.5% maintenance).
    These are estimates from arithmetic, not exchange liquidation data (exchanges do not publish it)."""
    out = {"note": "Estimated from leverage arithmetic around recent swings; not exchange liquidation data."}
    for lev in (10, 25, 50):
        m = 1 / lev - 0.005
        if swing_low:
            out[f"long_{lev}x"] = swing_low * (1 - m)
        if swing_high:
            out[f"short_{lev}x"] = swing_high * (1 + m)
    return out


def iv_rank(underlying: str, atm_iv: float | None) -> dict:
    if not atm_iv:
        return {"available": False}
    hist = [r["atm_iv"] for r in store.q("SELECT atm_iv FROM chain_snapshots WHERE underlying=? AND atm_iv>0 ORDER BY t DESC LIMIT 2000", (underlying,))]
    if len(hist) < 20:
        return {"available": False, "note": f"needs 20+ recorded snapshots (have {len(hist)}); the recorder adds one every 15 minutes"}
    lo, hi = min(hist), max(hist)
    return {"available": True, "rank": round(100 * (atm_iv - lo) / (hi - lo), 1) if hi > lo else 50.0,
            "percentile": round(100 * sum(1 for x in hist if x < atm_iv) / len(hist), 1), "samples": len(hist)}


def save_snapshot(ch: dict) -> None:
    if ch.get("error"):
        return
    store.execute("INSERT OR REPLACE INTO chain_snapshots VALUES (?,?,?,?,?,?,?)",
                  (ch["underlying"], int(time.time()), ch.get("expiry"), ch.get("spot"), ch.get("atm_iv") or 0,
                   ch.get("pcr_oi") or 0, json.dumps({k: v for k, v in ch.items() if k != "strikes"})))


def history_trends(underlying: str, hours: float = 6) -> dict:
    """PCR and ATM IV change over the recorded snapshots (for PCR divergence and IV expansion/contraction)."""
    rows = store.q("SELECT t, spot, atm_iv, pcr FROM chain_snapshots WHERE underlying=? AND t>=? ORDER BY t",
                   (underlying, int(time.time() - hours * 3600)))
    if len(rows) < 2:
        return {"available": False}
    a, b = rows[0], rows[-1]
    out = {"available": True, "from": a["t"], "to": b["t"], "pcr_change": b["pcr"] - a["pcr"],
           "iv_change": b["atm_iv"] - a["atm_iv"], "spot_change_pct": (b["spot"] / a["spot"] - 1) * 100 if a["spot"] else None}
    if out["spot_change_pct"] is not None:
        if out["pcr_change"] > 0.1 and out["spot_change_pct"] < -0.2:
            out["pcr_divergence"] = "PCR rising while price falls: puts being written into weakness (possible support)"
        elif out["pcr_change"] < -0.1 and out["spot_change_pct"] > 0.2:
            out["pcr_divergence"] = "PCR falling while price rises: calls being written into strength (possible cap)"
    out["iv_state"] = "expanding" if out["iv_change"] > 1 else "contracting" if out["iv_change"] < -1 else "stable"
    return out
