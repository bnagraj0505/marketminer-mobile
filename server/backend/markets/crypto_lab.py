"""Crypto options lab: confluence, liquidity sweeps and time decay for BTC / ETH / XAUT options on Delta Exchange India.

Same rules as the index version (backend/markets/opt_lab.py), on a 24/7 market whose day starts at 00:00 UTC:
- Underlying candles: Binance spot (BTCUSDT, ETHUSDT, PAXGUSDT as the gold proxy for XAUT) - daily for the
  monthly/weekly/daily liquidity and the CPR, 1h and 15m for swing liquidity.
- Option candles: Delta Exchange MARK price series, 1h (monthly/weekly/daily/hourly) and 15m.
- OI: Delta tickers (open interest in coins, 6h OI change in USD, 24h mark change).
- CPR: SmartEntry's rules from the previous UTC day.
- Confluence: OI, CPR and liquidity levels merged into zones; sweeps count only inside a zone backed by OI or CPR.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Any

from fastapi import APIRouter, HTTPException

from .crypto import BINANCE, DELTA
from .crypto_dash import options as delta_options
from .opt_lab import _group, _period_sweep, _swing_sweeps, decay, tf_keys

router = APIRouter(prefix="/api/crypto", tags=["Crypto options lab"])
UTC = dt.timezone.utc
KEYS = tf_keys(UTC)
SPOT = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "XAUT": "PAXGUSDT"}
_cache: dict[str, tuple[float, Any]] = {}
_locks: dict[str, asyncio.Lock] = {}
TTL = 240


async def _cached(key: str, ttl: float, make):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    async with _locks.setdefault(key, asyncio.Lock()):
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = await make()
        _cache[key] = (time.time(), val)
        return val


def _bars(rows) -> list[dict]:
    return [{"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4])} for r in rows or []]


async def _spot_bars(pair: str, interval: str, limit: int) -> list[dict]:
    return await _cached(f"bn:{pair}:{interval}", TTL if interval != "1d" else 900,
                         lambda: _wrap(BINANCE.klines_page(pair, interval, limit)))


async def _opt_bars(symbol: str, res: str, days: int) -> list[dict]:
    now = int(time.time())
    return await _cached(f"dl:{symbol}:{res}", TTL, lambda: _wrap(DELTA._candles(symbol, res, now - days * 86400, now)))


async def _wrap(coro):
    return _bars(await coro)


def _sweeps(daily: list[dict], h1: list[dict], m15: list[dict]) -> dict:
    return {"M": _period_sweep(daily, "M", KEYS), "W": _period_sweep(daily, "W", KEYS), "D": _period_sweep(daily, "D", KEYS),
            "1H": _swing_sweeps(h1, 2, UTC), "15m": _swing_sweeps(m15, 3, UTC)}


# ------------------------------------------------------------------------------------------------ CPR (SmartEntry rules)


def _cpr_of(d):
    p, bc = (d["h"] + d["l"] + d["c"]) / 3, (d["h"] + d["l"]) / 2
    tc = 2 * p - bc
    return {"pivot": p, "tc": max(tc, bc), "bc": min(tc, bc), "width": abs(tc - bc), "pct": 100 * abs(tc - bc) / p if p else 0}


def _side(x, c):
    return "above" if x > c["tc"] else "below" if x < c["bc"] else "inside"


def cpr(daily: list[dict], m15: list[dict]) -> dict | None:
    if len(daily) < 3 or not m15:
        return None
    today_start = int(dt.datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    done = [d for d in daily if d["t"] < today_start]
    if len(done) < 2:
        return None
    prev = done[-1]
    c = _cpr_of(prev)
    hist = [_cpr_of(d)["pct"] for d in done[-21:-1]]
    rank = 100 * sum(1 for x in hist if x < c["pct"]) / len(hist) if len(hist) >= 8 else None
    if rank is not None:
        kind = "narrow" if rank <= 30 else "wide" if rank >= 70 else "normal"
    else:
        kind = "narrow" if c["pct"] < 0.15 else "wide" if c["pct"] > 0.45 else "normal"
    price = m15[-1]["c"]
    today = [b for b in m15 if b["t"] >= today_start]
    os_ = _side(today[0]["o"], c) if today else None
    tol = max(c["width"] * 0.15, price * 0.0002)
    setup = None
    sign = 1 if os_ == "above" else -1 if os_ == "below" else 0
    for x in today[1:] if sign else []:
        if sign > 0:
            if x["c"] < c["bc"]:
                setup = {"side": "failed", "t": x["t"], "title": "Bias failed"}
                continue
            if c["bc"] - tol <= x["l"] <= c["tc"] + tol and x["c"] > c["tc"] and x["c"] > x["o"]:
                en, st = x["c"], c["bc"] - tol
                setup = {"side": "long", "t": x["t"], "entry": en, "stop": st, "target": en + 2 * (en - st), "title": "Buy: top CPR held"}
        else:
            if x["c"] > c["tc"]:
                setup = {"side": "failed", "t": x["t"], "title": "Bias failed"}
                continue
            if c["bc"] - tol <= x["h"] <= c["tc"] + tol and x["c"] < c["bc"] and x["c"] < x["o"]:
                en, st = x["c"], c["tc"] + tol
                setup = {"side": "short", "t": x["t"], "entry": en, "stop": st, "target": en - 2 * (st - en), "title": "Sell: bottom CPR rejected"}
    p, h, lo = c["pivot"], prev["h"], prev["l"]
    ps = _side(price, c)
    return {**c, "kind": kind, "rank": rank, "price": price, "price_side": ps, "open_side": os_, "setup": setup,
            "R1": 2 * p - lo, "S1": 2 * p - h, "R2": p + (h - lo), "S2": p - (h - lo), "from": prev,
            "kind_label": {"narrow": "Narrow CPR: trending day likely", "wide": "Wide CPR: slow / sideways day likely"}.get(kind, "Normal-width CPR"),
            "bias": {"above": "Above CPR: buy bias", "below": "Below CPR: sell bias"}.get(ps, "Inside CPR: sideways")}


# ------------------------------------------------------------------------------------------------ OI


def oi_build(side: str, d_oi: float, d_px: float | None):
    if not d_oi:
        return None
    up = (d_px or 0) > 0
    if d_oi > 0 and up:
        return "Long build-up", 1 if side == "ce" else -1
    if d_oi > 0:
        return ("Call writing", -1) if side == "ce" else ("Put writing", 1)
    if up:
        return "Short covering", 1 if side == "ce" else -1
    return "Long unwinding", -1 if side == "ce" else 1


def oi_levels(ch: dict) -> dict:
    spot, ks = ch["spot"], ch["strikes"]
    ce = lambda s: (s.get("ce") or {})
    pe = lambda s: (s.get("pe") or {})
    ai = min(range(len(ks)), key=lambda i: abs(ks[i]["strike"] - spot))
    band = ks[max(0, ai - 6): ai + 7]
    below = [s for s in band if s["strike"] <= spot] or band
    above = [s for s in band if s["strike"] >= spot] or band

    def pick(arr, chg, oi):
        fresh = sorted([s for s in arr if chg(s) > 0], key=chg, reverse=True)
        return fresh or sorted(arr, key=oi, reverse=True)
    S = pick(below, lambda s: pe(s).get("oi_chg6h_usd") or 0, lambda s: pe(s).get("oi") or 0)
    R = pick(above, lambda s: ce(s).get("oi_chg6h_usd") or 0, lambda s: ce(s).get("oi") or 0)
    wallS = max(below, key=lambda s: pe(s).get("oi") or 0)
    wallR = max(above, key=lambda s: ce(s).get("oi") or 0)
    b = s_ = 0.0
    for s in ks[max(0, ai - 8): ai + 9]:
        for sd, o in (("ce", ce(s)), ("pe", pe(s))):
            r = oi_build(sd, o.get("oi_chg6h_usd") or 0, o.get("mark_chg24h"))
            if r:
                if r[1] > 0:
                    b += abs(o["oi_chg6h_usd"])
                else:
                    s_ += abs(o["oi_chg6h_usd"])
    return {"sup": S[0]["strike"], "sup_chg": pe(S[0]).get("oi_chg6h_usd") or 0, "res": R[0]["strike"],
            "res_chg": ce(R[0]).get("oi_chg6h_usd") or 0, "wallS": wallS["strike"], "wallR": wallR["strike"],
            "mp": ch.get("max_pain"), "pcr": ch.get("pcr"), "flow": round(100 * (b - s_) / ((b + s_) or 1))}


# ------------------------------------------------------------------------------------------------ confluence


def confluence(spot: float, oi: dict, cp: dict | None, ix: dict, rows: list[dict], atm: float) -> dict:
    tol = max(spot * 0.0012, 0.01)
    L: list[dict] = []

    def add(px, lbl, fam):
        if px:
            L.append({"px": float(px), "lbl": lbl, "fam": fam})
    add(oi["sup"], "OI support" + (f" (+${oi['sup_chg']/1e3:,.0f}K PE 6h)" if oi["sup_chg"] > 0 else ""), "OI")
    add(oi["res"], "OI resistance" + (f" (+${oi['res_chg']/1e3:,.0f}K CE 6h)" if oi["res_chg"] > 0 else ""), "OI")
    if oi["wallS"] != oi["sup"]:
        add(oi["wallS"], "Put OI wall", "OI")
    if oi["wallR"] != oi["res"]:
        add(oi["wallR"], "Call OI wall", "OI")
    add(oi["mp"], "Max pain", "OI")
    if cp:
        for k, n in (("tc", "CPR top"), ("pivot", "CPR pivot"), ("bc", "CPR bottom"), ("R1", "Pivot R1"), ("S1", "Pivot S1"), ("R2", "Pivot R2"), ("S2", "Pivot S2")):
            add(cp[k], n, "CPR")
    names = {"M": ("Prev month high", "Prev month low"), "W": ("Prev week high", "Prev week low"), "D": ("Prev day high", "Prev day low"),
             "1H": ("1H swing-high liquidity", "1H swing-low liquidity"), "15m": ("15m swing-high liquidity", "15m swing-low liquidity")}
    for tf, (hn, ln) in names.items():
        x = ix.get(tf) or {}
        if x.get("state") != "new":
            add(x.get("bsl"), hn, "LIQ")
            add(x.get("ssl"), ln, "LIQ")
    L.sort(key=lambda l: l["px"])
    Z: list[dict] = []
    for l in L:
        if Z and l["px"] - Z[-1]["hi"] <= tol and l["px"] - Z[-1]["lo"] <= 2.5 * tol:
            Z[-1]["items"].append(l)
            Z[-1]["hi"] = l["px"]
        else:
            Z.append({"lo": l["px"], "hi": l["px"], "items": [l]})
    for z in Z:
        z["mid"] = (z["lo"] + z["hi"]) / 2
        z["fams"] = sorted({i["fam"] for i in z["items"]})
        z["stars"] = 3 if len(z["fams"]) >= 3 or len(z["items"]) + len(z["fams"]) - 1 >= 4 else 2 if len(z["fams"]) == 2 or len(z["items"]) + len(z["fams"]) - 1 >= 3 else 1
        z["side"] = "R" if z["mid"] > spot + tol * 0.3 else "S" if z["mid"] < spot - tol * 0.3 else "AT"
        z["labels"] = [i["lbl"] for i in z["items"]]
        del z["items"]
    res = sorted([z for z in Z if z["side"] == "R"], key=lambda z: z["mid"])[:3]
    sup = sorted([z for z in Z if z["side"] == "S"], key=lambda z: -z["mid"])[:3]
    at = [z for z in Z if z["side"] == "AT"]

    def zone_of(px):
        return next((z for z in Z if z["lo"] - tol <= px <= z["hi"] + tol and any(f != "LIQ" for f in z["fams"])), None)
    ev = []
    for tf in ("15m", "1H"):
        x = (ix.get(tf) or {}).get("latest")
        if x:
            z = zone_of(x["level"])
            d_ = 1 if x["side"] == "ssl" else -1
            same = next((e for e in ev if e["dir"] == d_ and abs(e["px"] - x["level"]) <= tol), None)
            if same:
                same["tf"] += " + " + tf  # the same sweep seen on both timeframes counts once
            elif z:
                ev.append({"dir": d_, "tf": tf, "px": x["level"], "at": x["at"], "zone": z})
    for tf in ("D", "W", "M"):
        x = ix.get(tf) or {}
        if x.get("state") == "ssl_swept" and zone_of(x["ssl"]):
            ev.append({"dir": 1, "tf": tf, "px": x["ssl"], "zone": zone_of(x["ssl"])})
        if x.get("state") == "bsl_swept" and zone_of(x["bsl"]):
            ev.append({"dir": -1, "tf": tf, "px": x["bsl"], "zone": zone_of(x["bsl"])})

    def lean(tfs):
        b = s = 0
        def one(x, d, tf):
            nonlocal b, s
            if not x:
                return
            st = (x["latest"]["side"] + "_swept" if x.get("latest") else "") if tf in ("1H", "15m") else x.get("state")
            if st == "ssl_swept":
                b, s = (b + 1, s) if d > 0 else (b, s + 1)
            if st == "bsl_swept":
                b, s = (b, s + 1) if d > 0 else (b + 1, s)
        for tf in tfs:
            one(ix.get(tf), 1, tf)
            for r in rows:
                one(r["sweeps"].get(tf), 1 if r["side"] == "CE" else -1, tf)
        return b, s, (b - s) / ((b + s) or 1)
    sc = []

    def push(name, v, txt):
        sc.append({"name": name, "v": max(-1.0, min(1.0, v)), "txt": txt})
    if cp:
        push("CPR", 1 if cp["price_side"] == "above" else -1 if cp["price_side"] == "below" else 0, f"{cp['bias']} · {cp['kind_label']}")
    push("OI flow", oi["flow"] / 100, f"OI flow {oi['flow']:+d} over 6h ({'bullish' if oi['flow'] > 25 else 'bearish' if oi['flow'] < -25 else 'neutral'})")
    if oi["pcr"]:
        push("PCR", 0.5 if oi["pcr"] > 1.2 else -0.5 if oi["pcr"] < 0.7 else 0, f"PCR {oi['pcr']:.2f}")
    dS, dR = spot - oi["sup"], oi["res"] - spot
    push("Room", 0.4 if dR > dS * 1.5 else -0.4 if dS > dR * 1.5 else 0, f"{dS:,.0f} above OI support, {dR:,.0f} below OI resistance")
    b, s, v = lean(("15m", "1H"))
    push("Intraday sweeps", v, f"15m + 1H: {b} bullish vs {s} bearish sweeps")
    b, s, v = lean(("D", "W", "M"))
    push("Higher-TF sweeps", v * 0.8, f"Daily/weekly/monthly: {b} bullish vs {s} bearish")
    for e in ev:
        push("Sweep at level", e["dir"] * 1.2, f"{'Sell-side' if e['dir'] > 0 else 'Buy-side'} sweep on {e['tf']} at {e['px']:,.0f} into {' + '.join(f for f in e['zone']['fams'] if f != 'LIQ')} zone")
    tot = sum(x["v"] for x in sc)
    norm = tot / max(3, len(sc)) * 1.6
    bias = "BULLISH" if norm > 0.3 else "BEARISH" if norm < -0.3 else "RANGE"
    agree = sum(1 for x in sc if x["v"] and (x["v"] > 0) == (tot > 0))
    a = [r for r in rows if r["strike"] == atm]
    ds = [r["decay"]["status"] for r in a]
    decaying = bool(ds) and all(x in ("decaying", "fast") for x in ds)
    beating = bool(ds) and all(x == "beating" for x in ds)
    plan = []
    s1, r1 = (sup[0] if sup else None), (res[0] if res else None)
    r2, s2 = (res[1] if len(res) > 1 else None), (sup[1] if len(sup) > 1 else None)
    fmt = lambda n: f"{n:,.0f}"
    rng = lambda z: fmt(z["mid"]) if abs(z["hi"] - z["lo"]) < 1e-9 else f"{fmt(z['lo'])}–{fmt(z['hi'])}"
    if bias == "BULLISH" and s1:
        plan.append(f"<b>Buy the dip:</b> wait for a 15m sell-side sweep into <b>{rng(s1)}</b> that closes back above it, then a call near {fmt(atm)}. Invalid below {fmt(s1['lo'] - tol)}. Targets {fmt(r1['mid']) if r1 else '—'}{' then ' + fmt(r2['mid']) if r2 else ''}.")
    if bias == "BEARISH" and r1:
        plan.append(f"<b>Sell the rally:</b> wait for a 15m buy-side sweep into <b>{rng(r1)}</b> that closes back below it, then a put near {fmt(atm)}. Invalid above {fmt(r1['hi'] + tol)}. Targets {fmt(s1['mid']) if s1 else '—'}{' then ' + fmt(s2['mid']) if s2 else ''}.")
    if bias == "RANGE" and s1 and r1:
        plan.append(f"<b>Range:</b> fade the edges — long only on a sell-side sweep at {fmt(s1['mid'])}, short only on a buy-side sweep at {fmt(r1['mid'])}. "
                    + ("With premiums decaying, selling premium between these zones has the edge." if decaying else "Wait for one edge to break with a close before taking a direction."))
    if cp and cp["kind"] == "narrow":
        plan.append(f"<b>Narrow CPR:</b> a trending day is likely — a clean close through {fmt(r1['mid']) if r1 else 'resistance'} or {fmt(s1['mid']) if s1 else 'support'} is more likely to run than reverse.")
    if cp and cp["kind"] == "wide":
        plan.append("<b>Wide CPR:</b> a slow, two-sided day is likely — favour sweeps and reversals at the zones over breakouts.")
    if at:
        plan.append(f"<b>Price is sitting on a zone</b> ({', '.join(fmt(z['mid']) for z in at)}): expect a reaction here first.")
    return {"bias": bias, "confidence": min(100, round(abs(norm) * 100)), "agree": agree, "signals": sc, "res": res, "sup": sup, "at": at,
            "events": ev, "tol": tol, "plan": plan,
            "decay": ("ATM call and put are both losing time value: the market is not paying for a move — sellers have the edge until a zone breaks." if decaying
                      else "ATM call and put are both holding time value: the market is pricing a move — buyers can be paid if a zone breaks." if beating
                      else "ATM premiums are mixed: decay is not the deciding factor right now.")}


# ------------------------------------------------------------------------------------------------ route


@router.get("/lab")
async def lab(coin: str = "BTC", expiry: str | None = None):
    coin = coin.upper()
    if coin not in SPOT:
        raise HTTPException(400, "coin must be BTC, ETH or XAUT")

    async def make():
        ch = await delta_options(coin, expiry, 12)
        spot, atm, pair = ch["spot"], ch["atm"], SPOT[coin]
        ks = [s["strike"] for s in ch["strikes"]]
        ai = ks.index(atm)
        near = ch["strikes"][max(0, ai - 3): ai + 4]
        opts = [(s["strike"], side.upper(), s[side]) for s in near for side in ("ce", "pe") if s.get(side) and s[side].get("symbol")]
        jobs = [_spot_bars(pair, "1d", 120), _spot_bars(pair, "1h", 500), _spot_bars(pair, "15m", 500)]
        for _, _, o in opts:
            jobs += [_opt_bars(o["symbol"], "1h", 40), _opt_bars(o["symbol"], "15m", 6)]
        res = await asyncio.gather(*jobs, return_exceptions=True)
        res = [r if isinstance(r, list) else [] for r in res]
        d1, h1, m15 = res[0], res[1], res[2]
        ix = _sweeps(d1, h1, m15) if d1 else {}
        exp_dt = dt.datetime.fromisoformat(ch["expiry"] + "T12:00:00+00:00")
        dte = max(0.0, (exp_dt - dt.datetime.now(UTC)).total_seconds() / 86400)
        rows = []
        for n, (k, side, o) in enumerate(opts):
            oh, om = res[3 + 2 * n], res[4 + 2 * n]
            days = _group(oh, KEYS["D"]) if oh else []
            sw = {"M": _period_sweep(oh, "M", KEYS) if oh else {"state": "new", "text": "No candles"},
                  "W": _period_sweep(oh, "W", KEYS) if oh else {"state": "new", "text": "No candles"},
                  "D": _period_sweep(oh, "D", KEYS) if oh else {"state": "new", "text": "No candles"},
                  "1H": _swing_sweeps(oh, 2, UTC) if oh else {"state": "new", "text": "No candles"},
                  "15m": _swing_sweeps(om, 3, UTC) if om else {"state": "new", "text": "No candles"}}
            ltp = o.get("mark") or 0
            g = {"theta": o.get("theta"), "iv": o.get("iv")}
            dc = decay(side, k, ltp, g, spot, om, m15, dte, UTC) if om else {"status": "holding", "status_text": "No candles", "ltp": ltp,
                                                                             "intrinsic": 0, "tv": 0, "tv_pct": 0, "theta": abs(o.get("theta") or 0), "theta_pct": 0, "tv_days": None, "trend": "—"}
            rows.append({"symbol": o["symbol"], "strike": k, "side": side, "ltp": ltp, "chg_pct": o.get("mark_chg24h") or 0,
                         "sweeps": sw, "decay": dc, "days": len(days)})
        cp = cpr(d1, m15)
        oi = oi_levels(ch)
        conf = confluence(spot, oi, cp, ix, rows, atm)
        return {"coin": coin, "pair": pair, "spot": spot, "atm": atm, "expiry": ch["expiry"], "expiries": ch["expiries"], "dte": dte,
                "index_sweeps": ix, "rows": rows, "cpr": cp, "oi": oi, "confluence": conf, "built_at": int(time.time()),
                "source": f"Delta Exchange India options (MARK candles) · Binance {pair} for the underlying"}

    return await _cached(f"lab:{coin}:{expiry or ''}", TTL, make)
