"""Market Intelligence Engine: turns one Signal Lab analysis (lab.analysis.analyze - the same engine the Lab screen
and the backtester use) into the agent's market picture, a data-quality verdict and de-correlated evidence.

Data quality (quality_gate): a trade may be considered only when ALL of these hold for the execution candles
  * closed candles only (no forming candle)          * not stale (last close <= 2 intervals + 2 min ago)
  * no gaps in the last 20 candles                   * not a fallback source (e.g. Delta standing in for Shark)
  * at least 300 candles                             * no impossible candles had to be removed in the last fetch
Failures are reported with the reason; the agent then stays in observation for that symbol.

Evidence families (evidence): the 10 Signal Lab score factors are not independent - HTF bias, ITF bias, execution
structure and the EMA stack all measure trend. They are grouped into families and each family votes once:
  trend          htf, itf, structure, ema
  momentum       momentum (RSI + MACD histogram)
  participation  vwap, volume (OBV)
  liquidity      liquidity (sweeps / MSS), zone (premium / discount)
  options        options (OI flow)
A family's vote is the sign of its members' weighted sum. If research.factor_correlation has measured the factors
on this symbol/timeframe, any factor whose measured |correlation| with another family's factor is >= 0.6 is moved
into that family (measured evidence beats the default grouping).
"""
from __future__ import annotations

import time

from backend.lab.candle import TF_SECONDS
from backend.lab import scoring as SC
from backend.lab import store

FAMILIES = {"trend": ["htf", "itf", "structure", "ema"], "momentum": ["momentum"], "participation": ["vwap", "volume"],
            "liquidity": ["liquidity", "zone"], "options": ["options"]}


def families_for(symbol: str, tf: str) -> dict:
    fam = {k: list(v) for k, v in FAMILIES.items()}
    corr = store.get_setting(f"agent_corr:{symbol}:{tf}") or {}
    owner = {f: k for k, v in fam.items() for f in v}
    for pair in corr.get("redundant", []):                # [[a, b, r], ...] measured |r| >= 0.6
        a, b = pair[0], pair[1]
        fa, fb = owner.get(a), owner.get(b)
        if fa and fb and fa != fb:
            # move the lighter factor into the heavier factor's family
            mover, target = (a, fb) if SC.WEIGHTS.get(a, 0) <= SC.WEIGHTS.get(b, 0) else (b, fa)
            fam[owner[mover]].remove(mover)
            fam[target].append(mover)
            owner[mover] = target
    return {k: v for k, v in fam.items() if v}


def evidence(score: dict, direction: str, symbol: str = "", tf: str = "") -> dict:
    d = 1 if direction == "LONG" else -1
    votes = {f["key"]: f["vote"] for f in score.get("factors", [])}
    out, agree, oppose, unavailable = {}, [], [], []
    for fam, members in families_for(symbol, tf).items():
        have = [(m, votes.get(m)) for m in members if votes.get(m) is not None]
        if not have:
            unavailable.append(fam)
            out[fam] = {"vote": None, "members": members}
            continue
        s = sum(SC.WEIGHTS[m] * v for m, v in have)
        vote = 1 if s > 0 else -1 if s < 0 else 0
        out[fam] = {"vote": vote, "members": [m for m, _ in have], "detail": {m: v for m, v in have}}
        if vote == d:
            agree.append(fam)
        elif vote == -d:
            oppose.append(fam)
    return {"families": out, "agree": agree, "oppose": oppose, "unavailable": unavailable,
            "independent_confirmations": len(agree), "independent_conflicts": len(oppose)}


