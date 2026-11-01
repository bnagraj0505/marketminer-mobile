"""Signal Lab data adapters. One function, `candles(symbol, tf, bars)`, returns validated, closed candles plus a
data-quality report (source, as-of timestamp, gaps, staleness). Never fabricates data: when a source fails and no
fallback has the data, it raises DataUnavailable and the caller shows DATA UNAVAILABLE.

Symbols
  NSE:SBIN-EQ, NSE:NIFTY50-INDEX, NSE:NIFTY-FUT, NSE:NIFTY-ATM-CE, BSE:SENSEX-INDEX, MCX:CRUDEOIL-FUT  -> FYERS
  SHARK:BTCUSDT   Shark Exchange perpetuals; if Shark fails or lacks the timeframe/history -> Delta Exchange
                  India perpetual BTCUSD (marked in the quality report as "fallback")
  DELTAF:BTCUSD   Delta Exchange India perpetuals (mark price)
  DELTA:C-BTC-80000-271126 / DELTA:BTC-ATM-CE  Delta options
  BINANCE:BTCUSDT Binance spot
  YF:^GSPC, YF:AAPL, YF:EURUSD=X, YF:^N225   US equities, global indices, forex (Yahoo Finance chart API,
                  unofficial: may change or rate-limit; marked in the report)
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from fastapi import HTTPException

from .candle import Candle, TF_SECONDS, from_rows, validate, closed_only, resample

FY_RES = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "4h": "240", "1d": "D"}
FY_MAX_DAYS = {"1m": 100, "3m": 100, "5m": 100, "15m": 100, "30m": 100, "1h": 100, "4h": 100, "1d": 365}
CRYPTO_IV = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1d", "1w": "1w"}
YF_IV = {"1m": ("1m", "7d"), "5m": ("5m", "60d"), "15m": ("15m", "60d"), "30m": ("30m", "60d"), "1h": ("60m", "730d"),
         "1d": ("1d", "10y"), "1w": ("1wk", "10y")}
_cache: dict[tuple, tuple[float, Any]] = {}
INDIA = ("NSE", "BSE", "MCX")


class DataUnavailable(Exception):
    pass


def market_of(symbol: str) -> str:
    head = symbol.split(":", 1)[0].upper()
    if head in ("SHARK", "DELTAF", "DELTA", "BINANCE", "YF"):
        return head
    if head == "MCX":
        return "MCX"
    if symbol.upper().endswith(("-EQ", "-BE", "-INDEX")):
        return "NSE"
    return "FNO"


def anchor_minutes(symbol: str) -> int:
    m = market_of(symbol)
    return 555 if m in ("NSE", "FNO") else 540 if m == "MCX" else 0


def session_key(symbol: str):
    """Function giving each candle's session (for VWAP resets): IST trading day for India, UTC day for the rest."""
    import datetime as dt
    off = 19800 if market_of(symbol) in ("NSE", "FNO", "MCX") else 0
    return lambda t: (t + off) // 86400


async def _cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = await fn()
    _cache[key] = (time.time(), val)
    if len(_cache) > 600:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:150]:
            _cache.pop(k, None)
    return val


def _ttl(tf: str) -> float:
    return min(600.0, max(20.0, TF_SECONDS.get(tf, 300) / 3))


