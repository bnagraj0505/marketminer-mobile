"""Crypto dashboard API for MarketMiner (/api/crypto/*). Public data only, no keys:

  Binance spot      24h tickers (prices, breadth, movers), daily candles (trend)
  Binance futures   funding rate, open interest (+24h change), global long/short account ratio, TradFi perps
  Delta Exchange    options open interest by strike (BTC / ETH / XAUT), mark IV, max pain, PCR
  alternative.me    Crypto Fear & Greed index

/api/crypto/assessment turns these into a rule-based market view, strategy notes and risk factors (no AI needed;
the page can also ask the configured AI provider for a write-up). Educational, not investment advice.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import math
import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException

from .crypto import BINANCE, BINANCEF, DELTA

router = APIRouter(prefix="/api/crypto", tags=["Crypto dashboard"])
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT",
         "TONUSDT", "TRXUSDT", "LTCUSDT"]
DERIV = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]
TRADFI = {"XAUUSDT": "Gold", "XAGUSDT": "Silver", "CLUSDT": "WTI crude", "BZUSDT": "Brent crude",
          "NATGASUSDT": "Natural gas"}
STABLE = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USD1", "EUR", "AEUR", "XUSD", "USDE", "PYUSD", "RLUSD"}
_cache: dict[str, tuple[float, Any]] = {}


async def cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = await fn()
    _cache[key] = (time.time(), val)
    return val


def f(x, d=None):
    try:
        v = float(x)
        return v if math.isfinite(v) else d
    except (TypeError, ValueError):
        return d


# ---------------------------------------------------------------- spot: prices, breadth, movers

async def _all_24h():
    return await cached("spot24", 30, lambda: BINANCE._request("GET", "/api/v3/ticker/24hr"))


@router.get("/overview")
async def overview():
    rows = await _all_24h()
    by = {r["symbol"]: r for r in rows if isinstance(r, dict)}
    coins = []
    for s in COINS:
        r = by.get(s)
        if r:
            coins.append({"symbol": s, "coin": s[:-4], "price": f(r.get("lastPrice")), "chg": f(r.get("priceChangePercent")),
                          "high": f(r.get("highPrice")), "low": f(r.get("lowPrice")), "vol_usd": f(r.get("quoteVolume"))})
    liquid = []
    for s, r in by.items():
        base = s[:-4]
        if not s.endswith("USDT") or base in STABLE or base.endswith(("UP", "DOWN", "BULL", "BEAR")):
            continue
        qv = f(r.get("quoteVolume"), 0)
        if qv >= 5_000_000 and f(r.get("lastPrice"), 0) > 0:
            liquid.append({"symbol": s, "coin": base, "price": f(r.get("lastPrice")), "chg": f(r.get("priceChangePercent"), 0),
                           "vol_usd": qv})
    adv = sum(1 for r in liquid if r["chg"] > 0)
    dec = sum(1 for r in liquid if r["chg"] < 0)
    srt = sorted(liquid, key=lambda r: r["chg"])
    vol_total = sum(r["vol_usd"] for r in liquid)
    return {"as_of": dt.datetime.now(IST).isoformat(timespec="seconds"), "coins": coins,
            "breadth": {"advances": adv, "declines": dec, "unchanged": len(liquid) - adv - dec, "universe": len(liquid),
                        "rule": "USDT pairs with > $5M 24h volume, stablecoins excluded"},
            "gainers": srt[::-1][:8], "losers": srt[:8], "volume_usd": vol_total}


# ---------------------------------------------------------------- trend (daily candles)

def rsi(c, n=14):
    if len(c) < n + 1:
        return None
    g = [max(0, c[i] - c[i - 1]) for i in range(1, len(c))]
    l = [max(0, c[i - 1] - c[i]) for i in range(1, len(c))]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    for i in range(n, len(g)):
        ag, al = (ag * (n - 1) + g[i]) / n, (al * (n - 1) + l[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


async def _trend(sym):
    async def load():
        rows = await BINANCE.klines_page(sym, "1d", 120)
        c = [r[4] for r in rows[:-1]]            # completed days only
        last = rows[-1][4] if rows else None
        if len(c) < 55:
            return None
        ma20, ma50 = sum(c[-20:]) / 20, sum(c[-50:]) / 50
        rets = [math.log(c[i] / c[i - 1]) for i in range(len(c) - 30, len(c))]
        vol30 = statistics_stdev(rets) * math.sqrt(365) * 100
        return {"symbol": sym, "price": last, "ma20": ma20, "ma50": ma50, "rsi": rsi(c), "hi20": max(c[-20:]),
                "lo20": min(c[-20:]), "chg7d": (c[-1] / c[-8] - 1) * 100, "chg30d": (c[-1] / c[-31] - 1) * 100,
                "realized_vol30": vol30}
    return await cached(f"trend:{sym}", 600, load)


def statistics_stdev(x):
    m = sum(x) / len(x)
    return math.sqrt(sum((v - m) ** 2 for v in x) / (len(x) - 1))


@router.get("/trend")
async def trend():
    out = await asyncio.gather(*[_trend(s) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT")], return_exceptions=True)
    return {"items": [o for o in out if isinstance(o, dict)]}


# ---------------------------------------------------------------- derivatives (Binance USD-M)

async def _deriv(sym):
    async def load():
        prem, oi_hist, ls = await asyncio.gather(
            BINANCEF._request("GET", "/fapi/v1/premiumIndex", params={"symbol": sym}),
            BINANCEF._request("GET", "/futures/data/openInterestHist", params={"symbol": sym, "period": "1h", "limit": 25}),
            BINANCEF._request("GET", "/futures/data/globalLongShortAccountRatio", params={"symbol": sym, "period": "1h", "limit": 1}),
            return_exceptions=True)
        out: dict[str, Any] = {"symbol": sym, "coin": sym[:-4]}
        if isinstance(prem, dict):
            out.update(mark=f(prem.get("markPrice")), index=f(prem.get("indexPrice")),
                       funding_pct=(f(prem.get("lastFundingRate"), 0) * 100),
                       next_funding=prem.get("nextFundingTime"))
        if isinstance(oi_hist, list) and oi_hist:
            first, last = f(oi_hist[0].get("sumOpenInterestValue")), f(oi_hist[-1].get("sumOpenInterestValue"))
            out.update(oi_usd=last, oi_chg24_pct=((last / first - 1) * 100) if first and last else None)
        if isinstance(ls, list) and ls:
            out.update(long_short=f(ls[-1].get("longShortRatio")), long_pct=f(ls[-1].get("longAccount"), 0) * 100)
        return out
    return await cached(f"deriv:{sym}", 60, load)


@router.get("/derivs")
async def derivs():
    rows = await _all_24h()
    by = {r["symbol"]: r for r in rows if isinstance(r, dict)}
    items = await asyncio.gather(*[_deriv(s) for s in DERIV], return_exceptions=True)
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        chg = f((by.get(it["symbol"]) or {}).get("priceChangePercent"))
        it["chg24"] = chg
        it["read"] = read_oi(chg, it.get("oi_chg24_pct"))
        out.append(it)
    return {"items": out}


def read_oi(price_chg, oi_chg):
    if price_chg is None or oi_chg is None:
        return None
    if abs(oi_chg) < 1:
        return "Open interest flat: no strong new positioning"
    if price_chg >= 0 and oi_chg > 0:
        return "Price up + OI up: new longs (trend confirmation)"
    if price_chg < 0 and oi_chg > 0:
        return "Price down + OI up: new shorts (bearish pressure)"
    if price_chg >= 0 and oi_chg < 0:
        return "Price up + OI down: short covering (weaker rally)"
    return "Price down + OI down: long liquidation"


@router.get("/tradfi")
async def tradfi():
    async def one(sym):
        r = await BINANCEF._request("GET", "/fapi/v1/ticker/24hr", params={"symbol": sym})
        return {"symbol": sym, "name": TRADFI[sym], "price": f(r.get("lastPrice")), "chg": f(r.get("priceChangePercent")),
                "vol_usd": f(r.get("quoteVolume"))}
    res = await cached("tradfi", 60, lambda: asyncio.gather(*[one(s) for s in TRADFI], return_exceptions=True))
    return {"items": [r for r in res if isinstance(r, dict)]}


# ---------------------------------------------------------------- Fear & Greed

@router.get("/fng")
async def fng():
    async def load():
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://api.alternative.me/fng/", params={"limit": 8})
        d = r.json().get("data") or []
        return {"now": int(d[0]["value"]), "label": d[0]["value_classification"],
                "yesterday": int(d[1]["value"]) if len(d) > 1 else None,
                "week_ago": int(d[7]["value"]) if len(d) > 7 else None, "source": "alternative.me"}
    try:
        return await cached("fng", 900, load)
    except Exception as exc:                     # noqa: BLE001
        raise HTTPException(502, f"Fear & Greed index unavailable ({type(exc).__name__})")


# ---------------------------------------------------------------- options (Delta Exchange India)

async def _opt_tickers(coin):
    return await cached(f"opt:{coin}", 60, lambda: DELTA._request(
        "GET", "/v2/tickers", params={"contract_types": "call_options,put_options", "underlying_asset_symbols": coin}))


def _expiry(symbol):                          # C-BTC-77200-011126 -> 2026-11-01
    try:
        d = symbol.rsplit("-", 1)[1]
        return f"20{d[4:6]}-{d[2:4]}-{d[0:2]}"
    except IndexError:
        return None


@router.get("/options")
async def options(coin: str = "BTC", expiry: str | None = None, width: int = 3):
    coin = coin.upper()
    width = max(1, min(int(width), 25))
    if coin not in ("BTC", "ETH", "XAUT"):
        raise HTTPException(400, "coin must be BTC, ETH or XAUT")
    data = await _opt_tickers(coin)
    rows = data.get("result") if isinstance(data, dict) else None
    if not rows:
        raise HTTPException(502, "Delta Exchange returned no option tickers")
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    by_exp: dict[str, list] = {}
    spot = None
    for r in rows:
        e = _expiry(r.get("symbol", ""))
        if not e or e < today:
            continue
        by_exp.setdefault(e, []).append(r)
        spot = spot or f(r.get("spot_price"))
    exps = sorted(by_exp)
    oi_by_exp = {e: sum(f(r.get("oi_value_usd"), 0) for r in by_exp[e]) for e in exps}
    # default: the nearest expiry with real open interest (dailies that expire today are skipped)
    default = next((e for e in exps if e > today and oi_by_exp[e] > 0), exps[0] if exps else None)
    chosen = expiry if expiry in by_exp else default
    strikes: dict[float, dict] = {}
    for r in by_exp.get(chosen, []):
        k = f(r.get("strike_price"))
        side = "ce" if r.get("contract_type") == "call_options" else "pe"
        s = strikes.setdefault(k, {"strike": k})
        s[side] = {"oi": f(r.get("oi"), 0), "oi_usd": f(r.get("oi_value_usd"), 0), "oi_chg6h_usd": f(r.get("oi_change_usd_6h"), 0),
                   "mark": f(r.get("mark_price")), "iv": (f(r.get("mark_vol"), 0) or 0) * 100,
                   "delta": f((r.get("greeks") or {}).get("delta")),
                   "mark_chg24h": f(r.get("mark_change_24h")), "symbol": r.get("symbol"),
                   "theta": f((r.get("greeks") or {}).get("theta")), "gamma": f((r.get("greeks") or {}).get("gamma"))}
    ks = sorted(strikes)
    if not ks:
        raise HTTPException(404, f"No {coin} options for {chosen}")
    atm = min(ks, key=lambda k: abs(k - (spot or k)))
    i = ks.index(atm)
    view = [strikes[k] for k in ks[max(0, i - width): i + width + 1]]
    tot_ce = sum((strikes[k].get("ce") or {}).get("oi", 0) for k in ks)
    tot_pe = sum((strikes[k].get("pe") or {}).get("oi", 0) for k in ks)
    pain = min(ks, key=lambda x: sum((strikes[k].get("ce") or {}).get("oi", 0) * max(0, x - k) +
                                     (strikes[k].get("pe") or {}).get("oi", 0) * max(0, k - x) for k in ks))
    atm_iv = [v for v in ((strikes[atm].get("ce") or {}).get("iv"), (strikes[atm].get("pe") or {}).get("iv")) if v]
    iv = sum(atm_iv) / len(atm_iv) if atm_iv else None
    exp_dt = dt.datetime.fromisoformat(chosen + "T12:00:00+00:00")
    days = max(1 / 24, (exp_dt - dt.datetime.now(dt.timezone.utc)).total_seconds() / 86400)
    move = spot * iv / 100 * math.sqrt(days / 365) if spot and iv else None
    return {"coin": coin, "spot": spot, "expiries": [{"date": e, "oi_usd": round(oi_by_exp[e])} for e in exps],
            "expiry": chosen, "atm": atm, "strikes": view, "pcr": (tot_pe / tot_ce) if tot_ce else None,
            "oi_ce": tot_ce, "oi_pe": tot_pe, "max_pain": pain, "atm_iv": iv, "days": days, "expected_move": move,
            "expiry_oi_usd": oi_by_exp.get(chosen), "source": "Delta Exchange India (oi in coins, mark IV)"}


# ---------------------------------------------------------------- assessment: market view, strategy, risks

async def _safe(coro):
    try:
        return await coro
    except Exception:                             # noqa: BLE001 - one missing source must not sink the view
        return None


@router.get("/assessment")
async def assessment():
    ov, tr, dv, fg, ob, oe = await asyncio.gather(_safe(overview()), _safe(trend()), _safe(derivs()), _safe(fng()),
                                                  _safe(options("BTC")), _safe(options("ETH")))
    score, why, strat, risks, used = 0.0, [], [], [], []
    btc = next((t for t in (tr or {}).get("items", []) if t["symbol"] == "BTCUSDT"), None)
    eth = next((t for t in (tr or {}).get("items", []) if t["symbol"] == "ETHUSDT"), None)
    fmt = lambda x: f"{x:,.0f}" if x and x > 100 else (f"{x:,.2f}" if x else "—")

    if btc:                                       # trend: 40 points
        used.append("trend")
        p = btc["price"]
        t = (15 if p > btc["ma20"] else -15) + (15 if p > btc["ma50"] else -15) + (10 if btc["ma20"] > btc["ma50"] else -10)
        score += t
        why.append(f"BTC {fmt(p)} is {'above' if p > btc['ma20'] else 'below'} its 20-day average ({fmt(btc['ma20'])}) and "
                   f"{'above' if p > btc['ma50'] else 'below'} the 50-day ({fmt(btc['ma50'])}); 7-day change {btc['chg7d']:+.1f}%.")
        if btc["rsi"] and btc["rsi"] > 70:
            risks.append(f"BTC daily RSI {btc['rsi']:.0f}: overbought, pullback risk is higher.")
        elif btc["rsi"] and btc["rsi"] < 30:
            risks.append(f"BTC daily RSI {btc['rsi']:.0f}: oversold; sharp relief bounces are common, shorts get squeezed.")
    if ov:                                        # breadth: 20 points
        used.append("breadth")
        b = ov["breadth"]
        tot = (b["advances"] + b["declines"]) or 1
        share = b["advances"] / tot
        score += (share - 0.5) * 40
        why.append(f"Breadth: {b['advances']} of {tot} liquid coins up in 24h ({share * 100:.0f}%).")
    if dv and dv.get("items"):                    # positioning: 20 points
        used.append("derivatives")
        bd = next((d for d in dv["items"] if d["coin"] == "BTC"), dv["items"][0])
        fr = bd.get("funding_pct")
        if fr is not None:
            if fr > 0.03:
                score -= 5
                risks.append(f"BTC funding {fr:.3f}%/8h is high: longs are crowded and pay to hold; long-squeeze risk.")
            elif fr < -0.01:
                score += 5
                risks.append(f"BTC funding {fr:.3f}%/8h is negative: shorts crowded; short-squeeze risk on good news.")
        if bd.get("read"):
            why.append(f"BTC derivatives: {bd['read']} (OI 24h {bd.get('oi_chg24_pct', 0):+.1f}%, funding {fr:.3f}%).")
            if "new longs" in bd["read"]:
                score += 10
            elif "new shorts" in bd["read"] or "liquidation" in bd["read"]:
                score -= 10
        ls = bd.get("long_short")
        if ls and ls > 2.0:
            risks.append(f"Long/short account ratio {ls:.2f}: retail heavily long; contrarian downside risk.")
        elif ls and ls < 0.8:
            risks.append(f"Long/short account ratio {ls:.2f}: crowd is short; squeeze risk.")
    if fg:                                        # sentiment: contrarian 10 points
        used.append("fear & greed")
        v = fg["now"]
        why.append(f"Fear & Greed {v} ({fg['label']}), a week ago {fg.get('week_ago')}.")
        if v >= 75:
            score -= 5
            risks.append(f"Fear & Greed {v} (extreme greed): late-cycle euphoria, tighten stops.")
        elif v <= 25:
            score += 5
            risks.append(f"Fear & Greed {v} (extreme fear): capitulation phases are volatile both ways.")
    if ob:                                        # options: 10 points
        used.append("options")
        pcr, mp, sp = ob.get("pcr"), ob.get("max_pain"), ob.get("spot")
        if pcr is not None:
            score += 5 if pcr < 0.7 else -5 if pcr > 1.2 else 0
        line = f"BTC options ({ob['expiry']}): PCR {pcr:.2f}, max pain {fmt(mp)}" if pcr is not None else ""
        if ob.get("atm_iv"):
            line += f", ATM IV {ob['atm_iv']:.0f}% → expected move ±{fmt(ob['expected_move'])} by expiry"
        if line:
            why.append(line + ".")
        if ob.get("atm_iv") and btc and btc.get("realized_vol30") and ob["atm_iv"] > btc["realized_vol30"] * 1.3:
            risks.append(f"Implied vol {ob['atm_iv']:.0f}% is well above 30-day realised {btc['realized_vol30']:.0f}%: "
                         "the options market is pricing an event.")
        if ob.get("days", 9) < 2 and (ob.get("expiry_oi_usd") or 0) > 0:
            risks.append(f"BTC options expiry {ob['expiry']} within 2 days (${(ob['expiry_oi_usd'] or 0) / 1e6:.1f}M OI on Delta): "
                         f"price can gravitate to max pain {fmt(mp)} into expiry.")
    wd = dt.datetime.now(IST).weekday()
    if wd >= 5:
        risks.append("Weekend: thinner order books and no CME/ETF flows; gaps and wicks are more likely.")
    risks.append("Event risk: US data (CPI, jobs, FOMC), ETF flow swings, exchange or regulatory news can override every signal here.")

    score = max(-100, min(100, round(score)))
    view = ("Bullish" if score >= 35 else "Mildly bullish" if score >= 12 else
            "Bearish" if score <= -35 else "Mildly bearish" if score <= -12 else "Neutral / range")
    tone = "bull" if score >= 12 else "bear" if score <= -12 else "ntl"

    if btc:
        p, m20, hi, lo = btc["price"], btc["ma20"], btc["hi20"], btc["lo20"]
        if tone == "bull":
            strat.append(f"Trend-following: favour longs on pullbacks toward the 20-day average ({fmt(m20)}); "
                         f"invalidate below the 20-day low ({fmt(lo)}).")
            strat.append("Intraday: take Day Desk opening-range breakouts on the buy side only; skip breakdowns.")
        elif tone == "bear":
            strat.append(f"Defensive: rallies into the 20-day average ({fmt(m20)}) are sell zones; "
                         f"no fresh longs until BTC closes above it.")
            strat.append("Intraday: favour breakdown (sell) setups on Binance futures / Shark, or stay flat; keep size small.")
        else:
            strat.append(f"Range: BTC between the 20-day low {fmt(lo)} and high {fmt(hi)}; buy near support, trim near resistance.")
            if ob and ob.get("expected_move") and ob.get("spot"):
                up, dn = ob["spot"] + ob["expected_move"], ob["spot"] - ob["expected_move"]
                strat.append(f"Options: with ATM IV {ob['atm_iv']:.0f}%, the 1-σ range to {ob['expiry']} is {fmt(dn)}–{fmt(up)}; "
                             "defined-risk premium selling outside it (iron condor) suits a range; buy options only for breakouts.")
    if dv and any((d.get("funding_pct") or 0) > 0.03 for d in dv["items"]):
        strat.append("Funding is rich: avoid chasing longs with leverage; wait for funding to reset or use spot.")
    if eth and btc:
        rel = eth["chg7d"] - btc["chg7d"]
        strat.append(f"Relative strength: ETH {'outperformed' if rel > 0 else 'lagged'} BTC by {abs(rel):.1f} pts over 7 days; "
                     f"{'alt exposure has momentum' if rel > 2 else 'keep alt exposure light' if rel < -2 else 'no clear alt rotation'}.")
    strat.append("Size every trade from the stop (Day Desk sizer); max 1–2% of capital at risk per idea.")

    return {"as_of": dt.datetime.now(IST).isoformat(timespec="seconds"), "score": score, "view": view, "tone": tone,
            "why": why, "strategy": strat, "risks": risks, "inputs": used,
            "note": "Rule-based summary of public market data. Educational only, not investment advice."}
