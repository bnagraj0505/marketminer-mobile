"""Agent configuration: operating mode, risk policy, watch-list.

Stored in the Signal Lab settings table under "agent" (risk under "agent_risk"). Only the user can change these,
through the localhost-only POST /api/agent/config. No agent code path writes to them: research.py may only change
*strategy parameters* and only after the user approves an experiment.

Risk policy defaults are the conservative paper-trading policy from MARKETMINER_RISK_POLICY.md. They are not an
assumption about the user's real capital; capital_inr / capital_usd are PAPER equity.
"""
from __future__ import annotations

import time

from backend.lab import store

MODES = ("intelligence", "paper", "live")

RISK_DEFAULTS = {
    "capital_inr": 1000000.0,         # paper equity for NSE/BSE/MCX (10 lakh; raised from 5 lakh on 2026-11-04)
    "capital_usd": 10000.0,           # paper equity for crypto
    # ---- INR book (2026-11-04): ONE fixed rule, the rest is decided by the AI risk engine (airisk.py) ----
    "unlimited_trades": True,         # 2026-11-10 user rule (paper): NO trade-count / loss-lockout rules (max open, per group,
                                      # day / weekly loss stops, AI day stop, loss-streak lockout, cooldown, part risk budget).
                                      # The agent trades as often as setups qualify; the capital allocated per day is the limit.
    "usd_day_capital": 2500.0,        # USD (crypto) book: capital in open positions (notional) never exceeds this
    "inr_day_capital": 200000.0,      # FIXED: capital the agent may use per day: open INR positions' capital (premium paid,
                                      # hedged-spread margin = worst-case loss, cash notional) never exceeds this
    "ai_base_risk_pct": 2.5,          # AI model: starting per-trade risk = 2.5% of the day capital, then scaled by
    "ai_min_risk_pct": 0.5,           #   confidence / track record / today / volatility, kept within 0.5%..5% of it
    "ai_max_risk_pct": 5.0,
    "ai_day_loss_trades": 3.0,        # AI day stop = 3 x base risk x track-record factor (between 1 base and 10% of day capital)
    "ai_profit_lock": 0.5,            # AI: close the day after giving back 50% of a peak day profit (>= 2 x base)
    # ---- capital rules for the USD (crypto) book ----
    "capital_parts": 4,               # divide equity into 4 parts; the agent trades with ONE part
    "part_risk_pct": 2.0,             # risk budget = 2% of that part, shared by all open trades (+ today's realised loss)
    "risk_slices": 2,                 # trades the budget is split across -> per-trade risk = budget / 2 (1% of the part)
    "lots_per_trade": 1,              # MINIMUM lots (2026-11-04: no lot rule - the agent sizes as many lots as fit the risk;
                                      # 2+ lots: half book at 1:2, rest trail; 1 lot: stop to breakeven at 1:2, then trails)
    "first_target_r": 2.0,            # lot 1 target in R (after costs)
    "trail_atr": 1.5,                 # futures / crypto runner: trail this x ATR behind the best price after lot 1 books
    "risk_pct": 0.25,                 # DERIVED: part_risk_pct / capital_parts / risk_slices (% of equity per trade)
    "daily_loss_pct": 0.75,           # USD book: stop opening trades for the day after this realised loss (INR: AI day stop, airisk.py)
    "weekly_loss_pct": 2.0,           # stop for the week
    "max_drawdown_pct": 5.0,          # HALT (until the user resets) when equity is this far below its peak
    "max_open": 4,                    # hard cap on concurrent positions; the part risk budget is the real limit (a runner at
                                      # breakeven carries no risk, so new trades can open while you are in profit)
    "max_per_group": 1,               # per correlation group (Indian indices, crypto majors, ...)
    "min_rr": 2.0,                    # reward:risk to T2, net of costs, against the full maximum loss
    "min_score": 60.0,                # Signal Lab setup score (a rank, not a probability)
    "min_independent_evidence": 2,    # evidence families agreeing with the trade (correlated factors count once)
    "max_opposing_evidence": 1,       # evidence families against the trade
    "loss_streak_lockout": 3,         # consecutive losing trades -> no new trades until the next IST day
    "derisk_after_losses": 2,         # after this many consecutive losses risk_pct is halved (never increased)
    "cooldown_min": 60,               # minutes before the same symbol may be traded again after a loss
    "max_notional_x": 4.0,            # position notional <= this x equity (leverage cap)
    "max_cost_share": 0.25,           # fees + slippage must be <= 25% of the maximum loss
    "gap_allowance_atr": 0.25,        # extra adverse move assumed beyond the stop (gaps / fast markets)
    "max_volume_share": 0.01,         # qty <= 1% of the average candle volume (price impact), when volume exists
    "option_premium_risk_x": 3.0,     # options: worst case at expiry (verified payoff) <= this x the per-trade risk
    "india_no_entry_last_min": 20,    # no new Indian intraday entries in the last N minutes of the session
    "time_stop_x": 3.0,               # exit after this x the setup's expected holding period
    "require_validated": False,       # paper: unvalidated strategies allowed (tagged experimental). Live: always required.
    # ---- options (options.py) ----
    "opt_max_leg_spread_pct": 5.0,    # index options: each leg's bid/ask spread <= this % of its mid
    "opt_max_leg_spread_pct_stock": 10.0,   # stock options
    "opt_min_dte_sell": 1.0,          # no option SELLING structure with less than this many days to expiry (no expiry-day selling)
    "opt_last_entry_min": 870,        # no new option positions after 14:30 IST (minutes after midnight)
    "opt_long_stop_pct": 50.0,        # long option / debit spread: the stop is never wider than this % of the premium
    "opt_min_stop_pct": 15.0,         # ... nor tighter than this % (R = |net delta| x underlying stop distance + spread)
    "opt_sell_stop_frac": 0.35,       # credit structures: stop when the loss reaches 35% of the credit (1:2 after costs ~80% captured)
    "opt_trail_r": 1.0,               # options runner: trail 1R behind the best value after lot 1 books
    "opt_wall_writing_ratio": 1.3,    # seller's lens: one side's fresh writing must exceed the other's by 1.3x to count as a view
    "opt_long_tp_pct": 120.0,         # long option: take profit at +120% (2.4R gross vs the 50% stop, so >= 2R after fees)
    "opt_credit_tp_pct": 50.0,        # credit structures: take profit when 50% of the credit is captured
    "opt_min_credit_ratio": 0.25,     # credit structures: credit >= 25% of the spread width
    "opt_iv_high_rank": 60.0, "opt_iv_low_rank": 30.0,
    "opt_short_delta": 0.30,          # short strike of credit spreads / far leg of debit spreads
    "opt_condor_delta": 0.20,         # iron condor short strikes
    "opt_wing_strikes": 2,            # protective wing distance in strikes
    "opt_max_chain_age_s": 180,       # option prices older than this are not used for a decision
}

