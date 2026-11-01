"""Day Desk API: scan -> entry -> exit plan -> Time Machine -> size -> risk -> paper order.

Markets and symbols:
  NSE       NSE/BSE cash (FYERS)                      NSE:SBIN-EQ, NSE:NIFTY50-INDEX
  FNO       NSE/BSE futures & options (FYERS)         NSE:NIFTY26NOVFUT, BSE:SENSEX26N0682000CE
  MCX       MCX commodity futures & options (FYERS)   MCX:CRUDEOIL26NOVFUT
  BINANCE   Binance spot (USDT)                       BINANCE:BTCUSDT
  BINANCEF  Binance USD-M perpetuals (USDT)           BINANCEF:BTCUSDT, BINANCEF:XAUUSDT
  SHARK     Shark Exchange perpetuals (INR/USDT)      SHARK:BTCINR
  DELTA     Delta Exchange India options (USD)        DELTA:C-BTC-80000-271126 (BTC, ETH, XAUT gold)
Aliases that roll by themselves: NSE:NIFTY-FUT (front-month future), NSE:NIFTY-ATM-CE / -ATM-PE (nearest expiry,
strike nearest the spot), MCX:CRUDEOIL-FUT, DELTA:BTC-ATM-CE.
Orders are PAPER ONLY: written to a local ledger file, never sent to FYERS, Binance, Shark, Delta or any broker.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import secrets
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from backend.brokers.fyers.client import data_get
from backend.markets import fyers_master as FM
from backend.markets.crypto import PROVIDERS
from backend.markets.state import market_enabled
from . import engine as E

router = APIRouter(prefix="/api/desk", tags=["Day Desk"])

ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = ROOT / ".desk_config.json"
LEDGER_FILE = ROOT / ".desk_paper.json"
_io_lock = asyncio.Lock()
_sem = asyncio.Semaphore(3)
_cache: dict[tuple, tuple[float, Any]] = {}
MAX_SCAN = 40
Market = Literal["NSE", "FNO", "MCX", "BINANCE", "BINANCEF", "SHARK", "DELTA"]
MARKETS = ("NSE", "FNO", "MCX", "BINANCE", "BINANCEF", "SHARK", "DELTA")
FYERS_MARKETS = ("NSE", "FNO", "MCX")
SESSION = {"NSE": E.NSE, "FNO": E.NSE, "MCX": E.MCX, "BINANCE": E.CRYPTO, "BINANCEF": E.CRYPTO,
           "SHARK": E.CRYPTO, "DELTA": E.CRYPTO}
CURRENCY = {"NSE": "INR", "FNO": "INR", "MCX": "INR", "BINANCE": "USDT", "BINANCEF": "USDT", "SHARK": "USDT",
            "DELTA": "USD"}
CRYPTO_HISTORY_DAYS = 45
_alias: dict[str, tuple[float, str]] = {}

# Kept short on purpose: each symbol costs requests against shared rate limits.
DEFAULT_WATCHLIST = {
    "NSE": [f"NSE:{s}-EQ" for s in (
        "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "BHARTIARTL", "ITC", "LT", "KOTAKBANK",
        "AXISBANK", "BAJFINANCE", "MARUTI", "SUNPHARMA", "M&M", "TITAN", "TATASTEEL", "HCLTECH", "ADANIENT", "TRENT")],
    "BINANCE": [f"BINANCE:{s}" for s in (
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT", "LTCUSDT")],
    "SHARK": [],                     # filled from Shark's live contract list (INR pairs first)
    "FNO": ["NSE:NIFTY-FUT", "NSE:BANKNIFTY-FUT", "NSE:FINNIFTY-FUT", "NSE:MIDCPNIFTY-FUT", "BSE:SENSEX-FUT",
            "BSE:BANKEX-FUT", "NSE:NIFTY-ATM-CE", "NSE:NIFTY-ATM-PE", "NSE:BANKNIFTY-ATM-CE", "NSE:BANKNIFTY-ATM-PE",
            "BSE:SENSEX-ATM-CE", "BSE:SENSEX-ATM-PE"],
    "MCX": [f"MCX:{u}-FUT" for u in ("CRUDEOIL", "CRUDEOILM", "NATURALGAS", "GOLD", "GOLDM", "SILVER", "SILVERM",
                                     "COPPER", "ZINC")],
    "BINANCEF": [f"BINANCEF:{s}" for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
                                           "XAUUSDT", "XAGUSDT", "CLUSDT")],
    "DELTA": ["DELTA:BTC-ATM-CE", "DELTA:BTC-ATM-PE", "DELTA:ETH-ATM-CE", "DELTA:ETH-ATM-PE"],
}
NSE_RE = re.compile(r"^(NSE|BSE):[A-Z0-9&_.\-]{1,30}-(EQ|BE|INDEX)$")
FO_RE = re.compile(r"^(NSE|BSE|MCX):[A-Z0-9&_\-]{2,40}$")
PAIR_RE = re.compile(r"^[A-Z0-9]{3,20}$")
DELTA_RE = re.compile(r"^([CP]-[A-Z0-9]{2,10}-[0-9]+(\.[0-9]+)?-[0-9]{6}|[A-Z0-9]{2,10}-ATM-(CE|PE))$")


def market_of(symbol: str) -> str:
    head = symbol.split(":", 1)[0]
    if head in ("BINANCE", "BINANCEF", "SHARK", "DELTA", "MCX"):
        return head
    return "NSE" if NSE_RE.match(symbol) else "FNO"


def is_option(symbol: str) -> bool:
    s = symbol.upper()
    return s.endswith(("CE", "PE")) or s.startswith(("DELTA:C-", "DELTA:P-"))


def pair_of(symbol: str) -> str:
    return symbol.split(":", 1)[1]


def norm_symbol(s: str, market: str | None = None) -> str:
    s = str(s or "").strip().upper().replace(" ", "").replace("/", "")
    if not s:
        raise HTTPException(400, "symbol is empty")
    if ":" not in s:
        m = market or "NSE"
        s = {"NSE": f"NSE:{s}-EQ", "FNO": f"NSE:{s}", "MCX": f"MCX:{s}"}.get(m, f"{m}:{s}")
    m = market_of(s)
    if m == "NSE":
        if not NSE_RE.match(s):
            raise HTTPException(400, f"{s!r} is not a FYERS symbol like NSE:SBIN-EQ or NSE:NIFTY50-INDEX")
    elif m in ("FNO", "MCX"):
        if not FO_RE.match(s):
            raise HTTPException(400, f"{s!r} is not a FYERS F&O symbol like NSE:NIFTY-FUT or MCX:CRUDEOIL-FUT")
    elif m == "DELTA":
        if not DELTA_RE.match(pair_of(s)):
            raise HTTPException(400, f"{s!r} is not a Delta option like DELTA:C-BTC-80000-271126 or DELTA:BTC-ATM-CE")
    elif not PAIR_RE.match(pair_of(s)):
        raise HTTPException(400, f"{s!r} is not a {m.title()} pair like {m}:BTCUSDT")
    return s


async def _spot(symbol: str) -> float:
    """Last price of an underlying for ATM strikes."""
    payload = await data_get("/quotes", {"symbols": symbol})
    for row in payload.get("d") or []:
        v = row.get("v") or {}
        if v.get("lp"):
            return float(v["lp"])
    raise HTTPException(502, f"No FYERS price for {symbol}")


async def resolve(symbol: str) -> str:
    """Rolling aliases -> today's contract (cached for a minute)."""
    s = symbol.upper()
    if not s.endswith(("-FUT", "-ATM-CE", "-ATM-PE")):
        return s
    hit = _alias.get(s)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    m = market_of(s)
    if s.endswith("-FUT"):
        out = await FM.resolve(s)
    elif m == "DELTA":
        need_enabled("DELTA")
        coin, _, side = pair_of(s).split("-")
        prov = PROVIDERS["DELTA"]
        info = await prov.load_info()
        now_ms = time.time() * 1000 + 86400 * 1000          # roll a day before expiry, like the futures
        opts = [v for v in info.values() if v["base"] == coin and v["kind"] == side and v["expiry_ms"] > now_ms]
        if not opts:
            raise HTTPException(400, f"No live {coin} options on Delta Exchange")
        exp = min(v["expiry_ms"] for v in opts)
        px = await prov.index(coin)
        best = min((v for v in opts if v["expiry_ms"] == exp), key=lambda v: abs(v["strike"] - px))
        out = f"DELTA:{best['pair']}"
    else:
        ex, rest = s.split(":", 1)
        und, side = rest[:-7], rest[-2:]
        ch = await FM.chain(ex, und)
        spot_sym = ch["spot_symbol"] or (ch["futures"][0]["symbol"] if ch["futures"] else None)
        if not spot_sym:
            raise HTTPException(400, f"No price reference for {und}")
        px = await _spot(spot_sym)
        rows = [r for r in ch["strikes"] if side in r]
        if not rows:
            raise HTTPException(400, f"No {und} {side} options listed for {ch['expiry']}")
        out = min(rows, key=lambda r: abs(r["strike"] - px))[side]
    _alias[s] = (time.time(), out)
    return out