def quality_gate(q: dict, tf: str, n_candles: int, now: float | None = None) -> dict:
    now = now or time.time()
    step = TF_SECONDS[tf]
    blocks = []
    if q.get("forming"):
        blocks.append("Last candle is still forming (decisions use closed candles only)")
    if q.get("stale"):
        blocks.append("Data is stale")
    as_of = q.get("as_of") or 0
    if as_of and now - as_of > 2 * step + 120 and q.get("stale") is not None:
        blocks.append(f"Last closed candle is {int((now - as_of) / 60)} min old (delayed feed)")
    if (q.get("recent_gaps") or 0) > 0:
        blocks.append(f"{q['recent_gaps']} gap(s) in the last 20 candles")
    if "fallback" in str(q.get("source", "")).lower():
        blocks.append(f"Fallback source in use: {q.get('source')}")
    if n_candles < 300:
        blocks.append(f"Only {n_candles} candles (need 300)")
    if (q.get("bad_removed") or 0) > 0:
        blocks.append(f"{q['bad_removed']} impossible candle(s) were removed from the feed")
    return {"ok": not blocks, "blocks": blocks, "source": q.get("source"), "as_of": as_of, "fetched_at": q.get("fetched_at")}


def picture(a: dict) -> dict:
    """Compact market picture from an analysis result. Every number here comes straight from the analysis (which
    carries its own source and timestamp)."""
    reg = a.get("regime") or {}
    ind = a.get("indicators") or {}
    ictr = a.get("ict") or {}
    der = a.get("derivatives") or {}
    ch = (der.get("chain") or {}) if der else {}
    perp = (der.get("perp") or {}) if der else {}
    pa = a.get("price_action") or {}
    unpredictable = reg.get("regime") in ("event", "unknown") or (reg.get("regime") == "choppy" and reg.get("volatility") == "high")
    opts = None
    if ch and not ch.get("error"):
        opts = {k: ch.get(k) for k in ("pcr", "max_pain", "oi_support", "oi_resistance", "atm_iv", "iv_rank", "oi_flow", "expected_move",
                                        "skew", "gex", "expiry", "spot") if k in ch}
    crypto = None
    if perp and not perp.get("error"):
        crypto = {k: perp.get(k) for k in ("funding_rate", "open_interest", "basis_pct", "mark", "index") if k in perp}
    return {
        "symbol": a.get("symbol"), "tf": a.get("tf"), "market": a.get("market"),
        "as_of": (a.get("quality") or {}).get("as_of"), "source": (a.get("quality") or {}).get("source"),
        "close": ind.get("close"),
        "regime": reg.get("regime"), "regime_label": reg.get("label"), "volatility": reg.get("volatility"),
        "regime_facts": reg.get("facts"), "unpredictable": unpredictable,
        "bias": (a.get("score") or {}).get("bias"), "bull": (a.get("score") or {}).get("bull"), "bear": (a.get("score") or {}).get("bear"),
        "mtf": a.get("mtf"),
        "trend": {"ema": ind.get("ema"), "supertrend": ind.get("supertrend"), "adx": ind.get("adx")},
        "momentum": {"rsi": ind.get("rsi"), "macd": ind.get("macd"), "mfi": ind.get("mfi")},
        "volatility_detail": {"atr": ind.get("atr"), "atr_pct": ind.get("atr_pct"), "hist_vol_pct": ind.get("hist_vol_pct"),
                              "bandwidth_pct": (ind.get("bollinger") or {}).get("bandwidth_pct"), "squeeze": ind.get("squeeze")},
        "vwap": ind.get("vwap"),
        "levels": a.get("levels"),
        "ict": {k: ictr.get(k) for k in ("dealing_range", "premium_discount", "kill_zone", "amd", "prev_levels", "equal_levels") if k in ictr},
        "price_action": {k: pa.get(k) for k in ("zones", "range", "volatility_state") if k in pa},
        "options": opts, "perp": crypto,
        "setups_seen": len(a.get("setups") or []), "ideas_rejected_by_lab": len(a.get("rejected") or []),
    }


def avg_volume(a: dict, n: int = 20) -> float | None:
    c = (a.get("candles") or [])[-n:]
    v = [x.get("volume") or 0 for x in c]
    return (sum(v) / len(v)) if v and any(v) else None