RISK_LIMITS = {                       # user-settable ranges (clamped); none of these can be loosened past the bound
    "capital_inr": (10000, 1e9), "capital_usd": (100, 1e8), "risk_pct": (0.01, 1.0),
    "inr_day_capital": (10000, 1e8), "usd_day_capital": (10, 1e8), "ai_base_risk_pct": (0.25, 10.0), "ai_min_risk_pct": (0.1, 5.0), "ai_max_risk_pct": (0.5, 20.0),
    "ai_day_loss_trades": (1.0, 10.0), "ai_profit_lock": (0.2, 1.0),
    "capital_parts": (1, 10), "part_risk_pct": (0.25, 5.0), "risk_slices": (1, 6), "lots_per_trade": (1, 1), "first_target_r": (1.0, 5.0),
    "trail_atr": (0.5, 5.0), "opt_min_stop_pct": (5.0, 40.0), "opt_sell_stop_frac": (0.2, 0.45), "opt_trail_r": (0.5, 3.0),
    "opt_wall_writing_ratio": (1.0, 3.0), "daily_loss_pct": (0.1, 3.0),
    "weekly_loss_pct": (0.25, 6.0), "max_drawdown_pct": (1.0, 15.0), "max_open": (1, 8), "max_per_group": (1, 3),
    "min_rr": (1.5, 5.0), "min_score": (45, 100), "min_independent_evidence": (1, 5), "max_opposing_evidence": (0, 3),
    "loss_streak_lockout": (1, 6), "derisk_after_losses": (1, 6), "cooldown_min": (0, 1440), "max_notional_x": (0.5, 10.0),
    "max_cost_share": (0.05, 0.5), "gap_allowance_atr": (0.0, 2.0), "max_volume_share": (0.0001, 0.1),
    "option_premium_risk_x": (1.0, 4.0), "india_no_entry_last_min": (0, 120), "time_stop_x": (1.0, 10.0),
    "opt_max_leg_spread_pct": (0.5, 15.0), "opt_max_leg_spread_pct_stock": (1.0, 25.0), "opt_min_dte_sell": (0.5, 30.0),
    "opt_last_entry_min": (570, 900), "opt_long_stop_pct": (20.0, 80.0), "opt_long_tp_pct": (30.0, 400.0), "opt_credit_tp_pct": (20.0, 90.0),
    "opt_min_credit_ratio": (0.1, 0.6), "opt_iv_high_rank": (40.0, 95.0), "opt_iv_low_rank": (5.0, 50.0), "opt_short_delta": (0.1, 0.45),
    "opt_condor_delta": (0.05, 0.35), "opt_wing_strikes": (1, 6), "opt_max_chain_age_s": (30, 600),
}

