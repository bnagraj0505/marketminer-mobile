/* SmartEntry Market Intelligence Engine v2 (MIE) — the same maths as SmartEntry_MIE_v2.pine, for any market Signal Lab charts.
 * Seven indicators in one pass over the candles:
 *   1 Session Range + Trend PRO  EMA 5/30, Supertrend (ATR 10 × 3), session-anchored VWAP, session high/low
 *   2 ZigZag++                    alternating swings (depth 12, deviation 5 ticks, backstep 2) with HH/HL/LH/LL
 *   3 Volume 20                   volume vs its 20-bar average (optional entry filter, spike marks)
 *   4 Pin Box Zones               pin bars (range ≥ 0.9 ATR14, body ≤ 30 %, opposite wick ≤ 10 %) → zones, retests
 *   5 Wick Box Liquidity          dominant wicks (≥ 70 % of range, ≥ 0.05 % of price) → liquidity boxes, sweeps
 *   6 Price/Time Cycles           a time mark every 28 bars from the last zigzag pivot
 *   7 Gann Price/Time Square      square-of-9 levels every 45° from the last pivot, time marks every 45 bars up to 360
 * Entries only on CLOSED candles: trend aligned + VWAP side (+ volume if asked) + a trigger (sweep / wick grab / BOS /
 * session break / pin retest). Stop 1.5 ATR, target 2 ATR, exit on stop, target, trend flip or an opposite signal.
 * Every candle time is UTC seconds; tz is the market's offset in seconds (19800 for NSE/MCX, 0 for crypto).
 * Analysis only, no orders. */
