"""FYERS public symbol masters for derivatives: NSE_FO.csv, BSE_FO.csv, MCX_COM.csv.

FYERS warns against building F&O symbols by hand, so contracts are picked from these files (same approach as
SmartEntry AI). They are public (no login), downloaded at most once a day and kept in .cache/fyers_master/.

Columns used: 3 lot size, 4 tick size, 6 trading session, 8 expiry (epoch), 9 symbol ticker, 13 underlying symbol,
15 strike price, 16 option type ("CE" / "PE"; "XX" or blank for a future).
"""
from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / ".cache" / "fyers_master"
URLS = {
    "NSE": "https://public.fyers.in/sym_details/NSE_FO.csv",
    "BSE": "https://public.fyers.in/sym_details/BSE_FO.csv",
    "MCX": "https://public.fyers.in/sym_details/MCX_COM.csv",
}
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ROLL = dt.timedelta(days=1)          # on expiry day the next contract is followed

# Underlyings offered first in the pickers (anything else in the master can still be typed).
INDEXES = {"NSE": ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"], "BSE": ["SENSEX", "BANKEX", "SENSEX50"]}
SPOT = {"NIFTY": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX", "FINNIFTY": "NSE:FINNIFTY-INDEX",
        "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX", "NIFTYNXT50": "NSE:NIFTYNXT50-INDEX", "SENSEX": "BSE:SENSEX-INDEX",
        "BANKEX": "BSE:BANKEX-INDEX", "SENSEX50": "BSE:SENSEX50-INDEX"}
MCX_POPULAR = ["CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI", "GOLD", "GOLDM", "GOLDPETAL", "SILVER",
               "SILVERM", "SILVERMIC", "COPPER", "ZINC", "ALUMINIUM", "LEAD", "NICKEL"]

# MCX quantities are in lots and the symbol master lists lot = 1, but each lot moves by a multiplier per 1-point
# price change (CRUDEOIL: 100 barrels -> Rs 100 per Rs 1). Standard MCX contract multipliers (check against the
# exchange's contract specs if MCX changes them); MCX_MULT_OVERRIDE in .env can patch one: "CRUDEOIL=100,GOLD=100".
MCX_MULT = {"CRUDEOIL": 100, "CRUDEOILM": 10, "NATURALGAS": 1250, "NATGASMINI": 250, "GOLD": 100, "GOLDM": 10,
            "GOLDGUINEA": 1, "GOLDPETAL": 1, "GOLDTEN": 1, "SILVER": 30, "SILVERM": 5, "SILVERMIC": 1,
            "COPPER": 2500, "ZINC": 5000, "ZINCMINI": 1000, "ALUMINIUM": 5000, "ALUMINI": 1000, "LEAD": 5000,
            "LEADMINI": 1000, "NICKEL": 1500, "MENTHAOIL": 360, "COTTONCNDY": 48, "KAPAS": 200}
try:
    import os as _os
    for _kv in (_os.getenv("MCX_MULT_OVERRIDE") or "").split(","):
        if "=" in _kv:
            _k, _v = _kv.split("=", 1)
            MCX_MULT[_k.strip().upper()] = int(_v)
except ValueError:
    pass

_rows: dict[str, list[dict[str, Any]]] = {}
_by_symbol: dict[str, dict[str, Any]] = {}
_loaded_day: dict[str, str] = {}
_lock = asyncio.Lock()


def _parse(exchange: str, text: str) -> list[dict[str, Any]]:
    out = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 17 or not row[9]:
            continue
        try:
            expiry = dt.datetime.fromtimestamp(int(row[8]), dt.timezone.utc)
        except (ValueError, OverflowError, OSError):
            continue
        kind = row[16].strip().upper()
        sym = row[9].strip().upper()
        if kind in ("XX", "") and sym.endswith("FUT"):
            kind = "FUT"
        elif kind not in ("CE", "PE"):
            continue
        try:
            lot = max(1, int(float(row[3])))
        except ValueError:
            lot = 1
        try:
            tick = float(row[4]) or 0.05
        except ValueError:
            tick = 0.05
        try:
            strike = float(row[15])
        except ValueError:
            strike = None
        und = row[13].strip().upper()
        mult = 1
        if exchange == "MCX" and lot == 1:
            mult = MCX_MULT.get(und, 1)
            lot = mult                     # units per lot, so qty x price move = rupees
        out.append({"symbol": sym, "exchange": exchange, "underlying": und, "kind": kind, "multiplier": mult,
                    "expiry": expiry, "strike": strike if kind != "FUT" else None, "lot": lot, "tick": tick,
                    "session": row[6].strip(), "name": row[1].strip()})
    return out


async def load(exchange: str) -> list[dict[str, Any]]:
    today = dt.datetime.now(IST).date().isoformat()
    if _loaded_day.get(exchange) == today:
        return _rows[exchange]
    async with _lock:
        if _loaded_day.get(exchange) == today:
            return _rows[exchange]
        CACHE.mkdir(parents=True, exist_ok=True)
        path = CACHE / f"{exchange}.csv"
        fresh = path.is_file() and dt.datetime.fromtimestamp(path.stat().st_mtime, IST).date().isoformat() == today
        text = None
        if not fresh:
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), follow_redirects=True) as c:
                    r = await c.get(URLS[exchange])
                r.raise_for_status()
                text = r.text
                path.write_text(text, encoding="utf-8")
            except httpx.HTTPError as exc:
                if not path.is_file():
                    raise HTTPException(502, f"Could not download the FYERS {exchange} contract list ({type(exc).__name__})")
        if text is None:
            text = path.read_text(encoding="utf-8")
        rows = _parse(exchange, text)
        if not rows:
            raise HTTPException(502, f"The FYERS {exchange} contract list was empty or in an unexpected format")
        _rows[exchange] = rows
        for r in rows:
            _by_symbol[r["symbol"]] = r
        _loaded_day[exchange] = today
        return rows


