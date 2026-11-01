/* MarketMiner Trend (MM Trend): trend band + Buy/Sell + TP1/TP2/TP3 tracking, for any market Signal Lab charts.
 * Original MarketMiner indicator: an ATR trailing stop (supertrend-style) drawn as a thick smoothed band, green in an
 * uptrend and red in a downtrend. A Buy/Sell is given when the band flips on a CLOSED candle (never repaints); its stop
 * is the band at that candle, TP1/TP2/TP3 are 1R/2R/3R. Each signal is followed candle by candle until the next flip:
 * TPs are marked "booked" where they are reached, the stop moves to breakeven after TP1, and a stop hit before TP1 is
 * marked SL. Analysis only, no orders. */
(function () {
  "use strict";
  const DEF = { period: 10, mult: 2.5, smooth: 4, maxSignals: 40 };

  function compute(candles, opt) {
    const o = { ...DEF, ...(opt || {}) }, n = candles.length;
    if (n < o.period + 5) return { band: [], signals: [], stats: null };
    const atr = Array(n).fill(null), up = Array(n).fill(null), dn = Array(n).fill(null), tr = Array(n).fill(0);
    let a = 0;
    for (let i = 0; i < n; i++) {
      const c = candles[i], p = candles[i - 1];
      const t = i ? Math.max(c.high - c.low, Math.abs(c.high - p.close), Math.abs(c.low - p.close)) : c.high - c.low;
      a = i < o.period ? (a * i + t) / (i + 1) : (a * (o.period - 1) + t) / o.period;     // Wilder's ATR
      atr[i] = a;
      const hl2 = (c.high + c.low) / 2, bu = hl2 - o.mult * a, bd = hl2 + o.mult * a;
      up[i] = i && p.close > up[i - 1] ? Math.max(bu, up[i - 1]) : bu;
      dn[i] = i && p.close < dn[i - 1] ? Math.min(bd, dn[i - 1]) : bd;
      tr[i] = !i ? 1 : c.close > dn[i - 1] ? 1 : c.close < up[i - 1] ? -1 : tr[i - 1];
    }
    const raw = tr.map((t, i) => (t > 0 ? up[i] : dn[i]));
    // display band: lightly smoothed so it reads as a curve (the stop itself uses the raw value)
    const k = 2 / (o.smooth + 1), band = [];
    let e = raw[0];
    for (let i = 0; i < n; i++) { e = i ? raw[i] * k + e * (1 - k) : raw[i]; if (i >= o.period) band.push({ time: candles[i].time, value: e, trend: tr[i] }); }

    // signals on closed candles: a flip of the trend
    const signals = [];
    for (let i = o.period + 1; i < n; i++) {
      if (tr[i] === tr[i - 1] || candles[i].forming) continue;
      const L = tr[i] > 0, entry = candles[i].close;
      let sl = raw[i];
      if (Math.abs(entry - sl) < 0.25 * atr[i]) sl = L ? entry - atr[i] : entry + atr[i];   // never a paper-thin stop
      const R = Math.abs(entry - sl), tps = [1, 2, 3].map((m) => (L ? entry + m * R : entry - m * R));
      const s = { i, time: candles[i].time, dir: L ? "LONG" : "SHORT", entry, sl, tps, hits: [], result: "running", end: null };
      let stop = sl;
      for (let j = i + 1; j < n; j++) {
        const c = candles[j];
        if (tr[j] !== tr[i]) { s.result = s.hits.length ? `TP${s.hits.length} then flip` : "flip"; s.end = { i: j, time: c.time, price: c.close }; break; }
        if (c.forming) break;                          // the candle still forming decides nothing
        if (L ? c.low <= stop : c.high >= stop) {      // stop first inside a candle (conservative)
          s.result = s.hits.length ? `TP${s.hits.length} then breakeven` : "SL"; s.end = { i: j, time: c.time, price: stop }; break;
        }
        while (s.hits.length < 3 && (L ? c.high >= tps[s.hits.length] : c.low <= tps[s.hits.length])) {
          s.hits.push({ tp: s.hits.length + 1, i: j, time: c.time, price: tps[s.hits.length] });
          if (s.hits.length === 1) stop = entry;       // breakeven after TP1
        }
        if (s.hits.length === 3) { s.result = "TP3"; s.end = { i: j, time: c.time, price: tps[2] }; break; }
      }
      signals.push(s);
    }
    const done = signals.filter((s) => s.result !== "running"), N = done.length;
    const pct = (f) => (N ? Math.round((100 * done.filter(f).length) / N) : 0);
    const stats = { signals: signals.length, closed: N, tp1: pct((s) => s.hits.length >= 1), tp2: pct((s) => s.hits.length >= 2), tp3: pct((s) => s.hits.length >= 3),
      sl: pct((s) => s.result === "SL"), trend: tr[n - 1] > 0 ? "up" : "down", last: signals[signals.length - 1] || null };
    return { band, signals: signals.slice(-o.maxSignals), stats };
  }

  /** Lightweight-charts markers for the signals (T maps candle time to chart time; fmt formats prices). */
  function markers(signals, T, fmt) {
    const out = [];
    for (const s of signals) {
      const L = s.dir === "LONG";
      out.push({ time: T(s.time), position: L ? "belowBar" : "aboveBar", color: L ? "#16a34a" : "#dc2626", shape: L ? "arrowUp" : "arrowDown",
        text: `${L ? "Buy" : "Sell"} · SL ${fmt(s.sl)}`, size: 1.4 });
      for (const h of s.hits) out.push({ time: T(h.time), position: L ? "aboveBar" : "belowBar", color: "#0d9488", shape: "circle", text: `TP${h.tp} ✓ booked`, size: 0.8 });
      if (s.result === "SL") out.push({ time: T(s.end.time), position: L ? "belowBar" : "aboveBar", color: "#dc2626", shape: "square", text: "SL ✗", size: 0.8 });
    }
    return out;
  }
  window.MMTREND = { compute, markers, DEF };
})();
