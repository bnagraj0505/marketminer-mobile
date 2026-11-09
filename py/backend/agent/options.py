"""Options desk for the agent: index options (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX, BANKEX) and NSE stock
options. PAPER ONLY. Option BUYING and DEFINED-RISK option SELLING (every short leg has a long wing); naked selling is
refused by the Risk Guardian.

Think like a seller first (seller_lens): call-writing wall, put-writing wall, fresh OI writing near the money, PCR,
ATM straddle. Then the structure (choose):
  writers positioned against the direction  -> no trade
  IV high   -> SELL: credit spread, short strike at the writers' wall on the supporting side, wing opt_wing_strikes out
  IV low    -> BUY: ~0.55-delta call/put, only if the underlying's 1:2 point is short of the writers' wall
  IV normal -> BUY a debit spread: long ~0.50 delta, SELL the strike at the writers' wall
  writers boxing a range (index only) -> iron condor, short strikes at or beyond the two walls
  IV state: IV rank from MarketMiner's own chain snapshots, else ATM IV vs historical volatility. Selling needs
  >= opt_min_dte_sell days to expiry.

Prices: real FYERS quotes - BUY at the ask, SELL at the bid. Legs need two-sided quotes, tight spreads and open interest.
Payoff verification (payoff): expiry P&L on a grid through every strike; worst point = structural maximum loss; a
structure whose loss keeps growing above the highest strike is UNBOUNDED and refused.

Lots (2026-11-04, no lot rule): as many lots as fit the AI's per-trade risk and the day capital left (min 1). R per unit = |net delta| x underlying stop distance + spread
(buys) or opt_sell_stop_frac x credit (sells). Lot 1 books at 1:2 AFTER costs; lot 2's stop goes to breakeven and trails
opt_trail_r x R behind the best value. Exits also on the underlying's stop, a short strike breached, and 15:15 IST.
"""
from __future__ import annotations

import datetime as dt
import math
import time

from backend.lab import costs as C
from backend.lab import options as LO

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
INDEX_SPOT = {"NIFTY": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX", "FINNIFTY": "NSE:FINNIFTY-INDEX",
              "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX", "SENSEX": "BSE:SENSEX-INDEX", "BANKEX": "BSE:BANKEX-INDEX"}
NON_STOCK = set(INDEX_SPOT) | {"NIFTYFPI", "NIFTYNXT50", "SENSEX50"}
CREDIT = ("credit_spread", "iron_condor")
LABEL = {"long_option": "Long option", "debit_spread": "Debit spread", "credit_spread": "Credit spread", "iron_condor": "Iron condor"}


class OptionsUnavailable(Exception):
    pass


def spot_symbol(underlying: str) -> str:
    u = underlying.upper()
    return INDEX_SPOT.get(u) or f"NSE:{u}-EQ"


def is_index(underlying: str) -> bool:
    return underlying.upper() in INDEX_SPOT


def underlying_of_symbol(symbol: str) -> str | None:
    s = symbol.upper()
    for k, v in INDEX_SPOT.items():
        if v == s:
            return k
    if s.startswith("NSE:") and s.endswith("-EQ"):
        return s[4:-3]
    return None


# ---------------------------------------------------------------- data
async def quotes(symbols: list[str]) -> dict[str, dict]:
    """FYERS quotes: {symbol: {lp, bid, ask, chp, volume}} for up to any number of symbols (50 per request)."""
    from backend.brokers.fyers.client import data_get
    out: dict[str, dict] = {}
    syms = list(dict.fromkeys(symbols))
    for i in range(0, len(syms), 50):
        p = await data_get("/quotes", {"symbols": ",".join(syms[i:i + 50])})
        for row in p.get("d") or []:
            v = row.get("v") or {}
            if row.get("s") == "ok":
                out[str(row.get("n")).upper()] = {"lp": _f(v.get("lp")), "bid": _f(v.get("bid")), "ask": _f(v.get("ask")),
                                                  "chp": _f(v.get("chp")), "volume": _f(v.get("volume")), "t": int(time.time())}
    return out


def _f(x):
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


