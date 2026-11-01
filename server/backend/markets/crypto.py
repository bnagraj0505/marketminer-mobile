"""Binance spot and Shark Exchange public market data for MarketMiner. Read-only, no API keys, no order code.

Same sources and parsing as SmartEntry AI's adapters, rebuilt standalone for MarketMiner:
  * Binance: https://data-api.binance.vision (public market-data mirror). Klines
    GET /api/v3/klines (rows [open_ms, o, h, l, c, v, ...], max 1000), prices GET /api/v3/ticker/price,
    tick/step from GET /api/v3/exchangeInfo.
  * Shark Exchange (INR / USDT perpetuals): https://api.sharkexchange.in. Contracts
    GET /v1/exchange/exchangeInfo; candles POST /v1/market/klines?priceType=LAST_PRICE with
    {"pair", "interval", "limit" (<= 1500), "endTime"}; rows {"startTime": ms, "open", ...}.
    Shark allows 60 REST requests a minute; MarketMiner stays under 50.

Candles are returned as [epoch_seconds_utc, open, high, low, close, volume], like FYERS.
"""
from __future__ import annotations

import asyncio
import collections
import json
import math
import os
import time
from typing import Any

import httpx
from fastapi import HTTPException

BINANCE_URL = os.getenv("BINANCE_REST_URL", "https://data-api.binance.vision").rstrip("/")
BINANCE_FUT_URL = os.getenv("BINANCE_FUTURES_URL", "https://fapi.binance.com").rstrip("/")
DELTA_URL = os.getenv("DELTA_REST_URL", "https://api.india.delta.exchange").rstrip("/")
SHARK_URL = os.getenv("SHARK_REST_URL", "https://api.sharkexchange.in").rstrip("/")
INTERVAL_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


class Limiter:
    def __init__(self, per_sec: int, per_min: int):
        self.per_sec, self.per_min = per_sec, per_min
        self.calls: collections.deque[float] = collections.deque()
        self.lock = asyncio.Lock()

    async def wait(self, max_wait: float = 120.0) -> None:
        start = time.monotonic()
        async with self.lock:
            while True:
                now = time.monotonic()
                while self.calls and now - self.calls[0] >= 60:
                    self.calls.popleft()
                last_sec = sum(1 for t in self.calls if now - t < 1)
                if len(self.calls) < self.per_min and last_sec < self.per_sec:
                    self.calls.append(now)
                    return
                wait = 1 - (now - self.calls[-last_sec]) if last_sec >= self.per_sec else 60 - (now - self.calls[0])
                if now - start + wait > max_wait:
                    raise HTTPException(429, "Pacing exchange requests; try again in a minute.")
                await asyncio.sleep(max(0.05, wait))


def _step_from_precision(p: Any, default: float) -> float:
    try:
        return 10 ** -int(p)
    except (TypeError, ValueError):
        return default