AGENT_DEFAULTS = {
    "enabled": False,                 # the loop starts only when the user presses Start
    "mode": "paper",                  # intelligence | paper   (live is refused)
    "tf": "15m",
    "symbols": ["SHARK:BTCUSDT", "SHARK:ETHUSDT"],   # direct watch-list (crypto etc.). Indices are traded ONLY as options (options desk)
    "loop_seconds": 60,
    "research_enabled": False,        # nightly research run (backtests); off until the user turns it on
    "research_hour_ist": 1,
    "llm_reviews": False,             # LLM narrative for trade reviews, if AI is configured in .env
    "options": {                      # options desk (options.py): index + stock options, buying and hedged selling
        "enabled": True,
        "index": ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"],
        "stocks": ["RELIANCE", "HDFCBANK", "SBIN", "INFY"],
        "scan_all": True,             # also scan every NSE F&O stock (cheap quotes pre-filter, then the top movers)
        "scan_top": 5,                # movers analysed per candle from the full F&O scan
        "structures": {"long_option": True, "debit_spread": True, "credit_spread": True, "iron_condor": True},
    },
    # Market Intelligence sweep (sweep.py): OBSERVE ONLY - all option indices, every F&O stock, MCX and crypto
    "intel_sweep": {"enabled": True},
}

# markets the agent may PAPER-trade; YF (unofficial Yahoo) and DELTA options are analysed but never traded
TRADABLE_MARKETS = ("NSE", "FNO", "MCX", "SHARK", "DELTAF", "BINANCE")


def _clamp(k: str, v):
    if isinstance(v, bool) or k not in RISK_LIMITS:
        return v
    lo, hi = RISK_LIMITS[k]
    v = float(v)
    v = min(hi, max(lo, v))
    return int(v) if isinstance(RISK_DEFAULTS[k], int) and not isinstance(RISK_DEFAULTS[k], bool) else v


def _clean_options(o: dict) -> dict:
    from .options import INDEX_SPOT
    d = AGENT_DEFAULTS["options"]
    names = lambda xs: [str(x).strip().upper() for x in (xs or []) if str(x).strip()]
    return {"enabled": bool(o.get("enabled", d["enabled"])),
            "index": [x for x in names(o.get("index", d["index"])) if x in INDEX_SPOT],
            "stocks": [x for x in names(o.get("stocks", d["stocks"])) if x not in INDEX_SPOT][:30],
            "scan_all": bool(o.get("scan_all", d["scan_all"])),
            "scan_top": int(min(15, max(0, int(o.get("scan_top", d["scan_top"]))))),
            "structures": {k: bool((o.get("structures") or {}).get(k, True)) for k in d["structures"]}}


def risk() -> dict:
    r = {**RISK_DEFAULTS, **(store.get_setting("agent_risk") or {})}
    r["risk_pct"] = round(r["part_risk_pct"] / r["capital_parts"] / r["risk_slices"], 4)   # derived from your rules
    return r


INDEX_NAMES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX", "NIFTYNXT50", "NIFTY50", "NIFTYBANK", "SENSEX50")


def index_of(symbol: str) -> str | None:
    """'NSE:NIFTY-FUT', 'NSE:NIFTY50-INDEX', 'BSE:SENSEX-FUT', 'NSE:BANKNIFTY26NOVFUT' ... -> the index name; else None."""
    s = str(symbol).upper()
    if s.split(":", 1)[0] not in ("NSE", "BSE"):
        return None
    body = s.split(":", 1)[-1]
    if body.endswith("-EQ"):
        return None
    alias = {"NIFTY50": "NIFTY", "NIFTYBANK": "BANKNIFTY", "SENSEX50": "SENSEX"}
    for k in sorted(INDEX_NAMES, key=len, reverse=True):
        if body.startswith(k):
            return alias.get(k, k)
    return None


def _split_index_symbols(symbols: list[str], opts: dict) -> tuple[list[str], dict]:
    """Your rule: indices are always traded through OPTIONS. Index futures / spot in the direct watch-list are removed and
    their index is added to the options desk instead."""
    from .options import INDEX_SPOT
    keep, idx = [], list(opts.get("index") or [])
    for s in symbols:
        n = index_of(s)
        if n:
            if n in INDEX_SPOT and n not in idx:
                idx.append(n)
        else:
            keep.append(s)
    return keep, {**opts, "index": idx}


def agent() -> dict:
    c = {**AGENT_DEFAULTS, **(store.get_setting("agent") or {})}
    c["options"] = _clean_options({**AGENT_DEFAULTS["options"], **(c.get("options") or {})})
    c["symbols"], c["options"] = _split_index_symbols(c.get("symbols") or [], c["options"])
    if c.get("mode") not in ("intelligence", "paper"):
        c["mode"] = "intelligence"            # live can never be loaded from storage
    return c