async def chain(underlying: str, min_days: float = 0.0, strikes: int = 15) -> dict:
    """Real FYERS option chain for the first expiry at least `min_days` away, with lot and tick from the contract master."""
    from backend.brokers.fyers.client import data_get
    from backend.markets import fyers_master as FM
    sym = spot_symbol(underlying)
    first = await data_get("/options-chain-v3", {"symbol": sym, "strikecount": strikes, "timestamp": "", "greeks": "1"})
    d = first.get("data") or first
    now = time.time()
    exps = sorted(d.get("expiryData") or [], key=lambda e: int(e.get("expiry") or 0))
    exps = [e for e in exps if (int(e["expiry"]) + 10 * 3600 - now) / 86400 >= min_days]
    if not exps:
        raise OptionsUnavailable(f"no {underlying} expiry at least {min_days:g} days away")
    pick = exps[0]
    if str(pick.get("expiry")) != str(((d.get("expiryData") or [{}])[0]).get("expiry")):
        d = (await data_get("/options-chain-v3", {"symbol": sym, "strikecount": strikes, "timestamp": str(pick["expiry"]), "greeks": "1"})).get("data") or {}
    rows = d.get("optionsChain") or []
    spot = next((float(r.get("ltp") or 0) for r in rows if float(r.get("strike_price") or 0) <= 0), 0.0)
    by: dict[float, dict] = {}
    for r in rows:
        if r.get("option_type") not in ("CE", "PE"):
            continue
        g = r.get("greeks") or {}
        k = float(r["strike_price"])
        by.setdefault(k, {"strike": k})[r["option_type"]] = {
            "symbol": str(r.get("symbol") or "").upper(), "type": r["option_type"], "strike": k, "ltp": _f(r.get("ltp")) or 0,
            "bid": _f(r.get("bid")) or 0, "ask": _f(r.get("ask")) or 0, "oi": _f(r.get("oi")) or 0, "vol": _f(r.get("volume")) or 0,
            "iv": _f(g.get("iv")) or 0, "delta": _f(g.get("delta")) or 0, "theta": _f(g.get("theta")) or 0, "vega": _f(g.get("vega")) or 0,
            "gamma": _f(g.get("gamma")) or 0, "oi_chg": _f(r.get("oich")) or 0}
    if not by or not spot:
        raise OptionsUnavailable(f"FYERS returned an empty {underlying} chain")
    any_sym = next(v[s]["symbol"] for v in by.values() for s in ("CE", "PE") if s in v)
    c = await FM.contract(any_sym)
    lot = (c or {}).get("lot") or LO.LOTS.get(underlying.upper()) or 1
    tick = (c or {}).get("tick") or 0.05
    days = max(0.0, (int(pick["expiry"]) + 10 * 3600 - now) / 86400)
    st = [by[k] for k in sorted(by)]
    atm = min(st, key=lambda r: abs(r["strike"] - spot))
    ivs = [x for x in ((atm.get("CE") or {}).get("iv"), (atm.get("PE") or {}).get("iv")) if x]
    atm_iv = sum(ivs) / len(ivs) if ivs else None
    return {"underlying": underlying.upper(), "spot": spot, "expiry": pick.get("date"), "expiry_ts": int(pick["expiry"]), "days": days,
            "lot": lot, "tick": tick, "strikes": st, "atm_strike": atm["strike"], "atm_iv": atm_iv,
            "iv_rank": LO.iv_rank(underlying.upper(), atm_iv), "as_of": int(now), "source": "FYERS option chain + quotes"}


async def fno_stocks() -> list[str]:
    from backend.markets import fyers_master as FM
    return [u["underlying"] for u in await FM.underlyings("NSE") if u["underlying"] not in NON_STOCK and u.get("options")]


async def scan_movers(exclude: list[str], top: int) -> list[dict]:
    """Cheap pre-filter for 'scan all F&O': one quotes call per 50 stocks, ranked by |% change| (today's move)."""
    names = [u for u in await fno_stocks() if u not in exclude]
    q = await quotes([f"NSE:{u}-EQ" for u in names])
    ranked = sorted(({"underlying": u, **q[f"NSE:{u}-EQ"]} for u in names if f"NSE:{u}-EQ" in q and q[f"NSE:{u}-EQ"].get("chp") is not None),
                    key=lambda x: -abs(x["chp"] or 0))
    return ranked[:top]


