"""Candles for the Signal Lab: one type, validation and resampling.

A candle's `time` is the epoch second its interval STARTS (UTC). Only closed candles are analysed: the candle still
forming is dropped by `closed_only`, so nothing computed here can change after the fact (no repainting).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, asdict

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
TF_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800}


@dataclass(frozen=True)
class Candle:
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def from_rows(rows) -> list[Candle]:
    """[epoch_s, o, h, l, c, v] rows -> Candles (bad rows skipped)."""
    out = []
    for r in rows or []:
        try:
            out.append(Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5] if len(r) > 5 and r[5] is not None else 0)))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def validate(bars: list[Candle], step: int, now: float | None = None, session_gap_ok: bool = True) -> tuple[list[Candle], dict]:
    """Sort, drop duplicates and impossible candles, and report gaps and staleness.

    Returns the cleaned candles and a quality report: duplicates removed, bad candles removed (high < low, close or
    open outside the range, non-positive prices), gaps (missing candles inside what should be continuous trading;
    gaps longer than 6 hours are treated as session breaks, not missing data) and whether the last candle is stale
    (older than 3 intervals, measured from `now`)."""
    seen: dict[int, Candle] = {}
    dupes = bad = 0
    for b in bars:
        if not (b.low > 0 and b.high >= b.low and b.low <= b.open <= b.high and b.low <= b.close <= b.high):
            bad += 1
            continue
        if b.time in seen:
            dupes += 1
        seen[b.time] = b
    clean = [seen[t] for t in sorted(seen)]
    gaps = []
    for a, b in zip(clean, clean[1:]):
        d = b.time - a.time
        if d > step and (not session_gap_ok or d <= 6 * 3600) and step < 86400:
            gaps.append({"after": a.time, "missing": d // step - 1})
    stale = None
    if now is not None and clean:
        stale = now - (clean[-1].time + step) > 3 * step
    return clean, {"candles": len(clean), "duplicates_removed": dupes, "bad_removed": bad,
                   "gaps": len(gaps), "missing_candles": sum(g["missing"] for g in gaps), "stale": stale,
                   "first": clean[0].time if clean else None, "last": clean[-1].time if clean else None}


def closed_only(bars: list[Candle], step: int, now: float) -> list[Candle]:
    """Drop the candle that has not closed yet."""
    return [b for b in bars if b.time + step <= now]


def bucket(t: int, tf: str, anchor_min: int = 0) -> int:
    """Start of the `tf` bucket that contains epoch second `t`. Intraday buckets are aligned to the session open
    (`anchor_min` minutes after midnight IST, e.g. 555 for 09:15 NSE) so 1h candles run 09:15-10:15. Daily and
    weekly buckets follow the IST calendar for Indian markets (anchor_min > 0) and UTC otherwise."""
    s = TF_SECONDS[tf]
    if tf == "1w":
        tz = IST if anchor_min else dt.timezone.utc
        d = dt.datetime.fromtimestamp(t, tz).date()
        monday = d - dt.timedelta(days=d.weekday())
        return int(dt.datetime(monday.year, monday.month, monday.day, tzinfo=tz).timestamp())
    if tf == "1d":
        tz = IST if anchor_min else dt.timezone.utc
        d = dt.datetime.fromtimestamp(t, tz).date()
        return int(dt.datetime(d.year, d.month, d.day, tzinfo=tz).timestamp())
    if anchor_min:
        local = dt.datetime.fromtimestamp(t, IST)
        day0 = int(dt.datetime(local.year, local.month, local.day, tzinfo=IST).timestamp())
        start = day0 + anchor_min * 60
        if t < start:
            return t - (t - day0) % s
        return start + ((t - start) // s) * s
    return t - t % s


def resample(bars: list[Candle], tf: str, anchor_min: int = 0) -> list[Candle]:
    """Aggregate candles into `tf` candles. The last bucket may be incomplete: callers that need closed candles
    pass the result through `closed_only` with the target step."""
    out: list[Candle] = []
    cur = None
    for b in bars:
        k = bucket(b.time, tf, anchor_min)
        if cur and cur[0] == k:
            cur[2] = max(cur[2], b.high)
            cur[3] = min(cur[3], b.low)
            cur[4] = b.close
            cur[5] += b.volume
        else:
            if cur:
                out.append(Candle(*cur))
            cur = [k, b.open, b.high, b.low, b.close, b.volume]
    if cur:
        out.append(Candle(*cur))
    return out


def ist(t: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(t, IST)
