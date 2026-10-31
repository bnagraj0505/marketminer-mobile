/* MarketMiner Mobile: Day Desk.
 * JavaScript port of the desktop backend/desk/engine.py (maths) and backend/desk/routes.py (data, limits, paper
 * book), plus backend/markets/fyers_master.py (F&O / MCX contract lists). Loaded after engine.js on desk.html.
 * Paper only: orders are kept in this browser on this device, never sent to any broker.
 * Shark Exchange stays on the PC version (it does not answer phone browsers).
 */
(function () {
  "use strict";
  const MM = window.MM, { FY, LS, HE, getJ, cached } = MM;
  const IST_MS = 19800e3, BAR = 5, OPEN_MIN = 555, CLOSE_MIN = 930, SESSION_BARS = 75, MIN_SESSIONS = 20;
  const SAMPLE_PATHS = 120, BAND_PATHS = 2000, BAND_Q = [0.05, 0.25, 0.5, 0.75, 0.95], HIST_BINS = 24;
  const COIN_FLIP = 0.05, STRONG = 0.15, MANY_NEITHER = 0.4, MIN_RISK_PCT = 0.05, ATR_BARS = 14;
  const DE = (m) => new HE(422, m);

  // ---------------------------------------------------------------- time
  const ist = (ms) => { const d = new Date(ms + IST_MS); return { y: d.getUTCFullYear(), mo: d.getUTCMonth(), d: d.getUTCDate(), h: d.getUTCHours(), mi: d.getUTCMinutes(), wd: (d.getUTCDay() + 6) % 7, date: d.toISOString().slice(0, 10) }; };
  const clock = (m) => String(Math.floor(m / 60)).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0");
  function parseClock(text, what = "time") {
    const p = String(text || "").trim().split(":"); const v = parseInt(p[0], 10) * 60 + parseInt(p[1], 10);
    if (p.length < 2 || !isFinite(v)) throw DE(`${what} must look like HH:MM (24-hour IST), got '${text}'`);
    if (v < 0 || v >= 1440) throw DE(`${what} '${text}' is not a time of day`);
    return v;
  }
  const isoIst = (ms) => new Date(ms + IST_MS).toISOString().slice(0, 19) + "+05:30";
  class Session {
    constructor(name, openMin, bars, weekdaysOnly) { Object.assign(this, { name, open_min: openMin, bars, weekdays_only: weekdaysOnly, length: bars * BAR }); }
    offset(localMin) { return (((localMin - this.open_min) % 1440) + 1440) % 1440; }
    label(off) { return clock((this.open_min + off) % 1440); }
    sessionDate(ms) { return new Date(ms + IST_MS - this.open_min * 60e3).toISOString().slice(0, 10); }
    nowOffset(ms) { const t = ist(ms); return this.offset(t.h * 60 + t.mi); }
    toOffset(hhmm, what) { return this.offset(parseClock(hhmm, what)); }
  }
  const NSE = new Session("NSE", OPEN_MIN, SESSION_BARS, true), CRYPTO = new Session("CRYPTO", 330, 288, false), MCX = new Session("MCX", 540, 174, true);

  // ---------------------------------------------------------------- ticks
  const decimals = (tick) => (tick > 0 ? Math.max(2, Math.min(10, tick < 1 ? -Math.floor(Math.log10(tick)) : 0)) : 2);
  const rnd = (x, d) => { const f = 10 ** d; return Math.round(x * f + (x >= 0 ? 1e-9 : -1e-9)) / f; };
  const fmtp = (x, tick = 0.05) => Number(x).toFixed(decimals(tick));
  const tickFloor = (x, t = 0.05) => rnd(Math.floor(rnd(x / t, 6)) * t, decimals(t));
  const tickCeil = (x, t = 0.05) => rnd(Math.ceil(rnd(x / t, 6)) * t, decimals(t));
  // Python's round() sends exact halves to the even neighbour; matching it keeps prices identical to the PC
  const roundEven = (v) => { const f = Math.floor(v), d = v - f; return Math.abs(d - 0.5) < 1e-9 ? (f % 2 === 0 ? f : f + 1) : Math.round(v); };
  const tickRound = (x, t = 0.05) => rnd(roundEven(x / t) * t, decimals(t));
  const tickAway = (x, long, t = 0.05) => (long ? tickFloor(x, t) : tickCeil(x, t));
  const mean = (a) => a.reduce((x, y) => x + y, 0) / a.length;

  // ---------------------------------------------------------------- bars
  function parseCandles(candles, nowMs = Date.now(), intervalMin = BAR, S = NSE) {
    const out = [];
    for (const row of candles || []) {
      const ts = +row[0], o = +row[1], h = +row[2], l = +row[3], c = +row[4], v = row.length > 5 && row[5] != null ? +row[5] : 0;
      if (![ts, o, h, l, c].every(isFinite)) continue;
      if (ts * 1000 + intervalMin * 60e3 > nowMs) continue;
      const t = ist(ts * 1000), mins = t.h * 60 + t.mi, off = S.offset(mins);
      if (off >= S.length || off % intervalMin) continue;
      out.push({ date: S.sessionDate(ts * 1000), slot: off / BAR, off, start: clock(mins), ts, o, h, l, c, v });
    }
    return out.sort((a, b) => a.ts - b.ts);
  }
  const splitSessions = (bars) => { const d = {}; for (const b of bars) (d[b.date] = d[b.date] || {})[b.slot] = b; return d; };
  function fullSessions(days, before, nbars = SESSION_BARS) {
    const full = [], short = [];
    for (const d of Object.keys(days).sort()) {
      if (d >= before) continue;
      const s = days[d], ok = Object.keys(s).length === nbars && [...Array(nbars).keys()].every((i) => s[i]);
      if (ok) full.push([d, [...Array(nbars).keys()].map((i) => s[i])]); else short.push({ date: d, bars: Object.keys(s).length });
    }
    return [full, short];
  }
  function atr(bars, n = ATR_BARS) {
    if (bars.length < 2) return null;
    const trs = []; for (let i = 1; i < bars.length; i++) { const pc = bars[i - 1].c, b = bars[i]; trs.push(Math.max(b.h - b.l, Math.abs(b.h - pc), Math.abs(b.l - pc))); }
    return mean(trs.slice(-n));
  }
  const SETUPS = { breakout_20d: "20-day closing-high breakout", bull_flag: "Bull flag", reclaim_ma20: "Reclaimed 20-day average", oversold: "Oversold (RSI < 30)", oversold_bounce: "Oversold bounce" };
  function rsi(c, n = 14) {
    if (c.length < n + 1) return null;
    const g = [], lo = []; for (let i = 1; i < c.length; i++) { g.push(Math.max(0, c[i] - c[i - 1])); lo.push(Math.max(0, c[i - 1] - c[i])); }
    let ag = mean(g.slice(0, n)), al = mean(lo.slice(0, n));
    for (let i = n; i < g.length; i++) { ag = (ag * (n - 1) + g[i]) / n; al = (al * (n - 1) + lo[i]) / n; }
    return al ? 100 - 100 / (1 + ag / al) : 100;
  }
  function findSetups(c) {
    if (c.length < 30) throw DE("need at least 30 daily closes");
    const L = c.length, last = c[L - 1], hi20 = Math.max(...c.slice(-21, -1)), ma20 = mean(c.slice(-20)), ma20p = mean(c.slice(-21, -1));
    const run = c[L - 6] / c[L - 16] - 1, rng5 = (Math.max(...c.slice(-5)) - Math.min(...c.slice(-5))) / last, r = rsi(c), rp = rsi(c.slice(0, -1)), tags = [];
    if (last > hi20) tags.push("breakout_20d");
    if (run > 0.1 && rng5 < 0.05) tags.push("bull_flag");
    if (c[L - 2] < ma20p && last > ma20) tags.push("reclaim_ma20");
    if (r != null && r < 30) tags.push("oversold");
    if (rp != null && rp < 30 && last > c[L - 2]) tags.push("oversold_bounce");
    return [tags, { rsi: r == null ? null : rnd(r, 1), high20: rnd(hi20, 2), ma20: rnd(ma20, 2), run_pct: rnd(run * 100, 2), range5_pct: rnd(rng5 * 100, 2) }];
  }
  function dailyFromCandles(candles, nowMs = Date.now(), S = NSE) {
    const t = ist(nowMs), today = t.date, done = t.h * 60 + t.mi >= CLOSE_MIN, out = {};
    for (const row of candles || []) {
      const ts = +row[0]; if (!isFinite(ts)) continue;
      const bar = { o: +row[1], h: +row[2], l: +row[3], c: +row[4], v: +(row[5] || 0) };
      if (S !== NSE) { if (ts + 86400 > nowMs / 1000) continue; const d = new Date(ts * 1000).toISOString().slice(0, 10); out[d] = { date: d, ...bar }; continue; }
      const d = ist(ts * 1000).date;
      if (d > today || (d === today && !done)) continue;
      out[d] = { date: d, ...bar };
    }
    return Object.keys(out).sort().map((k) => out[k]);
  }

  // ---------------------------------------------------------------- opening range breakout
  const openingRange = (D, n) => { const r = D.slice(0, n); return [Math.max(...r.map((b) => b.h)), Math.min(...r.map((b) => b.l))]; };
  function entryStatus(barsToday, nowMs, orMin = 15, side = "buy", noBefore = "09:30", noAfter = "15:00", level = null, S = NSE, tick = 0.05) {
    const long = side === "buy";
    if (orMin % BAR || !(orMin >= BAR && orMin <= 60)) throw DE("opening range must be 5 to 60 minutes, in steps of 5");
    const n = orMin / BAR, nowoff = S.nowOffset(nowMs), nb = S.toOffset(noBefore, "no entries before"), na = S.toOffset(noAfter, "no entries after");
    const today = S.sessionDate(nowMs), bars = barsToday.filter((b) => b.date === today), P = (x) => fmtp(x, tick), t = ist(nowMs);
    const base = { side, or_minutes: orMin, bars: bars.length, now: clock(t.h * 60 + t.mi), no_before: noBefore, no_after: noAfter, level, session_date: today };
    if (S.weekdays_only && t.wd >= 5) return { ...base, state: "closed", message: "Weekend: the NSE is closed." };
    if (S === NSE && t.h * 60 + t.mi < OPEN_MIN) return { ...base, state: "pre-open", message: "Market opens at 09:15 IST." };
    if (bars.length < n && level == null) {
      const oh = bars.length ? Math.max(...bars.map((b) => b.h)) : null, ol = bars.length ? Math.min(...bars.map((b) => b.l)) : null, st = nowoff < S.length ? "forming" : "no-data";
      return { ...base, state: st, or_high: oh, or_low: ol, message: st === "forming" ? `Opening range forming: ${bars.length} of ${n} bars (from ${S.label(0)} IST).` : "No intraday bars for today." };
    }
    let oh = null, ol = null; if (bars.length >= n) [oh, ol] = openingRange(bars, n);
    const trig = level != null ? level : long ? oh : ol;
    for (let k = level != null ? 0 : n; k < bars.length; k++) {
      const b = bars[k], co = b.off + BAR;
      if (co < nb) continue; if (co > na) break;
      if (long ? b.c > trig : b.c < trig)
        return { ...base, state: "triggered", or_high: oh, or_low: ol, trigger_level: trig, entry: tickRound(b.c, tick), entry_time: S.label(co), entry_bar: b.slot, bar: b,
          message: `Triggered: the ${b.start} bar closed at ${P(b.c)}, ${long ? "above" : "below"} ${P(trig)}.` };
    }
    const last = bars[bars.length - 1];
    if (nowoff >= na || nowoff >= S.length) return { ...base, state: "expired", or_high: oh, or_low: ol, trigger_level: trig, last: last && last.c, message: `No trigger before ${noAfter} IST. Setup expired for this session.` };
    return { ...base, state: "waiting", or_high: oh, or_low: ol, trigger_level: trig, last: last && last.c, distance: last ? (long ? trig - last.c : last.c - trig) : null,
      message: `Waiting for a 5-minute close ${long ? "above" : "below"} ${P(trig)}` + (last ? ` (last ${P(last.c)}).` : ".") };
  }
  function orbScanRow(bars, orMin = 15, S = NSE) {
    const n = orMin / BAR; if (bars.length < n) return null;
    const [oh, ol] = openingRange(bars, n), last = bars[bars.length - 1].c, up = bars.slice(n).find((b) => b.c > oh), dn = bars.slice(n).find((b) => b.c < ol);
    return { or_high: oh, or_low: ol, last, breakout: up ? { at: S.label(up.off + BAR), price: up.c } : null, breakdown: dn ? { at: S.label(dn.off + BAR), price: dn.c } : null,
      position: last > oh ? "above" : last < ol ? "below" : "inside" };
  }

  // ---------------------------------------------------------------- exit planner
  function planExit(entry, side = "buy", stopRule = "or_mid", stopValue = null, targetRule = "r", targetValue = 2, orHigh = null, orLow = null, bars = null, tick = 0.05) {
    const long = side === "buy", P = (x) => fmtp(x, tick), e = tickRound(+entry, tick);
    if (e <= 0) throw DE("the entry must be above 0");
    let raw, label, detail = "";
    if (stopRule === "or_mid" || stopRule === "or_far") {
      if (orHigh == null || orLow == null || orHigh <= orLow) throw DE("an opening-range stop needs the range high and low (high above low)");
      if (stopRule === "or_mid") { raw = (orHigh + orLow) / 2; label = "opening-range midpoint"; detail = `(${P(orHigh)} + ${P(orLow)}) / 2`; }
      else { raw = long ? orLow : orHigh; label = long ? "opening-range low" : "opening-range high"; }
    } else if (stopRule === "pct") {
      if (!stopValue || !(stopValue > 0 && stopValue < 50)) throw DE("a percent stop needs a value between 0 and 50");
      raw = long ? e * (1 - stopValue / 100) : e * (1 + stopValue / 100); label = `${stopValue}% ${long ? "under" : "over"} the entry`;
    } else if (stopRule === "atr") {
      const a = atr(bars || []); if (a == null || !stopValue) throw DE("an ATR stop needs a multiple and today's bars up to the entry");
      raw = long ? e - stopValue * a : e + stopValue * a; label = `${stopValue} x ATR(${Math.min(ATR_BARS, Math.max(1, (bars || []).length - 1))})`; detail = `ATR ${P(a)}`;
    } else if (stopRule === "price") { if (!stopValue) throw DE("a price stop needs a price"); raw = stopValue; label = "your price"; }
    else throw DE(`unknown stop rule '${stopRule}'`);
    const stop = tickAway(raw, long, tick);
    if (long ? stop >= e : stop <= e) throw DE(`the stop ${P(stop)} (${label}) is on the wrong side of the entry ${P(e)}: a ${long ? "buy" : "sell"}'s stop goes ${long ? "under" : "over"} it`);
    if (stop <= 0) throw DE("the stop works out at or below 0");
    const risk = rnd(Math.abs(e - stop), decimals(tick) + 2);
    if (risk < (e * MIN_RISK_PCT) / 100) throw DE(`the stop is only ${P(risk)} from the entry, under ${MIN_RISK_PCT}% of the price: too tight to survive normal wiggle. Widen it.`);
    let target;
    if (targetRule === "r") { if (!(targetValue > 0)) throw DE("the target needs an R multiple above 0"); target = tickRound(long ? e + targetValue * risk : e - targetValue * risk, tick); }
    else if (targetRule === "price") target = tickRound(+targetValue, tick);
    else throw DE(`unknown target rule '${targetRule}'`);
    if (long ? target <= e : target >= e) throw DE(`the target ${P(target)} is on the wrong side of the entry ${P(e)}`);
    return { side, entry: e, stop, stop_rule: stopRule, stop_label: label, stop_detail: detail, target, target_r: rnd(Math.abs(target - e) / risk, 2), risk_per_share: risk, or_high: orHigh, or_low: orLow, tick };
  }

  // ---------------------------------------------------------------- Time Machine
  function rng32(seed) { let a = (seed >>> 0) || 1; return () => { a = (a + 0x6d2b79f5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }
  function buildPool(sessions, nbars = SESSION_BARS) {
    const pool = [...Array(nbars)].map(() => []);
    for (const [, D] of sessions) for (let i = 1; i < nbars; i++) { const pc = D[i - 1].c; pool[i].push([D[i].o / pc, D[i].h / pc, D[i].l / pc, D[i].c / pc]); }
    return pool;
  }
  const pct = (a, q) => a[Math.min(a.length - 1, Math.floor(q * a.length))];
  function simulate(pool, entry, stop, target, k, last, replays = 10000, seed = 19, window = 2, side = "buy", nd = 2) {
    const nbars = pool.length, long = side === "buy", risk = long ? entry - stop : stop - entry, rT = rnd((long ? target - entry : entry - target) / risk, 4);
    const mk = () => { const r = rng32(seed); return { slot: (i) => (window === 0 ? i : Math.min(nbars - 1, Math.max(1, i + Math.floor(r() * (2 * window + 1)) - window))), pick: (arr) => arr[Math.floor(r() * arr.length)] }; };
    let R = mk(); const outcomes = [], closes = [], paths = [];
    for (let n = 0; n < replays; n++) {
      let p = entry, outcome = null; const keep = n < SAMPLE_PATHS, path = [entry];
      for (let i = k + 1; i <= last; i++) {
        const [, h, l, c] = R.pick(pool[R.slot(i)]), hi = p * h, lo = p * l;
        if (outcome === null) { if (long) { if (lo <= stop) outcome = "stop"; else if (hi >= target) outcome = "target"; } else { if (hi >= stop) outcome = "stop"; else if (lo <= target) outcome = "target"; } }
        p *= c; if (keep) path.push(rnd(p, nd));
      }
      outcomes.push(outcome || "close"); closes.push(p); if (keep) paths.push(path);
    }
    const cnt = { target: 0, stop: 0, close: 0 }; outcomes.forEach((o) => cnt[o]++);
    let green = 0; const Rs = outcomes.map((o, j) => { const p = closes[j]; if (o === "target" || (o === "close" && (long ? p > entry : p < entry))) green++; return o === "target" ? rT : o === "stop" ? -1 : (long ? p - entry : entry - p) / risk; });
    const srt = [...closes].sort((a, b) => a - b);
    R = mk(); const allp = [];
    for (let n = 0; n < BAND_PATHS; n++) { let p = entry; const path = [entry]; for (let i = k + 1; i <= last; i++) { p *= R.pick(pool[R.slot(i)])[3]; path.push(p); } allp.push(path); }
    const bands = []; for (let j = 0; j <= last - k; j++) { const col = allp.map((pp) => pp[j]).sort((a, b) => a - b); bands.push(BAND_Q.map((q) => rnd(pct(col, q), nd))); }
    const lo_ = pct(srt, 0.01), hi_ = pct(srt, 0.99), W = (hi_ - lo_) / HIST_BINS, bins = Array(HIST_BINS).fill(0);
    for (const c of srt) { const b = W > 0 ? Math.floor((c - lo_) / W) : 0; if (b >= 0 && b < HIST_BINS) bins[b]++; }
    return { replays, seed, window, bars_left: last - k, p_target: rnd(cnt.target / replays, 4), p_stop: rnd(cnt.stop / replays, 4), p_neither: rnd(cnt.close / replays, 4),
      p_green: rnd(green / replays, 4), median_close: rnd(pct(srt, 0.5), nd), p5_close: rnd(pct(srt, 0.05), nd), p95_close: rnd(pct(srt, 0.95), nd), avg_R: rnd(mean(Rs), 3),
      r_at_target: rT, bands, sample_paths: paths, hist: { lo: rnd(lo_, nd), hi: rnd(hi_, nd), bins } };
  }
  function ruleStop(rule, oh, ol, entry, long, tick) {
    if (rule.stop_rule === "or_mid") return tickAway((oh + ol) / 2, long, tick);
    if (rule.stop_rule === "or_far") return tickAway(long ? ol : oh, long, tick);
    const f = rule.stop_pct / 100; return tickAway(long ? entry * (1 - f) : entry * (1 + f), long, tick);
  }
  function historyReplay(sessions, rule, last, side = "buy", tick = 0.05, S = NSE) {
    const long = side === "buy", n = rule.or_minutes / BAR, trades = [];
    for (const [d, D] of sessions) {
      const [oh, ol] = openingRange(D, n);
      let kk = null; for (let i = n; i <= last; i++) if (long ? D[i].c > oh : D[i].c < ol) { kk = i; break; }
      if (kk === null) continue;
      const e = tickRound(D[kk].c, tick), s = ruleStop(rule, oh, ol, e, long, tick), risk = long ? e - s : s - e;
      if (risk <= 0) continue;
      const t = tickRound(long ? e + rule.target_r * risk : e - rule.target_r * risk, tick);
      let oc = null; for (const b of D.slice(kk + 1, last + 1)) { if (long ? b.l <= s : b.h >= s) { oc = "stop"; break; } if (long ? b.h >= t : b.l <= t) { oc = "target"; break; } }
      const cl = D[last].c, Rv = oc === "target" ? rule.target_r : oc === "stop" ? -1 : (long ? cl - e : e - cl) / risk;
      trades.push({ date: d, time: S.label(BAR * (kk + 1)), entry: e, stop: s, target: t, outcome: oc || "close", R: rnd(Rv, 2) });
    }
    const c = (o) => trades.filter((x) => x.outcome === o).length;
    return { sessions: sessions.length, triggered: trades.length, target: c("target"), stop: c("stop"), close: c("close"), green: trades.filter((x) => x.R > 0).length,
      avg_R: trades.length ? rnd(mean(trades.map((x) => x.R)), 3) : null, rule, trades };
  }
  function verdict(mc) {
    const pt = mc.p_target, ps = mc.p_stop, until = mc.until || "15:30";
    if (pt + ps <= 0) return { tone: "none", head: "No side reached", text: `No replay reached the target or the stop by ${until}: the price at ${until} decides every one.` };
    const r = mc.r_at_target || 1, share = pt / (pt + ps), even = 1 / (1 + r), d = share - even, oneR = Math.abs(r - 1) < 0.005;
    let head, tone;
    if (Math.abs(d) < COIN_FLIP) [head, tone] = [oneR ? "Close to a coin flip" : "Close to break-even", "flat"];
    else if (Math.abs(d) < STRONG) [head, tone] = d > 0 ? ["Leans toward the target", "good"] : ["Leans toward the stop", "bad"];
    else [head, tone] = d > 0 ? ["Strongly toward the target", "good"] : ["Strongly toward the stop", "bad"];
    const odds = `target first ${(100 * pt).toFixed(1)}% vs stop first ${(100 * ps).toFixed(1)}%`;
    const text = oneR ? `${odds}.` : `At ${+r.toFixed(2)}R the target has to come first in ${(100 * even).toFixed(0)}% of the replays that hit a side; it did in ${(100 * share).toFixed(0)}% (${odds}).`;
    const notes = [];
    if ((mc.p_neither || 0) > MANY_NEITHER) notes.push(`${(100 * mc.p_neither).toFixed(1)}% of replays hit neither by ${until}; the price at ${until} decides those.`);
    if (Math.abs(d) >= COIN_FLIP && mc.avg_R && mc.avg_R > 0 !== d > 0) notes.push(`Average R is ${(mc.avg_R >= 0 ? "+" : "") + mc.avg_R.toFixed(3)} all the same: replays that hit neither pull it the other way.`);
    return { tone, head, text, notes, break_even: rnd(even, 4), share: rnd(share, 4) };
  }
  function timeMachine(bars, plan, replays = 10000, seed = 19, window = 2, until = null, tick = 0.05, S = NSE, minSessions = MIN_SESSIONS) {
    if (!(replays >= 100 && replays <= 200000)) throw DE("replays must be between 100 and 200,000");
    if (!(window >= 0 && window <= 10)) throw DE("window must be 0 to 10 bars");
    const side = plan.side || "buy", long = side === "buy", nd = decimals(tick), P = (x) => fmtp(x, tick);
    const entry = +plan.entry, stop = +plan.stop, target = +plan.target;
    if (long ? stop >= entry || target <= entry : stop <= entry || target >= entry) throw DE("for a buy: stop under entry, target above; for a sell the other way round");
    const eo = S.toOffset(plan.entry_time, "entry time");
    if (eo % BAR || !(eo >= BAR && eo < S.length)) throw DE(`entry time must be a 5-minute bar close inside the session (${S.label(BAR)} to ${S.label(S.length - BAR)} IST)`);
    const k = eo / BAR - 1;
    let uo = until == null ? S.length : S.toOffset(until, "until"); if (uo === 0) uo = S.length;
    if (uo % BAR || uo > S.length) throw DE(`'until' must be on the 5-minute grid, no later than ${S.label(0)} IST (session end)`);
    const last = uo / BAR - 1;
    if (last <= k) throw DE(`out-by ${S.label(uo)} leaves nothing to replay after a ${plan.entry_time} entry`);
    const days = splitSessions(bars), date = plan.date, ed = days[date] || {}, warn = [];
    if (ed[k]) { const eb = ed[k]; if (!(eb.l - tick <= entry && entry <= eb.h + tick)) warn.push(`the entry ${P(entry)} is outside the ${eb.start} bar's range ${P(eb.l)}-${P(eb.h)}`); }
    else warn.push(`no completed bar closing ${plan.entry_time} on ${date} in the data yet; replaying as a hypothetical entry`);
    const [sessions, short] = fullSessions(days, date, S.bars);
    if (sessions.length < minSessions) throw DE(`only ${sessions.length} full session(s) before ${date}; the Time Machine needs at least ${minSessions}. New contracts (weekly options) have little history: replay the underlying future instead and translate the levels.`);
    if (sessions.length < MIN_SESSIONS) warn.push(`only ${sessions.length} past sessions in the pool (options are young contracts): treat the odds as rough`);
    const mc = simulate(buildPool(sessions, S.bars), entry, stop, target, k, last, replays, seed, window, side, nd);
    Object.assign(mc, { until: S.label(uo), pool_sessions: sessions.length, pool_from: sessions[0][0], pool_to: sessions[sessions.length - 1][0], band_times: [...Array(last - k + 1).keys()].map((j) => S.label(BAR * (k + j + 1))) });
    let history = null; const orm = parseInt(plan.or_minutes || 0, 10);
    if (orm && orm % BAR === 0 && orm / BAR <= k + 1) {
      const risk = Math.abs(entry - stop), sr = plan.stop_rule;
      history = historyReplay(sessions, { setup: "orb", or_minutes: orm, target_r: rnd(Math.abs(target - entry) / risk, 2), stop_rule: sr === "or_mid" || sr === "or_far" ? sr : "pct", stop_pct: rnd((100 * risk) / entry, 4) }, last, side, tick, S);
    }
    return { plan: { ...plan, entry_bar: k }, mc, history, verdict: verdict(mc), warnings: warn, short_sessions_left_out: short,
      note: `Built from the instrument's own last ${sessions.length} full sessions. Not a prediction: results, news, expiries, policy days and liquidations can do things those sessions never did. Same seed, same numbers on this device (the PC uses a different random generator, so its odds differ by a point or so). Costs and slippage not included.` };
  }

  // ---------------------------------------------------------------- sizing and risk
  const DEFAULT_CONFIG = { budget: 100000, cash_buffer_pct: 10, max_position_pct: 50, max_order_value: 100000, max_loss_per_trade: 500, max_daily_loss: 1500, max_trades_per_day: 3,
    min_reward_risk: 1.5, require_stop: true, no_entries_before: "09:30", no_entries_after: "15:00", flat_by: "15:15", allow_short: false, blocked: [], tick: 0.05, or_minutes: 15,
    target_r: 2.0, cost_per_order: 20, currency: "INR", watchlist: [] };
  const MCX_CONFIG = { ...DEFAULT_CONFIG, no_entries_before: "09:15", no_entries_after: "23:00", flat_by: "23:15", cost_per_order: 40, tick: null };
  const CRYPTO_CONFIG = { ...DEFAULT_CONFIG, budget: 1000, max_order_value: 1000, max_loss_per_trade: 10, max_daily_loss: 30, no_entries_before: "05:45", no_entries_after: "03:30",
    flat_by: "04:30", cost_per_order: 0, fee_bps: 10, allow_short: false, tick: null, currency: "USDT" };
  function floorStep(x, step) { if (step <= 0) return x; const n = Math.floor(rnd(x / step, 9)); return rnd(n * step, step < 1 ? decimals(step) : 0); }
  function sizePosition(plan, cfg, maxLoss = null, qtyStep = 1, minQty = null) {
    const c = { ...DEFAULT_CONFIG, ...(cfg || {}) }, entry = +plan.entry, rps = +plan.risk_per_share;
    if (!(rps > 0) || !(entry > 0)) throw DE("the plan needs an entry and a positive risk per unit");
    const ml = +(maxLoss != null ? maxLoss : c.max_loss_per_trade);
    let byRisk = floorStep(ml / rps, qtyStep);
    const investable = +c.budget * (1 - +c.cash_buffer_pct / 100), cap = Math.min((investable * +c.max_position_pct) / 100, +c.max_order_value);
    let byCap = floorStep(cap / entry, qtyStep), qty = Math.max(0, Math.min(byRisk, byCap));
    const binding = byRisk <= byCap ? "max loss" : "position cap", notes = [];
    if (minQty && qty > 0 && qty < minQty) { notes.push(`${qty} is under the exchange minimum of ${minQty}.`); qty = 0; }
    if (qty === 0) notes.push("Not even the minimum quantity fits: widen the max loss, tighten the stop, or skip the trade.");
    if (qtyStep >= 1) { qty = Math.trunc(qty); byRisk = Math.trunc(byRisk); byCap = Math.trunc(byCap); }
    const fee = ((+c.fee_bps || 0) / 10000) * qty * entry * 2, costs = 2 * (+c.cost_per_order || 0) + fee, target = plan.target;
    return { qty, by_risk: byRisk, by_cap: byCap, binding, max_loss: ml, cap_value: rnd(cap, 2), order_value: rnd(qty * entry, 2), loss_at_stop: rnd(qty * rps, 2),
      gain_at_target: target ? rnd(qty * Math.abs(+target - entry), 2) : null, est_costs: rnd(costs, 2), qty_step: qtyStep, currency: c.currency || "INR", notes };
  }
  function riskCheck(order, cfg, ledger, nowMs = Date.now(), S = NSE) {
    const c = { ...DEFAULT_CONFIG, ...(cfg || {}) }, cur = (c.currency || "INR") === "INR" ? "₹" : c.currency + " ", today = S.sessionDate(nowMs);
    const reasons = [], warns = [], checks = [], chk = (ok, label, why) => { checks.push({ ok: !!ok, label }); if (!ok) reasons.push(why || label); };
    const m = (x) => cur + Number(x).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const sym = String(order.symbol || "").toUpperCase(), side = order.side || "buy", qty = +(order.qty || 0), entry = +(order.entry || 0), target = order.target;
    let stop = order.stop;
    chk(qty > 0, "Quantity above zero", "quantity is 0");
    chk(!(c.blocked || []).map((s) => s.toUpperCase()).includes(sym), "Symbol not on the blocked list", `${sym} is blocked`);
    chk(side === "buy" || c.allow_short, "Short selling allowed", "short (sell-first) trades are off in your limits");
    chk(stop != null || !c.require_stop, "Has a stop-loss", "no stop-loss: every entry needs one");
    let loss = 0;
    if (stop != null && entry) {
      stop = +stop; const wrong = side === "buy" ? stop >= entry : stop <= entry;
      chk(!wrong, "Stop on the right side of the entry", "stop is on the wrong side of the entry");
      loss = Math.abs(entry - stop) * qty;
      chk(loss <= +c.max_loss_per_trade + 0.005, `Loss at stop ≤ ${m(c.max_loss_per_trade)}`, `loss at stop ${m(loss)} is over the ${m(c.max_loss_per_trade)} per-trade limit`);
      if (target != null && Math.abs(entry - stop) > 0) { const rr = Math.abs(+target - entry) / Math.abs(entry - stop); chk(rr + 1e-9 >= +c.min_reward_risk, `Reward:risk ≥ ${c.min_reward_risk}`, `reward:risk ${rr.toFixed(2)} is under your minimum ${c.min_reward_risk}`); }
    }
    const value = qty * entry;
    chk(value <= +c.max_order_value + 0.005, `Order value ≤ ${m(c.max_order_value)}`, `order value ${m(value)} is over the ${m(c.max_order_value)} limit`);
    const todays = ledger.filter((r) => r.date === today);
    chk(todays.length < +c.max_trades_per_day, `Under ${c.max_trades_per_day} trades this session`, `already ${todays.length} trade(s) this session, the limit is ${c.max_trades_per_day}`);
    const realized = todays.filter((r) => r.status === "closed").reduce((a, r) => a + Math.min(0, +(r.pnl || 0)), 0);
    const atRisk = todays.filter((r) => r.status === "open").reduce((a, r) => a + +(r.max_loss || 0), 0), worst = -realized + atRisk + loss;
    chk(worst <= +c.max_daily_loss + 0.005, `Worst-case day ≤ ${m(c.max_daily_loss)}`, `this session's losses + open risk + this trade = ${m(worst)}, over the ${m(c.max_daily_loss)} daily limit`);
    chk(!ledger.some((r) => r.status === "open" && String(r.symbol).toUpperCase() === sym), "No open position in this symbol", `you already hold an open ${sym} position`);
    const nb = S.toOffset(c.no_entries_before), na = S.toOffset(c.no_entries_after), off = S.nowOffset(nowMs), t = ist(nowMs), openDay = !S.weekdays_only || t.wd < 5;
    chk(openDay && nb <= off && off <= na, `Inside entry window ${c.no_entries_before}–${c.no_entries_after} IST`,
      `outside the entry window ${c.no_entries_before}–${c.no_entries_after} IST` + (S.weekdays_only ? " on a weekday" : "") + ` (now ${clock(t.h * 60 + t.mi)}${openDay ? "" : ", weekend"})`);
    return { pass: !reasons.length, reasons, warnings: warns, checks, loss_at_stop: rnd(loss, 2), order_value: rnd(value, 2), worst_case_day: rnd(worst, 2), currency: c.currency || "INR" };
  }

  // ================================================================ data layer (routes.py port)
  const MARKETS = ["NSE", "FNO", "MCX", "BINANCE", "BINANCEF", "SHARK", "DELTA"], FYM = ["NSE", "FNO", "MCX"];
  const SESSION = { NSE, FNO: NSE, MCX, BINANCE: CRYPTO, BINANCEF: CRYPTO, SHARK: CRYPTO, DELTA: CRYPTO };
  const CURRENCY = { NSE: "INR", FNO: "INR", MCX: "INR", BINANCE: "USDT", BINANCEF: "USDT", SHARK: "USDT", DELTA: "USD" };
  const MAX_SCAN = 40;
  const DEFAULT_WATCHLIST = {
    NSE: ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "BHARTIARTL", "ITC", "LT", "KOTAKBANK", "AXISBANK", "BAJFINANCE", "MARUTI", "SUNPHARMA", "M&M", "TITAN", "TATASTEEL", "HCLTECH", "ADANIENT", "TRENT"].map((s) => `NSE:${s}-EQ`),
    BINANCE: ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT", "LTCUSDT"].map((s) => `BINANCE:${s}`),
    SHARK: [],
    FNO: ["NSE:NIFTY-FUT", "NSE:BANKNIFTY-FUT", "NSE:FINNIFTY-FUT", "NSE:MIDCPNIFTY-FUT", "BSE:SENSEX-FUT", "BSE:BANKEX-FUT", "NSE:NIFTY-ATM-CE", "NSE:NIFTY-ATM-PE", "NSE:BANKNIFTY-ATM-CE", "NSE:BANKNIFTY-ATM-PE", "BSE:SENSEX-ATM-CE", "BSE:SENSEX-ATM-PE"],
    MCX: ["CRUDEOIL", "CRUDEOILM", "NATURALGAS", "GOLD", "GOLDM", "SILVER", "SILVERM", "COPPER", "ZINC"].map((u) => `MCX:${u}-FUT`),
    BINANCEF: ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "XAUUSDT", "XAGUSDT", "CLUSDT"].map((s) => `BINANCEF:${s}`),
    DELTA: ["DELTA:BTC-ATM-CE", "DELTA:BTC-ATM-PE", "DELTA:ETH-ATM-CE", "DELTA:ETH-ATM-PE"],
  };
  const NSE_RE = /^(NSE|BSE):[A-Z0-9&_.\-]{1,30}-(EQ|BE|INDEX)$/, FO_RE = /^(NSE|BSE|MCX):[A-Z0-9&_\-]{2,40}$/, PAIR_RE = /^[A-Z0-9]{3,20}$/,
    DELTA_RE = /^([CP]-[A-Z0-9]{2,10}-[0-9]+(\.[0-9]+)?-[0-9]{6}|[A-Z0-9]{2,10}-ATM-(CE|PE))$/;
  const marketOf = (s) => { const h = s.split(":")[0]; return ["BINANCE", "BINANCEF", "SHARK", "DELTA", "MCX"].includes(h) ? h : NSE_RE.test(s) ? "NSE" : "FNO"; };
  const isOption = (s) => { s = s.toUpperCase(); return s.endsWith("CE") || s.endsWith("PE") || s.startsWith("DELTA:C-") || s.startsWith("DELTA:P-"); };
  const pairOf = (s) => s.split(":").slice(1).join(":");
  function normSymbol(s, market) {
    s = String(s || "").trim().toUpperCase().replace(/[ /]/g, "");
    if (!s) throw new HE(400, "symbol is empty");
    if (!s.includes(":")) { const m = market || "NSE"; s = { NSE: `NSE:${s}-EQ`, FNO: `NSE:${s}`, MCX: `MCX:${s}` }[m] || `${m}:${s}`; }
    const m = marketOf(s);
    if (m === "NSE" && !NSE_RE.test(s)) throw new HE(400, `'${s}' is not a FYERS symbol like NSE:SBIN-EQ or NSE:NIFTY50-INDEX`);
    if ((m === "FNO" || m === "MCX") && !FO_RE.test(s)) throw new HE(400, `'${s}' is not a FYERS F&O symbol like NSE:NIFTY-FUT or MCX:CRUDEOIL-FUT`);
    if (m === "DELTA" && !DELTA_RE.test(pairOf(s))) throw new HE(400, `'${s}' is not a Delta option like DELTA:C-BTC-80000-271126 or DELTA:BTC-ATM-CE`);
    if (["BINANCE", "BINANCEF", "SHARK"].includes(m) && !PAIR_RE.test(pairOf(s))) throw new HE(400, `'${s}' is not a ${m} pair like ${m}:BTCUSDT`);
    return s;
  }
  const needEnabled = (m) => { if (m === "SHARK") throw new HE(409, "Shark Exchange is not available on the phone version (it does not answer phone browsers). Use it from the PC."); };

  // ---- FYERS symbol masters (fyers_master.py)
  const MASTER_URL = { NSE: "https://public.fyers.in/sym_details/NSE_FO.csv", BSE: "https://public.fyers.in/sym_details/BSE_FO.csv", MCX: "https://public.fyers.in/sym_details/MCX_COM.csv" };
  const INDEXES = { NSE: ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"], BSE: ["SENSEX", "BANKEX", "SENSEX50"] };
  const SPOT = { NIFTY: "NSE:NIFTY50-INDEX", BANKNIFTY: "NSE:NIFTYBANK-INDEX", FINNIFTY: "NSE:FINNIFTY-INDEX", MIDCPNIFTY: "NSE:MIDCPNIFTY-INDEX", NIFTYNXT50: "NSE:NIFTYNXT50-INDEX", SENSEX: "BSE:SENSEX-INDEX", BANKEX: "BSE:BANKEX-INDEX", SENSEX50: "BSE:SENSEX50-INDEX" };
  const MCX_POPULAR = ["CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI", "GOLD", "GOLDM", "GOLDPETAL", "SILVER", "SILVERM", "SILVERMIC", "COPPER", "ZINC", "ALUMINIUM", "LEAD", "NICKEL"];
  const MCX_MULT = { CRUDEOIL: 100, CRUDEOILM: 10, NATURALGAS: 1250, NATGASMINI: 250, GOLD: 100, GOLDM: 10, GOLDGUINEA: 1, GOLDPETAL: 1, GOLDTEN: 1, SILVER: 30, SILVERM: 5, SILVERMIC: 1,
    COPPER: 2500, ZINC: 5000, ZINCMINI: 1000, ALUMINIUM: 5000, ALUMINI: 1000, LEAD: 5000, LEADMINI: 1000, NICKEL: 1500, MENTHAOIL: 360, COTTONCNDY: 48, KAPAS: 200 };
  const masters = {}, bySymbol = {};
  async function masterText(ex) {
    const day = new Date(Date.now() + IST_MS).toISOString().slice(0, 10), key = MASTER_URL[ex] + "#" + day;
    try { if (window.caches) { const c = await caches.open("mm-fyers-masters"); const hit = await c.match(key); if (hit) return hit.text(); const r = await fetch(MASTER_URL[ex]); if (!r.ok) throw 0; const t = await r.text();
      for (const k of await c.keys()) if (k.url.startsWith(MASTER_URL[ex])) await c.delete(k); await c.put(key, new Response(t)); return t; } } catch (e) { /* fall through */ }
    const r = await fetch(MASTER_URL[ex]); if (!r.ok) throw new HE(502, `Could not download the FYERS ${ex} contract list`); return r.text();
  }
  async function loadMaster(ex) {
    if (masters[ex]) return masters[ex];
    masters[ex] = (async () => {
      const rows = [];
      for (const line of (await masterText(ex)).split("\n")) {
        const r = line.split(","); if (r.length < 17 || !r[9]) continue;
        const exp = +r[8]; if (!isFinite(exp)) continue;
        let kind = r[16].trim().toUpperCase(); const sym = r[9].trim().toUpperCase();
        if ((kind === "XX" || kind === "") && sym.endsWith("FUT")) kind = "FUT"; else if (kind !== "CE" && kind !== "PE") continue;
        let lot = Math.max(1, parseInt(parseFloat(r[3]), 10) || 1); const tick = parseFloat(r[4]) || 0.05, strike = parseFloat(r[15]), und = r[13].trim().toUpperCase();
        let mult = 1; if (ex === "MCX" && lot === 1) { mult = MCX_MULT[und] || 1; lot = mult; }
        const o = { symbol: sym, exchange: ex, underlying: und, kind, multiplier: mult, expiry: exp * 1000, strike: kind !== "FUT" && isFinite(strike) ? strike : null, lot, tick, session: r[6].trim(), name: r[1].trim() };
        rows.push(o); bySymbol[sym] = o;
      }
      if (!rows.length) throw new HE(502, `The FYERS ${ex} contract list was empty`);
      return rows;
    })();
    masters[ex].catch(() => delete masters[ex]);
    return masters[ex];
  }
  const live = (rows) => rows.filter((r) => r.expiry > Date.now());
  const istDate = (ms) => new Date(ms + IST_MS).toISOString().slice(0, 10);
  async function contract(symbol) { const ex = symbol.split(":")[0]; if (MASTER_URL[ex]) await loadMaster(ex); return bySymbol[symbol.toUpperCase()] || null; }
  async function frontFuture(ex, und) {
    const futs = (await loadMaster(ex)).filter((r) => r.underlying === und && r.kind === "FUT" && r.expiry > Date.now() + 864e5);
    if (!futs.length) throw new HE(400, `No live ${und} future on ${ex}`);
    return futs.reduce((a, b) => (b.expiry < a.expiry ? b : a));
  }
  async function underlyings(ex) {
    const seen = {};
    for (const r of live(await loadMaster(ex))) { const u = (seen[r.underlying] = seen[r.underlying] || { underlying: r.underlying, futures: 0, options: 0 }); u[r.kind === "FUT" ? "futures" : "options"]++; }
    const pref = (INDEXES[ex] || []).concat(ex === "MCX" ? MCX_POPULAR : []), first = pref.filter((p) => seen[p]).map((p) => ({ ...seen[p], popular: true }));
    return first.concat(Object.values(seen).filter((v) => !pref.includes(v.underlying)).sort((a, b) => (a.underlying < b.underlying ? -1 : 1)));
  }
  async function chain(ex, und, expiry) {
    const rows = live(await loadMaster(ex)).filter((r) => r.underlying === und);
    if (!rows.length) throw new HE(404, `No live ${und} contracts on ${ex}`);
    const futs = rows.filter((r) => r.kind === "FUT").sort((a, b) => a.expiry - b.expiry), opts = rows.filter((r) => r.kind !== "FUT");
    const exps = [...new Set(opts.map((r) => istDate(r.expiry)))].sort(), chosen = exps.includes(expiry) ? expiry : exps[0] || null, strikes = {};
    for (const r of opts) if (istDate(r.expiry) === chosen) (strikes[r.strike] = strikes[r.strike] || { strike: r.strike })[r.kind] = r.symbol;
    const pub = (r) => ({ symbol: r.symbol, expiry: istDate(r.expiry), lot: r.lot, tick: r.tick });
    return { exchange: ex, underlying: und, spot_symbol: SPOT[und] || null, futures: futs.map(pub), expiries: exps, expiry: chosen, lot: (futs[0] || opts[0] || { lot: 1 }).lot,
      strikes: Object.keys(strikes).map(Number).sort((a, b) => a - b).map((k) => strikes[k]) };
  }

  // ---- crypto instrument info
  const BN = "https://data-api.binance.vision", BF = "https://fapi.binance.com", DL = "https://api.india.delta.exchange";
  const binanceInfo = (pair) => cached("bninfo:" + pair, 3600e3, async () => {
    const d = await getJ(`${BN}/api/v3/exchangeInfo?symbol=${pair}`), s = ((d || {}).symbols || [])[0];
    if (!s) throw new HE(400, `${pair} is not a listed Binance instrument`);
    const f = Object.fromEntries((s.filters || []).map((x) => [x.filterType, x])), lot = f.LOT_SIZE || {};
    return { pair: s.symbol, base: s.baseAsset, quote: s.quoteAsset, tick: +((f.PRICE_FILTER || {}).tickSize || 0.01), qty_step: +(lot.stepSize || 0.00001), min_qty: +(lot.minQty || 0), name: `${s.baseAsset}/${s.quoteAsset} spot` };
  });
  const futInfoAll = () => cached("bfinfo", 3600e3, async () => {
    const d = await getJ(`${BF}/fapi/v1/exchangeInfo`), info = {};
    for (const s of (d || {}).symbols || []) {
      if (s.status !== "TRADING" || !String(s.contractType || "").includes("PERPETUAL")) continue;
      const f = Object.fromEntries((s.filters || []).map((x) => [x.filterType, x])), lot = f.LOT_SIZE || {};
      info[s.symbol] = { pair: s.symbol, base: s.baseAsset, quote: s.quoteAsset, tick: +((f.PRICE_FILTER || {}).tickSize || 0.01), qty_step: +(lot.stepSize || 0.001), min_qty: +(lot.minQty || 0), name: `${s.baseAsset}/${s.quoteAsset} perpetual` };
    }
    return info;
  });
  const deltaCoin = (coin) => cached("dlcoin:" + coin, 120e3, async () => {
    const rows = ((await getJ(`${DL}/v2/tickers?contract_types=call_options,put_options&underlying_asset_symbols=${coin}`)) || {}).result || [], info = {};
    let spot = null;
    for (const r of rows) {
      const p = String(r.symbol).split("-"), d = p[3]; if (!d || d.length !== 6) continue;
      const cv = +(r.contract_value || 0.001), exp = Date.parse(`20${d.slice(4, 6)}-${d.slice(2, 4)}-${d.slice(0, 2)}T12:00:00Z`);
      info[r.symbol] = { pair: r.symbol, base: coin, quote: "USD", tick: +(r.tick_size || 0.1), qty_step: cv, min_qty: cv, contract_value: cv, strike: +r.strike_price,
        kind: r.contract_type === "call_options" ? "CE" : "PE", expiry_ms: exp, name: r.description || r.symbol, mark: +r.mark_price };
      spot = spot || +r.spot_price;
    }
    if (!Object.keys(info).length) throw new HE(502, `Delta Exchange returned no live ${coin} options`);
    return { info, spot };
  });
  async function resolve(symbol) {
    const s = symbol.toUpperCase();
    if (!/(-FUT|-ATM-CE|-ATM-PE)$/.test(s)) return s;
    return cached("alias:" + s, 60e3, async () => {
      const m = marketOf(s);
      if (s.endsWith("-FUT")) { const [ex, rest] = s.split(":"); return (await frontFuture(ex, rest.slice(0, -4))).symbol; }
      if (m === "DELTA") {
        const [coin, , side] = pairOf(s).split("-"), { info, spot } = await deltaCoin(coin), cut = Date.now() + 864e5;
        const opts = Object.values(info).filter((v) => v.kind === side && v.expiry_ms > cut); if (!opts.length) throw new HE(400, `No live ${coin} options on Delta Exchange`);
        const exp = Math.min(...opts.map((v) => v.expiry_ms)); return "DELTA:" + opts.filter((v) => v.expiry_ms === exp).reduce((a, v) => (Math.abs(v.strike - spot) < Math.abs(a.strike - spot) ? v : a)).pair;
      }
      const [ex, rest] = s.split(":"), und = rest.slice(0, -7), side = rest.slice(-2), ch = await chain(ex, und);
      const spotSym = ch.spot_symbol || (ch.futures[0] || {}).symbol; if (!spotSym) throw new HE(400, `No price reference for ${und}`);
      const px = await spot(spotSym), rows = ch.strikes.filter((r) => r[side]); if (!rows.length) throw new HE(400, `No ${und} ${side} options listed for ${ch.expiry}`);
      return rows.reduce((a, r) => (Math.abs(r.strike - px) < Math.abs(a.strike - px) ? r : a))[side];
    });
  }
  async function spot(sym) { const d = ((await FY.quotes(sym)) || {}).d || []; for (const r of d) if (r.v && r.v.lp) return +r.v.lp; throw new HE(502, `No FYERS price for ${sym}`); }

  // ---- candles
  const fyCandles = (symbol, resolution, daysBack, ttl) => {
    const to = new Date(Date.now() + IST_MS), fr = new Date(to - daysBack * 864e5), f = (d) => d.toISOString().slice(0, 10);
    return cached(`dk:${symbol}:${resolution}:${f(fr)}`, ttl * 1000, async () => (((await FY.get("/history", { symbol, resolution, date_format: "1", range_from: f(fr), range_to: f(to), cont_flag: "1" })) || {}).candles || []));
  };
  function fillGaps(rows, maxGap = 7200, step = 300) {
    const out = [];
    for (const r of rows) { if (out.length) { const p = out[out.length - 1], gap = r[0] - p[0]; if (gap > step && gap <= maxGap) for (let t = p[0] + step; t < r[0]; t += step) out.push([t, p[4], p[4], p[4], p[4], 0]); } out.push(r); }
    return out;
  }
  const bnK = (base, path, pair, interval, limit, startMs) => getJ(`${base}${path}?` + new URLSearchParams({ symbol: pair, interval, limit, ...(startMs ? { startTime: startMs } : {}) }))
    .then((rows) => (rows || []).map((r) => [Math.floor(r[0] / 1000), +r[1], +r[2], +r[3], +r[4], +r[5]]));
  async function bnPaged(m, pair, fromTs) {
    const [base, path, per] = m === "BINANCE" ? [BN, "/api/v3/klines", 1000] : [BF, "/fapi/v1/klines", 1500];
    let rows = [], start = fromTs * 1000;
    for (let i = 0; i < 20; i++) { const page = await bnK(base, path, pair, "5m", per, start); rows = rows.concat(page); if (page.length < per) break; start = page[page.length - 1][0] * 1000 + 1; }
    return rows;
  }
  async function deltaCandles(pair, res, start, end) {
    let rows = []; const win = res === "5m" ? 6 * 86400 : 2000 * 86400;
    while (start < end) { const stop = Math.min(end, start + win), d = await getJ(`${DL}/v2/history/candles?` + new URLSearchParams({ resolution: res, symbol: "MARK:" + pair, start, end: stop }));
      rows = rows.concat(((d || {}).result || []).map((r) => [+r.time, +r.open, +r.high, +r.low, +r.close, +(r.volume || 0)])); start = stop; }
    const m = new Map(rows.map((r) => [r[0], r])); return [...m.keys()].sort((a, b) => a - b).map((k) => m.get(k));
  }
  const cryptoHistory = (m, pair) => cached(`ch:${m}:${pair}`, 20e3, () => (m === "DELTA" ? deltaCandles(pair, "5m", Math.floor(Date.now() / 1000) - 45 * 86400, Math.floor(Date.now() / 1000)) : bnPaged(m, pair, Math.floor(Date.now() / 1000) - 45 * 86400)));
  async function historyBars(symbol) {
    const m = marketOf(symbol), S = SESSION[m];
    if (FYM.includes(m)) { const rows = await fyCandles(symbol, "5", 90, 30); return parseCandles(isOption(symbol) ? fillGaps(rows) : rows, Date.now(), BAR, S); }
    needEnabled(m); const rows = await cryptoHistory(m, pairOf(symbol)); return parseCandles(m === "DELTA" ? fillGaps(rows) : rows, Date.now(), BAR, S);
  }
  async function todayBars(symbol) {
    const m = marketOf(symbol), S = SESSION[m]; let bars;
    if (FYM.includes(m)) { const rows = await fyCandles(symbol, "5", 0, 20); bars = parseCandles(isOption(symbol) ? fillGaps(rows) : rows, Date.now(), BAR, S); }
    else { needEnabled(m); const now = Math.floor(Date.now() / 1000);
      const rows = await cached(`tb:${symbol}`, 20e3, () => (m === "DELTA" ? deltaCandles(pairOf(symbol), "5m", now - 300 * 300, now) : bnK(m === "BINANCE" ? BN : BF, m === "BINANCE" ? "/api/v3/klines" : "/fapi/v1/klines", pairOf(symbol), "5m", 300)));
      bars = parseCandles(m === "DELTA" ? fillGaps(rows) : rows, Date.now(), BAR, S); }
    const sd = S.sessionDate(Date.now()); return bars.filter((b) => b.date === sd);
  }
  async function dailyBars(symbol) {
    const m = marketOf(symbol);
    if (FYM.includes(m)) return dailyFromCandles(await fyCandles(symbol, "D", 150, 6 * 3600), Date.now(), m !== "MCX" ? NSE : MCX);
    needEnabled(m); const now = Math.floor(Date.now() / 1000);
    const rows = await cached(`db:${symbol}`, 1800e3, () => (m === "DELTA" ? deltaCandles(pairOf(symbol), "1d", now - 150 * 86400, now) : bnK(m === "BINANCE" ? BN : BF, m === "BINANCE" ? "/api/v3/klines" : "/fapi/v1/klines", pairOf(symbol), "1d", 150)));
    return dailyFromCandles(rows, Date.now(), SESSION[m]);
  }
  async function instrument(symbol, cfg) {
    const m = marketOf(symbol);
    if (m === "NSE") return { tick: +(cfg.tick || 0.05), qty_step: 1, min_qty: 1, quote: "INR", lot: 1 };
    if (m === "FNO" || m === "MCX") { const c = await contract(symbol); if (!c) throw new HE(400, `${symbol} is not a live FYERS contract (check the expiry/strike)`);
      return { tick: c.tick, qty_step: c.lot, min_qty: c.lot, lot: c.lot, quote: "INR", kind: c.kind, underlying: c.underlying, expiry: istDate(c.expiry), strike: c.strike }; }
    needEnabled(m);
    let meta;
    if (m === "BINANCE") meta = await binanceInfo(pairOf(symbol));
    else if (m === "BINANCEF") { meta = (await futInfoAll())[pairOf(symbol)]; if (!meta) throw new HE(400, `${pairOf(symbol)} is not a listed Binance Futures instrument`); }
    else { const coin = pairOf(symbol).split("-")[1]; meta = (await deltaCoin(coin)).info[pairOf(symbol)]; if (!meta) throw new HE(400, `${pairOf(symbol)} is not a live Delta Exchange option`); }
    return { ...meta, lot: meta.qty_step };
  }
  async function lastPrices(symbols) {
    const out = {}, fy = symbols.filter((s) => FYM.includes(marketOf(s)));
    for (let i = 0; i < fy.length; i += 50) for (const r of (((await FY.quotes(fy.slice(i, i + 50).join(","))) || {}).d || [])) if (r.s === "ok" && r.v && r.v.lp != null) out[String(r.n).toUpperCase()] = +r.v.lp;
    for (const s of symbols) {
      const m = marketOf(s), p = pairOf(s);
      try {
        if (m === "BINANCE") out[s] = +(await getJ(`${BN}/api/v3/ticker/price?symbol=${p}`)).price;
        else if (m === "BINANCEF") out[s] = +(await getJ(`${BF}/fapi/v1/ticker/price?symbol=${p}`)).price;
        else if (m === "DELTA") { const r = ((await getJ(`${DL}/v2/tickers/${p}`)) || {}).result || {}; if (r.mark_price != null) out[s] = +r.mark_price; }
      } catch (e) { /* a missing price leaves the position unmarked */ }
    }
    return out;
  }

  // ---- config + paper ledger in this browser
  const allSaved = () => LS.get("desk.config") || {};
  function loadConfig(market = "NSE") {
    const base = { NSE: DEFAULT_CONFIG, FNO: DEFAULT_CONFIG, MCX: MCX_CONFIG }[market] || CRYPTO_CONFIG, cfg = { ...base, currency: CURRENCY[market] };
    if (market === "FNO" || market === "MCX") Object.assign(cfg, { budget: 300000, max_order_value: 1500000, max_position_pct: 100, cost_per_order: 40, max_loss_per_trade: 2500, max_daily_loss: 7500 });
    if (market === "DELTA") cfg.fee_bps = 3;
    Object.assign(cfg, allSaved()[market] || {});
    if (market === "SHARK") cfg.currency = "USDT";
    cfg.market = market;
    if (!cfg.watchlist || !cfg.watchlist.length) cfg.watchlist = [...DEFAULT_WATCHLIST[market]];
    return cfg;
  }
  const loadLedger = () => { const d = LS.get("desk.paper"); return Array.isArray(d) ? d : []; };
  const saveLedger = (l) => LS.set("desk.paper", l);
  const BOUNDS = { budget: [0, null, true], cash_buffer_pct: [0, 90], max_position_pct: [0, 100, true], max_order_value: [0, null, true], max_loss_per_trade: [0, null, true], max_daily_loss: [0, null, true],
    max_trades_per_day: [1, 50], min_reward_risk: [0, 20], tick: [0, 10, true], or_minutes: [5, 60], target_r: [0, 20, true], cost_per_order: [0, 10000], fee_bps: [0, 500] };
  function setConfig(market, body) {
    const upd = {};
    for (const [k, v] of Object.entries(body || {})) {
      if (v === null || v === undefined) continue;
      if (BOUNDS[k]) { const [lo, hi, excl] = BOUNDS[k], n = +v; if (!isFinite(n) || (excl ? n <= lo : n < lo) || (hi != null && n > hi)) throw new HE(422, `${k} is out of range`); upd[k] = n; }
      else if (["no_entries_before", "no_entries_after", "flat_by"].includes(k)) upd[k] = clock(parseClock(v, k));
      else if (["allow_short", "require_stop"].includes(k)) upd[k] = !!v;
      else if (k === "currency") { if (!["INR", "USDT", "USD"].includes(v)) throw new HE(422, "currency must be INR, USDT or USD"); upd[k] = v; }
      else if (k === "watchlist") { const wl = v.filter((s) => String(s).trim()).map((s) => normSymbol(s, market)); if (wl.some((s) => marketOf(s) !== market)) throw new HE(422, `this watchlist is for ${market} symbols only`); if (wl.length > MAX_SCAN) throw new HE(422, `watchlist is limited to ${MAX_SCAN} symbols (request limits)`); upd[k] = [...new Set(wl)]; }
      else if (k === "blocked") upd[k] = v.filter((s) => String(s).trim()).map((s) => normSymbol(s, market));
    }
    if (upd.or_minutes && upd.or_minutes % 5) throw new HE(422, "opening range must be a multiple of 5 minutes");
    const saved = allSaved(); saved[market] = { ...(saved[market] || {}), ...upd }; LS.set("desk.config", saved);
    return loadConfig(market);
  }

  // ---- routes
  const POPULAR_PERPS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "XAUUSDT", "XAGUSDT", "CLUSDT", "BZUSDT", "NATGASUSDT", "COPPERUSDT"];
  async function scanOne(alias, orm) {
    const row = { symbol: alias }, S = SESSION[marketOf(alias)];
    try {
      const sym = await resolve(alias); row.contract = sym;
      const daily = await dailyBars(sym);
      if (daily.length >= 30) { const [tags, nums] = findSetups(daily.map((b) => b.c)); Object.assign(row, { daily_tags: tags, daily: nums, prev_close: daily[daily.length - 1].c, avg_vol20: Math.round(mean(daily.slice(-20).map((b) => b.v))), daily_date: daily[daily.length - 1].date }); }
      else Object.assign(row, { daily_tags: [], daily_note: `only ${daily.length} daily bars` });
      const bars = await todayBars(sym); row.orb = orbScanRow(bars, orm, S); row.bars_today = bars.length;
    } catch (e) { if ([401, 409, 429].includes(e.status)) throw e; row.error = String(e.message || e).slice(0, 200); }
    return row;
  }
  async function riskOf(o, ledger) {
    const m = marketOf(o.symbol), cfg = loadConfig(m), rc = riskCheck(o, cfg, ledger.filter((r) => (r.market || "NSE") === m), Date.now(), SESSION[m]);
    if (!FYM.includes(m)) {
      try { const inst = await instrument(o.symbol, cfg), ok = inst.quote === cfg.currency; rc.checks.push({ ok, label: `Quoted in your limit currency (${cfg.currency})` });
        if (!ok) { rc.reasons.push(`${pairOf(o.symbol)} is quoted in ${inst.quote}, but your ${m} limits are in ${cfg.currency}. Change the currency under Limits.`); rc.pass = false; } }
      catch (e) { rc.reasons.push(e.message); rc.pass = false; }
    }
    return rc;
  }
  function closeRec(rec, price, reason, cfg) {
    const long = rec.side === "buy", gross = long ? (price - rec.entry) * rec.qty : (rec.entry - price) * rec.qty;
    const fee = ((+cfg.fee_bps || 0) / 10000) * rec.qty * (rec.entry + price), costs = rnd(2 * (+cfg.cost_per_order || 0) + fee, 4);
    Object.assign(rec, { status: "closed", exit: price, exit_reason: reason, exit_at: isoIst(Date.now()), gross: rnd(gross, 4), costs, pnl: rnd(gross - costs, 4), R: rec.stop ? rnd(gross / (Math.abs(rec.entry - rec.stop) * rec.qty), 2) : null });
  }
  const orderIn = (o) => ({ symbol: o.symbol, side: o.side === "sell" ? "sell" : "buy", qty: +(o.qty || 0), entry: +o.entry, stop: o.stop == null || o.stop === "" ? null : +o.stop,
    target: o.target == null || o.target === "" ? null : +o.target, setup: o.setup || null, tm_verdict: o.tm_verdict || null });

  async function route(p, q, method, body) {
    if (p === "/api/desk/config") return method === "POST" ? setConfig(q.get("market") || "NSE", body) : loadConfig(q.get("market") || "NSE");
    if (p === "/api/desk/derivs/underlyings") {
      const market = q.get("market");
      if (market === "FNO") { const out = []; for (const ex of ["NSE", "BSE"]) for (const u of await underlyings(ex)) out.push({ ...u, exchange: ex, id: `${ex}:${u.underlying}` }); out.sort((a, b) => !a.popular - !b.popular); return { market, items: out }; }
      if (market === "MCX") return { market, items: (await underlyings("MCX")).map((u) => ({ ...u, exchange: "MCX", id: `MCX:${u.underlying}` })) };
      if (market === "DELTA") return { market, items: ["BTC", "ETH", "XAUT"].map((c) => ({ underlying: c, exchange: "DELTA", id: `DELTA:${c}`, popular: true, options: true })) };
      if (market === "BINANCEF") { const info = await futInfoAll(), pref = POPULAR_PERPS.filter((p) => info[p]), rest = Object.keys(info).filter((p) => !pref.includes(p)).sort();
        return { market, items: pref.concat(rest).map((p) => ({ underlying: p, id: `${market}:${p}`, popular: pref.includes(p), name: info[p].name })) }; }
      if (market === "BINANCE") { const pref = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"]; return { market, items: pref.map((p) => ({ underlying: p, id: `${market}:${p}`, popular: true, name: p })) }; }
      needEnabled(market); throw new HE(400, "No derivatives for this market");
    }
    if (p === "/api/desk/derivs/chain") {
      const market = q.get("market"), underlying = (q.get("underlying") || "").toUpperCase(), expiry = q.get("expiry");
      if (market === "FNO" || market === "MCX") {
        const [ex, und] = underlying.includes(":") ? underlying.split(":") : [market === "MCX" ? "MCX" : "NSE", underlying], ch = await chain(ex, und, expiry);
        const ss = ch.spot_symbol || (ch.futures[0] || {}).symbol; let sp = null; if (ss) try { sp = await spot(ss); } catch (e) { sp = null; }
        const atm = sp && ch.strikes.length ? ch.strikes.reduce((a, r) => (Math.abs(r.strike - sp) < Math.abs(a.strike - sp) ? r : a)).strike : null;
        return { ...ch, market, spot: sp, atm, aliases: { FUT: `${ex}:${und}-FUT`, "ATM-CE": `${ex}:${und}-ATM-CE`, "ATM-PE": `${ex}:${und}-ATM-PE` } };
      }
      if (market === "DELTA") {
        const coin = underlying.split(":").pop(), { info, spot: sp } = await deltaCoin(coin), opts = Object.values(info).filter((v) => v.expiry_ms > Date.now());
        const exps = [...new Set(opts.map((v) => istDate(v.expiry_ms)))].sort(), chosen = exps.includes(expiry) ? expiry : exps[0] || null, strikes = {};
        for (const v of opts) if (istDate(v.expiry_ms) === chosen) (strikes[v.strike] = strikes[v.strike] || { strike: v.strike })[v.kind] = `DELTA:${v.pair}`;
        const rows = Object.keys(strikes).map(Number).sort((a, b) => a - b).map((k) => strikes[k]), atm = rows.length ? rows.reduce((a, r) => (Math.abs(r.strike - sp) < Math.abs(a.strike - sp) ? r : a)).strike : null;
        return { market, exchange: "DELTA", underlying: coin, futures: [], expiries: exps, expiry: chosen, strikes: rows, spot: sp, atm, lot: (opts[0] || {}).contract_value || 0.001,
          aliases: { "ATM-CE": `DELTA:${coin}-ATM-CE`, "ATM-PE": `DELTA:${coin}-ATM-PE` } };
      }
      if (market === "BINANCEF" || market === "BINANCE") { const sym = underlying.includes(":") ? underlying : `${market}:${underlying}`; return { market, underlying: pairOf(sym), futures: [{ symbol: sym, expiry: "perpetual" }], expiries: [], strikes: [], aliases: { FUT: sym } }; }
      needEnabled(market); throw new HE(400, "No derivatives for this market");
    }
    if (p === "/api/desk/scan") {
      const market = body.market || "NSE"; needEnabled(market);
      const cfg = loadConfig(market), syms = (body.symbols && body.symbols.length ? body.symbols : cfg.watchlist).map((s) => normSymbol(s, market)).slice(0, MAX_SCAN), orm = body.or_minutes || +cfg.or_minutes;
      const rows = []; for (let i = 0; i < syms.length; i += 4) rows.push(...(await Promise.all(syms.slice(i, i + 4).map((s) => scanOne(s, orm)))));
      return { as_of: isoIst(Date.now()), market, or_minutes: orm, session_open: SESSION[market].label(0), count: rows.length, setups: SETUPS, rows, note: "A scan is a list of charts that match a rule, not a list of things to buy." };
    }
    if (p === "/api/desk/entry") {
      const alias = normSymbol(q.get("symbol"), q.get("market")), sym = await resolve(alias), m = marketOf(sym), cfg = loadConfig(m), inst = await instrument(sym, cfg), bars = await todayBars(sym);
      const lv = q.get("level"), res = entryStatus(bars, Date.now(), +(q.get("or_minutes") || cfg.or_minutes), q.get("side") === "sell" ? "sell" : "buy", cfg.no_entries_before, cfg.no_entries_after, lv ? +lv : null, SESSION[m], inst.tick);
      return Object.assign(res, { symbol: sym, alias: alias !== sym ? alias : null, instrument: inst, lot: inst.lot, market: m, date: res.session_date, tick: inst.tick, session_open: SESSION[m].label(0), session_bars: SESSION[m].bars,
        currency: inst.quote || cfg.currency, candles: bars.map((b) => ({ t: b.start, slot: b.slot, o: b.o, h: b.h, l: b.l, c: b.c })) });
    }
    if (p === "/api/desk/exit") {
      const sym = await resolve(normSymbol(body.symbol)), m = marketOf(sym), cfg = loadConfig(m), inst = await instrument(sym, cfg);
      let bars = null; if (body.stop_rule === "atr") { bars = await todayBars(sym); if (body.entry_time) { const eo = SESSION[m].toOffset(body.entry_time); bars = bars.filter((b) => b.off + 5 <= eo); } }
      const plan = planExit(+body.entry, body.side || "buy", body.stop_rule || "or_mid", body.stop_value == null ? null : +body.stop_value, body.target_rule || "r", body.target_value == null ? 2 : +body.target_value,
        body.or_high == null ? null : +body.or_high, body.or_low == null ? null : +body.or_low, bars, +inst.tick);
      return Object.assign(plan, { flat_by: cfg.flat_by, symbol: sym, market: m, currency: inst.quote || cfg.currency });
    }
    if (p === "/api/desk/timemachine") {
      const sym = await resolve(normSymbol(body.symbol)), m = marketOf(sym), cfg = loadConfig(m), S = SESSION[m], inst = await instrument(sym, cfg), bars = await historyBars(sym);
      const plan = { symbol: sym, date: body.date || S.sessionDate(Date.now()), entry_time: body.entry_time, entry: +body.entry, stop: +body.stop, target: +body.target, side: body.side || "buy",
        or_minutes: body.or_minutes || null, stop_rule: body.stop_rule || null, until: body.until || null, replays: +(body.replays || 10000), seed: +(body.seed ?? 19), window: +(body.window ?? 2) };
      await new Promise((r) => setTimeout(r, 0));
      const doc = timeMachine(bars, plan, plan.replays, plan.seed, plan.window, body.until || cfg.flat_by, +inst.tick, S, isOption(sym) ? 8 : MIN_SESSIONS);
      return Object.assign(doc, { symbol: sym, market: m, tick: inst.tick });
    }
    if (p === "/api/desk/size") {
      const sym = await resolve(normSymbol(body.symbol)), m = marketOf(sym), cfg = loadConfig(m), inst = await instrument(sym, cfg);
      const out = sizePosition(body, cfg, body.max_loss || null, +inst.qty_step, inst.min_qty), lot = inst.lot || 1;
      if (m === "DELTA" && lot < 1) { out.lots = Math.round(out.qty / lot); out.lot = lot; out.notes.push(`1 contract = ${lot} ${inst.base || ""}: ${out.lots} contract(s).`); }
      else if ((m === "FNO" || m === "MCX") && lot > 1) { out.lots = Math.floor(out.qty / lot); out.lot = lot; out.notes.push(`Lot size ${lot}: ${out.lots} lot(s) = ${out.qty} units` + (m === "MCX" ? " (MCX: units = lots x the contract multiplier; order FYERS in lots)." : ".")); }
      return out;
    }
    if (p === "/api/desk/risk") { const o = orderIn(body); o.symbol = await resolve(normSymbol(o.symbol)); return riskOf(o, loadLedger()); }
    if (p === "/api/desk/paper/order") {
      if (String(body.confirm || "").trim().toUpperCase() !== "YES") throw new HE(400, "Type YES to confirm the paper order");
      const o = orderIn(body.order || {}); o.symbol = await resolve(normSymbol(o.symbol));
      const m = marketOf(o.symbol), cfg = loadConfig(m), S = SESSION[m], ledger = loadLedger(), rc = await riskOf(o, ledger);
      if (!rc.pass) throw new HE(409, "Risk manager blocked it: " + rc.reasons.join("; "));
      const rec = { id: [...crypto.getRandomValues(new Uint8Array(4))].map((b) => b.toString(16).padStart(2, "0")).join(""), market: m, currency: cfg.currency, date: S.sessionDate(Date.now()),
        placed_at: isoIst(Date.now()), status: "open", paper: true, ...o, max_loss: rc.loss_at_stop };
      ledger.push(rec); saveLedger(ledger);
      return { placed: rec, risk: rc, note: "Paper order recorded on this device. Nothing was sent to FYERS, Binance, Delta or any broker." };
    }
    if (p === "/api/desk/paper") {
      const ledger = loadLedger(), openSyms = [...new Set(ledger.filter((r) => r.status === "open").map((r) => r.symbol))];
      let marks = {}, err = null; if (q.get("mark") !== "false" && openSyms.length) try { marks = await lastPrices(openSyms); } catch (e) { err = e.message; }
      const now = Date.now(); let changed = false;
      for (const r of ledger) {
        if (r.status !== "open" || marks[r.symbol] == null) continue;
        const m = r.market || "NSE", cfg = loadConfig(m), S = SESSION[m], lp = marks[r.symbol], long = r.side === "buy";
        r.ltp = lp; r.unrealized = rnd((long ? lp - r.entry : r.entry - lp) * r.qty, 4);
        const flat = (!S.weekdays_only || ist(now).wd < 5) && (r.date !== S.sessionDate(now) || S.nowOffset(now) >= S.toOffset(cfg.flat_by));
        if (r.stop && (long ? lp <= r.stop : lp >= r.stop)) { closeRec(r, r.stop, "stop", cfg); changed = true; }
        else if (r.target && (long ? lp >= r.target : lp <= r.target)) { closeRec(r, r.target, "target", cfg); changed = true; }
        else if (flat) { closeRec(r, lp, `flat by ${cfg.flat_by}`, cfg); changed = true; }
      }
      saveLedger(ledger);
      const byCcy = {};
      for (const r of ledger.filter((x) => x.status === "closed")) {
        const d = (byCcy[r.currency || "INR"] = byCcy[r.currency || "INR"] || { today: 0, total: 0, wins: 0, closed: 0 }), S = SESSION[r.market || "NSE"];
        d.total += r.pnl || 0; d.today += r.date === S.sessionDate(now) ? r.pnl || 0 : 0; d.wins += (r.pnl || 0) > 0 ? 1 : 0; d.closed++;
      }
      for (const v of Object.values(byCcy)) { v.today = rnd(v.today, 2); v.total = rnd(v.total, 2); }
      return { as_of: isoIst(now), orders: [...ledger].reverse().slice(0, 200), mark_error: err, by_currency: byCcy, note: "Paper fills at the stop/target level when the last price crosses it; real fills can be worse. Kept on this device." };
    }
    const mc = p.match(/^\/api\/desk\/paper\/([^/]+)\/close$/);
    if (mc) {
      const ledger = loadLedger(), rec = ledger.find((r) => r.id === decodeURIComponent(mc[1]));
      if (!rec || rec.status !== "open") throw new HE(404, "No open paper position with that id");
      const lp = (await lastPrices([rec.symbol]))[rec.symbol]; if (lp == null) throw new HE(502, "Could not get a last price to close at");
      closeRec(rec, lp, "manual", loadConfig(rec.market || "NSE")); saveLedger(ledger); return rec;
    }
    throw new HE(404, "Unknown Day Desk request: " + p);
  }
  MM.desk = route;
  MM.deskEngine = { Session, NSE, CRYPTO, MCX, parseCandles, entryStatus, planExit, timeMachine, sizePosition, riskCheck, findSetups, dailyFromCandles, orbScanRow, historyReplay, simulate, buildPool, fullSessions, splitSessions, tickRound, tickAway };
})();
