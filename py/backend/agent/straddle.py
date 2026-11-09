"""Straddle monitor: where the index straddle is and where it is shifting. Information only (no trading).

Every `interval_s` (60 s) during NSE hours, for each index in the options desk (NIFTY, BANKNIFTY, FINNIFTY, SENSEX ...),
one FYERS option-chain call (nearest expiry) is turned into a snapshot:
  atm             strike nearest the spot
  atm_straddle    ATM call + ATM put (mid of bid/ask, else last price)
  centre          the strike with the CHEAPEST straddle within +/-8 strikes = where option prices say the index is
                  centred (it follows the synthetic forward); a centre moving up/down = the market re-centring
  forward         put-call parity at the ATM strike: K + C - P
  breakevens      ATM -/+ straddle;  expected move = 0.85 x straddle
Snapshots are kept in .lab/lab.db (straddle_snapshots). shift() compares now with the first snapshot of the session and
with 15 and 30 minutes ago, and writes plain-language readings from those numbers only.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import time

from . import config as CFG
from . import db
from . import options as OP

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SCHEMA = """CREATE TABLE IF NOT EXISTS straddle_snapshots (und TEXT, t INTEGER, expiry TEXT, spot REAL, atm REAL, atm_straddle REAL,
  ce REAL, pe REAL, centre REAL, centre_straddle REAL, forward REAL, iv REAL, step REAL, payload TEXT, PRIMARY KEY (und, t));"""
STATUS: dict = {"running": False, "last": None, "errors": {}}
_task: asyncio.Task | None = None
_ready = None


def _init():
    global _ready
    from backend.lab import store
    if _ready == str(store.DB):
        return
    db.init()
    with store._lock, store.conn() as c:
        c.executescript(SCHEMA)
    _ready = str(store.DB)


def _mid(o: dict | None) -> float | None:
    if not o:
        return None
    b, a = o.get("bid") or 0, o.get("ask") or 0
    if b > 0 and a > 0 and a >= b:
        return (a + b) / 2
    return o.get("ltp") or None


def compute(ch: dict) -> dict | None:
    """Straddle numbers from one option chain (pure function)."""
    rows = [r for r in ch["strikes"] if r.get("CE") and r.get("PE")]
    if not rows:
        return None
    spot = ch["spot"]
    atm_i = min(range(len(rows)), key=lambda i: abs(rows[i]["strike"] - spot))
    band = rows[max(0, atm_i - 8): atm_i + 9]
    strad = []
    for r in band:
        c, p = _mid(r["CE"]), _mid(r["PE"])
        if c and p:
            strad.append((r["strike"], c + p, c, p))
    if not strad:
        return None
    atm = rows[atm_i]
    ce, pe = _mid(atm["CE"]), _mid(atm["PE"])
    if not ce or not pe:
        return None
    centre = min(strad, key=lambda x: x[1])
    ks = [r["strike"] for r in rows]
    step = min((b - a for a, b in zip(ks, ks[1:]) if b > a), default=0)
    ivs = [x for x in (atm["CE"].get("iv"), atm["PE"].get("iv")) if x]
    s = ce + pe
    return {"und": ch["underlying"], "expiry": ch.get("expiry"), "spot": spot, "atm": atm["strike"], "atm_straddle": s, "ce": ce, "pe": pe,
            "centre": centre[0], "centre_straddle": centre[1], "forward": atm["strike"] + ce - pe, "iv": sum(ivs) / len(ivs) if ivs else None,
            "step": step, "be_low": atm["strike"] - s, "be_high": atm["strike"] + s, "expected_move": 0.85 * s,
            "curve": [{"strike": k, "straddle": round(v, 2)} for k, v, _, _ in strad], "days": ch.get("days"), "as_of": ch.get("as_of"),
            "oi": {f"{r['strike']:g}": [(r.get("CE") or {}).get("oi") or 0, (r.get("PE") or {}).get("oi") or 0] for r in ch["strikes"]}}


def save(x: dict, t: int | None = None) -> None:
    _init()
    db.execute("INSERT OR REPLACE INTO straddle_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (x["und"], t or int(time.time()), x.get("expiry"), x["spot"], x["atm"], x["atm_straddle"], x["ce"], x["pe"], x["centre"],
                x["centre_straddle"], x["forward"], x.get("iv"), x.get("step"), json.dumps({"curve": x.get("curve"), "days": x.get("days"), "oi": x.get("oi")})))


async def snapshot(und: str) -> dict | None:
    ch = await OP.chain(und, 0.0, 12)
    x = compute(ch)
    if x:
        save(x)
    return x


def _session_bounds(now: float) -> tuple[int, int]:
    d = dt.datetime.fromtimestamp(now, IST)
    d0 = int(dt.datetime(d.year, d.month, d.day, tzinfo=IST).timestamp())
    return d0, d0 + 86400


def _pct(a, b):
    return (b - a) / a * 100 if a else None


def shift(und: str, now: float | None = None) -> dict:
    """Now vs the session's first snapshot and vs 15 / 30 minutes ago, with readings. Uses the latest session on file."""
    _init()
    now = now or time.time()
    last = db.q("SELECT t FROM straddle_snapshots WHERE und=? ORDER BY t DESC LIMIT 1", (und,), one=True)
    if not last:
        return {"und": und, "available": False, "note": "No straddle snapshots yet: they are recorded every minute while NSE is open (needs the FYERS login)."}
    d0, d1 = _session_bounds(last["t"])
    rows = db.q("SELECT * FROM straddle_snapshots WHERE und=? AND t>=? AND t<? ORDER BY t", (und, d0, d1))
    cur, first = rows[-1], rows[0]

    def ago(m):
        target = cur["t"] - m * 60
        cand = [r for r in rows if r["t"] <= target]
        return cand[-1] if cand else None
    a15, a30 = ago(15), ago(30)
    step = cur["step"] or 1
    p = db.jload(cur["payload"], {}) or {}

    def mv(old):
        if not old:
            return None
        return {"t": old["t"], "centre_from": old["centre"], "centre_shift": cur["centre"] - old["centre"],
                "centre_shift_strikes": round((cur["centre"] - old["centre"]) / step), "straddle_from": old["atm_straddle"],
                "straddle_chg_pct": _pct(old["atm_straddle"], cur["atm_straddle"]), "spot_from": old["spot"],
                "spot_chg_pct": _pct(old["spot"], cur["spot"]), "iv_from": old["iv"], "iv_chg": (cur["iv"] - old["iv"]) if cur["iv"] and old["iv"] else None,
                "forward_shift": cur["forward"] - old["forward"]}
    since_open, m15, m30 = mv(first), mv(a15), mv(a30)
    reads = []
    if since_open:
        k = since_open["centre_shift_strikes"]
        if k:
            reads.append(f"Straddle centre has shifted {'UP' if k > 0 else 'DOWN'} {abs(k)} strike(s) since "
                         f"{dt.datetime.fromtimestamp(first['t'], IST):%H:%M} ({first['centre']:g} → {cur['centre']:g}): option prices have re-centred "
                         f"{'higher - bullish drift' if k > 0 else 'lower - bearish drift'}.")
        else:
            reads.append(f"Straddle centre unchanged at {cur['centre']:g} since {dt.datetime.fromtimestamp(first['t'], IST):%H:%M}: no re-centring (range-like).")
        sc, sp = since_open["straddle_chg_pct"], since_open["spot_chg_pct"]
        if sc is not None:
            if sc <= -10 and abs(sp or 0) < 0.4:
                reads.append(f"ATM straddle {sc:+.1f}% since open with the index almost flat ({sp:+.2f}%): time decay / IV falling - favours option sellers, works against buyers.")
            elif sc >= 8:
                reads.append(f"ATM straddle {sc:+.1f}% since open: premiums expanding (IV up or a fast move) - caution for sellers.")
            else:
                reads.append(f"ATM straddle {sc:+.1f}% since open (index {sp:+.2f}%).")
        if cur["spot"] > first["atm"] + first["atm_straddle"] or cur["spot"] < first["atm"] - first["atm_straddle"]:
            reads.append(f"Index {cur['spot']:,.0f} is OUTSIDE the opening straddle's breakevens ({first['atm'] - first['atm_straddle']:,.0f} – "
                         f"{first['atm'] + first['atm_straddle']:,.0f}): the opening range priced by options has been broken - short straddles are losing.")
        else:
            reads.append(f"Index is inside the opening straddle's breakevens ({first['atm'] - first['atm_straddle']:,.0f} – {first['atm'] + first['atm_straddle']:,.0f}).")
    if m15:
        k = m15["centre_shift_strikes"]
        s15 = m15["straddle_chg_pct"]
        reads.append(f"Last 15 min: centre {'up ' + str(k) if k > 0 else 'down ' + str(-k) if k < 0 else 'flat'}"
                     f"{' strike(s)' if k else ''}, straddle {s15:+.1f}%" + (" - premiums rising" if (s15 or 0) >= 3 else " - premiums decaying" if (s15 or 0) <= -3 else "") + ".")
    skew = (cur["ce"] - cur["pe"]) / cur["atm_straddle"] * 100 if cur["atm_straddle"] else 0
    reads.append(f"ATM call {cur['ce']:,.1f} vs put {cur['pe']:,.1f} ({'calls' if skew > 0 else 'puts'} richer by {abs(skew):.0f}% of the straddle; "
                 f"forward {cur['forward']:,.1f} vs spot {cur['spot']:,.1f}).")
    series = [{"t": r["t"], "straddle": round(r["atm_straddle"], 2), "centre": r["centre"], "spot": round(r["spot"], 2), "atm": r["atm"]} for r in rows]
    live = now - cur["t"] < 180
    return {"und": und, "available": True, "live": live, "session": dt.datetime.fromtimestamp(cur["t"], IST).date().isoformat(),
            "as_of": cur["t"], "expiry": cur["expiry"], "spot": cur["spot"], "atm": cur["atm"], "atm_straddle": cur["atm_straddle"], "ce": cur["ce"],
            "pe": cur["pe"], "centre": cur["centre"], "centre_straddle": cur["centre_straddle"], "forward": cur["forward"], "iv": cur["iv"],
            "step": step, "be_low": cur["atm"] - cur["atm_straddle"], "be_high": cur["atm"] + cur["atm_straddle"],
            "expected_move": 0.85 * cur["atm_straddle"], "curve": p.get("curve"), "days": p.get("days"),
            "open": {"t": first["t"], "centre": first["centre"], "atm": first["atm"], "atm_straddle": first["atm_straddle"], "spot": first["spot"]},
            "since_open": since_open, "last_15m": m15, "last_30m": m30, "readings": reads, "series": series[-400:],
            "source": "FYERS option chain (nearest expiry), mid of bid/ask", "snapshots": len(rows)}


