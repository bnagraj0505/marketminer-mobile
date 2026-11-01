"""Day Desk engine for MarketMiner: multi-market port of the day-trader-skill method.

Ported from github.com/Jakeschincariol/day-trader-skill (MIT licence, (c) Jake
Schincariol): the scanner setups, the opening-range-breakout entry timer, the
exit planner, the Time Machine (Monte Carlo replays + history replay), the
position sizer and the risk manager. The maths follows the originals; the
session, ticks, currency and data source are changed for Indian equities:

  * NSE: session 09:15 to 15:30 IST = 75 five-minute bars, weekdays, rupees, FYERS candles.
  * Crypto (Binance spot, Shark Exchange perpetuals): 24/7; a "day" runs from 05:30 IST
    (00:00 UTC, the exchanges' daily open) = 288 five-minute bars. The opening range is the
    first minutes after 05:30 IST. Times are shown in IST.
  * Prices round to each instrument's tick, stops away from the entry.
  * Candles everywhere are [epoch_seconds_utc, open, high, low, close, volume].

Pure functions only: no network, no files. routes.py does the I/O.
A simulation built from a stock's own recent bars is not a prediction.
"""
from __future__ import annotations

import datetime as dt
import math
import random
import statistics
from typing import Any

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
OPEN_MIN = 9 * 60 + 15        # 09:15 (NSE)
CLOSE_MIN = 15 * 60 + 30      # 15:30 (NSE)
BAR = 5
SESSION_BARS = (CLOSE_MIN - OPEN_MIN) // BAR   # 75
MIN_SESSIONS = 20
SAMPLE_PATHS = 120
BAND_PATHS = 2000
BAND_Q = (0.05, 0.25, 0.5, 0.75, 0.95)
HIST_BINS = 24
COIN_FLIP = 0.05
STRONG = 0.15
MANY_NEITHER = 0.40
MIN_RISK_PCT = 0.05            # a stop closer than 0.05 % of price is too tight
ATR_BARS = 14


class DeskError(ValueError):
    """A plain-English reason the request can't be answered."""


class Session:
    """A market's trading day in IST wall-clock: opens at `open_min`, lasts `bars` five-minute bars."""

    def __init__(self, name: str, open_min: int, bars: int, weekdays_only: bool):
        self.name, self.open_min, self.bars, self.weekdays_only = name, open_min, bars, weekdays_only
        self.length = bars * BAR

    def offset(self, local_min: int) -> int:
        """Minutes since the session opened for a wall-clock IST minute (wraps past midnight)."""
        return (local_min - self.open_min) % 1440

    def label(self, offset: int) -> str:
        return clock((self.open_min + offset) % 1440)

    def session_date(self, t: dt.datetime) -> str:
        local = t.astimezone(IST)
        return (local - dt.timedelta(minutes=self.open_min)).date().isoformat()

    def now_offset(self, t: dt.datetime) -> int:
        local = t.astimezone(IST)
        return self.offset(local.hour * 60 + local.minute)

    def is_open(self, t: dt.datetime) -> bool:
        if self.weekdays_only and t.astimezone(IST).weekday() >= 5:
            return False
        return self.now_offset(t) < self.length

    def to_offset(self, hhmm: str, what: str = "time") -> int:
        return self.offset(parse_clock(hhmm, what))


NSE = Session("NSE", OPEN_MIN, SESSION_BARS, True)
CRYPTO = Session("CRYPTO", 5 * 60 + 30, 288, False)    # 05:30 IST = 00:00 UTC
MCX = Session("MCX", 9 * 60, 174, True)                # 09:00-23:30 IST (bars after 23:30 in winter are left out)
SESSIONS = {"NSE": NSE, "CRYPTO": CRYPTO, "MCX": MCX}


def decimals(tick: float) -> int:
    return max(2, min(10, -int(math.floor(math.log10(tick))) if tick < 1 else 0)) if tick > 0 else 2


def fmtp(x: float, tick: float = 0.05) -> str:
    return f"{x:.{decimals(tick)}f}"


# ------------------------------------------------------------------ time and ticks

