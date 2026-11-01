"""NSE website gateway for the dashboard's /api/proxy calls.

Why: nseindia.com sits behind bot protection that stalls ordinary Python HTTP clients (the requests hang until
they time out), which made every NSE panel wait ~40 s before failing. This module:

  1. Talks to NSE with curl_cffi impersonating Chrome when that package is installed (the usual working approach
     for NSE), keeping NSE's session cookies and refreshing them every few minutes; plain httpx otherwise.
  2. Fails fast: after NSE stops answering it is paused for 30 s (doubling up to 5 min) and requests are answered
     at once with 503 instead of each waiting for a timeout.
  3. Caches responses briefly and merges identical requests that arrive together.
  4. Sector performance fallback: when NSE is unavailable, /api/allIndices is rebuilt from FYERS index quotes
     (same JSON shape the dashboard reads), if FYERS is connected.
"""
from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

try:                                            # optional: pip install curl_cffi
    from curl_cffi.requests import AsyncSession as CurlSession
except Exception:                               # noqa: BLE001 - any import problem means "not available"
    CurlSession = None

ROOT = Path(__file__).resolve().parents[1]
NSE_HOME = "https://www.nseindia.com/"
TIMEOUT = 8.0
CACHE_SECONDS = {"nse": 20.0, "other": 60.0}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json,text/plain,*/*", "Accept-Language": "en-IN,en;q=0.9",
           "Referer": NSE_HOME, "Origin": "https://www.nseindia.com", "Sec-Fetch-Site": "same-origin",
           "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}


class NSEUnavailable(Exception):
    pass


_state = {"down_until": 0.0, "fails": 0, "warmed_at": 0.0, "last_error": None, "last_ok": None}
_curl = None
_httpx: httpx.AsyncClient | None = None
_cache: dict[str, tuple[float, int, str, bytes, dict]] = {}
_inflight: dict[str, asyncio.Future] = {}
_warm_lock = asyncio.Lock()


def engine_name() -> str:
    return "curl_cffi (Chrome)" if CurlSession else "httpx"


def status() -> dict[str, Any]:
    left = max(0.0, _state["down_until"] - time.time())
    return {"engine": engine_name(), "paused_for_s": round(left), "fails": _state["fails"],
            "last_error": _state["last_error"], "last_ok": _state["last_ok"],
            "hint": None if CurlSession else "Install curl_cffi (the launcher does) for reliable NSE access."}


async def _client_get(url: str, accept_html: bool = False):
    global _curl, _httpx
    headers = dict(HEADERS)
    if accept_html:
        headers["Accept"] = "text/html,application/xhtml+xml,*/*"
        for k in ("Origin", "Sec-Fetch-Site", "Sec-Fetch-Mode", "Sec-Fetch-Dest"):
            headers.pop(k, None)
    if CurlSession:
        if _curl is None:
            _curl = CurlSession(impersonate="chrome", timeout=TIMEOUT)
        r = await _curl.get(url, headers=headers, allow_redirects=False)
        return r.status_code, r.headers.get("content-type", ""), r.content
    if _httpx is None:
        _httpx = httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT, connect=5.0), follow_redirects=False,
                                   headers={"User-Agent": UA})
    r = await _httpx.get(url, headers=headers)
    return r.status_code, r.headers.get("content-type", ""), r.content


_browser = None


async def browser_get(url: str):
    """Plain GET that looks like Chrome (TLS fingerprint included) for sites that refuse scripted clients,
    e.g. Moneycontrol's news API. Separate session from NSE so the two never share cookies."""
    global _browser
    if not CurlSession:
        return None
    if _browser is None:
        _browser = CurlSession(impersonate="chrome", timeout=TIMEOUT)
    r = await _browser.get(url, headers={"Accept": "application/json,text/plain,*/*", "Accept-Language": "en-IN,en;q=0.9",
                                         "Referer": "https://www.moneycontrol.com/"}, allow_redirects=False)
    return r.status_code, r.headers.get("content-type", "application/json"), r.content


