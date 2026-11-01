"""Options lab: liquidity sweeps on every timeframe and time decay, for the ATM ±3 strikes of an index.

Data: MarketMiner's own FYERS login. One option chain call (greeks) plus one 15-minute history call per contract
(and one for the index). Hourly, daily, weekly and monthly candles are built from the 15-minute candles, so the
whole board costs about 16 FYERS calls and is cached for a few minutes.

Liquidity sweeps
- Monthly, weekly, daily: the liquidity is the previous period's high (buy-side, BSL) and low (sell-side, SSL).
  The current period sweeps it when it trades beyond that level but is back inside it at the latest close;
  a close beyond it means the level was taken (a breakout), not swept.
- Hourly and 15 minutes: the liquidity is every untouched swing high/low (a pivot with SPAN candles each side), the
  same rule SmartEntry's SMC engine uses: a candle that wicks through and closes back inside sweeps it, a close
  beyond takes it, and a candle that opens a new session never counts as a sweep.

Time decay
- Time value = premium - intrinsic value (from the index spot). Theta from FYERS is the decay per day.
- Time value lost today = (time value at the session open) - (time value now), using the option's and the index's
  first 15-minute candle of the day, so the index's own move is taken out.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from backend.brokers.fyers.client import data_get

router = APIRouter(prefix="/api/optlab", tags=["Options lab"])

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
INDEX = {
    "NIFTY": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX", "FINNIFTY": "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX", "NIFTYNXT50": "NSE:NIFTYNXT50-INDEX",
    "SENSEX": "BSE:SENSEX-INDEX", "BANKEX": "BSE:BANKEX-INDEX",
}
SPAN = {"60": 2, "15": 3}  # pivot span per intraday timeframe
LOOKBACK = 300  # candles; older untouched swings stop counting as liquidity (SmartEntry LIQUIDITY_LOOKBACK)
HIST_DAYS = 62  # enough 15-minute candles for the previous month
_cache: dict[str, tuple[float, Any]] = {}
_locks: dict[str, asyncio.Lock] = {}


def _market_open(now: dt.datetime | None = None) -> bool:
    n = now or dt.datetime.now(IST)
    return n.weekday() < 5 and (9 * 60 + 15) <= n.hour * 60 + n.minute <= (15 * 60 + 30)


def _ttl() -> float:
    return 240 if _market_open() else 1800


async def _cached(key: str, make):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _ttl():
        return hit[1]
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < _ttl():
            return hit[1]
        val = await make()
        _cache[key] = (time.time(), val)
        return val


async def _candles(symbol: str) -> list[dict]:
    async def make():
        today = dt.datetime.now(IST).date()
        p = await data_get("/history", {"symbol": symbol, "resolution": "15", "date_format": "1",
                                        "range_from": (today - dt.timedelta(days=HIST_DAYS)).isoformat(),
                                        "range_to": today.isoformat(), "cont_flag": "1"})
        return [{"t": int(c[0]), "o": float(c[1]), "h": float(c[2]), "l": float(c[3]), "c": float(c[4])}
                for c in (p.get("candles") or []) if len(c) >= 5]
    return await _cached("h:" + symbol, make)


# ------------------------------------------------------------------------------------------------ candles


def _ist(t: int, tz: dt.tzinfo = IST) -> dt.datetime:
    return dt.datetime.fromtimestamp(t, tz)


def _group(bars: list[dict], key) -> list[dict]:
    out: list[dict] = []
    for b in bars:
        k = key(b)
        if out and out[-1]["k"] == k:
            g = out[-1]
            g["h"], g["l"], g["c"] = max(g["h"], b["h"]), min(g["l"], b["l"]), b["c"]
        else:
            out.append({"k": k, "t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]})
    return out


def _hour_key(b: dict):
    d = _ist(b["t"])
    m = d.hour * 60 + d.minute - 555  # FYERS hourly candles start at 09:15
    return (d.date(), m // 60)


def tf_keys(tz: dt.tzinfo = IST) -> dict:
    return {"M": lambda b: (_ist(b["t"], tz).year, _ist(b["t"], tz).month),
            "W": lambda b: tuple(_ist(b["t"], tz).isocalendar()[:2]),
            "D": lambda b: _ist(b["t"], tz).date()}


TF_KEYS = tf_keys(IST)

# ------------------------------------------------------------------------------------------------ sweeps


def _period_sweep(bars: list[dict], tf: str, keys: dict | None = None) -> dict:
    """Previous-period high/low as liquidity; how the current period has treated it."""
    g = _group(bars, (keys or TF_KEYS)[tf])
    if len(g) < 2:
        return {"state": "new", "text": "Contract too new for a previous " + {"M": "month", "W": "week", "D": "day"}[tf]}
    prev, cur = g[-2], g[-1]
    ev = []
    if cur["h"] > prev["h"]:
        ev.append("bsl_taken" if cur["c"] > prev["h"] else "bsl_swept")
    if cur["l"] < prev["l"]:
        ev.append("ssl_taken" if cur["c"] < prev["l"] else "ssl_swept")
    state = next((e for e in ev if e.endswith("swept")), ev[0] if ev else "inside")
    return {"state": state, "events": ev, "bsl": prev["h"], "ssl": prev["l"], "hi": cur["h"], "lo": cur["l"],
            "close": cur["c"], "at": cur["t"]}


def _pivot(bars: list[dict], j: int, kind: str, span: int) -> bool:
    w = bars[j - span: j + span + 1]
    if kind == "h":
        return bars[j]["h"] == max(b["h"] for b in w) and all(b["h"] < bars[j]["h"] for b in bars[j + 1: j + span + 1])
    return bars[j]["l"] == min(b["l"] for b in w) and all(b["l"] > bars[j]["l"] for b in bars[j + 1: j + span + 1])


def _swing_sweeps(bars: list[dict], span: int, tz: dt.tzinfo = IST) -> dict:
    """SmartEntry's liquidity rule on swing pools; reports sweeps in the latest session and the open pools."""
    if len(bars) < 2 * span + 2:
        return {"state": "new", "text": "Not enough candles yet"}
    gaps = sorted(b["t"] - a["t"] for a, b in zip(bars, bars[1:]))
    step = gaps[len(gaps) // 2] if gaps else 0
    pools: list[tuple[str, float, int, int]] = []  # (kind, price, bar index, confirmed time)
    sweeps: list[dict] = []
    for i, b in enumerate(bars):
        gap_bar = i > 0 and step > 0 and b["t"] - bars[i - 1]["t"] > 2 * step
        j = i - span
        if j >= span:
            for kind in ("h", "l"):
                if _pivot(bars, j, kind, span):
                    pools.append((kind, bars[j][kind], j, b["t"]))
        keep = []
        for kind, price, idx, conf in pools:
            if i - idx > LOOKBACK:
                continue
            if b["t"] <= conf or gap_bar:
                keep.append((kind, price, idx, conf))
                continue
            if kind == "h":
                if b["c"] > price:
                    continue  # taken by a close
                if b["h"] > price:
                    sweeps.append({"side": "bsl", "level": price, "at": b["t"], "extreme": b["h"]})
                    continue
            else:
                if b["c"] < price:
                    continue
                if b["l"] < price:
                    sweeps.append({"side": "ssl", "level": price, "at": b["t"], "extreme": b["l"]})
                    continue
            keep.append((kind, price, idx, conf))
        pools = keep
    day = _ist(bars[-1]["t"], tz).date()
    today = [s for s in sweeps if _ist(s["at"], tz).date() == day]
    last = bars[-1]["c"]
    above = sorted(p[1] for p in pools if p[0] == "h" and p[1] > last)
    below = sorted((p[1] for p in pools if p[0] == "l" and p[1] < last), reverse=True)
    latest = today[-1] if today else None
    return {"state": (latest["side"] + "_swept") if latest else "none", "latest": latest,
            "count_bsl": sum(1 for s in today if s["side"] == "bsl"), "count_ssl": sum(1 for s in today if s["side"] == "ssl"),
            "bsl": above[0] if above else None, "ssl": below[0] if below else None, "session": str(day)}


def sweeps_all(bars15: list[dict]) -> dict:
    if not bars15:
        return {}
    h1 = _group(bars15, _hour_key)
    return {"M": _period_sweep(bars15, "M"), "W": _period_sweep(bars15, "W"), "D": _period_sweep(bars15, "D"),
            "1H": _swing_sweeps(h1, SPAN["60"]), "15m": _swing_sweeps(bars15, SPAN["15"])}


# ------------------------------------------------------------------------------------------------ decay


def _trend(bars: list[dict], n: int = 8) -> str:
    w = bars[-n:]
    if len(w) < 4:
        return "—"
    lh = sum(1 for a, b in zip(w, w[1:]) if b["h"] < a["h"])
    ll = sum(1 for a, b in zip(w, w[1:]) if b["l"] < a["l"])
    hh = sum(1 for a, b in zip(w, w[1:]) if b["h"] > a["h"])
    hl = sum(1 for a, b in zip(w, w[1:]) if b["l"] > a["l"])
    k = len(w) - 1
    if lh + ll >= 1.3 * k:
        return "falling"
    if hh + hl >= 1.3 * k:
        return "rising"
    return "sideways"


def decay(side: str, strike: float, ltp: float, g: dict, spot: float, bars: list[dict], idx_bars: list[dict], dte: float,
          tz: dt.tzinfo = IST) -> dict:
    intrinsic = max(0.0, spot - strike) if side == "CE" else max(0.0, strike - spot)
    tv = max(0.0, ltp - intrinsic)
    theta = abs(float(g.get("theta") or 0))
    out: dict[str, Any] = {"ltp": ltp, "intrinsic": intrinsic, "tv": tv, "tv_pct": 100 * tv / ltp if ltp else 0,
                           "theta": theta, "theta_pct": 100 * theta / ltp if ltp else 0,
                           "tv_days": (tv / theta) if theta else None, "iv": g.get("iv")}
    day = _ist(bars[-1]["t"], tz).date() if bars else None
    today = [b for b in bars if _ist(b["t"], tz).date() == day] if day else []
    idx_today = [b for b in idx_bars if _ist(b["t"], tz).date() == day] if day else []
    lost = None
    if today and idx_today:
        s0 = idx_today[0]["o"]
        i0 = max(0.0, s0 - strike) if side == "CE" else max(0.0, strike - s0)
        tv0 = max(0.0, today[0]["o"] - i0)
        lost = tv0 - tv
        out.update({"tv_open": tv0, "tv_lost": lost, "tv_lost_pct": 100 * lost / tv0 if tv0 else None,
                    "day_open": today[0]["o"], "day_hi": max(b["h"] for b in today), "day_lo": min(b["l"] for b in today),
                    "session": str(day)})
    tr = _trend(bars)
    out["trend"] = tr
    # status
    if ltp <= 0.5 or (intrinsic == 0 and theta and ltp < theta):
        st, txt = "done", "Decay done: premium is close to zero"
    elif intrinsic > 0 and tv < max(theta * 0.5, 0.02 * ltp):  # noqa: E501
        st, txt = "done", "Decay done: trading at intrinsic value, moves 1:1 with the index now"
    elif lost is not None and lost > 0 and tr == "falling":
        st = "fast" if (out["theta_pct"] >= 8 or dte <= 1) else "decaying"
        txt = "Time value is bleeding and the premium is making lower highs/lows"
    elif lost is not None and lost > 0:
        st, txt = "decaying", "Time value is shrinking; the index move is holding the premium up"
    elif tr == "rising" or (lost is not None and lost <= 0):
        st, txt = "beating", "Price action is beating decay (time value held or grew, often IV rising)"
    else:
        st, txt = "holding", "Little change in time value"
    out.update({"status": st, "status_text": txt})
    return out


# ------------------------------------------------------------------------------------------------ route


def _ddmmyyyy(s: str) -> dt.date | None:
    try:
        d, m, y = (int(x) for x in s.split("-"))
        return dt.date(y, m, d)
    except Exception:
        return None


@router.get("/board")
async def board(index: str = Query("NIFTY"), expiry: str = Query("wk", pattern="^(wk|mo)$")):
    index = index.upper()
    if index not in INDEX:
        raise HTTPException(400, "index must be one of " + ", ".join(INDEX))
    usym = INDEX[index]

    async def make():
        first = await data_get("/options-chain-v3", {"symbol": usym, "strikecount": 3, "timestamp": "", "greeks": "1"})
        d = first.get("data") or first
        exps = sorted(d.get("expiryData") or [], key=lambda e: int(e.get("expiry") or 0))
        if not exps:
            raise HTTPException(502, "FYERS returned no expiries for " + index)
        pick = exps[0] if expiry == "wk" else next((e for e in exps if e.get("expiry_flag") == "M"), exps[0])
        if pick is not exps[0]:
            nxt = await data_get("/options-chain-v3", {"symbol": usym, "strikecount": 3, "timestamp": str(pick["expiry"]), "greeks": "1"})
            d = nxt.get("data") or nxt
        chain = d.get("optionsChain") or []
        spot = next((float(r.get("ltp") or 0) for r in chain if float(r.get("strike_price") or 0) <= 0), 0.0)
        opts = [r for r in chain if r.get("option_type") in ("CE", "PE") and r.get("symbol")]
        syms = [usym] + [r["symbol"] for r in opts]
        hist = await asyncio.gather(*(_candles(s) for s in syms), return_exceptions=True)
        bars = {s: (h if isinstance(h, list) else []) for s, h in zip(syms, hist)}
        exp_dt = dt.datetime.fromtimestamp(int(pick["expiry"]), IST).replace(hour=15, minute=30)
        dte = max(0.0, (exp_dt - dt.datetime.now(IST)).total_seconds() / 86400)
        rows = []
        for r in sorted(opts, key=lambda r: (float(r["strike_price"]), r["option_type"])):
            s, k, side = r["symbol"], float(r["strike_price"]), r["option_type"]
            b = bars.get(s) or []
            rows.append({"symbol": s, "strike": k, "side": side, "ltp": float(r.get("ltp") or 0),
                         "chg_pct": float(r.get("ltpchp") or 0), "candles": len(b), "sweeps": sweeps_all(b),
                         "decay": decay(side, k, float(r.get("ltp") or 0), r.get("greeks") or {}, spot, b, bars[usym], dte)})
        atm = min((float(r["strike_price"]) for r in opts), key=lambda k: abs(k - spot)) if opts else None
        return {"index": index, "symbol": usym, "spot": spot, "atm": atm, "expiry": pick.get("date"), "dte": dte,
                "index_sweeps": sweeps_all(bars[usym]), "rows": rows, "built_at": int(time.time()),
                "market_open": _market_open(), "missing": [s for s in syms if not bars.get(s)]}

    return await _cached(f"b:{index}:{expiry}", make)