def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def clock(minutes: int) -> str:
    return "%02d:%02d" % (minutes // 60, minutes % 60)


def parse_clock(text: str, what: str = "time") -> int:
    try:
        h, m = str(text).strip().split(":")[:2]
        v = int(h) * 60 + int(m)
    except (ValueError, TypeError):
        raise DeskError(f"{what} must look like HH:MM (24-hour IST), got {text!r}")
    if not 0 <= v < 24 * 60:
        raise DeskError(f"{what} {text!r} is not a time of day")
    return v


def tick_floor(x: float, tick: float = 0.05) -> float:
    return round(math.floor(round(x / tick, 6)) * tick, decimals(tick))


def tick_ceil(x: float, tick: float = 0.05) -> float:
    return round(math.ceil(round(x / tick, 6)) * tick, decimals(tick))


def tick_round(x: float, tick: float = 0.05) -> float:
    return round(round(x / tick) * tick, decimals(tick))


def tick_away(x: float, long: bool, tick: float = 0.05) -> float:
    """Round a stop away from the entry: down for a buy, up for a sell."""
    return tick_floor(x, tick) if long else tick_ceil(x, tick)


# ------------------------------------------------------------------ bars

def parse_candles(candles: list, now: dt.datetime | None = None, interval_min: int = BAR,
                  S: Session = NSE) -> list[dict]:
    """Candles -> 5-minute bars tagged with their session date and slot. Drops the bar still
    forming and anything outside the session. `start` is the bar's IST wall-clock start."""
    now = now or now_ist()
    out = []
    for row in candles or []:
        try:
            ts, o, h, l, c = row[0], float(row[1]), float(row[2]), float(row[3]), float(row[4])
            v = float(row[5]) if len(row) > 5 and row[5] is not None else 0.0
        except (TypeError, ValueError, IndexError):
            continue
        start = dt.datetime.fromtimestamp(int(ts), IST)
        if start + dt.timedelta(minutes=interval_min) > now:
            continue                                     # still forming
        mins = start.hour * 60 + start.minute
        off = S.offset(mins)
        if off >= S.length or off % interval_min:
            continue
        out.append({"date": S.session_date(start), "slot": off // BAR, "off": off,
                    "start": clock(mins), "ts": int(ts), "o": o, "h": h, "l": l, "c": c, "v": v})
    out.sort(key=lambda b: b["ts"])
    return out


def split_sessions(bars: list[dict]) -> dict[str, dict[int, dict]]:
    days: dict[str, dict[int, dict]] = {}
    for b in bars:
        days.setdefault(b["date"], {})[b["slot"]] = b
    return days


def full_sessions(days: dict, before: str, nbars: int = SESSION_BARS) -> tuple[list, list]:
    """Full sessions (every bar present) strictly before `before`, oldest first, plus the
    short ones left out (Muhurat / special sessions, data gaps)."""
    full, short = [], []
    for d in sorted(days):
        if d >= before:
            continue
        s = days[d]
        if len(s) == nbars and all(i in s for i in range(nbars)):
            full.append((d, [s[i] for i in range(nbars)]))
        else:
            short.append({"date": d, "bars": len(s)})
    return full, short


def atr(bars: list[dict], n: int = ATR_BARS) -> float | None:
    if len(bars) < 2:
        return None
    trs = []
    for i in range(1, len(bars)):
        pc = bars[i - 1]["c"]
        b = bars[i]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    trs = trs[-n:]
    return sum(trs) / len(trs)


# ------------------------------------------------------------------ scanner (daily setups)

SETUPS = {
    "breakout_20d": "20-day closing-high breakout",
    "bull_flag": "Bull flag",
    "reclaim_ma20": "Reclaimed 20-day average",
    "oversold": "Oversold (RSI < 30)",
    "oversold_bounce": "Oversold bounce",
}


def rsi(closes: list[float], n: int = 14) -> float | None:
    """Wilder's RSI."""
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def find_setups(c: list[float]) -> tuple[list[str], dict]:
    """Daily closes, oldest first, at least 30."""
    if len(c) < 30:
        raise DeskError("need at least 30 daily closes")
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r, r_prev = rsi(c), rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": None if r is None else round(r, 1), "high20": round(hi20, 2), "ma20": round(ma20, 2),
                  "run_pct": round(run * 100, 2), "range5_pct": round(rng5 * 100, 2)}


def daily_from_candles(candles: list, now: dt.datetime | None = None, S: Session = NSE) -> list[dict]:
    """Complete daily bars only: NSE drops today's bar until 15:30 IST; crypto drops the bar
    whose 24 hours have not ended."""
    now = now or now_ist()
    today = now.date()
    done = now.hour * 60 + now.minute >= CLOSE_MIN
    out = {}
    for row in candles or []:
        try:
            if S is not NSE:
                if int(row[0]) + 86400 > now.timestamp():
                    continue
                d = dt.datetime.fromtimestamp(int(row[0]), dt.timezone.utc).date()
                out[d] = {"date": d.isoformat(), "o": float(row[1]), "h": float(row[2]), "l": float(row[3]),
                          "c": float(row[4]), "v": float(row[5] or 0)}
                continue
            d = dt.datetime.fromtimestamp(int(row[0]), IST).date()
            bar = {"date": d.isoformat(), "o": float(row[1]), "h": float(row[2]), "l": float(row[3]),
                   "c": float(row[4]), "v": float(row[5] or 0)}
        except (TypeError, ValueError, IndexError):
            continue
        if d > today or (d == today and not done):
            continue
        out[d] = bar
    return [out[k] for k in sorted(out)]