def emergency() -> dict:
    return store.get_setting("agent_emergency") or {"on": False}


def save(agent_part: dict | None = None, risk_part: dict | None = None, who: str = "user") -> dict:
    """Validated save. Every change is versioned in agent_config_history (rollback = save the old values)."""
    from . import db
    if agent_part:
        cur = agent()
        new = dict(cur)
        for k, v in agent_part.items():
            if k not in AGENT_DEFAULTS:
                continue
            if k == "mode":
                if v == "live":
                    raise PermissionError("Live mode is disabled in this build: " + "; ".join(live_readiness()["missing"]))
                if v not in ("intelligence", "paper"):
                    raise ValueError("mode must be intelligence or paper")
            if k == "symbols":
                v = [str(s).strip().upper() for s in v if str(s).strip()][:20]
            if k == "tf" and v not in ("5m", "15m", "30m", "1h"):
                raise ValueError("agent timeframe must be 5m, 15m, 30m or 1h")
            if k == "loop_seconds":
                v = int(min(600, max(30, int(v))))
            if k == "options":
                v = _clean_options({**cur.get("options", {}), **(v or {})})
            if k == "intel_sweep":
                v = {"enabled": bool((v or {}).get("enabled", True))}
            new[k] = v
        new["symbols"], new["options"] = _split_index_symbols(new.get("symbols") or [], new.get("options") or {})   # indices -> options only
        db.config_change(who, "agent", cur, new)
        store.set_setting("agent", new)
    if risk_part:
        cur = risk()
        new = dict(cur)
        for k, v in risk_part.items():
            if k in RISK_DEFAULTS:
                new[k] = _clamp(k, v)
        db.config_change(who, "agent_risk", cur, new)
        store.set_setting("agent_risk", new)
    return {"agent": agent(), "risk": risk()}


def set_emergency(on: bool, reason: str = "") -> dict:
    v = {"on": bool(on), "reason": reason, "t": int(time.time())}
    store.set_setting("agent_emergency", v)
    return v


def live_readiness() -> dict:
    """Mode 3 checklist. Live execution stays off while anything here is missing - and in this build the broker
    adapter itself is missing, so it is always off."""
    from . import performance as PF
    p = PF.summary(mode="paper")
    regimes = {k for k, v in (p.get("by_regime") or {}).items() if v.get("trades", 0) >= 10}
    missing = ["No broker order adapter: the FYERS app is registered as data-only and MarketMiner has no order code",
               "No separately configured live capital limit",
               "Broker order-management tests (placement, partial fills, reconciliation) not run against a broker sandbox"]
    if (p.get("trades") or 0) < 200:
        missing.append(f"Paper sample too small: {p.get('trades') or 0} closed agent trades (need 200+)")
    if len(regimes) < 3:
        missing.append(f"Paper results cover {len(regimes)} regime(s) with 10+ trades (need 3+)")
    if (p.get("expectancy_r_ci95") or [None, None])[0] is None or p["expectancy_r_ci95"][0] <= 0:
        missing.append("Paper expectancy's 95% confidence interval is not above zero")
    if (p.get("max_drawdown_pct") or 0) > risk()["max_drawdown_pct"]:
        missing.append("Paper drawdown exceeded the policy limit")
    return {"live_allowed": False, "missing": missing}


def migrate_capital_10l() -> None:
    """One-time (2026-11-04): the user added 5 lakh to the INR paper capital (5L -> 10L), kept a 2L per-day capital limit
    and handed the stop, per-day loss and lot count to the AI risk engine. A capital_inr / lots_per_trade saved earlier
    would override the new defaults, so they are set once here; later changes in Settings are respected."""
    if store.get_setting("agent_mig_ai_risk"):
        return
    saved = store.get_setting("agent_risk") or {}
    if saved:
        save(risk_part={"capital_inr": 1000000.0, "inr_day_capital": 200000.0, "lots_per_trade": 1},
             who="user request: INR capital 10L, 2L/day capital, AI decides stop / day loss / lots")
    store.set_setting("agent_mig_ai_risk", {"done": True})


def migrate_add_indices() -> None:
    """One-time (2026-11-03): add MIDCPNIFTY and BANKEX to the options desk, as the user asked. Runs once; removing
    them again in Settings afterwards is respected."""
    if store.get_setting("agent_mig_midcp_bankex"):
        return
    idx = list((agent().get("options") or {}).get("index") or [])
    add = [x for x in ("MIDCPNIFTY", "BANKEX") if x not in idx]
    if add:
        save({"options": {"index": idx + add}}, who="user request: add MIDCPNIFTY, BANKEX")
    store.set_setting("agent_mig_midcp_bankex", {"done": True})