class Provider:
    name = "?"
    market = "?"
    base_url = ""
    limiter: Limiter

    def __init__(self) -> None:
        self._hist: dict[str, dict[str, Any]] = {}      # symbol -> {"rows": [...], "at": monotonic}
        self._daily: dict[str, tuple[float, list]] = {}
        self._info: dict[str, Any] = {}
        self._info_at = 0.0
        self.last_ok: float | None = None
        self.last_error: str | None = None

    async def _request(self, method: str, path: str, **kw) -> Any:
        await self.limiter.wait()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=8)) as c:
                r = await c.request(method, f"{self.base_url}{path}", **kw)
        except httpx.HTTPError as exc:
            self.last_error = f"Could not reach {self.name} ({type(exc).__name__})"
            raise HTTPException(502, self.last_error)
        if r.status_code == 429 or r.status_code == 418:
            self.last_error = f"{self.name} rate limit hit; wait a minute"
            raise HTTPException(429, self.last_error)
        if not r.is_success:
            msg = ""
            try:
                body = r.json()
                msg = body.get("msg") or body.get("message") or ""
            except ValueError:
                pass
            self.last_error = f"{self.name} returned HTTP {r.status_code} {msg}".strip()
            raise HTTPException(502 if r.status_code >= 500 else 400, self.last_error[:250])
        try:
            data = r.json()
        except ValueError:
            raise HTTPException(502, f"{self.name} returned a non-JSON response")
        self.last_ok, self.last_error = time.time(), None
        return data

    # subclasses: klines_page(pair, interval, limit, end_ms=None, start_ms=None) -> rows ; load_info() ; prices()

    async def history(self, pair: str, days: int, refresh_after: float = 20.0) -> list:
        """~`days` of 5-minute candles, kept in memory and topped up incrementally."""
        want_from = int(time.time()) - days * 86400
        h = self._hist.get(pair)
        if h and h["rows"] and h["rows"][0][0] <= want_from + 3600:
            if time.monotonic() - h["at"] < refresh_after:
                return h["rows"]
            new = await self._since(pair, h["rows"][-1][0])
            merged = {r[0]: r for r in h["rows"]}
            merged.update({r[0]: r for r in new})
            rows = [merged[k] for k in sorted(merged) if k >= want_from - 86400]
        else:
            rows = await self._full(pair, want_from)
        self._hist[pair] = {"rows": rows, "at": time.monotonic()}
        return rows

    async def daily(self, pair: str, n: int = 150, ttl: float = 1800) -> list:
        hit = self._daily.get(pair)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        rows = await self.klines_page(pair, "1d", n)
        self._daily[pair] = (time.monotonic(), rows)
        return rows

    async def instrument(self, pair: str) -> dict:
        info = await self.load_info()
        meta = info.get(pair.upper())
        if not meta:
            raise HTTPException(400, f"{pair} is not a listed {self.name} instrument")
        return meta