def bad(e: Exception):
    raise HTTPException(422, str(e))


def need_enabled(m: str) -> None:
    if m not in FYERS_MARKETS and not market_enabled(m):
        raise HTTPException(409, f"{m.title()} is switched off. Turn it on under Connections.")


# ------------------------------------------------------------------ storage

def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


def _all_saved() -> dict:
    raw = _read(CONFIG_FILE, {})
    if raw and not any(k in raw for k in MARKETS):        # first version stored NSE settings flat
        raw = {"NSE": raw}
    return raw


def load_config(market: str = "NSE") -> dict:
    base = {"NSE": E.DEFAULT_CONFIG, "FNO": E.DEFAULT_CONFIG, "MCX": E.MCX_CONFIG}.get(market, E.CRYPTO_CONFIG)
    saved = _all_saved().get(market) or {}
    cfg = {**base, "currency": CURRENCY[market]}
    if market in ("FNO", "MCX"):
        # One index-option lot can risk more than a stock trade: F&O/MCX get their own, larger defaults.
        cfg.update(budget=300000, max_order_value=1500000, max_position_pct=100, cost_per_order=40,
                   max_loss_per_trade=2500, max_daily_loss=7500)
    if market == "DELTA":
        cfg.update(fee_bps=3)
    cfg.update(saved)
    if market == "SHARK":
        cfg["currency"] = "USDT"          # Shark: USDT-quoted contracts only
    cfg["market"] = market
    if not cfg.get("watchlist"):
        cfg["watchlist"] = list(DEFAULT_WATCHLIST[market])
    return cfg


