# Reused from SmartEntry AI (backend/app/core/smc.py), unchanged apart from the imports.
"""Smart Money Concepts: market structure, BOS/CHoCH, liquidity sweeps and fair value gaps.

Pure and deterministic, on closed candles only. A swing point exists only once SWING_SPAN candles have closed on
its right, and every event is stamped with the candle that confirmed it, so nothing here repaints.

Definitions used:
- Swing high/low: the highest high (lowest low) of SWING_SPAN candles on each side.
- HH/LH: a swing high above/below the previous swing high. HL/LL: a swing low above/below the previous swing low.
- BOS / CHoCH: a candle CLOSES beyond the latest unbroken swing high (bullish) or low (bearish). With the current
  trend it is a break of structure (BOS); against it, a change of character (CHoCH) that flips the trend.
- Buy-side liquidity (BSL): resting above untouched swing highs. Sell-side liquidity (SSL): below untouched swing
  lows. A sweep is a candle that wicks through the level but closes back inside it.
- Fair value gap (FVG): candle 3 does not overlap candle 1 (bullish: low3 > high1; bearish: high3 < low1), at least
  FVG_MIN_ATR x ATR wide. It is filled once price trades back through the whole gap. Three candles that span a
  session break (overnight, weekend) are an opening gap, not an FVG, and are skipped.
- Retest of a break: the first later candle that comes back to a BOS/CHoCH level (within RETEST_TOLERANCE_ATR x
  ATR). It held if the candle closes back on the breakout side, failed if it closes through the level. Only within
  RETEST_WINDOW candles of the break and before an opposite break.
- FVG retest: the first candle that trades into an unfilled FVG and closes back outside it in the gap's direction.
- Fake breakout (trap): a candle closed beyond a BOS/CHoCH level, but within FAKE_BREAKOUT_WINDOW candles a close
  comes back through it. A failed break up is a bull trap; a failed break down is a bear trap.
- Session gaps: the first candle after a session break (overnight, weekend) moves on the gap, not on trading. It
  can still move structure (a BOS/CHoCH is recorded, marked gap=True), but it never counts as a sweep, a trap, a
  retest or the CHoCH that confirms a reversal.
- Reversal: liquidity taken (a buy-side sweep or bull trap for a bearish reversal; a sell-side sweep or bear trap
  for a bullish one), then a CHoCH the other way within REVERSAL_LOOKBACK candles. Invalidation is the extreme of
  the sweep or trap.
"""

from dataclasses import asdict, dataclass

from .candle import Candle
from .ind import atr_list as atr

SWING_SPAN = 5
LIQUIDITY_LOOKBACK = 300  # candles; older untouched swings stop counting as liquidity
FVG_MIN_ATR = 0.1
RETEST_TOLERANCE_ATR = 0.1
RETEST_WINDOW = 50  # candles after a break
FAKE_BREAKOUT_WINDOW = 3  # candles after a break in which a close back through it makes it a trap
REVERSAL_LOOKBACK = 20  # candles between the liquidity grab and the CHoCH


@dataclass
class Swing:
    time: int
    price: float
    kind: str  # high | low
    label: str | None  # HH | LH | HL | LL, None for the first of its kind
    confirmed_time: int


@dataclass
class StructureBreak:
    type: str  # BOS | CHoCH
    direction: str  # bullish | bearish
    level: float
    swing_time: int
    time: int  # the candle whose close broke the level
    gap: bool = False  # broken by a session-opening gap candle


@dataclass
class Sweep:
    side: str  # buy (BSL above highs taken) | sell (SSL below lows taken)
    level: float
    swing_time: int
    time: int
    extreme: float  # how far the wick went


@dataclass
class Gap:
    direction: str  # bullish | bearish
    top: float
    bottom: float
    time: int  # the middle candle
    created_time: int  # the third candle, when the gap is known
    filled_time: int | None = None
    retest_time: int | None = None
    touched: bool = False  # price has entered the gap at least once


@dataclass
class Retest:
    kind: str  # structure | fvg
    direction: str  # bullish | bearish: the direction the level should hold in
    level: float  # the broken level, or the gap edge that was retested
    source_time: int  # the break candle, or the gap's middle candle
    time: int
    held: bool


@dataclass
class Trap:
    """A fake breakout: a close beyond a structure level that failed within a few candles."""

    kind: str  # bull_trap (failed break up) | bear_trap (failed break down)
    level: float
    break_time: int  # the candle that closed beyond the level
    time: int  # the candle that closed back through it
    extreme: float  # the highest high (bull trap) / lowest low (bear trap) of the false move


