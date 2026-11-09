"""Crypto straddle monitor: BTC and ETH options on Delta Exchange India. Information only (no trading).

Same numbers and readings as the index straddle monitor (straddle.py): ATM straddle (call + put, mid of bid/ask,
else Delta's mark price), the centre (strike with the cheapest straddle within +/-8 strikes), breakevens, expected
move, ATM IV and the put-call-parity forward, compared with the first snapshot of the UTC day (00:00 UTC = 05:30 IST,
the crypto page's day) and with 15 / 30 minutes ago.

Expiry: the nearest Delta expiry with at least 2 hours left (Delta settles at 12:00 UTC = 17:30 IST), so the daily
contract is followed until 10:00 UTC and then the next one; comparisons only use snapshots of the same expiry.
Recorded every minute, around the clock. Delta's market data is public: it uses none of the FYERS daily requests.
Snapshots share the straddle_snapshots table (und = BTC / ETH).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time

from . import db
from . import straddle as SD

COINS = ("BTC", "ETH")
MIN_LEFT_S = 2 * 3600
STATUS: dict = {"running": False, "last": None, "errors": {}}
_task: asyncio.Task | None = None


def _num(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _expiry(symbol: str) -> dt.date | None:
    d = str(symbol).split("-")[-1]
    if len(d) != 6 or not d.isdigit():
        return None
    try:
        return dt.date(2000 + int(d[4:6]), int(d[2:4]), int(d[0:2]))
    except ValueError:
        return None


def chain_from_tickers(coin: str, rows: list[dict], now: float | None = None) -> dict:
    """Delta option tickers -> the option-chain shape straddle.compute() reads (pure function)."""
    now = now or time.time()
    by: dict = {}
    spot = None
    for r in rows or []:
        e = _expiry(r.get("symbol"))
        if not e:
            continue
        settle = dt.datetime(e.year, e.month, e.day, 12, 0, tzinfo=dt.timezone.utc).timestamp()
        if settle - now < MIN_LEFT_S:
            continue
        by.setdefault(e, []).append(r)
        spot = spot or _num(r.get("spot_price"))
    if not by:
        raise ValueError(f"Delta Exchange has no {coin} options with 2 hours or more left")
    e = min(by)
    settle = dt.datetime(e.year, e.month, e.day, 12, 0, tzinfo=dt.timezone.utc).timestamp()
    strikes: dict = {}
    for r in by[e]:
        k = _num(r.get("strike_price"))
        side = "CE" if r.get("contract_type") == "call_options" else "PE" if r.get("contract_type") == "put_options" else None
        if k is None or not side:
            continue
        q = r.get("quotes") or {}
        mark = _num(r.get("mark_price"))
        strikes.setdefault(k, {"strike": k})[side] = {
            "symbol": r.get("symbol"), "bid": _num(q.get("best_bid")) or 0, "ask": _num(q.get("best_ask")) or 0, "ltp": mark or _num(r.get("close")) or 0,
            "iv": (_num(r.get("mark_vol")) or 0) * 100 or None, "oi": _num(r.get("oi")) or 0}
    if not spot:
        raise ValueError(f"Delta Exchange returned no {coin} spot price")
    return {"underlying": coin, "spot": spot, "expiry": e.isoformat(), "days": max(0.0, (settle - now) / 86400), "as_of": int(now),
            "strikes": [strikes[k] for k in sorted(strikes)]}


async def snapshot(coin: str) -> dict | None:
    from backend.markets.crypto import DELTA
    d = await DELTA._get("/v2/tickers", contract_types="call_options,put_options", underlying_asset_symbols=coin)
    x = SD.compute(chain_from_tickers(coin, d.get("result") or []))
    if x:
        x["und"] = coin
        SD.save(x)
    return x


async def run_once() -> dict:
    out = {}
    for c in COINS:
        try:
            out[c] = bool(await snapshot(c))
            STATUS["errors"].pop(c, None)
        except Exception as exc:                          # noqa: BLE001 - Delta unreachable: report, keep going
            STATUS["errors"][c] = str(getattr(exc, "detail", exc))[:160]
    STATUS["last"] = int(time.time())
    return out


async def _loop(interval_s: int = 60) -> None:
    STATUS["running"] = True
    await asyncio.sleep(20)
    while True:
        try:
            await run_once()
        except Exception:                                 # noqa: BLE001
            pass
        await asyncio.sleep(interval_s)


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


def report() -> dict:
    return {"indices": [SD.shift(c, crypto=True) for c in COINS], "market": "crypto",
            "status": {"recording": STATUS["running"], "last": STATUS["last"], "errors": STATUS["errors"], "market_open": True}}