# ------------------------------------------------------------------ opening range breakout

def opening_range(D, n: int) -> tuple[float, float]:
    rows = [D[i] for i in range(n)]
    return max(b["h"] for b in rows), min(b["l"] for b in rows)


def entry_status(bars_today: list[dict], now: dt.datetime, or_minutes: int = 15, side: str = "buy",
                 no_before: str = "09:30", no_after: str = "15:00", level: float | None = None,
                 S: Session = NSE, tick: float = 0.05) -> dict:
    """The entry timer. ORB (or a custom level) on the current session's completed 5-minute
    bars: triggered = the first bar closing at/after `no_before` that CLOSES beyond the range
    high (buy) / low (sell), or beyond `level` when one is given."""
    long = side == "buy"
    if or_minutes % BAR or not BAR <= or_minutes <= 60:
        raise DeskError("opening range must be 5 to 60 minutes, in steps of 5")
    n = or_minutes // BAR
    nowoff = S.now_offset(now)
    nb, na = S.to_offset(no_before, "no entries before"), S.to_offset(no_after, "no entries after")
    today = S.session_date(now)
    bars = [b for b in bars_today if b["date"] == today]
    P = lambda x: fmtp(x, tick)
    base = {"side": side, "or_minutes": or_minutes, "bars": len(bars), "now": now.astimezone(IST).strftime("%H:%M"),
            "no_before": no_before, "no_after": no_after, "level": level, "session_date": today}
    if S.weekdays_only and now.astimezone(IST).weekday() >= 5:
        return {**base, "state": "closed", "message": "Weekend: the NSE is closed."}
    if S is NSE and now.astimezone(IST).hour * 60 + now.astimezone(IST).minute < OPEN_MIN:
        return {**base, "state": "pre-open", "message": "Market opens at 09:15 IST."}
    if len(bars) < n and level is None:
        oh = max((b["h"] for b in bars), default=None)
        ol = min((b["l"] for b in bars), default=None)
        st = "forming" if nowoff < S.length else "no-data"
        return {**base, "state": st, "or_high": oh, "or_low": ol,
                "message": f"Opening range forming: {len(bars)} of {n} bars (from {S.label(0)} IST)." if st == "forming"
                else "No intraday bars for today."}
    oh = ol = None
    if len(bars) >= n:
        oh, ol = opening_range(bars, n)
    trig_level = level if level is not None else (oh if long else ol)
    start_k = 0 if level is not None else n
    for k in range(start_k, len(bars)):
        b = bars[k]
        close_off = b["off"] + BAR
        if close_off < nb:
            continue
        if close_off > na:
            break
        if (b["c"] > trig_level) if long else (b["c"] < trig_level):
            return {**base, "state": "triggered", "or_high": oh, "or_low": ol, "trigger_level": trig_level,
                    "entry": tick_round(b["c"], tick), "entry_time": S.label(close_off), "entry_bar": b["slot"],
                    "bar": b, "message": f"Triggered: the {b['start']} bar closed at {P(b['c'])}, "
                    f"{'above' if long else 'below'} {P(trig_level)}."}
    last = bars[-1] if bars else None
    if nowoff >= na or nowoff >= S.length:
        return {**base, "state": "expired", "or_high": oh, "or_low": ol, "trigger_level": trig_level,
                "last": last and last["c"], "message": f"No trigger before {no_after} IST. Setup expired for this session."}
    gap = None if last is None else ((trig_level - last["c"]) if long else (last["c"] - trig_level))
    return {**base, "state": "waiting", "or_high": oh, "or_low": ol, "trigger_level": trig_level,
            "last": last and last["c"], "distance": gap,
            "message": f"Waiting for a 5-minute close {'above' if long else 'below'} {P(trig_level)}"
            + (f" (last {P(last['c'])})." if last else ".")}


def orb_scan_row(bars_today: list[dict], or_minutes: int = 15, S: Session = NSE) -> dict | None:
    n = or_minutes // BAR
    if len(bars_today) < n:
        return None
    oh, ol = opening_range(bars_today, n)
    last = bars_today[-1]["c"]
    up = next((b for b in bars_today[n:] if b["c"] > oh), None)
    dn = next((b for b in bars_today[n:] if b["c"] < ol), None)
    return {"or_high": oh, "or_low": ol, "last": last,
            "breakout": up and {"at": S.label(up["off"] + BAR), "price": up["c"]},
            "breakdown": dn and {"at": S.label(dn["off"] + BAR), "price": dn["c"]},
            "position": "above" if last > oh else "below" if last < ol else "inside"}


# ------------------------------------------------------------------ exit planner