# ---------------------------------------------------------------- structures
def iv_state(ch: dict, hv_pct: float | None, r: dict) -> tuple[str, str]:
    ivr = ch.get("iv_rank") or {}
    if ivr.get("available"):
        k = ivr["rank"]
        return ("high" if k >= r["opt_iv_high_rank"] else "low" if k <= r["opt_iv_low_rank"] else "normal"), f"IV rank {k:.0f}"
    iv = ch.get("atm_iv")
    if iv and hv_pct:
        x = iv / hv_pct
        return ("high" if x > 1.25 else "low" if x < 0.9 else "normal"), f"ATM IV {iv:.1f}% vs HV {hv_pct:.1f}% ({x:.2f}x; IV rank needs 20+ snapshots)"
    return "normal", "IV state unknown (no IV rank history or HV): treated as normal"


def _leg(ch: dict, typ: str, strike: float, side: str) -> dict | None:
    row = next((r for r in ch["strikes"] if r["strike"] == strike), None)
    o = (row or {}).get(typ)
    if not o:
        return None
    return {**o, "side": side, "expiry": ch.get("expiry"), "lot": ch["lot"]}


def _by_delta(ch: dict, typ: str, target: float, otm_of: float | None = None) -> dict | None:
    cands = [r[typ] for r in ch["strikes"] if r.get(typ) and r[typ].get("delta")]
    if otm_of is not None:
        cands = [o for o in cands if (o["strike"] > otm_of if typ == "CE" else o["strike"] < otm_of)]
    return min(cands, key=lambda o: abs(abs(o["delta"]) - target), default=None)


def _wing(ch: dict, typ: str, strike: float, n: int) -> float | None:
    ks = [r["strike"] for r in ch["strikes"] if r.get(typ)]
    if strike not in ks:
        return None
    i = ks.index(strike) + (n if typ == "CE" else -n)
    return ks[i] if 0 <= i < len(ks) else None


def leg_price(leg: dict, opening: bool = True) -> float | None:
    """Executable price: opening BUY at ask / SELL at bid; closing a long leg at bid / a short leg at ask."""
    buy = (leg["side"] == "BUY") == opening
    p = leg.get("ask") if buy else leg.get("bid")
    return p if p and p > 0 else None


def value(legs: list[dict], opening: bool = True) -> float | None:
    """Net position value per unit: + long legs, - short legs, at executable prices."""
    tot = 0.0
    for lg in legs:
        p = leg_price(lg, opening)
        if p is None:
            return None
        tot += p if lg["side"] == "BUY" else -p
    return tot


def seller_lens(ch: dict, r: dict) -> dict:
    """Read the chain the way an option WRITER would. Heavy put writing below spot = writers defending support
    (bullish); heavy call writing above = writers capping the move (bearish); both walls close together around spot =
    writers boxing a range. Fresh writing = OI added (oi_chg > 0) within +/-6 strikes; without OI-change data the
    put/call OI ratio is used instead. Walls = the strike with the most open interest above (calls) / below (puts)."""
    spot, rows = ch["spot"], ch["strikes"]
    ks = [x["strike"] for x in rows]
    step = (ks[1] - ks[0]) if len(ks) > 1 else 1.0
    above = [x for x in rows if x["strike"] >= spot and x.get("CE")]
    below = [x for x in rows if x["strike"] <= spot and x.get("PE")]
    call_wall = max(above, key=lambda x: x["CE"].get("oi") or 0)["strike"] if above else None
    put_wall = max(below, key=lambda x: x["PE"].get("oi") or 0)["strike"] if below else None
    band = [x for x in rows if abs(x["strike"] - spot) <= 6 * step]
    cw = sum(max(0.0, (x.get("CE") or {}).get("oi_chg") or 0) for x in band)
    pw = sum(max(0.0, (x.get("PE") or {}).get("oi_chg") or 0) for x in band)
    oi_ce = sum((x.get("CE") or {}).get("oi") or 0 for x in rows)
    oi_pe = sum((x.get("PE") or {}).get("oi") or 0 for x in rows)
    pcr = oi_pe / oi_ce if oi_ce else None
    atm = min(rows, key=lambda x: abs(x["strike"] - spot))
    mid = lambda o: ((o.get("bid") or 0) + (o.get("ask") or 0)) / 2 if o and o.get("bid") and o.get("ask") else (o or {}).get("ltp") or 0
    straddle = mid(atm.get("CE")) + mid(atm.get("PE"))
    k = r["opt_wall_writing_ratio"]
    basis = "fresh OI writing"
    if cw + pw > 0:
        view = "bullish" if pw > k * cw else "bearish" if cw > k * pw else None
    else:
        basis = "put/call OI ratio (no OI-change data)"
        view = "bullish" if (pcr or 1) >= 1.2 else "bearish" if (pcr or 1) <= 0.8 else None
    if view is None:
        boxed = call_wall is not None and put_wall is not None and straddle and (call_wall - put_wall) <= 2.5 * straddle
        view = "range" if boxed else "unclear"
    reads = [f"call-writing wall {call_wall:g}" if call_wall is not None else "no call wall",
             f"put-writing wall {put_wall:g}" if put_wall is not None else "no put wall",
             f"fresh writing calls {cw:,.0f} vs puts {pw:,.0f}", f"PCR {pcr:.2f}" if pcr else "PCR n/a",
             f"ATM straddle {straddle:,.2f} (expected move ~{straddle * 0.85:,.0f})"]
    return {"view": view, "basis": basis, "call_wall": call_wall, "put_wall": put_wall, "call_writing": cw, "put_writing": pw, "pcr": pcr,
            "straddle": straddle, "expected_move": straddle * 0.85, "reads": reads}


