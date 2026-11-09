"""AuctionFlow Market Structure Engine (Auction Market Theory). Analysis only: nothing here places or sizes a trade.

Profiles ("profile session: both")
  * SESSION profiles: one per trading day (IST 09:15-15:30 for NSE/BSE/MCX, UTC day for crypto/US), each with a
    Volume Profile (each candle's volume spread evenly over the price bins it traded) and a TPO profile (30-minute
    periods; a bin earns one TPO per period that traded it). POC = busiest bin; Value Area = 70% around the POC,
    grown two bins at a time toward the busier side (CBOT method). The current day's POC/VAH/VAL are also given
    candle by candle ("developing").
  * COMPOSITE profiles: the whole window and every balance area, Volume and TPO.
  Indices have no traded volume: their Volume Profile uses the front-month future's volume, candle by candle.

Balance (auto)
  Candle by candle, a balance is a block of candles rotating around its own value: it needs >= MIN_BAL candles
  (about an hour) and >= 2 crossings of its POC. When >= ACCEPT closes in a row land outside the block's value area
  ("acceptance"), the balance ends and an IMBALANCE leg starts (bullish up, bearish down) until a new block
  qualifies. All thresholds scale with ATR and the timeframe, so nothing needs a fixed lookback.
  Multi-day balance: consecutive sessions whose value areas overlap >= 50% (of the narrower one) are one balance.

Failed auctions
  A close beyond a balance's value area (or beyond the prior session's high/low) that returns inside the value
  within FAIL_BARS candles without acceptance: "look above and fail" (bearish) / "look below and fail" (bullish).
  TPO poor high/low: >= 2 TPOs at the extreme (no excess: unfinished auction, often revisited); excess: a tail of
  single TPOs at the extreme (the auction ended there). Single prints inside the day = initiative imbalance.

HVN / LVN: smoothed composite volume profile; peaks >= 1.25x average with clear prominence = HVN, troughs <= 0.6x
average between two HVNs = LVN (price tends to move fast through LVNs and stall at HVNs).

Next balance projection (direction of the latest imbalance, or both sides while in balance): the nearest of the
next historical balance area / HVN beyond price, else a measured move (edge +/- the height of the balance left).

Confluence: every VP and TPO level (POC/VAH/VAL of the current and previous sessions, the composite and the
balances, naked POCs, HVN/LVN) is clustered within 0.35 ATR; a zone where Volume AND TPO levels agree scores higher.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import math
import time

from .candle import Candle, TF_SECONDS

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
VA_PCT = 0.70
TPO_SEC = 1800
MIN_BAL_SEC = 3600            # a balance needs about an hour of candles
ACCEPT = 3                    # closes in a row outside value = acceptance
FAIL_BARS = 4                 # back inside value within this many candles = failed auction
OVERLAP = 0.5                 # multi-day balance: value areas overlapping >= 50%
INDEX_FUT = {"NSE:NIFTY50-INDEX": "NSE:NIFTY-FUT", "NSE:NIFTYBANK-INDEX": "NSE:BANKNIFTY-FUT", "NSE:FINNIFTY-INDEX": "NSE:FINNIFTY-FUT",
             "NSE:MIDCPNIFTY-INDEX": "NSE:MIDCPNIFTY-FUT", "BSE:SENSEX-INDEX": "BSE:SENSEX-FUT", "BSE:BANKEX-INDEX": "BSE:BANKEX-FUT"}
_cache: dict = {}


# ---------------------------------------------------------------- profile maths
def nice_bin(raw: float) -> float:
    if raw <= 0:
        return 0.01
    e = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * e:
            return m * e
    return 10 * e


class Grid:
    """Price bins of one size for every profile, so levels line up across sessions."""

    def __init__(self, lo: float, step: float):
        self.step = step
        self.base = math.floor(lo / step) * step

    def idx(self, p: float) -> int:
        return int(math.floor((p - self.base) / self.step + 1e-9))

    def price(self, i: int) -> float:                  # bin centre
        return self.base + (i + 0.5) * self.step


def add_bar(vp: dict, tpo_seen: dict, g: Grid, b: Candle, period: int, vol: float) -> None:
    lo, hi = g.idx(b.low), g.idx(b.high)
    n = hi - lo + 1
    for i in range(lo, hi + 1):
        vp[i] = vp.get(i, 0.0) + vol / n
        tpo_seen.setdefault(i, set()).add(period)


def poc_va(prof: dict) -> dict | None:
    """POC and 70% value area of {bin: amount}. Ties for POC go to the bin nearest the profile's middle."""
    if not prof:
        return None
    keys = sorted(prof)
    lo_k, hi_k = keys[0], keys[-1]
    full = [prof.get(i, 0.0) for i in range(lo_k, hi_k + 1)]
    tot = sum(full)
    if tot <= 0:
        return None
    mx = max(full)
    mid = (len(full) - 1) / 2
    p = min((i for i, v in enumerate(full) if v == mx), key=lambda i: abs(i - mid))
    a = b = p
    acc = full[p]
    while acc < VA_PCT * tot and (a > 0 or b < len(full) - 1):
        up = sum(full[b + 1:b + 3]) if b < len(full) - 1 else -1
        dn = sum(full[max(0, a - 2):a]) if a > 0 else -1
        if up >= dn:
            nb = min(len(full) - 1, b + 2)
            acc += sum(full[b + 1:nb + 1])
            b = nb
        else:
            na = max(0, a - 2)
            acc += sum(full[na:a])
            a = na
    return {"poc": lo_k + p, "val": lo_k + a, "vah": lo_k + b, "lo": lo_k, "hi": hi_k, "total": tot}


