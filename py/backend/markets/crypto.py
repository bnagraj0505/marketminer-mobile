"""Phone build: crypto market data through the browser. Shark Exchange does not answer phone browsers, so its
klines raise and the Signal Lab falls back to Delta Exchange (the same fallback the PC uses when Shark fails)."""
from fastapi import HTTPException
from _bridge import js


class _Shark:
    async def klines_page(self, pair, interval, limit, end_ms=None, start_ms=None):
        raise HTTPException(503, "Shark Exchange can't be reached from a phone browser")


class _Binance:
    async def klines_page(self, pair, interval, limit, end_ms=None, start_ms=None):
        return await js("binanceKlines", pair.upper(), interval, min(1000, int(limit)), end_ms)


class _Delta:
    async def _get(self, path, **params):
        d = await js("deltaGet", path, params)
        if isinstance(d, dict) and d.get("success") is not False and "result" in d:
            return d
        raise HTTPException(502, f"Delta Exchange {path} returned an error")

    @staticmethod
    def _rows(result):
        out = []
        for r in result or []:
            try:
                out.append([int(r["time"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r.get("volume") or 0)])
            except (KeyError, TypeError, ValueError):
                continue
        out.sort(key=lambda x: x[0])
        return out


SHARK, BINANCE, DELTA = _Shark(), _Binance(), _Delta()