def plan_exit(entry: float, side: str = "buy", stop_rule: str = "or_mid", stop_value: float | None = None,
              target_rule: str = "r", target_value: float = 2.0, or_high: float | None = None,
              or_low: float | None = None, bars: list[dict] | None = None, tick: float = 0.05) -> dict:
    """Stop and target before you're in. Stop rules: or_mid, or_far (range low for a
    buy / high for a sell), pct, atr (K x ATR14 of the bars to the entry), price."""
    long = side == "buy"
    P = lambda x: fmtp(x, tick)
    e = tick_round(float(entry), tick)
    if e <= 0:
        raise DeskError("the entry must be above 0")
    detail = ""
    if stop_rule in ("or_mid", "or_far"):
        if or_high is None or or_low is None or or_high <= or_low:
            raise DeskError("an opening-range stop needs the range high and low (high above low)")
        if stop_rule == "or_mid":
            raw, label = (or_high + or_low) / 2, "opening-range midpoint"
            detail = f"({P(or_high)} + {P(or_low)}) / 2"
        else:
            raw = or_low if long else or_high
            label = "opening-range low" if long else "opening-range high"
    elif stop_rule == "pct":
        if not stop_value or not 0 < stop_value < 50:
            raise DeskError("a percent stop needs a value between 0 and 50")
        raw = e * (1 - stop_value / 100) if long else e * (1 + stop_value / 100)
        label = f"{stop_value:g}% {'under' if long else 'over'} the entry"
    elif stop_rule == "atr":
        a = atr(bars or [])
        if a is None or not stop_value:
            raise DeskError("an ATR stop needs a multiple and today's bars up to the entry")
        raw = e - stop_value * a if long else e + stop_value * a
        label = f"{stop_value:g} x ATR({min(ATR_BARS, max(1, len(bars or []) - 1))})"
        detail = f"ATR {P(a)}"
    elif stop_rule == "price":
        if not stop_value:
            raise DeskError("a price stop needs a price")
        raw, label = stop_value, "your price"
    else:
        raise DeskError(f"unknown stop rule {stop_rule!r}")
    stop = tick_away(raw, long, tick)
    if (stop >= e) if long else (stop <= e):
        raise DeskError(f"the stop {P(stop)} ({label}) is on the wrong side of the entry {P(e)}: "
                        f"a {'buy' if long else 'sell'}'s stop goes {'under' if long else 'over'} it")
    if stop <= 0:
        raise DeskError("the stop works out at or below 0")
    risk = round(abs(e - stop), decimals(tick) + 2)
    if risk < e * MIN_RISK_PCT / 100:
        raise DeskError(f"the stop is only {P(risk)} from the entry, under {MIN_RISK_PCT}% of the price: "
                        "too tight to survive normal wiggle. Widen it.")
    if target_rule == "r":
        if not target_value or target_value <= 0:
            raise DeskError("the target needs an R multiple above 0")
        target = tick_round(e + target_value * risk if long else e - target_value * risk, tick)
    elif target_rule == "price":
        target = tick_round(float(target_value), tick)
    else:
        raise DeskError(f"unknown target rule {target_rule!r}")
    if (target <= e) if long else (target >= e):
        raise DeskError(f"the target {P(target)} is on the wrong side of the entry {P(e)}")
    rr = round(abs(target - e) / risk, 2)
    return {"side": side, "entry": e, "stop": stop, "stop_rule": stop_rule, "stop_label": label,
            "stop_detail": detail, "target": target, "target_r": rr, "risk_per_share": risk,
            "or_high": or_high, "or_low": or_low, "tick": tick}


# ------------------------------------------------------------------ the Time Machine

def build_pool(sessions, nbars: int = SESSION_BARS):
    pool = [[] for _ in range(nbars)]
    for _, D in sessions:
        for i in range(1, nbars):
            pc = D[i - 1]["c"]
            pool[i].append((D[i]["o"] / pc, D[i]["h"] / pc, D[i]["l"] / pc, D[i]["c"] / pc))
    return pool


def pct(a, q):
    return a[min(len(a) - 1, int(q * len(a)))]


