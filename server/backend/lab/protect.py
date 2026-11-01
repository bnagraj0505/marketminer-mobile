"""Capital protection for PAPER trades (the lab never places real orders).

Rules (all settings, stored in the settings table under "protection"):
  capital              paper capital per currency (INR for India, USD for crypto/US)
  risk_pct             risk per trade as % of capital (default 0.5)
  daily_loss_pct       stop opening trades for the day after this loss (default 2)
  weekly_loss_pct      stop for the week (default 5)
  max_open             maximum open paper trades (default 3)
  max_correlated       maximum open trades in one correlation group (default 2); groups below
  loss_streak_lockout  consecutive losses that lock new trades until the next day (default 3)
  cooldown_min         minutes after a losing trade before the same symbol can be traded again (default 30) -
                       blocks revenge trading
  no averaging         a second trade in the same symbol and direction while one is open is refused
  min_score            minimum setup score for a paper trade (default 60)
Position size = risk amount / per-unit risk (entry - stop), rounded DOWN to the lot size; if that is zero the trade
is refused (the stop is too wide for the risk budget).
"""
from __future__ import annotations

import datetime as dt
import math

from . import store

DEFAULTS = {"capital_inr": 500000.0, "capital_usd": 10000.0, "risk_pct": 0.5, "daily_loss_pct": 2.0, "weekly_loss_pct": 5.0,
            "max_open": 3, "max_correlated": 2, "loss_streak_lockout": 3, "cooldown_min": 30, "min_score": 60}
GROUPS = {
    "india_index": ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX", "NIFTYBANK", "NIFTY50"],
    "crypto_majors": ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "LTC"],
    "us_index": ["^GSPC", "^NDX", "^IXIC", "^DJI", "SPY", "QQQ", "^RUT"],
    "energy": ["CRUDEOIL", "NATURALGAS", "CL=F", "NG=F"],
    "metals": ["GOLD", "SILVER", "GC=F", "SI=F", "XAU", "PAXG"],
}
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def settings() -> dict:
    return {**DEFAULTS, **(store.get_setting("protection") or {})}


def currency(market: str) -> str:
    return "INR" if market in ("NSE", "FNO", "MCX") else "USD"


def group_of(symbol: str) -> str:
    s = symbol.upper().split(":", 1)[-1]
    for g, names in GROUPS.items():
        if any(n in s for n in names):
            return g
    return "single:" + s


def lot_of(symbol: str) -> float:
    from .options import LOTS
    s = symbol.upper().split(":", 1)[-1]
    for k in sorted(LOTS, key=len, reverse=True):
        if s.startswith(k) and not s.endswith("-INDEX"):
            return LOTS[k]
    return 1.0


def size(symbol: str, market: str, entry: float, stop: float, cfg: dict | None = None) -> dict:
    c = cfg or settings()
    cap = c["capital_inr"] if currency(market) == "INR" else c["capital_usd"]
    risk_amt = cap * c["risk_pct"] / 100
    per = abs(entry - stop)
    if per <= 0:
        return {"qty": 0, "why": "zero risk per unit"}
    lot = lot_of(symbol)
    if market in ("SHARK", "DELTAF", "DELTA", "BINANCE", "YF") and lot == 1:
        q = math.floor(risk_amt / per * 1e4) / 1e4
    else:
        q = math.floor(risk_amt / per / lot) * lot
    if q <= 0:
        return {"qty": 0, "why": f"one lot ({lot:g}) risks {per * lot:,.0f} {currency(market)}, more than the {risk_amt:,.0f} budget"}
    return {"qty": q, "lot": lot, "risk_amount": round(q * per, 2), "budget": risk_amt, "currency": currency(market)}


def _day_start(now: int) -> int:
    d = dt.datetime.fromtimestamp(now, IST)
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=IST).timestamp())


def check(symbol: str, market: str, direction: str, score: float, now: int | None = None) -> dict:
    """May a new paper trade be opened? Returns {ok, blocks: [...], warnings: [...]}."""
    c = settings()
    now = now or store.now()
    cur = currency(market)
    cap = c["capital_inr"] if cur == "INR" else c["capital_usd"]
    blocks = []
    if score < c["min_score"]:
        blocks.append(f"Score {score:.0f} is below the minimum {c['min_score']}")
    open_ = store.q("SELECT * FROM trades WHERE status='open'")
    if len(open_) >= c["max_open"]:
        blocks.append(f"{len(open_)} trades already open (limit {c['max_open']})")
    if any(t["symbol"] == symbol and t["side"] == direction for t in open_):
        blocks.append("Already in this symbol and direction: averaging is not allowed")
    g = group_of(symbol)
    same = [t for t in open_ if group_of(t["symbol"]) == g]
    if not g.startswith("single:") and len(same) >= c["max_correlated"]:
        blocks.append(f"{len(same)} open trades in the correlated group {g} (limit {c['max_correlated']})")
    d0 = _day_start(now)
    w0 = d0 - dt.datetime.fromtimestamp(now, IST).weekday() * 86400
    day = store.q("SELECT pnl FROM trades WHERE status='closed' AND closed_at>=? AND currency=?", (d0, cur))
    week = store.q("SELECT pnl FROM trades WHERE status='closed' AND closed_at>=? AND currency=?", (w0, cur))
    dl, wl = sum(t["pnl"] or 0 for t in day), sum(t["pnl"] or 0 for t in week)
    if dl <= -cap * c["daily_loss_pct"] / 100:
        blocks.append(f"Daily loss limit hit ({dl:,.0f} {cur})")
    if wl <= -cap * c["weekly_loss_pct"] / 100:
        blocks.append(f"Weekly loss limit hit ({wl:,.0f} {cur})")
    recent = store.q("SELECT pnl, closed_at, symbol FROM trades WHERE status='closed' ORDER BY closed_at DESC LIMIT 20")
    streak = 0
    for t in recent:
        if (t["pnl"] or 0) < 0:
            streak += 1
        else:
            break
    if streak >= c["loss_streak_lockout"] and recent and recent[0]["closed_at"] >= d0:
        blocks.append(f"{streak} losses in a row: locked until tomorrow")
    last_loss = next((t for t in recent if t["symbol"] == symbol and (t["pnl"] or 0) < 0), None)
    if last_loss and now - last_loss["closed_at"] < c["cooldown_min"] * 60:
        blocks.append(f"Cooldown: last {symbol} trade lost {int((now - last_loss['closed_at']) / 60)} min ago (wait {c['cooldown_min']} min)")
    return {"ok": not blocks, "blocks": blocks, "daily_pnl": dl, "weekly_pnl": wl, "open": len(open_), "loss_streak": streak, "currency": cur}