def _strike_leg(ch: dict, typ: str, strike: float | None) -> dict | None:
    if strike is None:
        return None
    row = next((x for x in ch["strikes"] if x["strike"] == strike), None)
    return (row or {}).get(typ)


def choose(direction: str, ch: dict, regime: str, ivs: str, r: dict, is_idx: bool, lens: dict | None = None,
           und: dict | None = None) -> tuple[dict | None, str]:
    """Think like a seller, then pick the structure. `und` = the underlying plan {entry, stop} for directional trades.
      writers against the direction           -> no trade
      IV high (premium is rich: sell it)       -> credit spread, short strike at the writers' wall on the supporting side
      IV low  (premium is cheap: buy it)       -> long call/put ~0.55 delta, only if the 1:2 target is short of the wall
      IV normal                                -> debit spread: buy ~0.50 delta, SELL the strike at the writers' wall
      NEUTRAL with writers boxing a range      -> iron condor, short strikes at (or beyond) the two walls"""
    lens = lens or seller_lens(ch, r)
    w = int(r["opt_wing_strikes"])
    spot = ch["spot"]
    extra = {}
    if direction == "NEUTRAL":
        if not is_idx:
            return None, "iron condors only on index options (stock option liquidity is too thin for four legs)"
        if lens["view"] != "range":
            return None, f"writers are not boxing a range (seller's view: {lens['view']})"
        if ch["days"] < r["opt_min_dte_sell"]:
            return None, f"{ch['days']:.1f} days to expiry: below the {r['opt_min_dte_sell']} minimum for selling"
        sc = _strike_leg(ch, "CE", lens["call_wall"]) if lens["call_wall"] and lens["call_wall"] > spot else None
        sp = _strike_leg(ch, "PE", lens["put_wall"]) if lens["put_wall"] and lens["put_wall"] < spot else None
        sc = sc if sc and abs(sc.get("delta") or 0) <= 0.35 else _by_delta(ch, "CE", r["opt_condor_delta"], spot)
        sp = sp if sp and abs(sp.get("delta") or 0) <= 0.35 else _by_delta(ch, "PE", r["opt_condor_delta"], spot)
        if not sc or not sp:
            return None, "no suitable short strikes"
        wc, wp = _wing(ch, "CE", sc["strike"], w), _wing(ch, "PE", sp["strike"], w)
        if wc is None or wp is None:
            return None, "chain too narrow for the wings"
        legs = [_leg(ch, "CE", sc["strike"], "SELL"), _leg(ch, "CE", wc, "BUY"), _leg(ch, "PE", sp["strike"], "SELL"), _leg(ch, "PE", wp, "BUY")]
        kind = "iron_condor"
    else:
        call = direction == "LONG"
        typ = "CE" if call else "PE"
        if (call and lens["view"] == "bearish") or (not call and lens["view"] == "bullish"):
            wall = lens["call_wall"] if call else lens["put_wall"]
            return None, (f"option writers are positioned against a {'rise' if call else 'fall'}: "
                          f"{'call' if call else 'put'} writing dominates, wall at {wall}")
        if ivs == "high":
            if ch["days"] < r["opt_min_dte_sell"]:
                return None, f"IV is high but {ch['days']:.1f} days to expiry is below the {r['opt_min_dte_sell']} minimum for selling"
            styp = "PE" if call else "CE"                 # sell where the writers defend: below spot for a rise, above for a fall
            wall = lens["put_wall"] if call else lens["call_wall"]
            s = _strike_leg(ch, styp, wall) if wall is not None and ((wall < spot) if call else (wall > spot)) else None
            if not s or abs(s.get("delta") or 0) > 0.45:
                s = _by_delta(ch, styp, r["opt_short_delta"], spot)
            wg = _wing(ch, styp, s["strike"], w) if s else None
            if not s or wg is None:
                return None, "no suitable credit-spread strikes"
            legs = [_leg(ch, styp, s["strike"], "SELL"), _leg(ch, styp, wg, "BUY")]
            kind = "credit_spread"
        else:
            if not und or und.get("stop") is None:
                return None, "no underlying stop for a buying trade"
            ur = abs(und["entry"] - und["stop"])
            tgt = und["entry"] + (2 if call else -2) * ur
            wall = lens["call_wall"] if call else lens["put_wall"]
            extra = {"und_risk": ur, "und_target_1": tgt, "wall": wall}
            if wall is not None and ((wall <= tgt) if call else (wall >= tgt)):
                return None, (f"the writers' {'call' if call else 'put'} wall at {wall:g} sits inside the 1:2 target {tgt:,.0f}: "
                              "sellers would cap the move before lot 1 books")
            b = _by_delta(ch, typ, 0.55 if ivs == "low" else 0.50)
            if not b:
                return None, "no option near the target delta"
            if ivs == "low":
                legs = [_leg(ch, typ, b["strike"], "BUY")]
                kind = "long_option"
            else:
                s = _strike_leg(ch, typ, wall) if wall is not None and ((wall > b["strike"]) if call else (wall < b["strike"])) else None
                s = s or _by_delta(ch, typ, r["opt_short_delta"], b["strike"])
                if not s:
                    return None, "no strike to sell against the long leg"
                legs = [_leg(ch, typ, b["strike"], "BUY"), _leg(ch, typ, s["strike"], "SELL")]
                kind = "debit_spread"
    if any(lg is None for lg in legs):
        return None, "a leg is missing from the chain"
    return {"kind": kind, "label": LABEL[kind], "direction": direction, "legs": legs, "underlying": ch["underlying"], "spot": spot,
            "expiry": ch["expiry"], "days": ch["days"], "lot": ch["lot"], "tick": ch["tick"], "iv_state": ivs, "atm_iv": ch.get("atm_iv"),
            "chain_as_of": ch["as_of"], "lens": lens, "und": {**(und or {}), **extra}}, \
        f"{LABEL[kind]} ({ivs} IV, {ch['days']:.1f} days; seller's view {lens['view']})"