def simulate(pool, entry, stop, target, k, last, replays=10000, seed=19, window=2, side="buy", nd: int = 2):
    """Monte Carlo: walk bars k+1..last from the entry, each bar drawn from past
    sessions' moves at about the same time of day (slot +/- window). Stop is
    checked before target inside a bar (the conservative call)."""
    nbars = len(pool)
    long = side == "buy"
    risk = (entry - stop) if long else (stop - entry)
    r_target = round(((target - entry) if long else (entry - target)) / risk, 4)

    def slot(rng, i):
        return i if window == 0 else min(nbars - 1, max(1, i + rng.randint(-window, window)))

    rng = random.Random(seed)
    choice = rng.choice
    outcomes, closes, paths = [], [], []
    for n in range(replays):
        p, outcome, keep = entry, None, n < SAMPLE_PATHS
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            hi, lo = p * h, p * l
            if outcome is None:
                if long:
                    if lo <= stop:
                        outcome = "stop"
                    elif hi >= target:
                        outcome = "target"
                else:
                    if hi >= stop:
                        outcome = "stop"
                    elif lo <= target:
                        outcome = "target"
            p = p * c
            if keep:
                path.append(round(p, nd))
        outcomes.append(outcome or "close")
        closes.append(p)
        if keep:
            paths.append(path)
    cnt = {x: outcomes.count(x) for x in ("target", "stop", "close")}
    green = sum(1 for o, p in zip(outcomes, closes)
                if o == "target" or (o == "close" and (p > entry if long else p < entry)))
    Rs = [r_target if o == "target" else -1.0 if o == "stop" else ((p - entry) if long else (entry - p)) / risk
          for o, p in zip(outcomes, closes)]
    srt = sorted(closes)
    rng = random.Random(seed)
    choice = rng.choice
    allp = []
    for _ in range(BAND_PATHS):
        p, path = entry, [entry]
        for i in range(k + 1, last + 1):
            p = p * choice(pool[slot(rng, i)])[3]
            path.append(p)
        allp.append(path)
    bands = []
    for j in range(last - k + 1):
        col = sorted(pp[j] for pp in allp)
        bands.append([round(pct(col, q), nd) for q in BAND_Q])
    lo_, hi_ = pct(srt, 0.01), pct(srt, 0.99)
    W = (hi_ - lo_) / HIST_BINS
    bins = [0] * HIST_BINS
    for c in srt:
        b = int((c - lo_) / W) if W > 0 else 0
        if 0 <= b < HIST_BINS:
            bins[b] += 1
    return {"replays": replays, "seed": seed, "window": window, "bars_left": last - k,
            "p_target": round(cnt["target"] / replays, 4), "p_stop": round(cnt["stop"] / replays, 4),
            "p_neither": round(cnt["close"] / replays, 4), "p_green": round(green / replays, 4),
            "median_close": round(pct(srt, 0.5), nd), "p5_close": round(pct(srt, 0.05), nd),
            "p95_close": round(pct(srt, 0.95), nd), "avg_R": round(statistics.mean(Rs), 3),
            "r_at_target": r_target, "bands": bands, "sample_paths": paths,
            "hist": {"lo": round(lo_, nd), "hi": round(hi_, nd), "bins": bins}}


def rule_stop(rule, oh, ol, entry, long, tick):
    r = rule["stop_rule"]
    if r == "or_mid":
        return tick_away((oh + ol) / 2, long, tick)
    if r == "or_far":
        return tick_away(ol if long else oh, long, tick)
    frac = rule["stop_pct"] / 100.0
    return tick_away(entry * (1 - frac) if long else entry * (1 + frac), long, tick)


def history_replay(sessions, rule, last, side="buy", tick=0.05, S: Session = NSE):
    """The same ORB rule on every past full session: first close beyond the range is the
    entry, then stop first, then target, else the close at `last`."""
    long = side == "buy"
    n = rule["or_minutes"] // BAR
    trades = []
    for d, D in sessions:
        oh, ol = opening_range(D, n)
        kk = next((i for i in range(n, last + 1) if (D[i]["c"] > oh if long else D[i]["c"] < ol)), None)
        if kk is None:
            continue
        e = tick_round(D[kk]["c"], tick)
        s = rule_stop(rule, oh, ol, e, long, tick)
        risk = (e - s) if long else (s - e)
        if risk <= 0:
            continue
        t = tick_round(e + rule["target_r"] * risk, tick) if long else tick_round(e - rule["target_r"] * risk, tick)
        oc = None
        for b in D[kk + 1:last + 1]:
            if (b["l"] <= s) if long else (b["h"] >= s):
                oc = "stop"
                break
            if (b["h"] >= t) if long else (b["l"] <= t):
                oc = "target"
                break
        cl = D[last]["c"]
        R = rule["target_r"] if oc == "target" else -1.0 if oc == "stop" else ((cl - e) if long else (e - cl)) / risk
        trades.append({"date": d, "time": S.label(BAR * (kk + 1)), "entry": e, "stop": s, "target": t,
                       "outcome": oc or "close", "R": round(R, 2)})
    return {"sessions": len(sessions), "triggered": len(trades),
            "target": sum(1 for x in trades if x["outcome"] == "target"),
            "stop": sum(1 for x in trades if x["outcome"] == "stop"),
            "close": sum(1 for x in trades if x["outcome"] == "close"),
            "green": sum(1 for x in trades if x["R"] > 0),
            "avg_R": round(statistics.mean(x["R"] for x in trades), 3) if trades else None,
            "rule": rule, "trades": trades}


