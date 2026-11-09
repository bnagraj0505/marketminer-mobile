"""Shared FYERS data store: every FYERS data request in MarketMiner goes through here, so one download serves every
page, the Signal Lab bot, the AI agent, the sweep and the Day Desk.

Why: the Standard (free) FYERS plan allows 5,000 data requests a day. Before this, each caller downloaded its own
copy (per timeframe, per candle count, every 10-120 s) and the day's budget was gone by early afternoon.

History (/history)
  * one BASE series per symbol: 5-minute candles serve 5/10/15/20/30/60/120/240-minute requests (resampled here,
    aligned to the session open 09:15 NSE/BSE, 09:00 MCX, the way FYERS builds them); 1-minute serves 1/2/3-minute;
    daily serves daily. Weekly/monthly are passed through.
  * the first request downloads the range asked for; later requests for older history fetch only the missing part;
    updates fetch only from the last stored day (one small request).
  * an update happens only right after a candle has CLOSED, at most once every MIN_REFRESH seconds per symbol
    (default 5 minutes; MARKETMINER_FYERS_MIN_REFRESH), never outside market hours once the session's final
    candles are in. The interval stretches as the day's budget is used (x2 at 40%, x4 at 70%, x10 at 85%).
    So a 5-minute chart gets every candle as it closes; the candle still forming is not polled (the live price
    stream will fill that in).
  * single flight: callers asking for the same symbol at the same moment share one request.
  * if FYERS refuses (daily limit, network), the stored candles are served (they are marked stale by the candle
    validator downstream) instead of an error, whenever there is something stored.
Quotes (/quotes): cached per symbol for QUOTE_AGE s; a batch is fetched only for the symbols not fresh.
Option chain: cached per request for CHAIN_AGE s.
Nothing here places orders.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import time
from typing import Any, Awaitable, Callable

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
MIN_REFRESH = float(os.getenv("MARKETMINER_FYERS_MIN_REFRESH", "300") or 300)   # seconds between updates of one symbol
CLOSE_GRACE = 4.0        # wait this long after a candle closes before fetching it
QUOTE_AGE = 5.0
CHAIN_AGE = 30.0
MAX_DAYS = {"1": 100, "5": 100, "D": 366}
BASE_OF = {"1": "1", "2": "1", "3": "1", "5": "5", "10": "5", "15": "5", "20": "5", "30": "5", "45": "5", "60": "5", "120": "5",
           "180": "5", "240": "5", "D": "D", "1D": "D"}

Fetch = Callable[[str, dict], Awaitable[dict]]


class Stats:
    def __init__(self):
        self.reset()

    def reset(self):
        self.day = dt.datetime.now(IST).date().isoformat()
        self.requests = 0          # asked of the store
        self.fetched = 0           # sent to FYERS
        self.stale_served = 0

    def roll(self):
        if self.day != dt.datetime.now(IST).date().isoformat():
            self.reset()


STATS = Stats()


def _anchor(symbol: str) -> int:
    """Session open in minutes after midnight IST."""
    return 540 if symbol.upper().startswith("MCX:") else 555


def _session(symbol: str) -> tuple[int, int]:
    s = symbol.upper()
    if s.startswith("MCX:"):
        return 540, 23 * 60 + 55
    return 555, 15 * 60 + 30


def market_open(symbol: str, now: float) -> bool:
    d = dt.datetime.fromtimestamp(now, IST)
    if d.weekday() >= 5:
        return False
    a, b = _session(symbol)
    m = d.hour * 60 + d.minute
    return a <= m < b + 2


def budget_factor(used: int, cap: int) -> float:
    f = used / cap if cap else 0
    return 1.0 if f < 0.4 else 2.0 if f < 0.7 else 4.0 if f < 0.85 else 10.0


def resample(rows: list[list], minutes: int, anchor: int) -> list[list]:
    """Intraday rows [t, o, h, l, c, v] -> `minutes` candles counted from the session open (IST)."""
    out: list[list] = []
    step = minutes * 60
    for r in rows:
        ist = int(r[0]) + 19800
        day = ist // 86400
        mins = (ist % 86400) // 60
        k = (mins - anchor) // minutes if mins >= anchor else -1 - (anchor - mins - 1) // minutes
        start = day * 86400 + anchor * 60 + k * step - 19800
        if out and out[-1][0] == start:
            o = out[-1]
            o[2] = max(o[2], r[2])
            o[3] = min(o[3], r[3])
            o[4] = r[4]
            o[5] = (o[5] or 0) + (r[5] or 0)
        else:
            out.append([start, r[1], r[2], r[3], r[4], r[5] or 0])
    return out


def _to_date(v: Any, fmt: str) -> dt.date:
    if str(fmt) == "0":
        return dt.datetime.fromtimestamp(int(float(v)), IST).date()
    return dt.date.fromisoformat(str(v)[:10])


class Series:
    def __init__(self, symbol: str, base: str, cont: str):
        self.symbol, self.base, self.cont = symbol, base, cont
        self.rows: list[list] = []
        self.cov_from: dt.date | None = None    # earliest day downloaded
        self.last_fetch = 0.0
        self.last_req = 0.0                    # when a caller last asked (the live feed follows these symbols)
        self.lock = asyncio.Lock()

    @property
    def step(self) -> int:
        return 86400 if self.base == "D" else int(self.base) * 60

    def merge(self, new: list[list]) -> None:
        if not new:
            return
        new = sorted(new, key=lambda r: r[0])
        if not self.rows or new[0][0] > self.rows[-1][0]:
            self.rows += new
            return
        if new[-1][0] < self.rows[0][0]:
            self.rows = new + self.rows
            return
        keep = {int(r[0]): r for r in self.rows}
        for r in new:
            keep[int(r[0])] = r
        self.rows = [keep[t] for t in sorted(keep)]

    def needs_update(self, now: float, factor: float) -> bool:
        if not self.rows:
            return True
        d = dt.datetime.fromtimestamp(now, IST)
        a, b = _session(self.symbol)
        close_ts = d.replace(hour=b // 60, minute=b % 60, second=0, microsecond=0).timestamp()
        if self.base == "D":
            if market_open(self.symbol, now):
                return now - self.last_fetch >= 900.0 * factor
            return d.weekday() < 5 and now >= close_ts + 60 and self.last_fetch < close_ts + 60
        if not market_open(self.symbol, now):
            # closed: one last update after the close (to get the final candles), then nothing until the next open
            if d.weekday() < 5 and now >= close_ts and self.last_fetch < close_ts + 60:
                return now - self.last_fetch >= 30
            return False
        # right after a candle closes, on a grid of max(candle, MIN_REFRESH) x budget factor: a 5-minute series is
        # updated a few seconds after every 5-minute close (every other close at 40% of the budget, ...)
        grid = max(self.step, MIN_REFRESH) * factor
        since = now % grid
        return since >= CLOSE_GRACE and self.last_fetch < now - since + CLOSE_GRACE

    def slice(self, a: dt.date, b: dt.date) -> list[list]:
        lo = dt.datetime.combine(a, dt.time(0), IST).timestamp()
        hi = dt.datetime.combine(b, dt.time(23, 59, 59), IST).timestamp()
        return [r for r in self.rows if lo <= r[0] <= hi]


class Store:
    def __init__(self, fetch: Fetch, budget: Callable[[], tuple[int, int]] = lambda: (0, 5000), clock: Callable[[], float] = time.time):
        self.fetch, self.budget, self.clock = fetch, budget, clock
        self.series: dict[tuple, Series] = {}
        self.quotes: dict[str, tuple[float, dict]] = {}
        self.chains: dict[tuple, tuple[float, dict]] = {}
        self.inflight: dict[tuple, asyncio.Future] = {}

    def _factor(self) -> float:
        used, cap = self.budget()
        return budget_factor(used, cap)

    async def _get_raw(self, params: dict) -> list[list]:
        STATS.fetched += 1
        p = await self.fetch("/history", params)
        return [list(r) for r in (p.get("candles") or [])]

    async def _download(self, s: Series, a: dt.date, b: dt.date) -> list[list]:
        span = MAX_DAYS.get(s.base, 100)
        out, cur = [], a
        while cur <= b:
            end = min(b, cur + dt.timedelta(days=span - 1))
            out += await self._get_raw({"symbol": s.symbol, "resolution": s.base, "date_format": "1", "range_from": cur.isoformat(),
                                        "range_to": end.isoformat(), "cont_flag": s.cont})
            cur = end + dt.timedelta(days=1)
        return out

    async def history(self, params: dict) -> dict:
        STATS.roll()
        STATS.requests += 1
        res = str(params.get("resolution", "5")).upper()
        base = BASE_OF.get(res)
        if base is None:                                    # W / M: not worth storing
            STATS.fetched += 1
            return await self.fetch("/history", params)
        sym = str(params["symbol"])
        cont = str(params.get("cont_flag", "1"))
        fmt = str(params.get("date_format", "1"))
        a, b = _to_date(params["range_from"], fmt), _to_date(params["range_to"], fmt)
        key = (sym, base, cont)
        s = self.series.get(key)
        if s is None:
            s = self.series[key] = Series(sym, base, cont)
        now = self.clock()
        s.last_req = now
        today = dt.datetime.fromtimestamp(now, IST).date()
        async with s.lock:
            try:
                if s.cov_from is None or a < s.cov_from:       # history not downloaded yet (or older history wanted)
                    stop = (s.cov_from - dt.timedelta(days=1)) if s.cov_from else max(b, today)
                    rows = await self._download(s, a, stop)
                    s.merge(rows)
                    s.cov_from = a
                    if stop >= today:
                        s.last_fetch = now
                if b >= today and s.needs_update(now, self._factor()):
                    since = dt.datetime.fromtimestamp(s.rows[-1][0], IST).date() if s.rows else a
                    s.merge(await self._download(s, since, today))
                    s.last_fetch = now
            except Exception:
                if not s.rows:
                    raise
                STATS.stale_served += 1                     # FYERS refused: serve what is stored
        rows = s.slice(a, b)
        if res != base and base != "D":
            rows = resample(rows, int(res), _anchor(sym))
        return {"s": "ok", "candles": [list(r) for r in rows]}

    async def quote(self, params: dict) -> dict:
        STATS.roll()
        STATS.requests += 1
        syms = [x.strip() for x in str(params.get("symbols", "")).split(",") if x.strip()]
        now = self.clock()
        age = QUOTE_AGE * self._factor()
        miss = [x for x in syms if x not in self.quotes or now - self.quotes[x][0] >= age]
        if miss:
            STATS.fetched += 1
            try:
                p = await self.fetch("/quotes", {**params, "symbols": ",".join(miss)})
            except Exception:
                if any(x not in self.quotes for x in syms):
                    raise
                STATS.stale_served += 1
                p = {"d": []}
            for d in p.get("d") or []:
                n = d.get("n") or (d.get("v") or {}).get("symbol")
                if n:
                    self.quotes[n] = (now, d)
            for x in miss:                                  # FYERS may echo a different spelling: keep the order we were asked
                if x not in self.quotes:
                    hit = next((d for d in p.get("d") or [] if (d.get("n") or "").upper() == x.upper()), None)
                    if hit:
                        self.quotes[x] = (now, hit)
        return {"s": "ok", "code": 200, "d": [self.quotes[x][1] for x in syms if x in self.quotes]}

    async def chain(self, params: dict) -> dict:
        STATS.roll()
        STATS.requests += 1
        key = tuple(sorted((k, str(v)) for k, v in params.items()))
        now = self.clock()
        hit = self.chains.get(key)
        if hit and now - hit[0] < CHAIN_AGE * self._factor():
            return hit[1]
        STATS.fetched += 1
        try:
            p = await self.fetch("/options-chain-v3", params)
        except Exception:
            if hit:
                STATS.stale_served += 1
                return hit[1]
            raise
        self.chains[key] = (now, p)
        if len(self.chains) > 200:
            for k in sorted(self.chains, key=lambda k: self.chains[k][0])[:50]:
                self.chains.pop(k, None)
        return p

    def wanted(self, now: float, within: float = 1800.0) -> list[str]:
        """Intraday symbols asked for recently, most recent first (the live feed subscribes to these)."""
        last: dict[str, float] = {}
        for (sym, base, _), s in self.series.items():
            if base in ("1", "5") and now - s.last_req <= within:
                last[sym] = max(last.get(sym, 0.0), s.last_req)
        return sorted(last, key=lambda k: -last[k])

    def apply_tick(self, symbol: str, price: float, t: float, dvol: float = 0.0) -> int:
        """A live price: extend / update the newest 1- and 5-minute candles of `symbol` (REST data replaces these
        candles at the next download, so the download stays the reference). Returns how many series changed."""
        n = 0
        for (sym, base, _), s in self.series.items():
            if sym != symbol or base not in ("1", "5") or not s.rows:
                continue
            step = s.step
            start = int(t - (t % step))
            last = s.rows[-1]
            if start == int(last[0]):
                last[2] = max(last[2], price)
                last[3] = min(last[3], price)
                last[4] = price
                last[5] = (last[5] or 0) + dvol
            elif start > int(last[0]):
                if not market_open(sym, t):
                    continue
                s.rows.append([start, price, price, price, price, dvol])
            else:
                continue
            n += 1
        return n

    def status(self) -> dict:
        STATS.roll()
        return {"symbols": len({k[0] for k in self.series}), "series": len(self.series),
                "candles": sum(len(s.rows) for s in self.series.values()), "requests_today": STATS.requests,
                "sent_to_fyers_today": STATS.fetched, "served_from_store_today": max(0, STATS.requests - STATS.fetched),
                "stale_served_today": STATS.stale_served, "update_every_s": round(MIN_REFRESH * self._factor())}