def describe(st: dict) -> str:
    return f"{st['underlying']} {st['expiry']} " + " / ".join(
        f"{'+' if lg['side'] == 'BUY' else '-'}{lg['strike']:g}{lg['type']}" for lg in st["legs"]) + f" ({st['label']})"


# ---------------------------------------------------------------- verification
def payoff(legs: list[dict], v0: float, spot: float) -> dict:
    """Expiry P&L per unit of the whole structure (v0 = net debit paid, negative for a net credit)."""
    ks = sorted({lg["strike"] for lg in legs})

    def pnl(s: float) -> float:
        tot = 0.0
        for lg in legs:
            intr = max(0.0, s - lg["strike"]) if lg["type"] == "CE" else max(0.0, lg["strike"] - s)
            tot += intr if lg["side"] == "BUY" else -intr
        return tot - v0

    grid = sorted({0.0, *ks, *(k * 0.999 for k in ks), *(k * 1.001 for k in ks), spot * 0.5, spot, spot * 2, spot * 4})
    pts = [(s, pnl(s)) for s in grid]
    hi = max(ks + [spot])
    unbounded = pnl(hi * 5) < pnl(hi * 4) - 1e-9          # P&L still falling above every strike -> uncapped loss
    worst = min(p for _, p in pts)
    best = max(p for _, p in pts)
    be = []
    for (s1, p1), (s2, p2) in zip(pts, pts[1:]):
        if (p1 < 0 <= p2) or (p1 >= 0 > p2):
            be.append(round(s1 + (s2 - s1) * (-p1) / (p2 - p1), 2) if p2 != p1 else s1)
    return {"max_loss": max(0.0, -worst), "max_gain": best, "unbounded": unbounded, "breakevens": be}