def _live(rows, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    return [r for r in rows if r["expiry"] > now]


async def contract(symbol: str) -> dict[str, Any] | None:
    ex = symbol.split(":", 1)[0]
    if ex in URLS:
        await load(ex)
    return _by_symbol.get(symbol.upper())


async def front_future(exchange: str, underlying: str, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = await load(exchange)
    futs = [r for r in rows if r["underlying"] == underlying and r["kind"] == "FUT" and r["expiry"] > now + ROLL]
    if not futs:
        raise HTTPException(400, f"No live {underlying} future on {exchange}")
    return min(futs, key=lambda r: r["expiry"])


async def resolve(symbol: str) -> str:
    """NSE:NIFTY-FUT / BSE:SENSEX-FUT / MCX:CRUDEOIL-FUT (front-month alias) -> the current contract's ticker."""
    s = symbol.upper()
    ex, rest = s.split(":", 1)
    if rest.endswith("-FUT") and ex in URLS:
        return (await front_future(ex, rest[:-4]))["symbol"]
    return s


async def underlyings(exchange: str) -> list[dict[str, Any]]:
    rows = _live(await load(exchange))
    seen: dict[str, dict] = {}
    for r in rows:
        u = seen.setdefault(r["underlying"], {"underlying": r["underlying"], "futures": 0, "options": 0})
        u["futures" if r["kind"] == "FUT" else "options"] += 1
    pref = INDEXES.get(exchange, []) + (MCX_POPULAR if exchange == "MCX" else [])
    first = [seen[p] for p in pref if p in seen]
    rest = sorted((v for k, v in seen.items() if k not in pref), key=lambda v: v["underlying"])
    for u in first:
        u["popular"] = True
    return first + rest


async def chain(exchange: str, underlying: str, expiry: str | None = None) -> dict[str, Any]:
    """Futures and option strikes for one underlying; `expiry` = YYYY-MM-DD (IST) of the options to list."""
    rows = [r for r in _live(await load(exchange)) if r["underlying"] == underlying]
    if not rows:
        raise HTTPException(404, f"No live {underlying} contracts on {exchange}")
    futs = sorted((r for r in rows if r["kind"] == "FUT"), key=lambda r: r["expiry"])
    opts = [r for r in rows if r["kind"] in ("CE", "PE")]
    exps = sorted({r["expiry"].astimezone(IST).date().isoformat() for r in opts})
    chosen = expiry if expiry in exps else (exps[0] if exps else None)
    strikes: dict[float, dict] = {}
    for r in opts:
        if r["expiry"].astimezone(IST).date().isoformat() == chosen:
            strikes.setdefault(r["strike"], {"strike": r["strike"]})[r["kind"]] = r["symbol"]
    lot = (futs[0] if futs else opts[0])["lot"] if (futs or opts) else 1
    pub = lambda r: {"symbol": r["symbol"], "expiry": r["expiry"].astimezone(IST).date().isoformat(),
                     "lot": r["lot"], "tick": r["tick"]}
    return {"exchange": exchange, "underlying": underlying, "spot_symbol": SPOT.get(underlying),
            "futures": [pub(f) for f in futs], "expiries": exps, "expiry": chosen, "lot": lot,
            "strikes": [strikes[k] for k in sorted(strikes)]}