def load_ledger() -> list[dict]:
    data = _read(LEDGER_FILE, [])
    return data if isinstance(data, list) else []


# ------------------------------------------------------------------ data per market

async def _fyers_candles(symbol: str, resolution: str, days_back: int, ttl: float) -> list:
    today = E.now_ist().date()
    frm = (today - dt.timedelta(days=days_back)).isoformat()
    key = (symbol, resolution, frm)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    async with _sem:
        payload = await data_get("/history", {"symbol": symbol, "resolution": resolution, "date_format": "1",
                                              "range_from": frm, "range_to": today.isoformat(), "cont_flag": "1"})
    rows = payload.get("candles") or []
    _cache[key] = (time.time(), rows)
    if len(_cache) > 400:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
            _cache.pop(k, None)
    return rows


def fill_gaps(rows: list, max_gap: int = 7200, step: int = 300) -> list:
    """Options print a candle only when they trade. Fill short gaps (under `max_gap` seconds, so never across a
    session break) with flat bars at the last close, so a quiet option still has complete sessions."""
    out = []
    for r in rows:
        if out:
            prev = out[-1]
            gap = r[0] - prev[0]
            if step < gap <= max_gap:
                c = prev[4]
                out.extend([t, c, c, c, c, 0.0] for t in range(prev[0] + step, r[0], step))
        out.append(r)
    return out


async def history_bars(symbol: str) -> list[dict]:
    m = market_of(symbol)
    S = SESSION[m]
    if m in FYERS_MARKETS:
        # FYERS allows up to 100 days per request for minute candles; 90 calendar days ~ 60 sessions.
        rows = await _fyers_candles(symbol, "5", 90, ttl=30)
        return E.parse_candles(fill_gaps(rows) if is_option(symbol) else rows, S=S)
    need_enabled(m)
    async with _sem:
        rows = await PROVIDERS[m].history(pair_of(symbol), CRYPTO_HISTORY_DAYS)
    return E.parse_candles(fill_gaps(rows) if m == "DELTA" else rows, S=S)


async def today_bars(symbol: str) -> list[dict]:
    m = market_of(symbol)
    S = SESSION[m]
    now = E.now_ist()
    if m in FYERS_MARKETS:
        rows = await _fyers_candles(symbol, "5", 0, ttl=20)
        bars = E.parse_candles(fill_gaps(rows) if is_option(symbol) else rows, S=S)
    else:
        need_enabled(m)
        async with _sem:
            rows = await PROVIDERS[m].klines_page(pair_of(symbol), "5m", 300)
        bars = E.parse_candles(fill_gaps(rows) if m == "DELTA" else rows, S=S)
    sd = S.session_date(now)
    return [b for b in bars if b["date"] == sd]


async def daily_bars(symbol: str) -> list[dict]:
    m = market_of(symbol)
    if m in FYERS_MARKETS:
        return E.daily_from_candles(await _fyers_candles(symbol, "D", 150, ttl=6 * 3600),
                                    S=E.NSE if m != "MCX" else E.MCX)
    need_enabled(m)
    async with _sem:
        rows = await PROVIDERS[m].daily(pair_of(symbol), 150)
    return E.daily_from_candles(rows, S=SESSION[m])