def covered(legs: list[dict]) -> tuple[bool, str]:
    """Every short leg needs a long leg of the same type and expiry, further out of the money, with at least its qty."""
    for s in (lg for lg in legs if lg["side"] == "SELL"):
        wing = [lg for lg in legs if lg["side"] == "BUY" and lg["type"] == s["type"] and lg.get("expiry") == s.get("expiry")
                and (lg["strike"] > s["strike"] if s["type"] == "CE" else lg["strike"] < s["strike"])]
        if not wing:
            return False, f"short {s['strike']:g}{s['type']} has no protective long {s['type']} further out of the money (naked)"
    return True, "every short leg has a long wing" if any(lg["side"] == "SELL" for lg in legs) else "long options only"


def leg_liquidity(legs: list[dict], r: dict, is_idx: bool) -> list[str]:
    lim = r["opt_max_leg_spread_pct"] if is_idx else r["opt_max_leg_spread_pct_stock"]
    bad = []
    for lg in legs:
        b, a = lg.get("bid") or 0, lg.get("ask") or 0
        if b <= 0 or a <= 0 or a < b:
            bad.append(f"{lg['strike']:g}{lg['type']}: no two-sided quote")
            continue
        mid = (a + b) / 2
        if (a - b) / mid * 100 > lim:
            bad.append(f"{lg['strike']:g}{lg['type']}: spread {(a - b) / mid * 100:.1f}% > {lim}%")
        if (lg.get("oi") or 0) <= 0:
            bad.append(f"{lg['strike']:g}{lg['type']}: no open interest")
    return bad


def plan(st: dict, r: dict) -> dict | None:
    """Per-unit plan for a 2-lot trade. R (planned loss per unit):
      long option / debit spread  |net delta| x underlying stop distance + the legs' bid/ask spread, kept between
                                  opt_min_stop_pct% and opt_long_stop_pct% of the premium paid
      credit spread / condor      opt_sell_stop_frac x the credit received
    Lot 1 books at value v0 + 2R + fees per unit (1:2 after costs); lot 2 then moves its stop to v0 (breakeven) and trails
    opt_trail_r x R behind the best value seen. A credit structure whose 1:2 point is beyond 90% of its credit is refused."""
    legs = st["legs"]
    v0 = value(legs, True)
    if v0 is None:
        return None
    pf = payoff(legs, v0, st["spot"])
    k = st["kind"]
    need = {"long_option": 1, "debit_spread": 2, "credit_spread": 2, "iron_condor": 4}[k]
    pair = lambda a, b: abs(legs[a]["strike"] - legs[b]["strike"]) if len(legs) > max(a, b) else 0.0
    qty = st["lot"]                                        # fee estimate per unit on one lot (the most expensive case)
    fee_unit = fees(legs, qty) / qty if qty else 0.0
    width, ok, why = 0.0, True, ""
    if k in ("long_option", "debit_spread"):
        width = pair(0, 1) if k == "debit_spread" else 0.0
        nd = abs(sum((lg.get("delta") or 0) * (1 if lg["side"] == "BUY" else -1) for lg in legs))
        spr = sum((lg.get("ask") or 0) - (lg.get("bid") or 0) for lg in legs)
        ur = (st.get("und") or {}).get("und_risk") or 0
        R = nd * ur + spr if ur else v0 * r["opt_long_stop_pct"] / 100
        R = min(max(R, v0 * r["opt_min_stop_pct"] / 100), v0 * r["opt_long_stop_pct"] / 100)
        tp1 = v0 + 2 * (R + fee_unit) + fee_unit            # lot 1 nets 2 x (R + its fees) after its own fees
        if k == "debit_spread" and tp1 > width * 0.95:
            ok, why = False, f"1:2 value {tp1:,.2f} is beyond the spread's maximum {width:g}"
    else:
        credit = -v0
        width = pair(0, 1) if k == "credit_spread" else max(pair(0, 1), pair(2, 3))
        R = r["opt_sell_stop_frac"] * credit
        tp1 = v0 + 2 * (R + fee_unit) + fee_unit
        if tp1 > v0 + 0.9 * credit:
            ok, why = False, f"1:2 after costs needs {(tp1 - v0) / credit * 100:.0f}% of the credit (max 90%)"
    if len(legs) != need:                                  # malformed (e.g. a wing removed): the Guardian rejects it
        pf = {**pf, "unbounded": pf["unbounded"] or k in CREDIT}
    return {"v0": v0, "width": width, "max_loss_unit": pf["max_loss"], "planned_loss_unit": R, "gain_unit": tp1 - v0,
            "sl_value": v0 - R, "tp1_value": tp1, "tp_value": tp1, "trail_unit": R * r["opt_trail_r"], "fee_unit": fee_unit,
            "payoff": pf, "rr_ok": ok, "rr_why": why, "credit_ratio": (-v0 / width) if k in CREDIT and width else None}