def lean(mc):
    pt, ps = mc["p_target"], mc["p_stop"]
    if pt + ps <= 0:
        return None
    r = mc.get("r_at_target") or 1.0
    share, even = pt / (pt + ps), 1.0 / (1.0 + r)
    return share - even, share, even


def verdict(mc) -> dict:
    """{'tone': good|flat|bad|none, 'head': ..., 'text': ...}"""
    pt, ps = mc["p_target"], mc["p_stop"]
    lv = lean(mc)
    until = mc.get("until", "15:30")
    if lv is None:
        return {"tone": "none", "head": "No side reached",
                "text": f"No replay reached the target or the stop by {until}: the price at {until} decides every one."}
    d, share, even = lv
    r = mc.get("r_at_target") or 1.0
    one_r = abs(r - 1.0) < 0.005
    if abs(d) < COIN_FLIP:
        head, tone = ("Close to a coin flip" if one_r else "Close to break-even"), "flat"
    elif abs(d) < STRONG:
        head, tone = ("Leans toward the target", "good") if d > 0 else ("Leans toward the stop", "bad")
    else:
        head, tone = ("Strongly toward the target", "good") if d > 0 else ("Strongly toward the stop", "bad")
    odds = f"target first {100 * pt:.1f}% vs stop first {100 * ps:.1f}%"
    text = (f"{odds}." if one_r else
            f"At {round(r, 2):g}R the target has to come first in {100 * even:.0f}% of the replays that hit a side; "
            f"it did in {100 * share:.0f}% ({odds}).")
    notes = []
    if mc.get("p_neither", 0) > MANY_NEITHER:
        notes.append(f"{100 * mc['p_neither']:.1f}% of replays hit neither by {until}; the price at {until} decides those.")
    avg = mc.get("avg_R")
    if abs(d) >= COIN_FLIP and avg and (avg > 0) != (d > 0):
        notes.append(f"Average R is {avg:+.3f} all the same: replays that hit neither pull it the other way.")
    return {"tone": tone, "head": head, "text": text, "notes": notes, "break_even": round(even, 4),
            "share": round(share, 4)}


def time_machine(bars: list[dict], plan: dict, replays: int = 10000, seed: int = 19, window: int = 2,
                 until: str | None = None, tick: float = 0.05, S: Session = NSE, min_sessions: int = MIN_SESSIONS) -> dict:
    """bars: recent 5-minute bars incl. the plan's session. plan: date (session date),
    entry_time (IST HH:MM the entry bar CLOSED), entry, stop, target, side, optional
    or_minutes and stop_rule for the history replay. `until` = IST time to be out by."""
    if not 100 <= replays <= 200000:
        raise DeskError("replays must be between 100 and 200,000")
    if not 0 <= window <= 10:
        raise DeskError("window must be 0 to 10 bars")
    side = plan.get("side", "buy")
    long = side == "buy"
    nd = decimals(tick)
    P = lambda x: fmtp(x, tick)
    entry, stop, target = float(plan["entry"]), float(plan["stop"]), float(plan["target"])
    if (stop >= entry or target <= entry) if long else (stop <= entry or target >= entry):
        raise DeskError("for a buy: stop under entry, target above; for a sell the other way round")
    eo = S.to_offset(plan["entry_time"], "entry time")
    if eo % BAR or not BAR <= eo < S.length:
        raise DeskError(f"entry time must be a 5-minute bar close inside the session "
                        f"({S.label(BAR)} to {S.label(S.length - BAR)} IST)")
    k = eo // BAR - 1
    uo = S.length if until is None else S.to_offset(until, "until")
    if uo == 0:
        uo = S.length                      # "until" at the session open means its end
    if uo % BAR or uo > S.length:
        raise DeskError(f"'until' must be on the 5-minute grid, no later than {S.label(0)} IST (session end)")
    last = uo // BAR - 1
    if last <= k:
        raise DeskError(f"out-by {S.label(uo)} leaves nothing to replay after a {plan['entry_time']} entry")
    days = split_sessions(bars)
    date = plan["date"]
    entry_day = days.get(date, {})
    warn = []
    if k in entry_day:
        eb = entry_day[k]
        if not eb["l"] - tick <= entry <= eb["h"] + tick:
            warn.append(f"the entry {P(entry)} is outside the {eb['start']} bar's range {P(eb['l'])}-{P(eb['h'])}")
    else:
        warn.append(f"no completed bar closing {plan['entry_time']} on {date} in the data yet; "
                    "replaying as a hypothetical entry")
    sessions, short = full_sessions(days, date, S.bars)
    if len(sessions) < min_sessions:
        raise DeskError(f"only {len(sessions)} full session(s) before {date}; the Time Machine needs at least "
                        f"{min_sessions}. New contracts (weekly options) have little history: replay the "
                        "underlying future instead and translate the levels.")
    if len(sessions) < MIN_SESSIONS:
        warn.append(f"only {len(sessions)} past sessions in the pool (options are young contracts): treat the odds "
                    "as rough")
    pool = build_pool(sessions, S.bars)
    mc = simulate(pool, entry, stop, target, k, last, replays, seed, window, side, nd)
    mc.update(until=S.label(uo), pool_sessions=len(sessions), pool_from=sessions[0][0], pool_to=sessions[-1][0],
              band_times=[S.label(BAR * (j + 1)) for j in range(k, last + 1)])
    history = None
    orm = int(plan.get("or_minutes") or 0)
    if orm and orm % BAR == 0 and orm // BAR <= k + 1:
        risk = abs(entry - stop)
        sr = plan.get("stop_rule")
        rule = {"setup": "orb", "or_minutes": orm, "target_r": round(abs(target - entry) / risk, 2),
                "stop_rule": sr if sr in ("or_mid", "or_far") else "pct",
                "stop_pct": round(100 * risk / entry, 4)}
        history = history_replay(sessions, rule, last, side, tick, S)
    return {"plan": {**plan, "entry_bar": k}, "mc": mc, "history": history, "verdict": verdict(mc),
            "warnings": warn, "short_sessions_left_out": short,
            "note": f"Built from the instrument's own last {len(sessions)} full sessions. Not a prediction: results, "
                    "news, expiries, policy days and liquidations can do things those sessions never did. "
                    "Same seed, same numbers. Costs and slippage not included."}