@dataclass
class Reversal:
    direction: str  # bullish | bearish
    time: int  # the CHoCH candle that confirmed it
    level: float  # the CHoCH level
    trigger: str  # sweep | fake breakout
    trigger_time: int
    trigger_level: float
    invalidation: float  # beyond this extreme the reversal idea is wrong


@dataclass
class SmcReport:
    trend: str  # bullish | bearish | neutral
    swings: list[Swing]
    structure: list[StructureBreak]
    sweeps: list[Sweep]
    fvgs: list[Gap]
    retests: list[Retest]
    traps: list[Trap]
    reversals: list[Reversal]
    buy_side: list[Swing]  # untouched swing highs above the last close, nearest first
    sell_side: list[Swing]  # untouched swing lows below the last close, nearest first
    summary: str


def _is_pivot(bars: list[Candle], j: int, kind: str, span: int) -> bool:
    window = bars[j - span : j + span + 1]
    if kind == "high":
        return bars[j].high == max(b.high for b in window) and all(b.high < bars[j].high for b in bars[j + 1 : j + span + 1])
    return bars[j].low == min(b.low for b in window) and all(b.low > bars[j].low for b in bars[j + 1 : j + span + 1])


def _typical_step(bars: list[Candle]) -> int:
    """The usual time between candles (the median), so session breaks stand out."""
    gaps = sorted(b.time - a.time for a, b in zip(bars, bars[1:]))
    return gaps[len(gaps) // 2] if gaps else 0


def analyze_smc(bars: list[Candle], span: int = SWING_SPAN) -> SmcReport:
    swings: list[Swing] = []
    structure: list[StructureBreak] = []
    sweeps: list[Sweep] = []
    fvgs: list[Gap] = []
    trend = "neutral"
    last_high: Swing | None = None  # latest swing high not yet closed above
    last_low: Swing | None = None
    prev_high_price: float | None = None
    prev_low_price: float | None = None
    pools: list[tuple[Swing, int]] = []  # untouched swings acting as liquidity, with their bar index
    atrs = atr(bars)
    step = _typical_step(bars)
    retests: list[Retest] = []
    awaiting: list[tuple[StructureBreak, int]] = []  # breaks not yet retested, with their bar index
    traps: list[Trap] = []
    reversals: list[Reversal] = []
    fresh: list[tuple[StructureBreak, int, float]] = []  # recent breaks that could still fail, with the false move's extreme
    grabs: list[tuple[str, int, int, float, float, str]] = []  # (direction, bar, time, level, extreme, kind) liquidity grabs

    for i, b in enumerate(bars):
        gap_bar = i > 0 and step > 0 and b.time - bars[i - 1].time > 2 * step  # opens a new session

        # 1. Confirm the swing whose right-hand side just completed.
        j = i - span
        if j >= span:
            for kind in ("high", "low"):
                if not _is_pivot(bars, j, kind, span):
                    continue
                price = bars[j].high if kind == "high" else bars[j].low
                if kind == "high":
                    label = None if prev_high_price is None else ("HH" if price > prev_high_price else "LH")
                    prev_high_price = price
                else:
                    label = None if prev_low_price is None else ("HL" if price > prev_low_price else "LL")
                    prev_low_price = price
                swing = Swing(bars[j].time, price, kind, label, b.time)
                swings.append(swing)
                pools.append((swing, j))
                if kind == "high":
                    last_high = swing
                else:
                    last_low = swing

        # 2. Retests of earlier breaks (checked before new breaks, so a break candle never retests itself).
        tol = RETEST_TOLERANCE_ATR * (atrs[i] or 0)
        still = []
        for brk, idx in awaiting:
            if i - idx > RETEST_WINDOW:
                continue
            if gap_bar:
                still.append((brk, idx))
            elif brk.direction == "bullish" and b.low <= brk.level + tol:
                retests.append(Retest("structure", "bullish", brk.level, brk.time, b.time, b.close >= brk.level))
            elif brk.direction == "bearish" and b.high >= brk.level - tol:
                retests.append(Retest("structure", "bearish", brk.level, brk.time, b.time, b.close <= brk.level))
            else:
                still.append((brk, idx))
        awaiting = still

        # 2b. Fake breakouts: a recent break closed back through within a few candles.
        alive = []
        for brk, idx, extreme in fresh:
            if i - idx > FAKE_BREAKOUT_WINDOW:
                continue
            if gap_bar:
                continue  # a gap through the level is not traders failing at it; stop watching this break
            if brk.direction == "bullish":
                extreme = max(extreme, b.high)
                if b.close < brk.level:
                    traps.append(Trap("bull_trap", brk.level, brk.time, b.time, extreme))
                    grabs.append(("bearish", i, b.time, brk.level, extreme, "fake breakout"))
                    continue
            else:
                extreme = min(extreme, b.low)
                if b.close > brk.level:
                    traps.append(Trap("bear_trap", brk.level, brk.time, b.time, extreme))
                    grabs.append(("bullish", i, b.time, brk.level, extreme, "fake breakout"))
                    continue
            alive.append((brk, idx, extreme))
        fresh = alive
        new_breaks: list[StructureBreak] = []

        # 3. Break of structure / change of character, on the close.
        if last_high is not None and b.close > last_high.price:
            kind = "BOS" if trend in ("bullish", "neutral") else "CHoCH"
            brk = StructureBreak(kind, "bullish", last_high.price, last_high.time, b.time, gap_bar)
            structure.append(brk)
            if not gap_bar:
                new_breaks.append(brk)
                fresh.append((brk, i, b.high))
            awaiting = [(x, k) for x, k in awaiting if x.direction == "bullish"] + [(brk, i)]
            trend, last_high = "bullish", None
        if last_low is not None and b.close < last_low.price:
            kind = "BOS" if trend in ("bearish", "neutral") else "CHoCH"
            brk = StructureBreak(kind, "bearish", last_low.price, last_low.time, b.time, gap_bar)
            structure.append(brk)
            if not gap_bar:
                new_breaks.append(brk)
                fresh.append((brk, i, b.low))
            awaiting = [(x, k) for x, k in awaiting if x.direction == "bearish"] + [(brk, i)]
            trend, last_low = "bearish", None

        # 4. Liquidity: a close beyond a pool takes it; a wick beyond it with a close back inside sweeps it.
        remaining = []
        for swing, idx in pools:
            if i - idx > LIQUIDITY_LOOKBACK:
                continue  # too old to count
            if b.time <= swing.confirmed_time or gap_bar:
                remaining.append((swing, idx))  # only candles after confirmation can take it; gaps don't sweep
                continue
            if swing.kind == "high":
                if b.close > swing.price:
                    continue  # taken by a close (structure), not a sweep
                if b.high > swing.price:
                    sweeps.append(Sweep("buy", swing.price, swing.time, b.time, b.high))
                    grabs.append(("bearish", i, b.time, swing.price, b.high, "sweep"))
                    continue
            else:
                if b.close < swing.price:
                    continue
                if b.low < swing.price:
                    sweeps.append(Sweep("sell", swing.price, swing.time, b.time, b.low))
                    grabs.append(("bullish", i, b.time, swing.price, b.low, "sweep"))
                    continue
            remaining.append((swing, idx))
        pools = remaining

        # 4b. Reversal: a CHoCH that follows a liquidity grab the other way.
        grabs = [g for g in grabs if i - g[1] <= REVERSAL_LOOKBACK]
        for brk in new_breaks:
            if brk.type != "CHoCH":
                continue
            matching = [g for g in grabs if g[0] == brk.direction]
            if not matching:
                continue
            g = matching[-1]
            extreme = max(x[4] for x in matching) if brk.direction == "bearish" else min(x[4] for x in matching)
            reversals.append(Reversal(brk.direction, brk.time, brk.level, g[5], g[2], g[3], extreme))
            grabs = [x for x in grabs if x[0] != brk.direction]  # each grab confirms one reversal

        # 5. Fair value gaps: fill or retest old ones, then look for a new one ending on this candle.
        for g in fvgs:
            if g.filled_time is None and b.time > g.created_time:
                if (g.direction == "bullish" and b.low <= g.bottom) or (g.direction == "bearish" and b.high >= g.top):
                    g.filled_time = b.time
                    g.touched = True
                elif not g.touched and g.direction == "bullish" and b.low <= g.top:
                    g.touched = True
                    if b.close > g.top:
                        g.retest_time = b.time
                        retests.append(Retest("fvg", "bullish", g.top, g.time, b.time, True))
                elif not g.touched and g.direction == "bearish" and b.high >= g.bottom:
                    g.touched = True
                    if b.close < g.bottom:
                        g.retest_time = b.time
                        retests.append(Retest("fvg", "bearish", g.bottom, g.time, b.time, True))
        if i >= 2 and b.time - bars[i - 2].time <= 2 * step * 1.5:
            first = bars[i - 2]
            min_size = FVG_MIN_ATR * (atrs[i] or 0)
            if b.low > first.high and b.low - first.high > min_size:
                fvgs.append(Gap("bullish", b.low, first.high, bars[i - 1].time, b.time))
            elif b.high < first.low and first.low - b.high > min_size:
                fvgs.append(Gap("bearish", first.low, b.high, bars[i - 1].time, b.time))

    last = bars[-1].close if bars else 0.0
    buy_side = sorted((s for s, _ in pools if s.kind == "high" and s.price > last), key=lambda s: s.price)
    sell_side = sorted((s for s, _ in pools if s.kind == "low" and s.price < last), key=lambda s: -s.price)
    summary = _summary(trend, structure, sweeps, fvgs, buy_side, sell_side, last, retests)
    recent = bars[-30].time if len(bars) >= 30 else (bars[0].time if bars else 0)
    if reversals and reversals[-1].time >= recent:
        r = reversals[-1]
        summary += f" A {r.direction} reversal was confirmed recently (CHoCH at {r.level:,.2f} after a {r.trigger}); invalid beyond {r.invalidation:,.2f}."
    if traps and traps[-1].time >= recent:
        t = traps[-1]
        summary += f" Recent fake breakout: a {'bull' if t.kind == 'bull_trap' else 'bear'} trap at {t.level:,.2f}."
    return SmcReport(trend, swings, structure, sweeps, fvgs, retests, traps, reversals, buy_side[:5], sell_side[:5], summary)


def _summary(trend, structure, sweeps, fvgs, buy_side, sell_side, last, retests=()) -> str:
    if not structure:
        return "Not enough confirmed swings yet to read market structure."
    s = structure[-1]
    parts = [f"Structure is {trend}: the last event was a {s.direction} {s.type} at {s.level:,.2f}."]
    structure_retests = [t for t in retests if t.kind == "structure"]
    if structure_retests and structure_retests[-1].source_time == s.time:
        t = structure_retests[-1]
        parts.append(f"That break was retested at {t.level:,.2f} and {'held' if t.held else 'failed (closed back through it)'}.")
    else:
        parts.append("That break has not been retested yet.")
    if sweeps:
        w = sweeps[-1]
        parts.append(f"Latest sweep took {'buy-side liquidity above' if w.side == 'buy' else 'sell-side liquidity below'} {w.level:,.2f}.")
    open_gaps = [g for g in fvgs if g.filled_time is None]
    above = [g for g in open_gaps if g.bottom > last]
    below = [g for g in open_gaps if g.top < last]
    if below:
        g = max(below, key=lambda g: g.top)
        parts.append(f"Nearest unfilled FVG below: {g.bottom:,.2f}–{g.top:,.2f} ({g.direction}).")
    if above:
        g = min(above, key=lambda g: g.bottom)
        parts.append(f"Nearest unfilled FVG above: {g.bottom:,.2f}–{g.top:,.2f} ({g.direction}).")
    if buy_side:
        parts.append(f"Buy-side liquidity above at {buy_side[0].price:,.2f}.")
    if sell_side:
        parts.append(f"Sell-side liquidity below at {sell_side[0].price:,.2f}.")
    return " ".join(parts)


def trend_at(report: SmcReport):
    """Structure trend as of any candle time: the direction of the latest BOS/CHoCH at or before it."""
    breaks = [(s.time, s.direction) for s in report.structure]

    def at(time: int) -> str:
        current = "neutral"
        for t, direction in breaks:
            if t > time:
                break
            current = direction
        return current

    return at


def smc_payload(r: SmcReport, max_items: int = 120, max_open_gaps: int = 15, max_filled_gaps: int = 10) -> dict:
    """Recent events only: the chart cannot show hundreds of labels legibly."""
    open_gaps = [g for g in r.fvgs if g.filled_time is None][-max_open_gaps:] if max_open_gaps else []
    filled = [g for g in r.fvgs if g.filled_time is not None][-max_filled_gaps:] if max_filled_gaps else []
    gaps = open_gaps + filled
    return {
        "trend": r.trend,
        "summary": r.summary,
        "swings": [asdict(s) for s in r.swings[-max_items:]],
        "structure": [asdict(s) for s in r.structure[-max_items // 3 :]],
        "sweeps": [asdict(s) for s in r.sweeps[-max_items // 3 :]],
        "fvgs": sorted(({k: v for k, v in asdict(g).items() if k != "touched"} for g in gaps), key=lambda g: g["time"]),
        "retests": [asdict(t) for t in r.retests[-max_items // 3 :]],
        "traps": [asdict(t) for t in r.traps[-max_items // 3 :]],
        "reversals": [asdict(t) for t in r.reversals[-max_items // 3 :]],
        "buy_side": [{"price": s.price, "time": s.time} for s in r.buy_side],
        "sell_side": [{"price": s.price, "time": s.time} for s in r.sell_side],
    }