def nse_open(now: float) -> bool:
    t = dt.datetime.fromtimestamp(now, IST)
    m = t.hour * 60 + t.minute
    return t.weekday() < 5 and 555 <= m <= 930


def indices() -> list[str]:
    return list((CFG.agent().get("options") or {}).get("index") or ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"])


async def run_once() -> dict:
    out = {}
    for und in indices():
        try:
            x = await snapshot(und)
            out[und] = bool(x)
            STATUS["errors"].pop(und, None)
        except Exception as exc:                          # noqa: BLE001 - no login / no data: report, keep going
            STATUS["errors"][und] = str(getattr(exc, "detail", exc))[:160]
        await asyncio.sleep(0.4)
    STATUS["last"] = int(time.time())
    return out


async def _loop(interval_s: int = 60) -> None:
    STATUS["running"] = True
    await asyncio.sleep(30)
    while True:
        if nse_open(time.time()):
            try:
                await run_once()
            except Exception:                             # noqa: BLE001
                pass
        await asyncio.sleep(budget_interval(interval_s))


def budget_interval(base: int = 60) -> int:
    """Each snapshot costs 1-2 option-chain requests per index, which on the FYERS free plan (5,000 data requests a
    day) was most of the day's budget at one a minute. Every minute only while < 25% of the day is used, then every
    3 / 5 / 10 minutes at 25% / 50% / 75%."""
    try:
        from backend.brokers.fyers.client import day_used, _PER_DAY
        f = day_used() / (_PER_DAY or 5000)
    except Exception:                                     # noqa: BLE001
        return base
    return base if f < 0.25 else 180 if f < 0.5 else 300 if f < 0.75 else 600


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


def report() -> dict:
    return {"indices": [shift(u) for u in indices()], "status": {"recording": STATUS["running"], "last": STATUS["last"],
            "errors": STATUS["errors"], "market_open": nse_open(time.time())}}