# ---------------------------------------------------------------- FYERS
async def _fyers(symbol: str, tf: str, bars: int) -> list[list]:
    from backend.brokers.fyers.client import data_get
    from backend.desk.routes import resolve
    sym = await resolve(symbol)
    if tf == "1w":
        rows = await _fyers(sym, "1d", bars * 5 + 10)
        return rows
    step = TF_SECONDS[tf]
    per_day = 375 * 60 // step if tf != "1d" else 1
    days = min(FY_MAX_DAYS[tf], max(5, int(bars / max(1, per_day) * 1.6) + 5)) if tf != "1d" else min(365, int(bars * 1.5) + 10)
    import datetime as dt
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).date()
    chunks = [(today - dt.timedelta(days=days), today)]
    if tf == "1d" and bars > 240:                       # FYERS: 366 days per daily request
        chunks = [(today - dt.timedelta(days=d + 365), today - dt.timedelta(days=d)) for d in range(0, int(bars * 1.5) + 365, 366)][::-1]
    rows: list = []
    for a, b in chunks:
        p = await data_get("/history", {"symbol": sym, "resolution": FY_RES[tf], "date_format": "1",
                                        "range_from": a.isoformat(), "range_to": b.isoformat(), "cont_flag": "1"})
        rows += p.get("candles") or []
    return rows


# ---------------------------------------------------------------- crypto
async def _shark(pair: str, tf: str, bars: int) -> list[list]:
    from backend.markets.crypto import SHARK
    rows: list = []
    end = None
    while len(rows) < bars:
        page = await SHARK.klines_page(pair, CRYPTO_IV[tf], min(1500, bars - len(rows) + 5), end_ms=end)
        if not page:
            break
        rows = page + [r for r in rows if r[0] > page[-1][0]]
        if len(page) < 1500:
            break
        end = page[0][0] * 1000 - 1
    return rows


def delta_perp_for(pair: str) -> str:
    base = pair.upper().replace("USDT", "").replace("INR", "").replace("USD", "")
    return f"{base}USD"


async def _delta(symbol: str, tf: str, bars: int) -> list[list]:
    from backend.markets.crypto import DELTA
    res = CRYPTO_IV[tf]
    now = int(time.time())
    step = TF_SECONDS[tf]
    start = now - (bars + 5) * step
    rows: list = []
    win = 2000 * step
    while start < now:
        stop = min(now, start + win)
        d = await DELTA._get("/v2/history/candles", resolution=res, symbol=f"MARK:{symbol}", start=start, end=stop)
        rows += DELTA._rows(d.get("result"))
        start = stop
    return rows


async def _binance(pair: str, tf: str, bars: int) -> list[list]:
    from backend.markets.crypto import BINANCE
    rows: list = []
    end = None
    while len(rows) < bars:
        page = await BINANCE.klines_page(pair, CRYPTO_IV[tf], min(1000, bars - len(rows) + 5), end_ms=end)
        if not page:
            break
        rows = page + [r for r in rows if r[0] > page[-1][0]]
        if len(page) < 1000:
            break
        end = page[0][0] * 1000 - 1
    return rows


# ---------------------------------------------------------------- Yahoo Finance (US / global / forex)
async def _yahoo(sym: str, tf: str, bars: int) -> list[list]:
    base_tf = tf if tf in YF_IV else {"3m": "1m", "4h": "1h"}[tf]
    iv, rng = YF_IV[base_tf]
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
    params = {"interval": iv, "range": rng, "includePrePost": "false"}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=8)) as c:
        r = await c.get(url, params=params, headers=headers)
    if r.status_code in (401, 403, 429):
        from backend import nse_gateway as NG
        alt = await NG.browser_get(url + "?" + "&".join(f"{k}={v}" for k, v in params.items()))
        if not alt or alt[0] >= 400:
            raise DataUnavailable(f"Yahoo Finance refused the request (HTTP {r.status_code})")
        import json
        j = json.loads(alt[2])
    else:
        j = r.json()
    res = ((j.get("chart") or {}).get("result") or [None])[0]
    if not res:
        raise DataUnavailable(f"Yahoo Finance has no data for {sym}")
    ts = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    rows = []
    for k, t in enumerate(ts):
        o, h, l, cl, v = (q.get(x, [None] * len(ts))[k] for x in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, cl):
            continue
        rows.append([int(t), o, h, l, cl, v or 0])
    if base_tf != tf:
        bars_ = resample(from_rows(rows), tf)
        rows = [[b.time, b.open, b.high, b.low, b.close, b.volume] for b in bars_]
    return rows


