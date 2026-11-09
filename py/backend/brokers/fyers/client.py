"""Phone build: FYERS data calls go through the browser's FYERS client (js/engine.js MM.FY.get), which holds the
token on this device. Same return shape as the PC's data_get.

Like the PC, every /history, /quotes and /options-chain-v3 request goes through the shared store (hist.py, the
PC's own file): one download serves every timeframe and every caller, updates only after a candle closes, and
stored candles are served when FYERS refuses (daily limit / network)."""
import json

from _bridge import js

from . import hist as _hist

_usage = {"used": 0, "cap": 5000, "t": 0.0}


async def _raw_get(path, params):
    return await js("fyGet", path, {k: ("" if v is None else v) for k, v in params.items()})


def _budget():
    return _usage["used"], _usage["cap"]


async def refresh_usage():
    """Today's FYERS request count on this device (kept by js/engine.js)."""
    try:
        u = await js("fyUsage")
        _usage.update(used=int(u.get("today") or 0), cap=int(u.get("per_day_cap") or 5000))
    except Exception:                                     # noqa: BLE001
        pass


STORE = _hist.Store(fetch=_raw_get, budget=_budget)


async def data_get(path, params):
    import time
    if time.time() - _usage["t"] > 60:
        _usage["t"] = time.time()
        await refresh_usage()
    if path == "/history":
        return await STORE.history(params)
    if path == "/quotes":
        return await STORE.quote(params)
    if path == "/options-chain-v3":
        return await STORE.chain(params)
    return await _raw_get(path, params)


_PER_DAY = 5000


def day_used():
    return _usage["used"]


def rate_status():
    return {"today": _usage["used"], "per_day_cap": _usage["cap"], "store": STORE.status()}