def fees(legs: list[dict], qty: float, exit_values: dict | None = None) -> float:
    """Round-trip statutory fees for all legs (Indian options rates in lab/costs.py); exit at the given per-leg prices,
    else at the entry prices (the estimate used for sizing)."""
    tot = 0.0
    for lg in legs:
        e = leg_price(lg, True) or 0
        x = (exit_values or {}).get(lg["symbol"], e)
        tot += C.round_trip(lg["symbol"], "FNO", qty, e, x, lg["side"] == "BUY")
    return tot


def size(st: dict, p: dict, budget: float, r: dict, max_capital: float | None = None) -> dict:
    """As many lots as fit: planned loss at the stop (incl. fees) <= the per-trade risk, and the capital the position
    ties up (premium paid; worst-case loss for credit structures) <= max_capital when given. Minimum 1 lot."""
    lot = st["lot"]
    credit = st["kind"] in CREDIT

    def calc(n: int) -> dict:
        q = lot * n
        f = fees(st["legs"], q)
        return {"q": q, "f": f, "planned": q * p["planned_loss_unit"] + f, "structural": q * p["max_loss_unit"] + f,
                "premium": q * abs(p["v0"])}

    per_lot = lot * p["planned_loss_unit"] + fees(st["legs"], lot)
    n = max(1, int(budget // per_lot)) if per_lot > 0 else 1
    n = min(n, 200)
    c = calc(n)
    while n > 1 and c["planned"] > budget + 1e-6:
        n -= 1
        c = calc(n)
    cap_x = budget * r["option_premium_risk_x"]             # worst case at expiry also bounds the lot count
    while n > 1 and c["structural"] > cap_x + 1e-6:
        n -= 1
        c = calc(n)
    used = lambda c: c["structural"] if credit else c["premium"]
    if max_capital is not None:
        while n > 1 and used(c) > max_capital + 1e-6:
            n -= 1
            c = calc(n)
    base = {"lot": lot, "lots": n, "budget": round(budget, 2), "fees": round(c["f"], 2), "max_loss": round(c["planned"], 2),
            "structural_max_loss": round(c["structural"], 2), "premium": round(c["premium"], 2), "currency": "INR"}
    if c["planned"] > budget + 1e-6:
        return {**base, "qty": 0, "why": f"one lot ({lot}) risks {c['planned']:,.0f} at the stop, more than the AI's {budget:,.0f} per-trade risk"}
    if max_capital is not None and used(c) > max_capital + 1e-6:
        return {**base, "qty": 0, "why": f"one lot ties up {used(c):,.0f} INR, more than the {max_capital:,.0f} day capital left"}
    q = c["q"]
    bq = (n // 2) * lot
    h = bq if bq else q
    share = h / q
    lot1_gain = h * (p["tp1_value"] - p["v0"]) - c["f"] * share
    lot1_loss = h * p["planned_loss_unit"] + c["f"] * share
    return {**base, "qty": q, "book_qty": bq, "target_gain": round(lot1_gain, 2), "rr_net": round(lot1_gain / lot1_loss, 2) if lot1_loss else 0}