async def instrument(symbol: str, cfg: dict) -> dict:
    m = market_of(symbol)
    if m == "NSE":
        return {"tick": float(cfg.get("tick") or 0.05), "qty_step": 1.0, "min_qty": 1.0, "quote": "INR", "lot": 1}
    if m in ("FNO", "MCX"):
        c = await FM.contract(symbol)
        if not c:
            raise HTTPException(400, f"{symbol} is not a live FYERS contract (check the expiry/strike)")
        return {"tick": c["tick"], "qty_step": float(c["lot"]), "min_qty": float(c["lot"]), "lot": c["lot"],
                "quote": "INR", "kind": c["kind"], "underlying": c["underlying"],
                "expiry": c["expiry"].astimezone(E.IST).date().isoformat(), "strike": c["strike"]}
    need_enabled(m)
    meta = await PROVIDERS[m].instrument(pair_of(symbol))
    if m == "SHARK" and meta.get("quote") != "USDT":
        raise HTTPException(400, f"{pair_of(symbol)} is quoted in {meta.get('quote')}; MarketMiner uses Shark's USDT pairs only")
    return {**meta, "lot": meta.get("qty_step")}


async def last_prices(symbols: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    nse = [s for s in symbols if market_of(s) in FYERS_MARKETS]
    for i in range(0, len(nse), 50):
        payload = await data_get("/quotes", {"symbols": ",".join(nse[i:i + 50])})
        for row in payload.get("d") or []:
            v = row.get("v") or {}
            if row.get("s") == "ok" and v.get("lp") is not None:
                out[str(row.get("n")).upper()] = float(v["lp"])
    for m in ("BINANCE", "BINANCEF", "SHARK", "DELTA"):
        mine = [s for s in symbols if market_of(s) == m]
        if mine and market_enabled(m):
            got = await PROVIDERS[m].prices([pair_of(s) for s in mine])
            out.update({f"{m}:{k}": v for k, v in got.items()})
    return out


# ------------------------------------------------------------------ config

class ConfigIn(BaseModel):
    budget: float | None = Field(None, gt=0)
    cash_buffer_pct: float | None = Field(None, ge=0, le=90)
    max_position_pct: float | None = Field(None, gt=0, le=100)
    max_order_value: float | None = Field(None, gt=0)
    max_loss_per_trade: float | None = Field(None, gt=0)
    max_daily_loss: float | None = Field(None, gt=0)
    max_trades_per_day: int | None = Field(None, ge=1, le=50)
    min_reward_risk: float | None = Field(None, ge=0, le=20)
    no_entries_before: str | None = None
    no_entries_after: str | None = None
    flat_by: str | None = None
    allow_short: bool | None = None
    blocked: list[str] | None = None
    tick: float | None = Field(None, gt=0, le=10)
    or_minutes: int | None = Field(None, ge=5, le=60)
    target_r: float | None = Field(None, gt=0, le=20)
    cost_per_order: float | None = Field(None, ge=0, le=10000)
    fee_bps: float | None = Field(None, ge=0, le=500)
    currency: Literal["INR", "USDT", "USD"] | None = None
    watchlist: list[str] | None = None


@router.get("/config")
async def get_config(market: Market = "NSE"):
    cfg = load_config(market)
    if market == "SHARK" and cfg["watchlist"] == [] and market_enabled("SHARK"):
        try:
            info = await PROVIDERS["SHARK"].load_info()
            pairs = sorted(p for p in info if info[p]["quote"] == "USDT")
            pref = [p for p in pairs if p.startswith(("BTC", "ETH", "SOL", "XRP", "BNB", "DOGE"))]
            cfg["watchlist"] = [f"SHARK:{p}" for p in (pref + [p for p in pairs if p not in pref])[:10]]
        except HTTPException:
            pass
    return cfg


@router.post("/config")
async def set_config(body: ConfigIn, market: Market = "NSE"):
    upd = body.model_dump(exclude_none=True)
    for k in ("no_entries_before", "no_entries_after", "flat_by"):
        if k in upd:
            try:
                upd[k] = E.clock(E.parse_clock(upd[k], k))
            except E.DeskError as e:
                bad(e)
    if "or_minutes" in upd and upd["or_minutes"] % 5:
        raise HTTPException(422, "opening range must be a multiple of 5 minutes")
    if "watchlist" in upd:
        wl = [norm_symbol(s, market) for s in upd["watchlist"] if str(s).strip()]
        if any(market_of(s) != market for s in wl):
            raise HTTPException(422, f"this watchlist is for {market} symbols only")
        if len(wl) > MAX_SCAN:
            raise HTTPException(422, f"watchlist is limited to {MAX_SCAN} symbols (request limits)")
        upd["watchlist"] = list(dict.fromkeys(wl))
    if "blocked" in upd:
        upd["blocked"] = [norm_symbol(s, market) for s in upd["blocked"] if str(s).strip()]
    async with _io_lock:
        saved = _all_saved()
        saved.setdefault(market, {}).update(upd)
        _write(CONFIG_FILE, saved)
    return await get_config(market)


# ------------------------------------------------------------------ derivatives: contract picker

POPULAR_PERPS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "XAUUSDT", "XAGUSDT", "CLUSDT",
                 "BZUSDT", "NATGASUSDT", "COPPERUSDT"]