def _trip(err: str) -> None:
    _state["fails"] += 1
    _state["down_until"] = time.time() + min(300.0, 30.0 * 2 ** (_state["fails"] - 1))
    _state["last_error"] = err[:200]
    _state["warmed_at"] = 0.0


async def _warm() -> None:
    async with _warm_lock:
        if time.time() - _state["warmed_at"] < 240:
            return
        code, _, _ = await _client_get(NSE_HOME, accept_html=True)
        if code >= 400:
            raise NSEUnavailable(f"NSE home page answered HTTP {code}")
        _state["warmed_at"] = time.time()


async def _nse_fetch(url: str):
    if url.rstrip("/") in ("https://www.nseindia.com", "https://nseindia.com"):
        # the dashboard's warm-up call: refresh our NSE session cookies instead of returning NSE's home page
        if _state["down_until"] > time.time():
            return 200, "text/plain", b"paused"
        try:
            await asyncio.wait_for(_warm(), TIMEOUT + 2)
        except Exception as exc:                  # noqa: BLE001
            _trip(f"warm-up: {type(exc).__name__}")
        return 200, "text/plain", b"ok"
    left = _state["down_until"] - time.time()
    if left > 0:
        raise NSEUnavailable(f"NSE is not answering this PC (bot protection or network); retrying automatically "
                             f"in {int(left) + 1}s")
    try:
        await asyncio.wait_for(_warm(), TIMEOUT + 2)
        code, ctype, body = await asyncio.wait_for(_client_get(url), TIMEOUT + 2)
        if code in (401, 403):                    # cookies stale: warm up once more and retry
            _state["warmed_at"] = 0.0
            await asyncio.wait_for(_warm(), TIMEOUT + 2)
            code, ctype, body = await asyncio.wait_for(_client_get(url), TIMEOUT + 2)
    except NSEUnavailable as exc:
        _trip(str(exc))
        raise
    except Exception as exc:                      # noqa: BLE001 - timeouts / TLS / connection errors
        _trip(f"{type(exc).__name__}: {exc}")
        raise NSEUnavailable(f"NSE did not answer ({type(exc).__name__}); paused for "
                             f"{int(_state['down_until'] - time.time())}s")
    if code >= 400 or body[:1] == b"<":
        _trip(f"HTTP {code}")
        raise NSEUnavailable(f"NSE answered HTTP {code}" + (" with a web page instead of data" if code < 400 else ""))
    _state.update(fails=0, down_until=0.0, last_ok=dt.datetime.now().isoformat(timespec="seconds"))
    return code, ctype or "application/json", body


async def cached(url: str, is_nse: bool, fetch_other):
    """Returns (status, content_type, body, extra_headers). Raises NSEUnavailable for NSE failures."""
    hit = _cache.get(url)
    ttl = CACHE_SECONDS["nse" if is_nse else "other"]
    if hit and time.time() - hit[0] < ttl:
        return hit[1], hit[2], hit[3], {**hit[4], "X-MarketMiner-Cache": "hit"}
    if url in _inflight:
        return await asyncio.shield(_inflight[url])
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _inflight[url] = fut
    try:
        if is_nse:
            try:
                code, ctype, body = await _nse_fetch(url)
                extra = {}
            except NSEUnavailable:
                fb = await _fallback(url)
                if fb is None:
                    raise
                code, ctype, body, extra = fb
        else:
            code, ctype, body = await fetch_other(url)
            extra = {}
        res = (code, ctype, body, extra)
        if code == 200:
            _cache[url] = (time.time(), code, ctype, body, extra)
            if len(_cache) > 300:
                for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
                    _cache.pop(k, None)
        fut.set_result(res)
        return res
    except BaseException as exc:
        fut.set_exception(exc)
        fut.exception()                           # mark retrieved
        raise
    finally:
        _inflight.pop(url, None)


# ---------------------------------------------------------------- FYERS fallback for sector performance