class Binance(Provider):
    name, market, base_url = "Binance", "BINANCE", BINANCE_URL
    limiter = Limiter(8, 600)              # weight budget is 6,000/min; klines cost 2

    @staticmethod
    def parse_klines(payload: Any) -> list:
        if not isinstance(payload, list):
            raise HTTPException(502, "Binance returned an unexpected klines payload")
        out = []
        for row in payload:
            try:
                out.append([int(row[0]) // 1000, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])])
            except (IndexError, TypeError, ValueError):
                raise HTTPException(502, "Binance returned a malformed kline row")
        return out

    async def klines_page(self, pair, interval, limit, end_ms=None, start_ms=None):
        params = {"symbol": pair.upper(), "interval": interval, "limit": min(1000, limit)}
        if end_ms is not None:
            params["endTime"] = end_ms
        if start_ms is not None:
            params["startTime"] = start_ms
        return self.parse_klines(await self._request("GET", "/api/v3/klines", params=params))

    async def _full(self, pair, from_ts):
        rows, start = [], from_ts * 1000
        while True:
            page = await self.klines_page(pair, "5m", 1000, start_ms=start)
            rows += page
            if len(page) < 1000:
                return rows
            start = page[-1][0] * 1000 + 1

    async def _since(self, pair, last_ts):
        rows, start = [], last_ts * 1000
        while True:
            page = await self.klines_page(pair, "5m", 1000, start_ms=start)
            rows += page
            if len(page) < 1000:
                return rows
            start = page[-1][0] * 1000 + 1

    async def load_info(self, force=False):
        if self._info and not force and time.monotonic() - self._info_at < 3600:
            return self._info
        data = await self._request("GET", "/api/v3/exchangeInfo", params={"permissions": "SPOT"})
        info = {}
        for s in data.get("symbols", []):
            if s.get("status") != "TRADING":
                continue
            f = {x.get("filterType"): x for x in s.get("filters", [])}
            tick = float((f.get("PRICE_FILTER") or {}).get("tickSize") or 0.01)
            lot = f.get("LOT_SIZE") or {}
            info[s["symbol"]] = {"pair": s["symbol"], "base": s.get("baseAsset"), "quote": s.get("quoteAsset"),
                                 "tick": tick, "qty_step": float(lot.get("stepSize") or 0.00001),
                                 "min_qty": float(lot.get("minQty") or 0),
                                 "name": f"{s.get('baseAsset')}/{s.get('quoteAsset')} spot"}
        self._info, self._info_at = info, time.monotonic()
        return info

    async def prices(self, pairs: list[str]) -> dict[str, float]:
        if not pairs:
            return {}
        data = await self._request("GET", "/api/v3/ticker/price",
                                   params={"symbols": json.dumps([p.upper() for p in pairs], separators=(",", ":"))})
        return {d["symbol"]: float(d["price"]) for d in data if d.get("price")}

    async def ping(self) -> dict:
        t = time.monotonic()
        await self._request("GET", "/api/v3/ping")
        return {"ok": True, "ms": round(1000 * (time.monotonic() - t))}


class Shark(Provider):
    name, market, base_url = "Shark Exchange", "SHARK", SHARK_URL
    limiter = Limiter(3, 50)               # Shark allows 60 REST requests a minute

    @staticmethod
    def parse_klines(payload: Any) -> list:
        rows = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise HTTPException(502, "Shark Exchange returned an unexpected klines payload")
        out = []
        for r in rows:
            try:
                out.append([int(r["startTime"]) // 1000, float(r["open"]), float(r["high"]), float(r["low"]),
                            float(r["close"]), float(r.get("volume") or 0)])
            except (KeyError, TypeError, ValueError):
                raise HTTPException(502, "Shark Exchange returned a malformed candle")
        return sorted(out, key=lambda x: x[0])

    async def klines_page(self, pair, interval, limit, end_ms=None, start_ms=None):
        body = {"pair": pair.upper(), "interval": interval, "limit": min(1500, limit)}
        if end_ms is not None:
            body["endTime"] = end_ms
        data = await self._request("POST", "/v1/market/klines", params={"priceType": "LAST_PRICE"}, json=body)
        return self.parse_klines(data)

    async def _full(self, pair, from_ts):
        rows: list = []
        end = None
        while True:
            page = await self.klines_page(pair, "5m", 1500, end_ms=end)
            if end is not None:
                page = [r for r in page if r[0] * 1000 <= end]
            rows = page + rows
            if len(page) < 1500 or not page or page[0][0] <= from_ts:
                return [r for r in rows if r[0] >= from_ts - 86400]
            end = page[0][0] * 1000 - 1

    async def _since(self, pair, last_ts):
        need = int((time.time() - last_ts) // 300) + 3
        if need > 1500:
            return await self._full(pair, last_ts)
        return await self.klines_page(pair, "5m", max(2, need))

    async def load_info(self, force=False):
        if self._info and not force and time.monotonic() - self._info_at < 3600:
            return self._info
        body = await self._request("GET", "/v1/exchange/exchangeInfo")
        data = body.get("data", body) if isinstance(body, dict) else {}
        contracts = data.get("contracts", []) if isinstance(data, dict) else []
        info = {}
        for c in contracts:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            min_qty = None
            for f in c.get("filters") or []:
                if f.get("filterType") == "LIMIT_QTY_SIZE":
                    try:
                        min_qty = float(f["minQty"])
                    except (KeyError, TypeError, ValueError):
                        pass
            step = _step_from_precision(c.get("quantityPrecision"), 0.001)
            name = str(c["name"]).upper()
            info[name] = {"pair": name, "base": c.get("baseAsset"), "quote": str(c.get("quoteAsset", "")).upper(),
                          "tick": _step_from_precision(c.get("pricePrecision"), 0.01), "qty_step": step,
                          "min_qty": min_qty or step, "max_leverage": c.get("maxLeverage"),
                          "name": f"{c.get('baseAsset', name)}/{str(c.get('quoteAsset', '')).upper()} perpetual"}
        self._info, self._info_at = info, time.monotonic()
        return info

    async def prices(self, pairs: list[str]) -> dict[str, float]:
        out = {}
        for p in pairs:                      # no REST ticker documented: last 1-minute close
            rows = await self.klines_page(p, "1m", 2)
            if rows:
                out[p.upper()] = rows[-1][4]
        return out

    async def ping(self) -> dict:
        t = time.monotonic()
        info = await self.load_info(force=True)
        return {"ok": True, "ms": round(1000 * (time.monotonic() - t)), "contracts": len(info)}


class BinanceFutures(Binance):
    """Binance USD-M perpetual futures (public market data at fapi.binance.com). Includes BTC, ETH, SOL and the
    TradFi perpetuals Binance lists in USDT such as XAUUSDT (gold), XAGUSDT (silver) and CLUSDT (WTI crude)."""
    name, market, base_url = "Binance Futures", "BINANCEF", BINANCE_FUT_URL
    limiter = Limiter(8, 600)              # 2,400 weight/min; klines cost 1-5

    async def klines_page(self, pair, interval, limit, end_ms=None, start_ms=None):
        params = {"symbol": pair.upper(), "interval": interval, "limit": min(1500, limit)}
        if end_ms is not None:
            params["endTime"] = end_ms
        if start_ms is not None:
            params["startTime"] = start_ms
        return self.parse_klines(await self._request("GET", "/fapi/v1/klines", params=params))

    async def _full(self, pair, from_ts):
        rows, start = [], from_ts * 1000
        while True:
            page = await self.klines_page(pair, "5m", 1500, start_ms=start)
            rows += page
            if len(page) < 1500:
                return rows
            start = page[-1][0] * 1000 + 1

    _since = _full

    async def load_info(self, force=False):
        if self._info and not force and time.monotonic() - self._info_at < 3600:
            return self._info
        data = await self._request("GET", "/fapi/v1/exchangeInfo")
        info = {}
        for s in data.get("symbols", []):
            if s.get("status") != "TRADING" or "PERPETUAL" not in str(s.get("contractType", "")):
                continue
            f = {x.get("filterType"): x for x in s.get("filters", [])}
            lot = f.get("LOT_SIZE") or {}
            info[s["symbol"]] = {"pair": s["symbol"], "base": s.get("baseAsset"), "quote": s.get("quoteAsset"),
                                 "tick": float((f.get("PRICE_FILTER") or {}).get("tickSize") or 0.01),
                                 "qty_step": float(lot.get("stepSize") or 0.001), "min_qty": float(lot.get("minQty") or 0),
                                 "name": f"{s.get('baseAsset')}/{s.get('quoteAsset')} perpetual",
                                 "underlying_type": s.get("underlyingType")}
        self._info, self._info_at = info, time.monotonic()
        return info

    async def prices(self, pairs):
        if not pairs:
            return {}
        data = await self._request("GET", "/fapi/v1/ticker/price")
        want = {p.upper() for p in pairs}
        return {d["symbol"]: float(d["price"]) for d in data if d.get("symbol") in want}

    async def ping(self):
        t = time.monotonic()
        await self._request("GET", "/fapi/v1/ping")
        return {"ok": True, "ms": round(1000 * (time.monotonic() - t))}


class DeltaIndia(Provider):
    """Delta Exchange India public market data (no key): BTC, ETH and XAUT (gold) options, premiums in USD.
    Products: GET /v2/products?contract_types=call_options,put_options&states=live (paged with `after`).
    Candles:  GET /v2/history/candles?resolution=5m&symbol=MARK:<symbol>&start=&end= (seconds). The MARK: series is
              the option's mark (fair) price, continuous even when the option does not trade; trade candles of
              thin strikes have gaps. Prices: GET /v2/tickers/<symbol> (mark_price, spot_price).
    One contract = contract_value of the coin (0.001 BTC), so quantities are kept in coin units, stepped by it."""
    name, market, base_url = "Delta Exchange India", "DELTA", DELTA_URL
    limiter = Limiter(5, 150)
    WINDOW = 6 * 86400                      # 1,728 five-minute candles per request

    async def _get(self, path, **params):
        data = await self._request("GET", path, params=params)
        if isinstance(data, dict) and data.get("success") is not False and "result" in data:
            return data
        raise HTTPException(502, f"Delta Exchange {path} returned an error: {str(data.get('error') if isinstance(data, dict) else data)[:150]}")

    @staticmethod
    def _rows(result):
        out = []
        for r in result or []:
            try:
                out.append([int(r["time"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                            float(r.get("volume") or 0)])
            except (KeyError, TypeError, ValueError):
                continue
        out.sort(key=lambda x: x[0])
        return out

    async def _candles(self, pair, resolution, start, end):
        rows = []
        while start < end:
            stop = min(end, start + (self.WINDOW if resolution == "5m" else 2000 * 86400))
            d = await self._get("/v2/history/candles", resolution=resolution, symbol=f"MARK:{pair}", start=start, end=stop)
            rows += self._rows(d.get("result"))
            start = stop
        dedup = {r[0]: r for r in rows}
        return [dedup[k] for k in sorted(dedup)]

    async def klines_page(self, pair, interval, limit, end_ms=None, start_ms=None):
        end = int((end_ms or time.time() * 1000) // 1000)
        sec = INTERVAL_SEC.get(interval, 300)
        start = int(start_ms // 1000) if start_ms else end - limit * sec
        return (await self._candles(pair, "1d" if interval == "1d" else "5m", start, end))[-limit:]

    async def _full(self, pair, from_ts):
        return await self._candles(pair, "5m", from_ts, int(time.time()))

    async def _since(self, pair, last_ts):
        return await self._candles(pair, "5m", last_ts, int(time.time()))

    async def load_info(self, force=False):
        if self._info and not force and time.monotonic() - self._info_at < 900:
            return self._info
        info, after = {}, None
        for _ in range(20):
            params = {"contract_types": "call_options,put_options", "states": "live", "page_size": 500}
            if after:
                params["after"] = after
            d = await self._get("/v2/products", **params)
            for p in d.get("result") or []:
                try:
                    sym = p["symbol"]
                    cv = float(p.get("contract_value") or 0.001)
                    exp = p.get("settlement_time") or ""
                    import datetime as _dt
                    exp_ms = int(_dt.datetime.fromisoformat(exp.replace("Z", "+00:00")).timestamp() * 1000) if exp else 0
                    info[sym] = {"pair": sym, "base": (p.get("underlying_asset") or {}).get("symbol", ""),
                                 "quote": (p.get("quoting_asset") or {}).get("symbol", "USD") or "USD",
                                 "tick": float(p.get("tick_size") or 0.01), "qty_step": cv, "min_qty": cv,
                                 "contract_value": cv, "strike": float(p.get("strike_price") or 0),
                                 "kind": "CE" if p.get("contract_type") == "call_options" else "PE",
                                 "expiry_ms": exp_ms, "name": p.get("description") or sym}
                except (KeyError, TypeError, ValueError):
                    continue
            after = (d.get("meta") or {}).get("after")
            if not after:
                break
        if not info:
            raise HTTPException(502, "Delta Exchange returned no live options")
        self._info, self._info_at = info, time.monotonic()
        return info

    async def prices(self, pairs):
        out = {}
        for p in pairs:
            d = await self._get(f"/v2/tickers/{p}")
            r = d.get("result") or {}
            if r.get("mark_price") is not None:
                out[p] = float(r["mark_price"])
        return out

    async def index(self, coin: str) -> float:
        for sym in (f"{coin}USD", f"{coin}USDT"):
            try:
                r = (await self._get(f"/v2/tickers/{sym}")).get("result") or {}
                px = r.get("spot_price") or r.get("mark_price")
                if px:
                    return float(px)
            except HTTPException:
                continue
        info = await self.load_info()            # fall back to the spot an option ticker reports
        any_opt = next((k for k, v in info.items() if v["base"] == coin), None)
        if any_opt:
            r = (await self._get(f"/v2/tickers/{any_opt}")).get("result") or {}
            if r.get("spot_price"):
                return float(r["spot_price"])
        raise HTTPException(502, f"No {coin} spot price from Delta Exchange")

    async def ping(self):
        t = time.monotonic()
        await self._get("/v2/products", page_size=1)
        return {"ok": True, "ms": round(1000 * (time.monotonic() - t))}


BINANCE = Binance()
SHARK = Shark()
BINANCEF = BinanceFutures()
DELTA = DeltaIndia()
PROVIDERS = {"BINANCE": BINANCE, "BINANCEF": BINANCEF, "SHARK": SHARK, "DELTA": DELTA}