@router.get("/derivs/underlyings")
async def deriv_underlyings(market: Market):
    if market == "FNO":
        out = []
        for ex in ("NSE", "BSE"):
            for u in await FM.underlyings(ex):
                out.append({**u, "exchange": ex, "id": f"{ex}:{u['underlying']}"})
        out.sort(key=lambda u: not u.get("popular"))          # stable: keeps NIFTY, BANKNIFTY, ... SENSEX order
        return {"market": market, "items": out}
    if market == "MCX":
        return {"market": market, "items": [{**u, "exchange": "MCX", "id": f"MCX:{u['underlying']}"}
                                            for u in await FM.underlyings("MCX")]}
    if market == "DELTA":
        need_enabled(market)
        return {"market": market, "items": [{"underlying": c, "exchange": "DELTA", "id": f"DELTA:{c}", "popular": True,
                                             "options": True} for c in ("BTC", "ETH", "XAUT")]}
    if market in ("BINANCEF", "SHARK", "BINANCE"):
        need_enabled(market)
        info = await PROVIDERS[market].load_info()
        if market == "SHARK":
            info = {k: v for k, v in info.items() if v.get("quote") == "USDT"}
        pref = [p for p in POPULAR_PERPS if p in info]
        rest = sorted(p for p in info if p not in pref)
        return {"market": market, "items": [{"underlying": p, "id": f"{market}:{p}", "popular": p in pref,
                                             "name": info[p].get("name")} for p in pref + rest]}
    raise HTTPException(400, "No derivatives for this market")


@router.get("/derivs/chain")
async def deriv_chain(market: Market, underlying: str, expiry: str | None = None):
    underlying = underlying.upper()
    if market in ("FNO", "MCX"):
        ex, und = underlying.split(":", 1) if ":" in underlying else ("MCX" if market == "MCX" else "NSE", underlying)
        ch = await FM.chain(ex, und, expiry)
        spot_sym = ch["spot_symbol"] or (ch["futures"][0]["symbol"] if ch["futures"] else None)
        spot = None
        if spot_sym:
            try:
                spot = await _spot(spot_sym)
            except HTTPException:
                spot = None
        atm = min(ch["strikes"], key=lambda r: abs(r["strike"] - spot))["strike"] if spot and ch["strikes"] else None
        return {**ch, "market": market, "spot": spot, "atm": atm,
                "aliases": {"FUT": f"{ex}:{und}-FUT", "ATM-CE": f"{ex}:{und}-ATM-CE", "ATM-PE": f"{ex}:{und}-ATM-PE"}}
    if market == "DELTA":
        need_enabled(market)
        coin = underlying.split(":")[-1]
        prov = PROVIDERS["DELTA"]
        info = await prov.load_info()
        now_ms = time.time() * 1000
        opts = [v for v in info.values() if v["base"] == coin and v["expiry_ms"] > now_ms]
        exps = sorted({dt.datetime.fromtimestamp(v["expiry_ms"] / 1000, E.IST).date().isoformat() for v in opts})
        chosen = expiry if expiry in exps else (exps[0] if exps else None)
        strikes: dict[float, dict] = {}
        for v in opts:
            if dt.datetime.fromtimestamp(v["expiry_ms"] / 1000, E.IST).date().isoformat() == chosen:
                strikes.setdefault(v["strike"], {"strike": v["strike"]})[v["kind"]] = f"DELTA:{v['pair']}"
        spot = await prov.index(coin)
        rows = [strikes[k] for k in sorted(strikes)]
        atm = min(rows, key=lambda r: abs(r["strike"] - spot))["strike"] if rows else None
        return {"market": market, "exchange": "DELTA", "underlying": coin, "futures": [], "expiries": exps,
                "expiry": chosen, "strikes": rows, "spot": spot, "atm": atm,
                "lot": next((v["contract_value"] for v in opts), 0.001),
                "aliases": {"ATM-CE": f"DELTA:{coin}-ATM-CE", "ATM-PE": f"DELTA:{coin}-ATM-PE"}}
    if market in ("BINANCEF", "SHARK", "BINANCE"):
        sym = underlying if ":" in underlying else f"{market}:{underlying}"
        return {"market": market, "underlying": pair_of(sym), "futures": [{"symbol": sym, "expiry": "perpetual"}],
                "expiries": [], "strikes": [], "aliases": {"FUT": sym}}
    raise HTTPException(400, "No derivatives for this market")