# ------------------------------------------------------------------ sizing and risk

MARKET_LABEL = {}
DEFAULT_CONFIG = {
    "budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 50, "max_order_value": 100000,
    "max_loss_per_trade": 500, "max_daily_loss": 1500, "max_trades_per_day": 3, "min_reward_risk": 1.5,
    "require_stop": True, "no_entries_before": "09:30", "no_entries_after": "15:00", "flat_by": "15:15",
    "allow_short": False, "blocked": [], "tick": 0.05, "or_minutes": 15, "target_r": 2.0,
    "cost_per_order": 20, "currency": "INR", "watchlist": [],
}
MCX_CONFIG = {**DEFAULT_CONFIG, "no_entries_before": "09:15", "no_entries_after": "23:00", "flat_by": "23:15",
              "cost_per_order": 40, "tick": None}
# Crypto: a day runs 05:30 IST to 05:30 IST. Defaults: no entries in the first 15 minutes or the last
# 2 hours, flat by 04:30 IST. Money in the quote currency (USDT on Binance; INR or USDT on Shark).
CRYPTO_CONFIG = {
    **DEFAULT_CONFIG, "budget": 1000, "max_order_value": 1000, "max_loss_per_trade": 10, "max_daily_loss": 30,
    "no_entries_before": "05:45", "no_entries_after": "03:30", "flat_by": "04:30", "cost_per_order": 0.0,
    "fee_bps": 10, "allow_short": False, "tick": None, "currency": "USDT",
}


def floor_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    n = math.floor(round(x / step, 9))
    return round(n * step, max(0, decimals(step) if step < 1 else 0))


def size_position(plan: dict, cfg: dict, max_loss: float | None = None, qty_step: float = 1.0,
                  min_qty: float | None = None) -> dict:
    """Quantity that fits both the max loss and the budget caps, floored to the instrument's step."""
    c = {**DEFAULT_CONFIG, **(cfg or {})}
    entry, rps = float(plan["entry"]), float(plan["risk_per_share"])
    if rps <= 0 or entry <= 0:
        raise DeskError("the plan needs an entry and a positive risk per unit")
    ml = float(max_loss if max_loss is not None else c["max_loss_per_trade"])
    by_risk = floor_step(ml / rps, qty_step)
    investable = float(c["budget"]) * (1 - float(c["cash_buffer_pct"]) / 100)
    cap = min(investable * float(c["max_position_pct"]) / 100, float(c["max_order_value"]))
    by_cap = floor_step(cap / entry, qty_step)
    qty = max(0.0, min(by_risk, by_cap))
    binding = "max loss" if by_risk <= by_cap else "position cap"
    notes = []
    if min_qty and 0 < qty < min_qty:
        notes.append(f"{qty:g} is under the exchange minimum of {min_qty:g}.")
        qty = 0.0
    if qty == 0:
        notes.append("Not even the minimum quantity fits: widen the max loss, tighten the stop, or skip the trade.")
    if qty_step >= 1:
        qty, by_risk, by_cap = int(qty), int(by_risk), int(by_cap)
    fee = float(c.get("fee_bps") or 0) / 10000 * qty * entry * 2
    costs = 2 * float(c.get("cost_per_order") or 0) + fee
    target = plan.get("target")
    return {"qty": qty, "by_risk": by_risk, "by_cap": by_cap, "binding": binding, "max_loss": ml,
            "cap_value": round(cap, 2), "order_value": round(qty * entry, 2),
            "loss_at_stop": round(qty * rps, 2),
            "gain_at_target": round(qty * abs(float(target) - entry), 2) if target else None,
            "est_costs": round(costs, 2), "qty_step": qty_step, "currency": c.get("currency", "INR"), "notes": notes}


