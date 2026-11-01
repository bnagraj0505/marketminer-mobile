"""Connections API: status of FYERS, Binance and Shark Exchange; switch crypto feeds on/off; test them."""
from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.brokers.fyers.client import token_status
from .crypto import PROVIDERS
from .state import market_enabled, set_enabled

router = APIRouter(prefix="/api/markets", tags=["Connections"])
SAMPLE = {"BINANCE": ["BTCUSDT", "ETHUSDT", "SOLUSDT"], "BINANCEF": ["BTCUSDT", "XAUUSDT", "CLUSDT"], "SHARK": None,
          "DELTA": None}
FEEDS = ("BINANCE", "BINANCEF", "SHARK", "DELTA")


def _feed(m: str) -> dict:
    p = PROVIDERS[m]
    return {"name": p.name, "enabled": market_enabled(m), "url": p.base_url, "key_needed": False,
            "last_ok": p.last_ok, "last_error": p.last_error}


@router.get("/status")
async def status():
    return {"FYERS": {"name": "FYERS (NSE / BSE / MCX)", **token_status()}, **{m: _feed(m) for m in FEEDS}}


class EnableIn(BaseModel):
    enabled: bool


@router.post("/{market}/enable")
async def enable(market: Literal["BINANCE", "BINANCEF", "SHARK", "DELTA"], body: EnableIn):
    set_enabled(market, body.enabled)
    return _feed(market)


@router.get("/{market}/test")
async def test(market: Literal["BINANCE", "BINANCEF", "SHARK", "DELTA"]):
    if not market_enabled(market):
        raise HTTPException(409, f"{PROVIDERS[market].name} is switched off")
    p = PROVIDERS[market]
    ping = await p.ping()
    info = await p.load_info()
    if market == "DELTA":
        near = sorted((v for v in info.values() if v["expiry_ms"] > time.time() * 1000 + 3600e3),
                      key=lambda v: v["expiry_ms"])
        idx = {c: await p.index(c) for c in ("BTC", "ETH")}
        pick = []
        for c in ("BTC", "ETH"):
            mine = [v for v in near if v["base"] == c]
            if mine:
                e = mine[0]["expiry_ms"]
                pick.append(min((v for v in mine if v["expiry_ms"] == e and v["kind"] == "CE"),
                                key=lambda v: abs(v["strike"] - idx[c]))["pair"])
        pairs = pick
    else:
        pairs = [x for x in (SAMPLE[market] or []) if x in info] or [x for x in ("BTCUSDT", "ETHUSDT", "SOLUSDT") if x in info] or sorted(x for x in info if x.endswith("USDT"))[:3]
    prices = await p.prices(pairs)
    return {**_feed(market), "ping": ping, "instruments": len(info),
            "sample": [{"pair": k, "price": v, **{kk: info.get(k, {}).get(kk) for kk in ("quote", "tick", "name")}}
                       for k, v in prices.items()]}


@router.get("/{market}/instruments")
async def instruments(market: Literal["BINANCE", "BINANCEF", "SHARK", "DELTA"], q: str = "", limit: int = 30):
    if not market_enabled(market):
        raise HTTPException(409, f"{PROVIDERS[market].name} is switched off")
    info = await PROVIDERS[market].load_info()
    q = q.strip().upper()
    hits = [v for k, v in sorted(info.items()) if not q or q in k][:max(1, min(limit, 100))]
    return {"count": len(info), "items": hits}