# ------------------------------------------------------------------ 1. scanner

class ScanIn(BaseModel):
    market: Market = "NSE"
    symbols: list[str] | None = None
    or_minutes: int | None = Field(None, ge=5, le=60)


async def _scan_one(alias: str, orm: int) -> dict:
    row: dict[str, Any] = {"symbol": alias}
    S = SESSION[market_of(alias)]
    try:
        sym = await resolve(alias)
        row["contract"] = sym
        daily = await daily_bars(sym)
        if len(daily) >= 30:
            tags, nums = E.find_setups([b["c"] for b in daily])
            vol20 = sum(b["v"] for b in daily[-20:]) / 20
            row.update(daily_tags=tags, daily=nums, prev_close=daily[-1]["c"], avg_vol20=round(vol20),
                       daily_date=daily[-1]["date"])
        else:
            row.update(daily_tags=[], daily_note=f"only {len(daily)} daily bars")
        bars = await today_bars(sym)
        row["orb"] = E.orb_scan_row(bars, orm, S)
        row["bars_today"] = len(bars)
    except HTTPException as exc:
        if exc.status_code in (401, 409, 503) or (exc.status_code == 429):
            raise
        row["error"] = str(exc.detail)[:200]
    except Exception as exc:                 # one bad symbol must not sink the scan
        row["error"] = f"{type(exc).__name__}: {exc}"[:200]
    return row


@router.post("/scan")
async def scan(body: ScanIn):
    cfg = await get_config(body.market)
    need_enabled(body.market)
    syms = [norm_symbol(s, body.market) for s in (body.symbols or cfg["watchlist"])][:MAX_SCAN]
    orm = body.or_minutes or int(cfg["or_minutes"])
    rows = await asyncio.gather(*[_scan_one(s, orm) for s in syms])
    S = SESSION[body.market]
    return {"as_of": E.now_ist().isoformat(timespec="seconds"), "market": body.market, "or_minutes": orm,
            "session_open": S.label(0), "count": len(rows), "setups": E.SETUPS, "rows": rows,
            "note": "A scan is a list of charts that match a rule, not a list of things to buy."}


# ------------------------------------------------------------------ 2. entry timer

@router.get("/entry")
async def entry(symbol: str, side: Literal["buy", "sell"] = "buy", or_minutes: int | None = None,
                level: float | None = None, market: Market | None = None):
    alias = norm_symbol(symbol, market)
    sym = await resolve(alias)
    m = market_of(sym)
    cfg = load_config(m)
    inst = await instrument(sym, cfg)
    bars = await today_bars(sym)
    try:
        res = E.entry_status(bars, E.now_ist(), int(or_minutes or cfg["or_minutes"]), side,
                             cfg["no_entries_before"], cfg["no_entries_after"], level, SESSION[m], inst["tick"])
    except E.DeskError as e:
        bad(e)
    res.update(symbol=sym, alias=alias if alias != sym else None, instrument=inst, lot=inst.get("lot"),
               market=m, date=res["session_date"], tick=inst["tick"], session_open=SESSION[m].label(0),
               session_bars=SESSION[m].bars, currency=inst.get("quote", cfg["currency"]),
               candles=[{"t": b["start"], "slot": b["slot"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]} for b in bars])
    return res


# ------------------------------------------------------------------ 3. exit planner

class ExitIn(BaseModel):
    symbol: str
    side: Literal["buy", "sell"] = "buy"
    entry: float = Field(gt=0)
    entry_time: str | None = None
    stop_rule: Literal["or_mid", "or_far", "pct", "atr", "price"] = "or_mid"
    stop_value: float | None = None
    target_rule: Literal["r", "price"] = "r"
    target_value: float = 2.0
    or_high: float | None = None
    or_low: float | None = None


@router.post("/exit")
async def exit_plan(body: ExitIn):
    sym = await resolve(norm_symbol(body.symbol))
    m = market_of(sym)
    cfg = load_config(m)
    inst = await instrument(sym, cfg)
    bars = None
    if body.stop_rule == "atr":
        bars = await today_bars(sym)
        if body.entry_time:
            eo = SESSION[m].to_offset(body.entry_time)
            bars = [b for b in bars if b["off"] + 5 <= eo]
    try:
        plan = E.plan_exit(body.entry, body.side, body.stop_rule, body.stop_value, body.target_rule,
                           body.target_value, body.or_high, body.or_low, bars, float(inst["tick"]))
    except E.DeskError as e:
        bad(e)
    plan.update(flat_by=cfg["flat_by"], symbol=sym, market=m, currency=inst.get("quote", cfg["currency"]))
    return plan