def risk_check(order: dict, cfg: dict, ledger: list[dict], now: dt.datetime | None = None,
               S: Session = NSE) -> dict:
    """PASS / BLOCK an entry order against the limits. order: symbol, side, qty, entry, stop, target.
    `ledger` = this market's paper trades."""
    c = {**DEFAULT_CONFIG, **(cfg or {})}
    cur = "₹" if c.get("currency", "INR") == "INR" else c.get("currency") + " "
    now = now or now_ist()
    today = S.session_date(now)
    reasons, warns, checks = [], [], []

    def chk(ok, label, why=None):
        checks.append({"ok": bool(ok), "label": label})
        if not ok:
            reasons.append(why or label)

    sym = str(order.get("symbol", "")).upper()
    side = order.get("side", "buy")
    qty = float(order.get("qty") or 0)
    entry = float(order.get("entry") or 0)
    stop = order.get("stop")
    target = order.get("target")
    m = lambda x: f"{cur}{x:,.2f}"
    chk(qty > 0, "Quantity above zero", "quantity is 0")
    chk(sym not in {s.upper() for s in c.get("blocked") or []}, "Symbol not on the blocked list", f"{sym} is blocked")
    chk(side == "buy" or c.get("allow_short"), "Short selling allowed",
        "short (sell-first) trades are off in your limits")
    chk(stop is not None or not c.get("require_stop"), "Has a stop-loss", "no stop-loss: every entry needs one")
    loss = 0.0
    if stop is not None and entry:
        stop = float(stop)
        wrong = (stop >= entry) if side == "buy" else (stop <= entry)
        chk(not wrong, "Stop on the right side of the entry", "stop is on the wrong side of the entry")
        loss = abs(entry - stop) * qty
        chk(loss <= float(c["max_loss_per_trade"]) + 0.005, f"Loss at stop ≤ {m(c['max_loss_per_trade'])}",
            f"loss at stop {m(loss)} is over the {m(c['max_loss_per_trade'])} per-trade limit")
        if target is not None and abs(entry - stop) > 0:
            rr = abs(float(target) - entry) / abs(entry - stop)
            chk(rr + 1e-9 >= float(c["min_reward_risk"]), f"Reward:risk ≥ {c['min_reward_risk']}",
                f"reward:risk {rr:.2f} is under your minimum {c['min_reward_risk']}")
    value = qty * entry
    chk(value <= float(c["max_order_value"]) + 0.005, f"Order value ≤ {m(c['max_order_value'])}",
        f"order value {m(value)} is over the {m(c['max_order_value'])} limit")
    todays = [r for r in ledger if r.get("date") == today]
    chk(len(todays) < int(c["max_trades_per_day"]), f"Under {c['max_trades_per_day']} trades this session",
        f"already {len(todays)} trade(s) this session, the limit is {c['max_trades_per_day']}")
    realized = sum(min(0.0, float(r.get("pnl") or 0)) for r in todays if r.get("status") == "closed")
    at_risk = sum(float(r.get("max_loss") or 0) for r in todays if r.get("status") == "open")
    worst = -realized + at_risk + loss
    chk(worst <= float(c["max_daily_loss"]) + 0.005, f"Worst-case day ≤ {m(c['max_daily_loss'])}",
        f"this session's losses + open risk + this trade = {m(worst)}, over the {m(c['max_daily_loss'])} daily limit")
    chk(sym not in {str(r.get("symbol")).upper() for r in ledger if r.get("status") == "open"},
        "No open position in this symbol", f"you already hold an open {sym} position")
    nb, na = S.to_offset(c["no_entries_before"]), S.to_offset(c["no_entries_after"])
    off = S.now_offset(now)
    open_day = not S.weekdays_only or now.astimezone(IST).weekday() < 5
    chk(open_day and nb <= off <= na, f"Inside entry window {c['no_entries_before']}–{c['no_entries_after']} IST",
        f"outside the entry window {c['no_entries_before']}–{c['no_entries_after']} IST"
        + (" on a weekday" if S.weekdays_only else "")
        + f" (now {now.astimezone(IST).strftime('%H:%M')}{'' if open_day else ', weekend'})")
    return {"pass": not reasons, "reasons": reasons, "warnings": warns, "checks": checks,
            "loss_at_stop": round(loss, 2), "order_value": round(value, 2), "worst_case_day": round(worst, 2),
            "currency": c.get("currency", "INR")}