def levels(g: Grid, pv: dict | None) -> dict | None:
    if not pv:
        return None
    s = g.step
    return {"poc": round(g.price(pv["poc"]), 6), "vah": round(g.base + (pv["vah"] + 1) * s, 6), "val": round(g.base + pv["val"] * s, 6)}


def profile_of(bars: list[Candle], vols: list[float], g: Grid, t0: int) -> tuple[dict, dict]:
    vp, seen = {}, {}
    for b, v in zip(bars, vols):
        add_bar(vp, seen, g, b, (b.time - t0) // TPO_SEC, v)
    return vp, {i: len(s) for i, s in seen.items()}


def _atr(bars: list[Candle], n: int = 14) -> float:
    if len(bars) < 2:
        return (bars[0].high - bars[0].low) if bars else 0.0
    trs = [max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close)) for a, b in zip(bars, bars[1:])]
    k = trs[-max(n * 20, n):]
    return sum(k) / len(k) if k else 0.0


# ---------------------------------------------------------------- sessions
def session_id(symbol: str):
    india = symbol.split(":", 1)[0].upper() in ("NSE", "BSE", "MCX") or symbol.upper().endswith(("-EQ", "-INDEX", "-FUT"))
    off = 19800 if india else 0
    return (lambda t: (t + off) // 86400), off


def tpo_detail(tpo: dict, g: Grid, first_hour: tuple[float, float] | None) -> dict:
    if not tpo:
        return {}
    keys = sorted(tpo)
    top, bot = keys[-1], keys[0]
    singles, run = [], []
    for i in range(bot, top + 1):                     # inside single prints (initiative)
        if tpo.get(i, 0) == 1:
            run.append(i)
        else:
            if len(run) >= 2 and run[0] != bot and run[-1] != top:
                singles.append([round(g.base + run[0] * g.step, 6), round(g.base + (run[-1] + 1) * g.step, 6)])
            run = []

    def tail(rng):
        n = 0
        for i in rng:
            if tpo.get(i, 0) == 1:
                n += 1
            else:
                break
        return n
    th, tl = tail(range(top, bot - 1, -1)), tail(range(bot, top + 1))
    out = {"poor_high": tpo.get(top, 0) >= 2 and tpo.get(top - 1, 0) >= 2, "poor_low": tpo.get(bot, 0) >= 2 and tpo.get(bot + 1, 0) >= 2,
           "excess_high": th >= 2, "excess_low": tl >= 2, "single_prints": singles[:6]}
    if first_hour:
        out["ib"] = [round(first_hour[0], 6), round(first_hour[1], 6)]
    return out


# ---------------------------------------------------------------- balance / imbalance / failed auctions (candle by candle)
def segment(bars: list[Candle], g: Grid, step_sec: int, at: float) -> dict:
    """Walk the candles once: balance blocks, the imbalance legs between them and failed breakouts of a balance."""
    min_bal = max(6, MIN_BAL_SEC // step_sec)
    balances, legs, failed = [], [], []
    st = {"s": 0, "pr": {}}

    def add(k, sign=1):
        for i in range(g.idx(bars[k].low), g.idx(bars[k].high) + 1):
            st["pr"][i] = st["pr"].get(i, 0) + sign

    def reset(s0, k_end):
        st["s"], st["pr"] = s0, {}
        for k in range(s0, k_end + 1):
            add(k)

    def rotations(a, b, poc_p):
        n, side = 0, 0
        for k in range(a, b + 1):
            sd = 1 if bars[k].close > poc_p else -1 if bars[k].close < poc_p else 0
            if sd and side and sd != side:
                n += 1
            side = sd or side
        return n

    def qualifies(a, b):
        """Levels of the block a..b (profile in st['pr']) if it is a balance, else None."""
        if b - a + 1 < min_bal:
            return None
        pv = poc_va({i: v for i, v in st["pr"].items() if v > 0})
        if not pv or rotations(a, b, g.price(pv["poc"])) < 2:
            return None
        return pv

    def block_pv(a, b):
        pr = {}
        for k in range(a, b + 1):
            for i in range(g.idx(bars[k].low), g.idx(bars[k].high) + 1):
                pr[i] = pr.get(i, 0) + 1
        return poc_va(pr)

    def close_block(a, b, pv, status):
        balances.append({"start": bars[a].time, "end": bars[b].time, "i0": a, "i1": b, "bars": b - a + 1, "status": status, "kind": "intraday",
                         "high": max(x.high for x in bars[a:b + 1]), "low": min(x.low for x in bars[a:b + 1]), **levels(g, pv)})

    up = dn = 0
    probe = None                                       # (side, candle index, the block extreme it went beyond)
    brk_watch = None                                   # a fresh breakout: (old start, balance, leg, side, candle index, old VAL, old VAH, POC)
    if bars:
        add(0)
    for k in range(1, len(bars)):
        s = st["s"]
        c = bars[k].close
        # a breakout that comes straight back inside the old value: failed auction, the balance continues
        if brk_watch and k - brk_watch[4] <= ACCEPT + FAIL_BARS:
            os_, bal, leg, side, kb, oval, ovah, opoc = brk_watch
            if oval <= c <= ovah:
                ext = max(x.high for x in bars[kb:k + 1]) if side == "high" else min(x.low for x in bars[kb:k + 1])
                failed.append({"t": bars[k].time, "t_probe": bars[kb].time, "side": side, "price": round(ext, 6),
                               "ref": "balance breakout", "ref_price": round(ovah if side == "high" else oval, 6),
                               "target": round(opoc, 6), "target2": round(oval if side == "high" else ovah, 6)})
                balances.remove(bal)
                legs.remove(leg)
                reset(os_, k)
                brk_watch, up, dn, probe = None, 0, 0, None
                continue
        elif brk_watch:
            brk_watch = None
        pv = qualifies(s, k - 1)
        if pv:
            vah, val = g.base + (pv["vah"] + 1) * g.step, g.base + pv["val"] * g.step
            hi_b = max(x.high for x in bars[s:k])
            lo_b = min(x.low for x in bars[s:k])
            if probe and k - probe[1] > FAIL_BARS:
                probe = None
            if probe is None and bars[k].high > hi_b:
                probe = ("high", k, hi_b)
            elif probe is None and bars[k].low < lo_b:
                probe = ("low", k, lo_b)
            if c > vah:
                up, dn = up + 1, 0
            elif c < val:
                dn, up = dn + 1, 0
            else:
                if probe and probe[1] < k:             # went beyond the balance range and closed back in value
                    side, kp, lvl = probe
                    ext = max(x.high for x in bars[kp:k]) if side == "high" else min(x.low for x in bars[kp:k])
                    failed.append({"t": bars[k].time, "t_probe": bars[kp].time, "side": side, "price": round(ext, 6),
                                   "ref": f"balance {side}", "ref_price": round(lvl, 6),
                                   "target": round(g.price(pv["poc"]), 6), "target2": round(val if side == "high" else vah, 6)})
                    probe = None
                up = dn = 0
            run = up or dn
            if run >= ACCEPT:
                r0 = k - ACCEPT + 1
                ref_hi = max(x.high for x in bars[s:r0]) if r0 > s else hi_b
                ref_lo = min(x.low for x in bars[s:r0]) if r0 > s else lo_b
                beyond = any(x.close > ref_hi for x in bars[r0:k + 1]) if up else any(x.close < ref_lo for x in bars[r0:k + 1])
                if beyond:                             # acceptance: closes outside value AND beyond the balance range
                    old = block_pv(s, r0 - 1)
                    close_block(s, r0 - 1, old, "historical")
                    leg = {"i0": r0, "dir": "bull" if up else "bear"}
                    legs.append(leg)
                    lv = levels(g, old)
                    brk_watch = (s, balances[-1], leg, "high" if up else "low", r0, lv["val"], lv["vah"], lv["poc"])
                    reset(r0, k)
                    up = dn = 0
                    probe = None
                    continue
            add(k)
        else:
            add(k)
            # not a balance yet: a run-away stretch is part of the move, so the candidate block slides forward
            if k - s + 1 > min_bal:
                hi, lo = max(x.high for x in bars[s:k + 1]), min(x.low for x in bars[s:k + 1])
                if (hi - lo) > 3.5 * at and rotations(s, k, (hi + lo) / 2) < 2:
                    reset(k - min_bal + 1, k)
    s = st["s"]
    pv = qualifies(s, len(bars) - 1)
    if pv:
        close_block(s, len(bars) - 1, pv, "developing")
    for lg in legs:                                    # a leg runs from the breakout until the next balance begins
        nxt = next((b for b in balances if b["i0"] >= lg["i0"]), None)
        if nxt:
            i1 = min(nxt["i1"], max(nxt["i0"] - 1, lg["i0"] + ACCEPT - 1))
        else:
            i1 = len(bars) - 1
        seg = bars[lg["i0"]:i1 + 1] or [bars[lg["i0"]]]
        a, b = seg[0].open, seg[-1].close
        lg.update(start=seg[0].time, end=seg[-1].time, i1=i1, frm=round(a, 6), to=round(b, 6), high=max(x.high for x in seg),
                  low=min(x.low for x in seg), size_atr=round(abs(b - a) / at, 2) if at else None, status="historical" if nxt else "developing")
    return {"balances": balances, "imbalances": legs, "failed": failed, "min_bal": min_bal}


def multiday(sessions: list[dict]) -> list[dict]:
    """Consecutive sessions whose value areas overlap >= 50% form one multi-day balance."""
    out, cur = [], []
    for s in sessions:
        va = s.get("tpo_lv") or s.get("vp_lv")
        if not va:
            continue
        if cur:
            cl, ch = min(x["va"][0] for x in cur), max(x["va"][1] for x in cur)
            ov = min(ch, va["vah"]) - max(cl, va["val"])
            narrow = min(ch - cl, va["vah"] - va["val"]) or 1e-9
            if ov / narrow >= OVERLAP:
                cur.append({"s": s, "va": (va["val"], va["vah"])})
                continue
            out.append(cur)
        cur = [{"s": s, "va": (va["val"], va["vah"])}]
    if cur:
        out.append(cur)
    return [c for c in out if len(c) >= 2]


# ---------------------------------------------------------------- HVN / LVN
def nodes(prof: dict, g: Grid) -> dict:
    if not prof:
        return {"hvn": [], "lvn": []}
    keys = range(min(prof), max(prof) + 1)
    raw = [prof.get(i, 0.0) for i in keys]
    k = [1, 2, 3, 2, 1]
    sm = []
    for i in range(len(raw)):
        acc = w = 0.0
        for j, kk in enumerate(k):
            x = i + j - 2
            if 0 <= x < len(raw):
                acc += raw[x] * kk
                w += kk
        sm.append(acc / w)
    avg = sum(sm) / len(sm) if sm else 0
    peaks = [i for i in range(1, len(sm) - 1) if sm[i] >= sm[i - 1] and sm[i] > sm[i + 1] and sm[i] >= 1.25 * avg]
    sep = max(3, len(sm) // 25)
    hv: list[int] = []
    for i in sorted(peaks, key=lambda i: -sm[i]):
        if all(abs(i - j) >= sep for j in hv):
            hv.append(i)
    hv.sort()
    lv = []
    for a, b in zip(hv, hv[1:]):
        m = min(range(a, b + 1), key=lambda i: sm[i])
        if sm[m] <= 0.6 * avg and sm[m] <= 0.5 * min(sm[a], sm[b]):
            lv.append(m)
    base = min(prof)
    P = lambda i: round(g.price(base + i), 6)
    return {"hvn": [{"price": P(i), "strength": round(sm[i] / avg, 2)} for i in hv],
            "lvn": [{"price": P(i), "depth": round(sm[i] / avg, 2)} for i in lv]}


# ---------------------------------------------------------------- the engine
def analyze(bars: list[Candle], symbol: str, tf: str, vol_bars: list[Candle] | None = None, vol_source: str = "own") -> dict:
    if len(bars) < 30:
        return {"symbol": symbol, "tf": tf, "error": "not enough candles", "candles": len(bars)}
    step_sec = TF_SECONDS.get(tf, 300)
    vmap = {b.time: b.volume for b in (vol_bars or [])}
    vols = [vmap.get(b.time, 0.0) if vol_bars is not None else b.volume for b in bars]
    has_vol = sum(vols) > 0
    if not has_vol:
        vols = [1.0] * len(bars)                       # no volume anywhere: the volume profile becomes a time profile
        vol_source = "none (time-weighted)"
    at = _atr(bars) or (bars[-1].close * 0.001)
    sid, off = session_id(symbol)
    # bin: about a quarter of a typical candle's range, at least 0.02% of price
    med = sorted(b.high - b.low for b in bars[-300:])[len(bars[-300:]) // 2] or at
    g = Grid(min(b.low for b in bars), nice_bin(max(med / 4, bars[-1].close * 0.0002)))

    # sessions
    groups: dict = {}
    for i, b in enumerate(bars):
        groups.setdefault(sid(b.time), []).append(i)
    sessions = []
    for key in sorted(groups):
        ix = groups[key]
        sb, sv = [bars[i] for i in ix], [vols[i] for i in ix]
        t0 = sb[0].time
        vp, tpo = profile_of(sb, sv, g, t0)
        fh = [b for b in sb if b.time < t0 + 3600]
        sessions.append({"key": int(key), "date": dt.datetime.fromtimestamp(key * 86400, dt.timezone.utc).strftime("%Y-%m-%d"),
                         "start": t0, "end": sb[-1].time + step_sec, "last_t": sb[-1].time, "i0": ix[0], "i1": ix[-1],
                         "high": max(b.high for b in sb), "low": min(b.low for b in sb), "open": sb[0].open, "close": sb[-1].close,
                         "vp_lv": levels(g, poc_va(vp)), "tpo_lv": levels(g, poc_va(tpo)),
                         "tpo": tpo_detail(tpo, g, (min(b.low for b in fh), max(b.high for b in fh)) if fh else None),
                         "_vp": vp, "_tpo": tpo})
    cur = sessions[-1]
    # developing values for the last two sessions (candle by candle)
    for s in sessions[-2:]:
        vp, seen, dev = {}, {}, []
        for i in range(s["i0"], s["i1"] + 1):
            add_bar(vp, seen, g, bars[i], (bars[i].time - s["start"]) // TPO_SEC, vols[i])
            a = levels(g, poc_va(vp))
            tp = levels(g, poc_va({k: len(v) for k, v in seen.items()}))
            dev.append({"t": bars[i].time, "vp": a, "tpo": tp})
        s["developing"] = dev
    # naked POCs: earlier session POCs price has not traded through since
    for n, s in enumerate(sessions[:-1]):
        p = (s["vp_lv"] or {}).get("poc")
        later = bars[s["i1"] + 1:]
        s["naked_poc"] = bool(p is not None and later and not any(b.low <= p <= b.high for b in later))

    # composite (whole window) + nodes
    comp_vp, comp_tpo = {}, {}
    for s in sessions:
        for k2, v in s["_vp"].items():
            comp_vp[k2] = comp_vp.get(k2, 0.0) + v
        for k2, v in s["_tpo"].items():
            comp_tpo[k2] = comp_tpo.get(k2, 0) + v
    nd = nodes(comp_vp, g)
    seg = segment(bars, g, step_sec, at)
    # composite profile of every intraday balance (VP and TPO)
    for bl in seg["balances"]:
        vp, tp = profile_of(bars[bl["i0"]:bl["i1"] + 1], vols[bl["i0"]:bl["i1"] + 1], g, bars[bl["i0"]].time)
        bl["vp"], bl["tpo"] = levels(g, poc_va(vp)), levels(g, poc_va(tp))
    md = []
    for grp in multiday(sessions):
        vp, tp = {}, {}
        for x in grp:
            for k2, v in x["s"]["_vp"].items():
                vp[k2] = vp.get(k2, 0.0) + v
            for k2, v in x["s"]["_tpo"].items():
                tp[k2] = tp.get(k2, 0) + v
        lv = levels(g, poc_va(tp))
        md.append({"kind": "multi-day", "start": grp[0]["s"]["start"], "end": grp[-1]["s"]["end"], "sessions": len(grp),
                   "status": "developing" if grp[-1]["s"] is cur else "historical", "high": max(x["s"]["high"] for x in grp),
                   "low": min(x["s"]["low"] for x in grp), **(lv or {}), "vp": levels(g, poc_va(vp)), "tpo": lv})

    # failed auctions against the prior session's high/low (look above / below and fail)
    failed = list(seg["failed"])
    for prev, s in zip(sessions, sessions[1:]):
        sb = bars[s["i0"]:s["i1"] + 1]
        for side, lvl in (("high", prev["high"]), ("low", prev["low"])):
            for j, b in enumerate(sb):
                beyond = b.high > lvl if side == "high" else b.low < lvl
                if not beyond:
                    continue
                back = next((x for x in sb[j:j + FAIL_BARS + 1] if (x.close < lvl if side == "high" else x.close > lvl)), None)
                accepted = sum(1 for x in sb[j:j + ACCEPT] if (x.close > lvl if side == "high" else x.close < lvl)) >= ACCEPT
                if back is not None and not accepted:
                    ext = max(x.high for x in sb[j:sb.index(back) + 1]) if side == "high" else min(x.low for x in sb[j:sb.index(back) + 1])
                    pv = prev["vp_lv"] or prev["tpo_lv"] or {}
                    failed.append({"t": back.time, "t_probe": b.time, "side": side, "price": round(ext, 6), "ref": f"prior session {side}",
                                   "ref_price": round(lvl, 6), "target": pv.get("poc"), "target2": pv.get("val" if side == "high" else "vah")})
                break
    # a probe must go at least 0.15 ATR beyond its reference; one probe seen by two references is one failed auction
    failed = [f for f in failed if abs(f["price"] - f["ref_price"]) >= 0.15 * at or f["ref"] == "balance breakout"]
    failed.sort(key=lambda f: f["t"])
    merged: list[dict] = []
    for f in failed:
        m = next((x for x in merged if x["side"] == f["side"] and abs(x["t_probe"] - f["t_probe"]) <= 2 * step_sec), None)
        if m:
            if f["ref"] not in m["ref"]:
                m["ref"] += " + " + f["ref"]
            m["price"] = max(m["price"], f["price"]) if f["side"] == "high" else min(m["price"], f["price"])
        else:
            merged.append(dict(f))
    failed = merged

    last = bars[-1].close
    out = {"symbol": symbol, "tf": tf, "as_of": bars[-1].time + step_sec, "last": last, "atr": round(at, 6), "bin": g.step,
           "volume_source": vol_source, "candles": len(bars), "session_offset": off,
           "sessions": [{k: v for k, v in s.items() if not k.startswith("_") and k not in ("i0", "i1")} for s in sessions[-12:]],
           "composite": {"vp": levels(g, poc_va(comp_vp)), "tpo": levels(g, poc_va(comp_tpo)), "from": bars[0].time},
           "balances": [{k: v for k, v in b.items() if k not in ("i0", "i1")} for b in seg["balances"]][-25:],
           "multiday": md[-6:], "imbalances": [{k: v for k, v in x.items() if k not in ("i0", "i1")} for x in seg["imbalances"]][-25:],
           "failed": failed[-20:], "hvn": nd["hvn"], "lvn": nd["lvn"]}
    # profiles for drawing: composite (VP+TPO) and current session
    def rows(vp, tp):
        ks = sorted(set(vp) | set(tp))
        return [[round(g.base + k2 * g.step, 6), round(vp.get(k2, 0.0), 3), tp.get(k2, 0)] for k2 in ks]
    out["profile"] = {"composite": rows(comp_vp, comp_tpo), "session": rows(cur["_vp"], cur["_tpo"]), "step": g.step}
    out["confluence"] = confluence(out, at)
    out["projections"] = projections(out, at)
    out["state"], out["plan"] = read(out, at)
    return out


# ---------------------------------------------------------------- confluence, projections, the trading read
def confluence(o: dict, at: float) -> list[dict]:
    L = []

    def add(price, name, kind, w):
        if price is not None:
            L.append({"p": float(price), "name": name, "kind": kind, "w": w})
    ss = o["sessions"]
    for tag, s in (("today", ss[-1]), ("prev day", ss[-2] if len(ss) > 1 else None)):
        if not s:
            continue
        w = 1.0 if tag == "today" else 1.2
        for src, key in (("VP", "vp_lv"), ("TPO", "tpo_lv")):
            for lv in ("poc", "vah", "val"):
                add((s.get(key) or {}).get(lv), f"{tag} {src} {lv.upper()}", src, w * (1.3 if lv == "poc" else 1.0))
    for src in ("vp", "tpo"):
        for lv in ("poc", "vah", "val"):
            add((o["composite"].get(src) or {}).get(lv), f"composite {src.upper()} {lv.upper()}", src.upper(), 1.3)
    for b in o["multiday"][-3:] + [x for x in o["balances"] if x["status"] == "developing"] + o["balances"][-3:]:
        nm = "multi-day balance" if b["kind"] == "multi-day" else "balance"
        for src in ("vp", "tpo"):
            for lv in ("poc", "vah", "val"):
                add((b.get(src) or {}).get(lv), f"{nm} {src.upper()} {lv.upper()}", src.upper(), 1.1)
    for s in ss[:-1]:
        if s.get("naked_poc"):
            add(s["vp_lv"]["poc"], f"naked POC {s['date']}", "VP", 1.4)
    for h in o["hvn"]:
        add(h["price"], "HVN", "VP", 0.8)
    for x in o["lvn"]:
        add(x["price"], "LVN", "VP", 0.6)
    L.sort(key=lambda x: x["p"])
    tol = 0.35 * at
    zones, cur = [], []
    for x in L:
        if cur and x["p"] - cur[0]["p"] > tol:
            zones.append(cur)
            cur = []
        cur.append(x)
    if cur:
        zones.append(cur)
    out = []
    for z in zones:
        kinds = {x["kind"] for x in z}
        names = sorted({x["name"] for x in z})
        if len(names) < 2:
            continue
        score = sum(x["w"] for x in {x["name"]: x for x in z}.values()) * (1.5 if {"VP", "TPO"} <= kinds else 1.0)
        lo, hi = min(x["p"] for x in z), max(x["p"] for x in z)
        out.append({"lo": round(lo, 6), "hi": round(hi, 6), "mid": round((lo + hi) / 2, 6), "score": round(score, 2),
                    "vp_tpo": {"VP", "TPO"} <= kinds, "members": names, "side": "above" if lo > o["last"] else "below" if hi < o["last"] else "at price"})
    out.sort(key=lambda z: -z["score"])
    return out[:12]


def projections(o: dict, at: float) -> list[dict]:
    last = o["last"]
    bal = [b for b in o["balances"] if b["status"] == "historical"]
    dev = next((b for b in o["balances"] if b["status"] == "developing"), None)
    leg = next((x for x in reversed(o["imbalances"]) if x["status"] == "developing"), None)
    ref = dev or (bal[-1] if bal else None)
    areas = [{"lo": b["val"], "hi": b["vah"], "poc": b["poc"], "what": "historical balance"} for b in bal + o["multiday"] if b.get("val") is not None]
    areas += [{"lo": h["price"] - 0.25 * at, "hi": h["price"] + 0.25 * at, "poc": h["price"], "what": f"HVN ×{h['strength']}"} for h in o["hvn"]]

    def side(d, edge, height, why):
        beyond = [a for a in areas if (a["lo"] > edge if d > 0 else a["hi"] < edge)]
        beyond.sort(key=lambda a: (a["lo"] - edge) if d > 0 else (edge - a["hi"]))
        mm = {"lo": edge + (0.5 if d > 0 else -1.0) * height, "hi": edge + (1.0 if d > 0 else -0.5) * height}
        pick = beyond[0] if beyond and abs(beyond[0]["poc"] - edge) <= 2.5 * max(height, at) else None
        if pick:
            return {"dir": "up" if d > 0 else "down", "lo": round(pick["lo"], 6), "hi": round(pick["hi"], 6), "poc": round(pick["poc"], 6),
                    "basis": f"next {pick['what']} {'above' if d > 0 else 'below'}", "trigger": why, "measured": {k: round(v, 6) for k, v in mm.items()}}
        return {"dir": "up" if d > 0 else "down", "lo": round(min(mm.values()), 6), "hi": round(max(mm.values()), 6), "poc": round((mm["lo"] + mm["hi"]) / 2, 6),
                "basis": "measured move (height of the balance left)", "trigger": why}
    out = []
    if leg and dev is None:
        b = bal[-1] if bal else None
        h = (b["vah"] - b["val"]) if b else 2 * at
        d = 1 if leg["dir"] == "bull" else -1
        edge = (b["vah"] if d > 0 else b["val"]) if b else leg["frm"]
        out.append(side(d, max(edge, last) if d > 0 else min(edge, last), h, f"{leg['dir']}ish imbalance in progress"))
    elif ref:
        h = max(ref["vah"] - ref["val"], at)
        out.append(side(1, ref["vah"], h, f"acceptance above {ref['vah']:g} (3 closes)"))
        out.append(side(-1, ref["val"], h, f"acceptance below {ref['val']:g} (3 closes)"))
    return out


def read(o: dict, at: float) -> tuple[dict, list[dict]]:
    last = o["last"]
    ss = o["sessions"]
    cur, prev = ss[-1], (ss[-2] if len(ss) > 1 else None)
    dev = next((b for b in o["balances"] if b["status"] == "developing"), None)
    leg = next((x for x in reversed(o["imbalances"]) if x["status"] == "developing"), None)
    va = cur.get("vp_lv") or cur.get("tpo_lv") or {}
    pva = (prev or {}).get("vp_lv") or {}

    def loc(v):
        if not v:
            return "?"
        return "above value" if last > v["vah"] else "below value" if last < v["val"] else "inside value"
    st = {"phase": "balance" if dev else "imbalance" if leg else "transition", "today": loc(va), "vs_prev_day": loc(pva),
          "open_vs_prev": "?",
          "balance": dev and {k: dev[k] for k in ("vah", "val", "poc", "high", "low", "start", "bars")},
          "imbalance": leg and {k: leg[k] for k in ("dir", "frm", "to", "size_atr", "start")}}
    if pva and cur.get("open") is not None:
        o_ = cur["open"]
        st["open_vs_prev"] = "opened above prior value" if o_ > pva["vah"] else "opened below prior value" if o_ < pva["val"] else "opened inside prior value"
    recent_fail = [f for f in o["failed"] if f["t"] >= cur["start"]]
    st["failed_today"] = recent_fail[-3:]
    tp = cur.get("tpo") or {}
    st["poor_high"], st["poor_low"] = tp.get("poor_high"), tp.get("poor_low")
    naked = [s for s in ss[:-1] if s.get("naked_poc")]
    st["naked_pocs"] = [{"date": s["date"], "poc": s["vp_lv"]["poc"]} for s in naked[-4:]]
    plan = []
    r = lambda x: round(x, 2)
    if recent_fail:
        f = recent_fail[-1]
        d = -1 if f["side"] == "high" else 1
        plan.append({"setup": f"Failed auction {'above' if d < 0 else 'below'} {f['ref']}", "bias": "short" if d < 0 else "long",
                     "entry": f"{'below' if d < 0 else 'above'} {r(f['ref_price'])} (back inside)", "stop": r(f["price"] + (0.15 if d < 0 else -0.15) * at),
                     "targets": [x for x in (f.get("target"), f.get("target2")) if x is not None],
                     "why": "Look above/below and fail: the market rejected that price and should rotate back through value."})
    if dev:
        h = dev["vah"] - dev["val"]
        # stop just beyond the balance, but never further than half the value height past the edge (a spike that
        # started the balance must not set the stop)
        s_hi = min(dev["high"], dev["vah"] + 0.5 * h) + 0.2 * at
        s_lo = max(dev["low"], dev["val"] - 0.5 * h) - 0.2 * at
        plan.append({"setup": "Inside balance: fade the edges (responsive)", "bias": "two-sided",
                     "entry": f"short near VAH {r(dev['vah'])} / long near VAL {r(dev['val'])}", "stop": f"{r(s_hi)} / {r(s_lo)}",
                     "targets": [r(dev["poc"]), r(dev["val"])] if last >= dev["poc"] else [r(dev["poc"]), r(dev["vah"])],
                     "why": "In balance, buyers and sellers agree on value: edges get rejected, the POC acts as a magnet. Avoid trading in the middle."})
        prj = o["projections"]
        if prj:
            u = next((p for p in prj if p["dir"] == "up"), None)
            dn = next((p for p in prj if p["dir"] == "down"), None)
            plan.append({"setup": "Breakout of balance (initiative) – only on acceptance", "bias": "with the break",
                         "entry": f"after 3 closes above {r(dev['vah'])} buy the retest of it / after 3 closes below {r(dev['val'])} sell the retest",
                         "stop": f"back inside value ({r(dev['poc'])})", "targets": [x for x in ((u or {}).get("poc"), (dn or {}).get("poc")) if x is not None],
                         "why": f"Height of value {r(h)}: acceptance outside means the auction is looking for a new balance (projected zones)."})
    elif leg:
        d = 1 if leg["dir"] == "bull" else -1
        prj = (o["projections"] or [None])[0]
        lastbal = next((b for b in reversed(o["balances"]) if b["status"] == "historical"), None)
        edge = (lastbal["vah"] if d > 0 else lastbal["val"]) if lastbal else leg["frm"]
        plan.append({"setup": f"{'Bullish' if d > 0 else 'Bearish'} imbalance: trade with it (initiative)", "bias": "long" if d > 0 else "short",
                     "entry": f"pullback to the broken edge {r(edge)} or a single-print / LVN zone", "stop": r(edge - d * 0.5 * at),
                     "targets": [prj["lo"] if d > 0 else prj["hi"], prj["poc"]] if prj else [],
                     "why": "Price left value with acceptance: the old edge should now hold, and the market travels to the next balance area."})
    if st["poor_high"] or st["poor_low"]:
        plan.append({"setup": f"Unfinished auction: poor {'high' if st['poor_high'] else 'low'} {r(cur['high'] if st['poor_high'] else cur['low'])}",
                     "bias": "magnet", "entry": "—", "stop": "—", "targets": [r(cur["high"] if st["poor_high"] else cur["low"])],
                     "why": "No excess at the extreme (2+ TPOs): price usually comes back to finish the auction. Do not fade it tightly."})
    for n in st["naked_pocs"][-2:]:
        if abs(n["poc"] - last) <= 4 * at:
            plan.append({"setup": f"Naked POC {n['date']} at {r(n['poc'])}", "bias": "magnet", "entry": "—", "stop": "—", "targets": [r(n["poc"])],
                         "why": "An untested prior POC often attracts price; good first target, or a level to fade into."})
    return st, plan


# ---------------------------------------------------------------- gathering (needs the running app)
async def compute(symbol: str, tf: str = "", days: int = 10) -> dict:
    from . import data as D
    symbol = symbol.strip().upper()
    india = symbol.split(":", 1)[0] in ("NSE", "BSE", "MCX")
    tf = tf or ("5m" if india else "15m")
    if tf not in ("1m", "3m", "5m", "15m", "30m"):
        tf = "5m"
    per_day = (375 if india else 1440) * 60 // TF_SECONDS[tf]
    n = max(200, min(2000, per_day * max(3, min(30, days)) + 10))
    key = (symbol, tf, n)
    hit = _cache.get(key)
    ttl = 300 if india else 60                        # FYERS free plan: 5,000 data requests a day, so India refreshes every 5 minutes
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    fut = INDEX_FUT.get(symbol)
    if fut:
        (bars, q), fv = await asyncio.gather(D.candles(symbol, tf, n), _try(D.candles(fut, tf, n)))
        vol_bars, src = (fv[0], f"front-month future ({fut})") if fv else (None, "none (future unavailable)")
    else:
        bars, q = await D.candles(symbol, tf, n)
        vol_bars, src = None, "own volume"
    out = analyze(bars, symbol, tf, vol_bars, src)
    out["source"] = q.get("source")
    out["method"] = __doc__
    _cache[key] = (time.time(), out)
    return out


async def _try(coro):
    try:
        return await coro
    except Exception:                                 # noqa: BLE001 - volume is optional
        return None