INDEX_NAMES = ["NIFTY 50", "NIFTY BANK", "INDIA VIX", "NIFTY AUTO", "NIFTY FMCG", "NIFTY IT", "NIFTY METAL",
               "NIFTY PHARMA", "NIFTY REALTY", "NIFTY HEALTHCARE", "NIFTY OIL AND GAS", "NIFTY CHEMICALS",
               "NIFTY ENERGY", "NIFTY INFRA", "NIFTY SERV SECTOR", "NIFTY IND DIGITAL", "NIFTY INDIA MFG",
               "NIFTY IND DEFENCE", "NIFTY IND TOURISM", "NIFTY CAPITAL MKT", "NIFTY EV", "NIFTY MOBILITY",
               "NIFTY HOUSING", "NIFTY SME EMERGE", "NIFTY FIN SERVICE", "NIFTY PSU BANK", "NIFTY PVT BANK",
               "NIFTY MEDIA", "NIFTY MIDCAP 100", "NIFTY SMLCAP 100", "NIFTY NEXT 50"]
CM_URL = "https://public.fyers.in/sym_details/NSE_CM.csv"
_index_map: dict[str, str] = {}
_index_day = ""
KNOWN = {"NIFTY 50": "NSE:NIFTY50-INDEX", "NIFTY BANK": "NSE:NIFTYBANK-INDEX", "INDIA VIX": "NSE:INDIAVIX-INDEX",
         "NIFTY FIN SERVICE": "NSE:FINNIFTY-INDEX", "NIFTY NEXT 50": "NSE:NIFTYNXT50-INDEX"}


def _norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


async def _load_index_map() -> dict[str, str]:
    global _index_day
    today = dt.date.today().isoformat()
    if _index_map and _index_day == today:
        return _index_map
    cache = ROOT / ".cache" / "fyers_master" / "NSE_CM_indices.json"
    try:
        saved = json.loads(cache.read_text(encoding="utf-8"))
        if saved.get("day") == today:
            _index_map.update(saved["map"])
            _index_day = today
            return _index_map
    except (OSError, ValueError, KeyError):
        pass
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), follow_redirects=True) as c:
        r = await c.get(CM_URL)
    r.raise_for_status()
    found: dict[str, str] = {}
    by_norm = {_norm(n): n for n in INDEX_NAMES}
    for row in csv.reader(io.StringIO(r.text)):
        if len(row) < 10 or not row[9].upper().endswith("-INDEX"):
            continue
        tick = row[9].strip().upper()
        body = tick.split(":", 1)[-1][:-6]
        for key in (_norm(row[1]), _norm(body)):
            name = by_norm.get(key)
            if name and name not in found:
                found[name] = tick
    found = {**found, **{k: v for k, v in KNOWN.items() if k not in found}}
    _index_map.clear()
    _index_map.update(found)
    _index_day = today
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"day": today, "map": found}), encoding="utf-8")
    return _index_map


async def _fallback(url: str):
    if not url.rstrip("/").endswith("/api/allIndices"):
        return None
    try:
        from backend.brokers.fyers.client import data_get
        mapping = await _load_index_map()
        syms = list(dict.fromkeys(mapping.values()))
        rows = []
        for i in range(0, len(syms), 50):
            payload = await data_get("/quotes", {"symbols": ",".join(syms[i:i + 50])})
            rows += payload.get("d") or []
    except Exception:                             # noqa: BLE001 - no FYERS login, no network: no fallback
        return None
    back = {v: k for k, v in mapping.items()}
    data = []
    for r in rows:
        v = r.get("v") or {}
        name = back.get(str(r.get("n", "")).upper())
        if r.get("s") != "ok" or not name or v.get("lp") is None:
            continue
        data.append({"index": name, "indexSymbol": name, "last": v.get("lp"), "change": v.get("ch"),
                     "percentChange": v.get("chp"), "previousClose": v.get("prev_close_price"),
                     "open": v.get("open_price"), "high": v.get("high_price"), "low": v.get("low_price")})
    if not data:
        return None
    body = json.dumps({"data": data, "timestamp": dt.datetime.now().strftime("%d-%b-%Y %H:%M:%S"),
                       "source": "FYERS (NSE website unavailable)"}).encode()
    return 200, "application/json", body, {"X-MarketMiner-Fallback": "FYERS"}