# ---------------------------------------------------------------- public
async def candles(symbol: str, tf: str, bars: int = 400) -> tuple[list[Candle], dict]:
    """Closed, validated candles for `symbol` on `tf`, newest last, plus the quality report."""
    symbol = symbol.strip().upper() if not symbol.upper().startswith("YF:") else "YF:" + symbol.split(":", 1)[1].strip()
    if tf not in TF_SECONDS:
        raise HTTPException(400, f"timeframe must be one of {', '.join(TF_SECONDS)}")
    m = market_of(symbol)
    src, note = m, None
    step = TF_SECONDS[tf]

    async def load():
        nonlocal src, note
        if m in ("NSE", "FNO", "MCX"):
            src = "FYERS"
            rows = await _fyers(symbol, tf, bars)
            if tf == "1w":
                return [[b.time, b.open, b.high, b.low, b.close, b.volume] for b in resample(from_rows(rows), "1w", 555)]
            return rows
        if m == "SHARK":
            pair = symbol.split(":", 1)[1]
            try:
                rows = await _shark(pair, tf, bars)
                if len(rows) >= min(bars, 60):
                    src = "Shark Exchange"
                    return rows
                note = f"Shark returned only {len(rows)} candles"
            except Exception as exc:                  # noqa: BLE001 - any Shark failure falls back to Delta
                note = f"Shark unavailable ({str(getattr(exc, 'detail', exc))[:80]})"
            src = f"Delta Exchange {delta_perp_for(pair)} (fallback: {note})"
            return await _delta(delta_perp_for(pair), tf, bars)
        if m == "DELTAF":
            src = "Delta Exchange (mark)"
            return await _delta(symbol.split(":", 1)[1], tf, bars)
        if m == "DELTA":
            from backend.desk.routes import resolve
            src = "Delta Exchange options (mark)"
            return await _delta((await resolve(symbol)).split(":", 1)[1], tf, bars)
        if m == "BINANCE":
            src = "Binance spot"
            return await _binance(symbol.split(":", 1)[1], tf, bars)
        if m == "YF":
            src = "Yahoo Finance (unofficial)"
            return await _yahoo(symbol.split(":", 1)[1], tf, bars)
        raise HTTPException(400, f"unknown market for {symbol}")

    async def load_with_source():
        rows = await load()
        return rows, src

    try:
        rows, src = await _cached((symbol, tf, bars), _ttl(tf), load_with_source)
    except HTTPException as exc:
        if exc.status_code in (401, 403):
            raise
        raise DataUnavailable(str(exc.detail))
    except DataUnavailable:
        raise
    except Exception as exc:                          # noqa: BLE001
        raise DataUnavailable(f"{type(exc).__name__}: {exc}")
    now = time.time()
    bars_ = closed_only(from_rows(rows), step, now)
    clean, q = validate(bars_, step, now)
    if not clean:
        raise DataUnavailable(f"No {tf} candles for {symbol}")
    if m in ("NSE", "FNO", "MCX") and tf not in ("1d", "1w"):
        q["stale"] = None if not q["stale"] else _india_stale(clean[-1].time, step, now, m)
    q.update(source=src, symbol=symbol, tf=tf, as_of=clean[-1].time + step, fetched_at=int(now))
    return clean[-bars:], q


def _india_stale(last_t: int, step: int, now: float, m: str) -> bool:
    """Outside market hours the last candle is naturally old: only call it stale during the session."""
    import datetime as dt
    t = dt.datetime.fromtimestamp(now, dt.timezone(dt.timedelta(hours=5, minutes=30)))
    mins = t.hour * 60 + t.minute
    open_ = (555, 930) if m != "MCX" else (540, 1410)
    if t.weekday() >= 5 or not open_[0] + step // 60 <= mins <= open_[1]:
        return False
    return now - (last_t + step) > 3 * step