# ------------------------------------------------------------------ 4. Time Machine

class TMIn(BaseModel):
    symbol: str
    date: str | None = None
    entry_time: str
    entry: float = Field(gt=0)
    stop: float = Field(gt=0)
    target: float = Field(gt=0)
    side: Literal["buy", "sell"] = "buy"
    or_minutes: int | None = None
    stop_rule: str | None = None
    until: str | None = None
    replays: int = Field(10000, ge=100, le=200000)
    seed: int = 19
    window: int = Field(2, ge=0, le=10)


@router.post("/timemachine")
async def timemachine(body: TMIn):
    sym = await resolve(norm_symbol(body.symbol))
    m = market_of(sym)
    cfg = load_config(m)
    S = SESSION[m]
    inst = await instrument(sym, cfg)
    bars = await history_bars(sym)
    plan = body.model_dump()
    plan["symbol"] = sym
    plan["date"] = body.date or S.session_date(E.now_ist())
    until = body.until or cfg["flat_by"]
    try:
        doc = await run_in_threadpool(E.time_machine, bars, plan, body.replays, body.seed, body.window,
                                      until, float(inst["tick"]), S, 8 if is_option(sym) else E.MIN_SESSIONS)
    except E.DeskError as e:
        bad(e)
    doc.update(symbol=sym, market=m, tick=inst["tick"])
    return doc


# ------------------------------------------------------------------ 5. sizing / 6. risk

class SizeIn(BaseModel):
    symbol: str
    entry: float = Field(gt=0)
    risk_per_share: float = Field(gt=0)
    target: float | None = None
    max_loss: float | None = Field(None, gt=0)