(function () {
  "use strict";
  const DEF = {
    fast: 5, slow: 30, atrLen: 10, stFactor: 3,
    tz: 0, session: null,                 // session: [startMin, endMin] in market-local minutes, null = whole day
    zzDepth: 12, zzDev: 5, zzBack: 2,
    volLen: 20, useVol: false,
    pinAtr: 14, pinMinRange: 0.9, pinBody: 0.3, pinOpp: 0.1, pinAge: 200, pinTouch: 2,
    wickRatio: 0.7, wickMinPct: 0.05, wickAge: 200,
    cycleLen: 28, gannStep: 45, gannMax: 360,
    useVWAP: true, trigSweep: true, trigBOS: true, trigSess: true, trigPin: true,
    slMult: 1.5, tpMult: 2,
  };

  const sma0 = (src, len, i) => { let s = 0; for (let k = i - len + 1; k <= i; k++) s += src[k]; return s / len; };
  function ema(src, len) {                       // Pine ta.ema: SMA seed, then 2/(n+1)
    const out = Array(src.length).fill(null), a = 2 / (len + 1);
    for (let i = 0; i < src.length; i++) {
      if (i < len - 1) continue;
      out[i] = out[i - 1] == null ? sma0(src, len, i) : a * src[i] + (1 - a) * out[i - 1];
    }
    return out;
  }
  function rma(src, len) {                       // Pine ta.rma (Wilder), SMA seed
    const out = Array(src.length).fill(null);
    for (let i = 0; i < src.length; i++) {
      if (i < len - 1) continue;
      out[i] = out[i - 1] == null ? sma0(src, len, i) : (out[i - 1] * (len - 1) + src[i]) / len;
    }
    return out;
  }
  function atrOf(c, len) {
    const tr = c.map((x, i) => (i ? Math.max(x.high - x.low, Math.abs(x.high - c[i - 1].close), Math.abs(x.low - c[i - 1].close)) : x.high - x.low));
    return rma(tr, len);
  }
  function supertrend(c, factor, atr) {          // Pine ta.supertrend: dir -1 = up, +1 = down
    const n = c.length, st = Array(n).fill(null), dir = Array(n).fill(1);
    let pu = 0, pl = 0, pst = null;
    for (let i = 0; i < n; i++) {
      const src = (c[i].high + c[i].low) / 2, a = atr[i];
      if (a == null) { pu = 0; pl = 0; continue; }
      let u = src + factor * a, l = src - factor * a;
      const pc = i ? c[i - 1].close : c[i].close;
      l = l > pl || pc < pl ? l : pl;
      u = u < pu || pc > pu ? u : pu;
      let d;
      if (i === 0 || atr[i - 1] == null) d = 1;
      else if (pst === pu) d = c[i].close > u ? -1 : 1;
      else d = c[i].close < l ? 1 : -1;
      st[i] = d === -1 ? l : u; dir[i] = d;
      pu = u; pl = l; pst = st[i];
    }
    return { st, dir };
  }

  function compute(candles, opt) {
    const o = { ...DEF, ...(opt || {}) }, c = candles, n = c.length;
    if (n < Math.max(o.slow, o.zzDepth + o.zzBack) + 5) return null;
    const close = c.map((x) => x.close), vol = c.map((x) => +x.volume || 0);
    const emaF = ema(close, o.fast), emaS = ema(close, o.slow), atr = atrOf(c, o.atrLen), atrP = atrOf(c, o.pinAtr);
    const { st, dir: stDir } = supertrend(c, o.stFactor, atr);
    const P = Math.abs(close[n - 1]), dp = P >= 100 ? 2 : P >= 10 ? 3 : P >= 1 ? 4 : 6, tick = Math.pow(10, -dp);

    // per-bar series
    const vwap = Array(n).fill(null), sessHi = Array(n).fill(null), sessLo = Array(n).fill(null), volSma = Array(n).fill(null);
    const ev = Array.from({ length: n }, () => ({}));
    let cpv = 0, cv = 0, day = null, sh = null, sl = null, prevIn = false;
    const local = (t) => t + o.tz, dayOf = (t) => Math.floor(local(t) / 86400), minOf = (t) => Math.floor((local(t) % 86400) / 60);

    // zigzag state
    const zz = [];                                // {i, price, dir, tag}
    const tagOf = (k) => { const p = zz[k]; if (k < 2) return p.dir > 0 ? "SH" : "SL"; const q = zz[k - 2].price;
      return p.dir > 0 ? (p.price > q ? "HH" : "LH") : (p.price > q ? "HL" : "LL"); };
    const addZZ = (dir, price, i) => {
      const L = zz[zz.length - 1];
      if (!L) { zz.push({ i, price, dir }); }
      else if (L.dir === dir) { if (dir > 0 ? price > L.price : price < L.price) { L.price = price; L.i = i; } else return; }
      else if (Math.abs(price - L.price) >= o.zzDev * tick) zz.push({ i, price, dir });
      else return;
      zz[zz.length - 1].tag = tagOf(zz.length - 1);
    };
    const lastOf = (dir) => { for (let k = zz.length - 1; k >= Math.max(0, zz.length - 2); k--) if (zz[k].dir === dir) return zz[k].price; return null; };
    let sweptLo = null, sweptHi = null, pLsh = null;
    const swing = Array(n).fill(null);

    // zones
    let pins = [], wicks = [];
    const pinOut = [], wickOut = [];               // finished zones/boxes for drawing history (capped)

    // trade sim
    let side = 0, ent = null, stp = null, tgt = null, entI = null;
    const trades = [];

    for (let i = 0; i < n; i++) {
      const x = c[i], forming = !!x.forming;
      // VWAP + session (session-anchored: resets at the market's day / session start)
      const d = dayOf(x.time), m = minOf(x.time);
      const inS = !o.session || (m >= o.session[0] && m < o.session[1]);
      const newDay = d !== day; day = d;
      if (newDay) { cpv = 0; cv = 0; }
      const hlc3 = (x.high + x.low + x.close) / 3; cpv += hlc3 * vol[i]; cv += vol[i];
      vwap[i] = cv > 0 ? cpv / cv : null;
      const newSess = inS && (!prevIn || newDay);
      const psh = sh, psl = sl;
      if (newSess) { sh = x.high; sl = x.low; } else if (inS) { sh = Math.max(sh ?? x.high, x.high); sl = Math.min(sl ?? x.low, x.low); }
      if (inS) { sessHi[i] = sh; sessLo[i] = sl; }
      const e = ev[i];
      e.sessUp = inS && !newSess && psh != null && x.close > psh && c[i - 1].close <= psh;
      e.sessDn = inS && !newSess && psl != null && x.close < psl && c[i - 1].close >= psl;
      prevIn = inS;
      if (i >= o.volLen - 1) volSma[i] = sma0(vol, o.volLen, i);
      const hasVol = volSma[i] > 0;
      e.volSpike = hasVol && vol[i] >= 2 * volSma[i];

      // zigzag: pivot at p = i - back, highest/lowest of [p-depth, p+back]
      const p = i - o.zzBack;
      if (p - o.zzDepth >= 0) {
        let isH = true, isL = true;
        for (let k = p - o.zzDepth; k <= i; k++) {
          if (k === p) continue;
          if (c[k].high >= c[p].high) isH = false;
          if (c[k].low <= c[p].low) isL = false;
        }
        if (isH) addZZ(1, c[p].high, p);
        if (isL) addZZ(-1, c[p].low, p);
      }
      const lsh = lastOf(1), lsl = lastOf(-1);
      swing[i] = { hi: lsh, lo: lsl };
      const pr = i ? swing[i - 1] : { hi: null, lo: null };
      e.bullBOS = lsh != null && pr.hi != null && x.close > lsh && c[i - 1].close <= pr.hi;
      e.bearBOS = lsl != null && pr.lo != null && x.close < lsl && c[i - 1].close >= pr.lo;
      e.bullSweep = lsl != null && x.low < lsl && x.close > lsl && sweptLo !== lsl;
      e.bearSweep = lsh != null && x.high > lsh && x.close < lsh && sweptHi !== lsh;
      if (e.bullSweep) sweptLo = lsl;
      if (e.bearSweep) sweptHi = lsh;
      pLsh = lsh;

      // pin zones: check the live ones, then add this candle's
      pins = pins.filter((z) => {
        let kill = i - z.i > o.pinAge;
        if (!kill) {
          if (z.dir > 0) { if (x.close < z.bot) kill = true; else if (x.low <= z.top && x.close > z.top && x.close > x.open) { e.bullPin = true; z.touch++; } }
          else { if (x.close > z.top) kill = true; else if (x.high >= z.bot && x.close < z.bot && x.close < x.open) { e.bearPin = true; z.touch++; } }
          if (z.touch >= o.pinTouch) kill = true;
        }
        if (kill) { z.end = i; pinOut.push(z); }
        return !kill;
      });
      const rng = x.high - x.low, body = Math.abs(x.close - x.open), upW = x.high - Math.max(x.open, x.close), loW = Math.min(x.open, x.close) - x.low;
      if (rng > 0 && atrP[i] != null && rng >= o.pinMinRange * atrP[i] && body <= o.pinBody * rng) {
        if (upW <= o.pinOpp * rng) pins.push({ dir: 1, top: Math.min(x.open, x.close), bot: x.low, i, touch: 0 });
        else if (loW <= o.pinOpp * rng) pins.push({ dir: -1, top: x.high, bot: Math.max(x.open, x.close), i, touch: 0 });
        if (pins.length > 30) pinOut.push({ ...pins.shift(), end: i });
      }

      // wick boxes
      wicks = wicks.filter((z) => {
        let kill = i - z.i > o.wickAge;
        if (!kill) {
          if (z.dir > 0) { if (x.low < z.bot && x.close > z.bot) { e.bullGrab = true; kill = true; } else if (x.close < z.bot) kill = true; }
          else { if (x.high > z.top && x.close < z.top) { e.bearGrab = true; kill = true; } else if (x.close > z.top) kill = true; }
        }
        if (kill) { z.end = i; wickOut.push(z); }
        return !kill;
      });
      const minW = x.close * o.wickMinPct / 100;
      if (rng > 0 && upW >= o.wickRatio * rng && upW >= minW) wicks.push({ dir: -1, top: x.high, bot: Math.max(x.open, x.close), i });
      else if (rng > 0 && loW >= o.wickRatio * rng && loW >= minW) wicks.push({ dir: 1, top: Math.min(x.open, x.close), bot: x.low, i });
      if (wicks.length > 30) wickOut.push({ ...wicks.shift(), end: i });

      // time cycles from the last zigzag pivot bar
      const lp = zz[zz.length - 1];
      if (lp) { const b = i - lp.i; e.cycle = b > 0 && b % o.cycleLen === 0; e.gann = b > 0 && b <= o.gannMax && b % o.gannStep === 0; }

      // trend + entry engine
      const bull = emaF[i] != null && emaS[i] != null && emaF[i] > emaS[i] && stDir[i] < 0;
      const bear = emaF[i] != null && emaS[i] != null && emaF[i] < emaS[i] && stDir[i] > 0;
      e.trend = bull ? 1 : bear ? -1 : 0;
      const vwB = !o.useVWAP || vwap[i] == null || x.close > vwap[i], vwS = !o.useVWAP || vwap[i] == null || x.close < vwap[i];
      const volOK = !o.useVol || !hasVol || vol[i] >= volSma[i];
      const bullTrig = (o.trigSweep && (e.bullSweep || e.bullGrab)) || (o.trigBOS && e.bullBOS) || (o.trigSess && e.sessUp) || (o.trigPin && e.bullPin);
      const bearTrig = (o.trigSweep && (e.bearSweep || e.bearGrab)) || (o.trigBOS && e.bearBOS) || (o.trigSess && e.sessDn) || (o.trigPin && e.bearPin);
      const why = (b) => [b ? (e.bullSweep && "sweep") : (e.bearSweep && "sweep"), b ? (e.bullGrab && "wick grab") : (e.bearGrab && "wick grab"),
        b ? (e.bullBOS && "BOS") : (e.bearBOS && "BOS"), b ? (e.sessUp && "session break") : (e.sessDn && "session break"),
        b ? (e.bullPin && "pin retest") : (e.bearPin && "pin retest")].filter(Boolean).join(" + ");
      const longSig = !forming && bull && vwB && volOK && bullTrig, shortSig = !forming && bear && vwS && volOK && bearTrig;

      if (side !== 0 && !forming) {                // exits first (so a reversal closes the old trade)
        let px = null, kind = null;
        if (side > 0) { if (x.low <= stp) { px = stp; kind = "SL"; } else if (x.high >= tgt) { px = tgt; kind = "TP"; } else if (bear || shortSig) { px = x.close; kind = "EXIT"; } }
        else { if (x.high >= stp) { px = stp; kind = "SL"; } else if (x.low <= tgt) { px = tgt; kind = "TP"; } else if (bull || longSig) { px = x.close; kind = "EXIT"; } }
        if (kind) { const t = trades[trades.length - 1]; t.exit = px; t.exitTime = x.time; t.result = kind; t.r = side * (px - ent) / Math.abs(ent - stp); side = 0; }
      }
      if (longSig && side !== 1) { side = 1; ent = x.close; stp = ent - atr[i] * o.slMult; tgt = ent + atr[i] * o.tpMult; entI = i;
        trades.push({ dir: "LONG", time: x.time, entry: ent, sl: stp, tp: tgt, why: why(true), result: "running" }); }
      else if (shortSig && side !== -1) { side = -1; ent = x.close; stp = ent + atr[i] * o.slMult; tgt = ent - atr[i] * o.tpMult; entI = i;
        trades.push({ dir: "SHORT", time: x.time, entry: ent, sl: stp, tp: tgt, why: why(false), result: "running" }); }
    }

    // Gann square-of-9 levels around the current price, from the last pivot
    const lp = zz[zz.length - 1], gann = [];
    if (lp && lp.price > 0) {
      const sq = (k) => { const s = Math.sqrt(lp.price) + (k * o.gannStep) / 180; return s > 0 ? s * s : null; };
      const kc = Math.floor(((Math.sqrt(close[n - 1]) - Math.sqrt(lp.price)) * 180) / o.gannStep);
      for (let k = kc - 2; k <= kc + 3; k++) if (k !== 0) { const v = sq(k); if (v != null) gann.push({ deg: k * o.gannStep, price: v }); }
    }

    const done = trades.filter((t) => t.result !== "running"), N = done.length;
    const last = n - 1, lt = ev[last];
    const stats = {
      trades: N, open: trades.length - N, win: N ? Math.round((100 * done.filter((t) => t.r > 0).length) / N) : 0,
      tp: N ? Math.round((100 * done.filter((t) => t.result === "TP").length) / N) : 0,
      sl: N ? Math.round((100 * done.filter((t) => t.result === "SL").length) / N) : 0,
      avgR: N ? done.reduce((a, t) => a + t.r, 0) / N : 0,
      trend: lt.trend, structure: zz.length ? zz[zz.length - 1].tag : "", vwapSide: vwap[last] == null ? null : close[last] > vwap[last] ? "above" : "below",
      volume: volSma[last] > 0 ? vol[last] / volSma[last] : null, swingHi: swing[last].hi, swingLo: swing[last].lo,
      sessHi: sessHi[last], sessLo: sessLo[last], last: trades[trades.length - 1] || null,
      barsFromPivot: lp ? last - lp.i : null, nextCycle: lp ? o.cycleLen - ((last - lp.i) % o.cycleLen) : null,
    };
    return { emaF, emaS, st, stDir, vwap, sessHi, sessLo, volSma, zz, ev, pins, wicks, pinOut: pinOut.slice(-60), wickOut: wickOut.slice(-60),
      gann, trades, stats, atr, dp };
  }

  /** Lightweight-charts markers. T maps candle time to chart time; show = {zz, sig, sweep, cycle, vol}. */
  function markers(r, candles, T, fmt, show) {
    const s = { zz: true, sig: true, sweep: true, cycle: true, vol: false, ...(show || {}) }, out = [];
    if (s.zz) for (const p of r.zz) out.push({ time: T(candles[p.i].time), position: p.dir > 0 ? "aboveBar" : "belowBar",
      color: p.tag === "HH" || p.tag === "HL" ? "#16a34a" : p.tag === "LH" || p.tag === "LL" ? "#dc2626" : "#64748b", shape: p.dir > 0 ? "arrowDown" : "arrowUp", text: p.tag, size: 0.6 });
    r.ev.forEach((e, i) => {
      const t = T(candles[i].time);
      if (s.sweep && (e.bullSweep || e.bullGrab)) out.push({ time: t, position: "belowBar", color: "#0d9488", shape: "circle", text: "SWEEP", size: 0.6 });
      if (s.sweep && (e.bearSweep || e.bearGrab)) out.push({ time: t, position: "aboveBar", color: "#f59e0b", shape: "circle", text: "SWEEP", size: 0.6 });
      if (s.cycle && e.cycle) out.push({ time: t, position: "belowBar", color: "#a855f7", shape: "square", text: "T", size: 0.4 });
      if (s.cycle && e.gann) out.push({ time: t, position: "belowBar", color: "#e879f9", shape: "square", text: "G", size: 0.4 });
      if (s.vol && e.volSpike) out.push({ time: t, position: "belowBar", color: "#facc15", shape: "circle", size: 0.3 });
    });
    if (s.sig) for (const tr of r.trades) {
      const L = tr.dir === "LONG";
      out.push({ time: T(tr.time), position: L ? "belowBar" : "aboveBar", color: L ? "#22c55e" : "#ef4444", shape: L ? "arrowUp" : "arrowDown", text: `${L ? "BUY" : "SELL"} · SL ${fmt(tr.sl)}`, size: 1.4 });
      if (tr.exitTime) out.push({ time: T(tr.exitTime), position: L ? "aboveBar" : "belowBar", color: tr.result === "TP" ? "#22c55e" : tr.result === "SL" ? "#ef4444" : "#94a3b8",
        shape: "circle", text: tr.result === "TP" ? "TP ✓" : tr.result === "SL" ? "SL ✗" : `EXIT ${tr.r >= 0 ? "+" : ""}${tr.r.toFixed(1)}R`, size: 0.8 });
    }
    return out.sort((a, b) => a.time - b.time);
  }

  /** Market-local timezone offset and session for a symbol's market. */
  function sessionFor(market, symbol) {
    if (["NSE", "FNO", "BSE"].includes(market) || /^(NSE|BSE):/.test(symbol || "")) return { tz: 19800, session: [555, 930] };   // 09:15–15:30 IST
    if (market === "MCX" || /^MCX:/.test(symbol || "")) return { tz: 19800, session: [540, 1410] };                         // 09:00–23:30 IST
    return { tz: 0, session: null };                                                                                         // crypto: whole UTC day
  }
  window.SEMIE = { compute, markers, sessionFor, DEF };
})();