@router.post("/size")
async def size(body: SizeIn):
    sym = await resolve(norm_symbol(body.symbol))
    cfg = load_config(market_of(sym))
    inst = await instrument(sym, cfg)
    try:
        out = E.size_position(body.model_dump(), cfg, body.max_loss, float(inst["qty_step"]), inst.get("min_qty"))
        lot = inst.get("lot") or 1
        if market_of(sym) == "DELTA" and lot < 1:
            out["lots"] = int(round(out["qty"] / lot))
            out["lot"] = lot
            out["notes"].append(f"1 contract = {lot:g} {inst.get('base', '')}: {out['lots']} contract(s).")
        elif market_of(sym) in ("FNO", "MCX") and lot > 1:
            out["lots"] = int(out["qty"] // lot)
            out["lot"] = lot
            out["notes"].append(f"Lot size {lot}: {out['lots']} lot(s) = {out['qty']} units"
                                + (" (MCX: units = lots x the contract multiplier; order FYERS in lots)." if market_of(sym) == "MCX" else "."))
        return out
    except E.DeskError as e:
        bad(e)


class OrderIn(BaseModel):
    symbol: str
    side: Literal["buy", "sell"] = "buy"
    qty: float = Field(ge=0, le=1e9)
    entry: float = Field(gt=0)
    stop: float | None = Field(None, gt=0)
    target: float | None = Field(None, gt=0)
    setup: str | None = Field(None, max_length=60)
    tm_verdict: str | None = Field(None, max_length=80)


async def _risk(o: dict, ledger: list[dict]) -> dict:
    m = market_of(o["symbol"])
    cfg = load_config(m)
    rc = E.risk_check(o, cfg, [r for r in ledger if r.get("market", "NSE") == m], E.now_ist(), SESSION[m])
    if m not in FYERS_MARKETS:
        try:
            inst = await instrument(o["symbol"], cfg)
            ok = inst.get("quote") == cfg["currency"]
            rc["checks"].append({"ok": ok, "label": f"Quoted in your limit currency ({cfg['currency']})"})
            if not ok:
                rc["reasons"].append(f"{pair_of(o['symbol'])} is quoted in {inst.get('quote')}, but your {m.title()} "
                                     f"limits are in {cfg['currency']}. Change the currency under Limits.")
                rc["pass"] = False
        except HTTPException as exc:
            rc["reasons"].append(str(exc.detail))
            rc["pass"] = False
    return rc


@router.post("/risk")
async def risk(body: OrderIn):
    o = body.model_dump()
    o["symbol"] = await resolve(norm_symbol(o["symbol"]))
    return await _risk(o, load_ledger())


# ------------------------------------------------------------------ 7. paper order desk

class PlaceIn(BaseModel):
    order: OrderIn
    confirm: str


def _close(rec: dict, price: float, reason: str, cfg: dict) -> None:
    long = rec["side"] == "buy"
    gross = (price - rec["entry"]) * rec["qty"] if long else (rec["entry"] - price) * rec["qty"]
    fee = float(cfg.get("fee_bps") or 0) / 10000 * rec["qty"] * (rec["entry"] + price)
    costs = round(2 * float(cfg.get("cost_per_order") or 0) + fee, 4)
    rec.update(status="closed", exit=price, exit_reason=reason,
               exit_at=E.now_ist().isoformat(timespec="seconds"), gross=round(gross, 4), costs=costs,
               pnl=round(gross - costs, 4),
               R=round(gross / (abs(rec["entry"] - rec["stop"]) * rec["qty"]), 2) if rec.get("stop") else None)


@router.post("/paper/order")
async def paper_order(body: PlaceIn):
    if body.confirm.strip().upper() != "YES":
        raise HTTPException(400, "Type YES to confirm the paper order")
    o = body.order.model_dump()
    o["symbol"] = await resolve(norm_symbol(o["symbol"]))
    m = market_of(o["symbol"])
    cfg = load_config(m)
    S = SESSION[m]
    async with _io_lock:
        ledger = load_ledger()
        rc = await _risk(o, ledger)
        if not rc["pass"]:
            raise HTTPException(409, "Risk manager blocked it: " + "; ".join(rc["reasons"]))
        now = E.now_ist()
        rec = {"id": secrets.token_hex(4), "market": m, "currency": cfg["currency"], "date": S.session_date(now),
               "placed_at": now.isoformat(timespec="seconds"), "status": "open", "paper": True, **o,
               "max_loss": rc["loss_at_stop"]}
        ledger.append(rec)
        _write(LEDGER_FILE, ledger)
    return {"placed": rec, "risk": rc,
            "note": "Paper order recorded locally. Nothing was sent to FYERS, Binance, Shark or any broker."}


@router.get("/paper")
async def paper(mark: bool = True):
    ledger = load_ledger()
    open_syms = sorted({r["symbol"] for r in ledger if r.get("status") == "open"})
    marks, err = {}, None
    if mark and open_syms:
        try:
            marks = await last_prices(open_syms)
        except HTTPException as exc:
            err = str(exc.detail)
    now = E.now_ist()
    changed = False
    for r in ledger:
        if r.get("status") != "open" or r["symbol"] not in marks:
            continue
        m = r.get("market", "NSE")
        cfg, S = load_config(m), SESSION[m]
        lp = marks[r["symbol"]]
        long = r["side"] == "buy"
        r["ltp"] = lp
        r["unrealized"] = round(((lp - r["entry"]) if long else (r["entry"] - lp)) * r["qty"], 4)
        flat = (not S.weekdays_only or now.weekday() < 5) and (
            r.get("date") != S.session_date(now) or S.now_offset(now) >= S.to_offset(cfg["flat_by"]))
        if r.get("stop") and ((lp <= r["stop"]) if long else (lp >= r["stop"])):
            _close(r, r["stop"], "stop", cfg); changed = True
        elif r.get("target") and ((lp >= r["target"]) if long else (lp <= r["target"])):
            _close(r, r["target"], "target", cfg); changed = True
        elif flat:
            _close(r, lp, f"flat by {cfg['flat_by']}", cfg); changed = True
    if changed:
        async with _io_lock:
            _write(LEDGER_FILE, ledger)
    closed = [r for r in ledger if r.get("status") == "closed"]
    by_ccy: dict[str, dict] = {}
    for r in closed:
        cur = r.get("currency", "INR")
        d = by_ccy.setdefault(cur, {"today": 0.0, "total": 0.0, "wins": 0, "closed": 0})
        S = SESSION[r.get("market", "NSE")]
        d["total"] += r.get("pnl") or 0
        d["today"] += (r.get("pnl") or 0) if r.get("date") == S.session_date(now) else 0
        d["wins"] += 1 if (r.get("pnl") or 0) > 0 else 0
        d["closed"] += 1
    return {"as_of": now.isoformat(timespec="seconds"), "orders": list(reversed(ledger))[:200], "mark_error": err,
            "by_currency": {k: {kk: round(vv, 2) if isinstance(vv, float) else vv for kk, vv in v.items()}
                            for k, v in by_ccy.items()},
            "note": "Paper fills at the stop/target level when the last price crosses it; real fills can be worse."}


@router.post("/paper/{oid}/close")
async def paper_close(oid: str):
    async with _io_lock:
        ledger = load_ledger()
        rec = next((r for r in ledger if r.get("id") == oid), None)
        if not rec or rec.get("status") != "open":
            raise HTTPException(404, "No open paper position with that id")
        lp = (await last_prices([rec["symbol"]])).get(rec["symbol"])
        if lp is None:
            raise HTTPException(502, "Could not get a last price to close at")
        _close(rec, lp, "manual", load_config(rec.get("market", "NSE")))
        _write(LEDGER_FILE, ledger)
    return rec
