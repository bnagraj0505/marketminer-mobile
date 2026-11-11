/* MarketMiner Mobile · Market Pulse engine.
 * The PC answers /api/pulse/* from backend/markets/pulse.py (NSE website + FYERS). Phones can't read NSE's website,
 * so this file rebuilds the same answers from FYERS alone, inside the browser:
 *   overview, options, oiflow, liquidity, strength, radar, screeners/basis, screeners/rsi, calm, symbols.
 * Differences from the PC: sectors come from MarketMiner's built-in list (others show as "Other"), futures basis has
 * no OI change (FYERS quotes carry none), OI spurts need the PC, and Calm Zone uses realised volatility from FYERS
 * daily candles for the 40 most traded F&O stocks (downloaded once a day on this device).
 * Loaded after engine.js on pulse.html. Requests go through MM.FY (shared rate limit and daily count).
 */
(function () {
  "use strict";
  const MM = window.MM, FY = MM.FY, HE = MM.HE, cached = MM.cached, LS = MM.LS;
  const IST = 19800e3;
  const INDEX_SPOT = { NIFTY: "NSE:NIFTY50-INDEX", BANKNIFTY: "NSE:NIFTYBANK-INDEX", FINNIFTY: "NSE:FINNIFTY-INDEX", MIDCPNIFTY: "NSE:MIDCPNIFTY-INDEX",
    SENSEX: "BSE:SENSEX-INDEX", BANKEX: "BSE:BANKEX-INDEX" };
  const VIX = "NSE:INDIAVIX-INDEX", PULSE_INDICES = ["NIFTY", "BANKNIFTY", "SENSEX"], CARRY = 0.065;
  const SECTOR = {};
  Object.entries({
    "Banks": "HDFCBANK ICICIBANK KOTAKBANK AXISBANK INDUSINDBK FEDERALBNK IDFCFIRSTB BANDHANBNK AUBANK RBLBANK YESBANK",
    "PSU Bank": "SBIN BANKBARODA PNB CANBK UNIONBANK BANKINDIA INDIANB",
    "NBFC": "BAJFINANCE BAJAJFINSV CHOLAFIN SHRIRAMFIN MUTHOOTFIN MANAPPURAM LICHSGFIN PFC RECLTD IREDA M&MFIN SBICARD JIOFIN POONAWALLA",
    "IT": "TCS INFY HCLTECH WIPRO TECHM LTIM PERSISTENT COFORGE MPHASIS OFSS KPITTECH TATAELXSI",
    "Auto": "MARUTI M&M TATAMOTORS BAJAJ-AUTO HEROMOTOCO EICHERMOT TVSMOTOR ASHOKLEY BHARATFORG MOTHERSON TMPV",
    "Pharma": "SUNPHARMA CIPLA DRREDDY DIVISLAB LUPIN AUROPHARMA ZYDUSLIFE TORNTPHARM ALKEM GLENMARK BIOCON MANKIND LAURUSLABS",
    "Metals": "TATASTEEL JSWSTEEL HINDALCO VEDL JINDALSTEL SAIL NMDC NATIONALUM HINDZINC",
    "Energy": "RELIANCE ONGC IOC BPCL HINDPETRO GAIL PETRONET OIL NTPC POWERGRID TATAPOWER ADANIGREEN COALINDIA",
    "FMCG": "HINDUNILVR ITC NESTLEIND BRITANNIA DABUR MARICO GODREJCP COLPAL TATACONSUM VBL",
    "Cap goods": "LT SIEMENS ABB BHEL CGPOWER HAL BEL BDL CUMMINSIND POLYCAB KEI",
    "Cement": "ULTRACEMCO SHREECEM AMBUJACEM ACC DALBHARAT GRASIM",
    "Telecom": "BHARTIARTL IDEA INDUSTOWER",
  }).forEach(([s, names]) => names.split(" ").forEach((n) => { SECTOR[n] = s; }));

  const f = (x, d = null) => { const v = Number(x); return x !== null && x !== undefined && x !== "" && isFinite(v) ? v : d; };
  const clip = (x, a = -1, b = 1) => Math.max(a, Math.min(b, x));
  const istNow = () => new Date(Date.now() + IST);
  const marketOpen = () => { const n = istNow(), m = n.getUTCHours() * 60 + n.getUTCMinutes(), w = n.getUTCDay(); return w >= 1 && w <= 5 && m >= 555 && m <= 930; };
  const ttl = (live, closed) => (marketOpen() ? live : closed) * 1000;
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const stamp = (fmt) => { const n = istNow(), dd = String(n.getUTCDate()).padStart(2, "0"), hh = String(n.getUTCHours()).padStart(2, "0"), mm = String(n.getUTCMinutes()).padStart(2, "0");
    return fmt === "t" ? `${hh}:${mm} IST` : `${dd}-${MON[n.getUTCMonth()]}-${n.getUTCFullYear()} ${hh}:${mm} IST`; };
  const spotSym = (u) => INDEX_SPOT[u] || `NSE:${u}-EQ`;
  const dstr = (ms) => new Date(ms + IST).toISOString().slice(0, 10);

  // ---------------------------------------------------------------- FYERS helpers
  async function quotes(symbols) {
    const out = {}, list = [...new Set(symbols)];
    for (let i = 0; i < list.length; i += 50) {
      const p = await FY.quotes(list.slice(i, i + 50).join(","));
      for (const r of (p && p.d) || []) {
        if (r.s !== "ok") continue;
        const v = r.v || {};
        out[String(r.n).toUpperCase()] = { lp: f(v.lp), ch: f(v.ch), chp: f(v.chp), open: f(v.open_price), high: f(v.high_price), low: f(v.low_price),
          prev: f(v.prev_close_price), volume: f(v.volume, 0), atp: f(v.atp) };
      }
    }
    return out;
  }
  async function candles(symbol, res, days) {
    const out = new Map(), today = Date.now();
    const span = res === "D" ? 100 : 30;
    for (let start = today - days * 864e5; start <= today; start += (span + 1) * 864e5) {
      const end = Math.min(today, start + span * 864e5);
      const p = await FY.history({ symbol, resolution: res, date_format: "1", range_from: dstr(start), range_to: dstr(end), cont_flag: "1" });
      for (const c of (p && p.candles) || []) if (c.length >= 5) out.set(+c[0], { t: +c[0], o: +c[1], h: +c[2], l: +c[3], c: +c[4], v: c[5] == null ? 0 : +c[5] });
    }
    return [...out.values()].sort((a, b) => a.t - b.t);
  }
  // FYERS NSE F&O contract list (public file, cached for the day in the browser)
  const MASTER = "https://public.fyers.in/sym_details/NSE_FO.csv";
  let masterP = null;
  function master() {
    if (masterP) return masterP;
    masterP = (async () => {
      const day = dstr(Date.now()), key = MASTER + "#" + day;
      let txt = null;
      try { if (window.caches) { const c = await caches.open("mm-fyers-masters"); const hit = await c.match(key); if (hit) txt = await hit.text(); else {
        const r = await fetch(MASTER); if (r.ok) { txt = await r.text(); for (const k of await c.keys()) if (k.url.startsWith(MASTER)) await c.delete(k); await c.put(key, new Response(txt)); } } } } catch (e) { txt = null; }
      if (!txt) { const r = await fetch(MASTER); if (!r.ok) throw new HE(502, "Could not download the FYERS F&O contract list"); txt = await r.text(); }
      const futs = {}, opts = new Set();
      for (const line of txt.split("\n")) {
        const r = line.split(","); if (r.length < 17 || !r[9]) continue;
        const exp = +r[8] * 1000, sym = r[9].trim().toUpperCase(), und = r[13].trim().toUpperCase(); let kind = r[16].trim().toUpperCase();
        if (!isFinite(exp) || exp < Date.now()) continue;
        if ((kind === "XX" || !kind) && sym.endsWith("FUT")) kind = "FUT";
        if (kind === "FUT") { (futs[und] = futs[und] || []).push({ symbol: sym, expiry: exp, lot: parseInt(parseFloat(r[3]), 10) || 1 }); }
        else if (kind === "CE" || kind === "PE") opts.add(und);
      }
      Object.values(futs).forEach((a) => a.sort((x, y) => x.expiry - y.expiry));
      const stocks = [...opts].filter((u) => !INDEX_SPOT[u] && u !== "NIFTYNXT50").sort();
      return { futs, stocks };
    })();
    masterP.catch(() => { masterP = null; });
    return masterP;
  }

  // ---------------------------------------------------------------- F&O universe (FYERS quotes)
  function universe() {
    return cached("pulse:uni", ttl(60, 900), async () => {
      const m = await master();
      const q = await quotes(m.stocks.map((s) => `NSE:${s}-EQ`));
      const rows = [];
      for (const s of m.stocks) {
        const v = q[`NSE:${s}-EQ`]; if (!v || v.lp == null) continue;
        rows.push({ symbol: s, lp: v.lp, chp: v.chp || 0, open: v.open, high: v.high, low: v.low, prev: v.prev, volume: v.volume || 0,
          value: (v.volume || 0) * (v.atp || v.lp), sector: SECTOR[s] || "Other" });
      }
      if (!rows.length) throw new HE(503, "No F&O stock prices: is FYERS connected on this device?");
      return { source: "FYERS (this device)", rows, as_of: stamp() };
    });
  }

  // ---------------------------------------------------------------- option chain and its reading (pulse.py port)
  function chain(u, strikes = 25, ttlS = 120) {
    u = u.toUpperCase();
    return cached(`pulse:ch:${u}:${strikes}`, ttl(ttlS, 900), async () => {
      const p = await FY.chain(spotSym(u), strikes);
      const d = (p && p.data) || p || {}, rows = d.optionsChain || [];
      let spot = 0;
      for (const r of rows) if ((f(r.strike_price, 0) || 0) <= 0) { spot = f(r.ltp, 0); break; }
      const by = {};
      for (const r of rows) {
        if (r.option_type !== "CE" && r.option_type !== "PE") continue;
        const g = r.greeks || {}, k = +r.strike_price;
        (by[k] = by[k] || { strike: k })[r.option_type] = { symbol: String(r.symbol || ""), ltp: f(r.ltp, 0), ltp_chg: f(r.ltpch), oi: f(r.oi, 0), oi_chg: f(r.oich, 0),
          vol: f(r.volume, 0), iv: f(g.iv, 0), delta: f(g.delta, 0) };
      }
      const st = Object.keys(by).map(Number).sort((a, b) => a - b).map((k) => by[k]);
      if (!st.length || !spot) throw new HE(503, `FYERS returned an empty ${u} option chain`);
      const exps = (d.expiryData || []).slice().sort((a, b) => +a.expiry - +b.expiry), e0 = exps[0] || {};
      const days = Math.max(0, ((+e0.expiry || Date.now() / 1000) + 10 * 3600 - Date.now() / 1000) / 86400);
      const atm = st.reduce((a, b) => (Math.abs(b.strike - spot) < Math.abs(a.strike - spot) ? b : a));
      const ivs = [(atm.CE || {}).iv, (atm.PE || {}).iv].filter((x) => x);
      return { underlying: u, spot, expiry: e0.date || null, days, strikes: st, atm_strike: atm.strike, atm_iv: ivs.length ? ivs.reduce((a, b) => a + b) / ivs.length : null,
        iv_rank: null, source: "FYERS option chain" };
    });
  }
  function readChain(ch) {
    const spot = ch.spot, st = ch.strikes, ks = st.map((r) => r.strike);
    const ce = {}, pe = {}; st.forEach((r) => { ce[r.strike] = r.CE || {}; pe[r.strike] = r.PE || {}; });
    const S = (fn) => ks.reduce((a, k) => a + fn(k), 0);
    const totCe = S((k) => ce[k].oi || 0), totPe = S((k) => pe[k].oi || 0), pcr = totCe ? totPe / totCe : null;
    const dCe = S((k) => ce[k].oi_chg || 0), dPe = S((k) => pe[k].oi_chg || 0), dpcr = dCe > 0 && dPe > 0 ? dPe / dCe : null;
    const pain = ks.reduce((best, x) => { const v = S((k) => (ce[k].oi || 0) * Math.max(0, x - k) + (pe[k].oi || 0) * Math.max(0, k - x)); return v < best[1] ? [x, v] : best; }, [ks[0], Infinity])[0];
    const above = ks.filter((k) => k >= spot), below = ks.filter((k) => k <= spot), A = above.length ? above : ks, B = below.length ? below : ks;
    const argmax = (arr, fn) => arr.reduce((a, b) => (fn(b) > fn(a) ? b : a));
    const callWall = argmax(A, (k) => ce[k].oi || 0), putWall = argmax(B, (k) => pe[k].oi || 0);
    const callAdd = argmax(A, (k) => ce[k].oi_chg || 0), putAdd = argmax(B, (k) => pe[k].oi_chg || 0);
    const ceiling = (ce[callAdd].oi_chg || 0) > 0 ? callAdd : callWall, floor = (pe[putAdd].oi_chg || 0) > 0 ? putAdd : putWall;
    const atm = ch.atm_strike, straddle = ((ce[atm] || {}).ltp || 0) + ((pe[atm] || {}).ltp || 0), iv = ch.atm_iv || 0, days = Math.max(ch.days || 0, 0.25);
    const sigma = iv ? spot * iv / 100 * Math.sqrt(days / 365) : straddle * 0.8;
    const w = (Math.abs(dPe) + Math.abs(dCe)) ? (dPe - dCe) / (Math.abs(dPe) + Math.abs(dCe)) : 0;
    const pos = ceiling > floor ? (spot - floor) / (ceiling - floor) : 0.5;
    const score = clip(0.6 * w + 0.4 * (pcr ? (pcr - 1) / 0.5 : 0));
    const bias = score > 0.25 ? "Bullish" : score < -0.25 ? "Bearish" : "Neutral";
    const conviction = Math.round(50 + 50 * Math.min(1, Math.abs(score) * 1.3 + (Math.abs(w) > 0.4 ? 0.15 : 0)));
    const fmt = (x) => Number(x).toLocaleString("en-IN", { maximumFractionDigits: 0 });
    const notes = [];
    if (pcr !== null) notes.push(`PCR ${pcr.toFixed(2)}: ` + (pcr > 1.2 ? "put writers dominate (support below)." : pcr < 0.8 ? "call writers dominate (resistance above)." : "balanced positioning."));
    notes.push(`Biggest call OI wall ${fmt(callWall)}, biggest put OI wall ${fmt(putWall)}; today's fresh writing marks the floor ${fmt(floor)} and ceiling ${fmt(ceiling)}.`);
    notes.push(`Max pain ${fmt(pain)} is ${(Math.abs(pain - spot) / spot * 100).toFixed(2)}% ${pain > spot ? "above" : "below"} spot.`);
    if (iv) notes.push(`ATM IV ${iv.toFixed(1)}% prices a ±${fmt(sigma)} (${(sigma / spot * 100).toFixed(1)}%) move by expiry.`);
    notes.push(`Today ${dPe > dCe ? "more puts" : "more calls"} were written (${dPe >= 0 ? "+" : ""}${fmt(dPe)} PE vs ${dCe >= 0 ? "+" : ""}${fmt(dCe)} CE OI).`);
    const rows = ks.map((k) => ({ strike: k, ce_oi: ce[k].oi || 0, pe_oi: pe[k].oi || 0, ce_chg: ce[k].oi_chg || 0, pe_chg: pe[k].oi_chg || 0, ce_ltp: ce[k].ltp, pe_ltp: pe[k].ltp,
      ce_iv: ce[k].iv, pe_iv: pe[k].iv, ce_vol: ce[k].vol || 0, pe_vol: pe[k].vol || 0 }));
    return { spot, expiry: ch.expiry, days: ch.days, lot: null, atm, atm_iv: iv || null, iv_rank: ch.iv_rank, straddle, max_pain: pain, pcr, dpcr,
      call_wall: callWall, put_wall: putWall, ceiling, floor, band: [spot - sigma, spot + sigma], sigma, bias, score: Math.round(score * 1000) / 1000, conviction,
      range_pos: Math.round(pos * 1000) / 1000, tot_ce: totCe, tot_pe: totPe, d_ce: dCe, d_pe: dPe, rows,
      smile: ks.map((k) => ({ strike: k, ce_iv: ce[k].iv || null, pe_iv: pe[k].iv || null })), notes, source: ch.source };
  }

  // ---------------------------------------------------------------- overview
  function overview() {
    return cached("pulse:ov", ttl(60, 600), async () => {
      const safe = (p) => p.then((v) => v, (e) => ({ __error__: e.message || String(e) }));
      const [uni, chains, q, vixH] = await Promise.all([safe(universe()), Promise.all(PULSE_INDICES.map((u) => safe(chain(u, 12, 300)))),
        safe(quotes(PULSE_INDICES.map((u) => INDEX_SPOT[u]).concat([VIX]))), safe(candles(VIX, "D", 380))]);
      const rows = (uni && uni.rows) || [], qq = q && !q.__error__ ? q : {}, vh = Array.isArray(vixH) ? vixH : [];
      const adv = rows.filter((r) => (r.chp || 0) > 0.05).length, dec = rows.filter((r) => (r.chp || 0) < -0.05).length;
      const by = {};
      for (const r of rows) {
        const fl = (r.value || 0) * clip((r.chp || 0) / 2), s = (by[r.sector] = by[r.sector] || { sector: r.sector, flow: 0, value: 0, n: 0, chg: 0, stocks: [] });
        s.flow += fl; s.value += r.value || 0; s.n += 1; s.chg += (r.chp || 0) * (r.value || 0); s.stocks.push({ s: r.symbol, c: Math.round((r.chp || 0) * 100) / 100, v: r.value || 0, lp: r.lp });
      }
      const sectors = Object.values(by).map((s) => ({ ...s, chg: s.value ? s.chg / s.value : 0, stocks: s.stocks.sort((a, b) => b.v - a.v).slice(0, 14) })).sort((a, b) => b.value - a.value);
      const net = sectors.reduce((a, s) => a + s.flow, 0), gross = sectors.reduce((a, s) => a + Math.abs(s.flow), 0) || 1;
      let niftySig = 0;
      const idx = PULSE_INDICES.map((u, i) => {
        const v = qq[INDEX_SPOT[u]] || {}, ch = chains[i];
        const item = { name: u, lp: v.lp, ch: v.ch, chp: v.chp, high: v.high, low: v.low, open: v.open };
        if (ch && !ch.__error__) { const rc = readChain(ch); Object.assign(item, { lp: item.lp || rc.spot, range: rc.band, expiry: rc.expiry, iv: rc.atm_iv, bias: rc.bias, pcr: rc.pcr, max_pain: rc.max_pain }); }
        else item.error = (ch && ch.__error__) || "Option chain not available";
        if (u === "NIFTY") { const pos = item.high && item.low && item.lp && item.high > item.low ? (item.lp - item.low) / (item.high - item.low) * 2 - 1 : 0; niftySig = clip(0.6 * (item.chp || 0) + 0.4 * pos); }
        return item;
      });
      const vq = qq[VIX] || {}, closes = vh.map((c) => c.c).slice(-252), vlp = vq.lp || (closes.length ? closes[closes.length - 1] : null);
      const pct = closes.length && vlp ? closes.filter((c) => c <= vlp).length / closes.length * 100 : null;
      const vix = { lp: vlp, ch: vq.ch, chp: vq.chp, lo1y: closes.length ? Math.min(...closes) : null, hi1y: closes.length ? Math.max(...closes) : null, pct1y: pct,
        spark: closes.slice(-30), daily_move: vlp ? vlp / Math.sqrt(252) : null,
        regime: vlp == null ? null : vlp < 12 ? "Calm" : vlp < 17 ? "Normal" : vlp < 22 ? "Elevated" : "Fearful" };
      const b = adv + dec ? (adv - dec) / (adv + dec) : 0, mf = net / gross, vol = vix.chp != null ? clip(-vix.chp / 5) : 0;
      const parts = [["Breadth", 0.35, b], ["Money flow", 0.30, mf], ["NIFTY signal", 0.20, niftySig], ["Volatility", 0.15, vol]];
      const score = Math.round(50 + 50 * parts.reduce((a, p) => a + p[1] * p[2], 0));
      const label = score < 30 ? "Risk-off" : score < 45 ? "Cautious" : score < 55 ? "Balanced" : score < 70 ? "Constructive" : "Risk-on";
      const top = sectors.slice().sort((x, y) => y.chg - x.chg);
      return { as_of: stamp(), market_open: marketOpen(), mood: { score, label, parts: parts.map((p) => ({ name: p[0], weight: p[1], value: Math.round(p[2] * 1000) / 1000 })) },
        breadth: { adv, dec, unch: rows.length - adv - dec, total: rows.length }, flow: { net, gross, note: "Estimated: turnover × direction of each stock's move." },
        indices: idx, vix, sectors, leaders: top.slice(0, 2).map((s) => [s.sector, Math.round(s.chg * 100) / 100]),
        laggards: top.slice(-2).reverse().map((s) => [s.sector, Math.round(s.chg * 100) / 100]), source: (uni && uni.source) || "unavailable",
        errors: [uni && uni.__error__].filter(Boolean) };
    });
  }

  // ---------------------------------------------------------------- snapshots for OI flow (kept on this device for the day)
  function snapKey(u) { return `pulse.snap.${u}.${dstr(Date.now())}`; }
  function snap(u, rc) {
    let arr = LS.get(snapKey(u)) || [];
    const hm = stamp("t").slice(0, 5);
    if (arr.length && arr[arr.length - 1].t === hm) return arr;
    arr.push({ t: hm, spot: rc.spot, pcr: rc.pcr, ceiling: rc.ceiling, floor: rc.floor });
    arr = arr.slice(-400);
    LS.set(snapKey(u), arr);
    return arr;
  }
  const build = (oi, px) => (oi >= 0 ? (px >= 0 ? "Long buildup" : "Short buildup") : (px >= 0 ? "Short covering" : "Long unwinding"));

  async function options(sym) {
    const u = sym.toUpperCase(), rc = readChain(await chain(u, 20));
    snap(u, rc);
    return { symbol: u, as_of: stamp(), ...rc };
  }
  async function oiflow(sym) {
    const u = sym.toUpperCase(), ch = await chain(u, 20), rc = readChain(ch), arr = snap(u, rc);
    const flows = [];
    for (const r of ch.strikes) for (const side of ["CE", "PE"]) {
      const o = r[side] || {}, chg = o.oi_chg || 0;
      flows.push({ strike: r.strike, side, oi: o.oi || 0, chg, build: build(chg, o.ltp_chg || 0) });
    }
    const mom = clip((rc.d_pe - rc.d_ce) / ((Math.abs(rc.d_pe) + Math.abs(rc.d_ce)) || 1)) * 100;
    const fmt = (x) => Number(x).toLocaleString("en-IN", { maximumFractionDigits: 0 });
    const next = Math.abs(mom) < 35 ? `Range-bound between ${fmt(rc.floor)} and ${fmt(rc.ceiling)}` : mom > 0 ? "Upside drift: puts being written under price" : "Downside pressure: calls being written over price";
    let fut = null;
    try { const m = await master(), fr = ((m.futs[u] || []).filter((x) => x.expiry > Date.now() + 864e5))[0]; if (fr) { const q = await quotes([fr.symbol]); const v = q[fr.symbol.toUpperCase()] || {}; fut = { symbol: fr.symbol, lp: v.lp, ch: v.ch, chp: v.chp }; } } catch (e) { fut = null; }
    return { symbol: u, as_of: stamp("t"), spot: rc.spot, pcr: rc.pcr, ceiling: rc.ceiling, floor: rc.floor, expiry: rc.expiry, momentum: Math.round(mom), speed: null, next,
      conviction: rc.conviction, flows, hot: flows.slice().sort((a, b) => Math.abs(b.chg) - Math.abs(a.chg)).slice(0, 8), futures: fut, snapshots: arr };
  }

  function profile(bars, timeW, bins = 40) {
    if (!bars.length) return null;
    const lo = Math.min(...bars.map((c) => c.l)), hi = Math.max(...bars.map((c) => c.h));
    if (hi <= lo) return null;
    const step = (hi - lo) / bins, hist = new Array(bins).fill(0);
    for (const c of bars) { const w = timeW || !c.v ? 1 : c.v, a = Math.floor((c.l - lo) / step), b = Math.min(bins - 1, Math.floor((c.h - lo) / step)); for (let i = a; i <= b; i++) hist[Math.min(i, bins - 1)] += w / (b - a + 1); }
    let poc = 0; hist.forEach((h, i) => { if (h > hist[poc]) poc = i; });
    const total = hist.reduce((a, b) => a + b, 0); let lo_i = poc, hi_i = poc, acc = hist[poc];
    while (acc < 0.7 * total && (lo_i > 0 || hi_i < bins - 1)) { const up = hi_i < bins - 1 ? hist[hi_i + 1] : -1, dn = lo_i > 0 ? hist[lo_i - 1] : -1; if (up >= dn) { hi_i++; acc += up; } else { lo_i--; acc += dn; } }
    const mid = (i) => Math.round((lo + (i + 0.5) * step) * 100) / 100;
    return { poc: mid(poc), vah: mid(hi_i), val: mid(lo_i), bins: hist.map((h, i) => [mid(i), Math.round(h * 100) / 100]) };
  }
  const dayOf = (t) => dstr(t * 1000);
  async function liquidity(sym, res) {
    const u = sym.toUpperCase(), isIdx = !!INDEX_SPOT[u], days = { "5": 6, "15": 15, "60": 45, D: 300 }[res] || 6;
    const [bars, ch] = await Promise.all([candles(spotSym(u), res, days), chain(u, 20)]);
    const r = readChain(ch), vw = []; let day = null, pv = 0, vv = 0;
    for (const c of bars) { const d = dayOf(c.t); if (d !== day) { day = d; pv = 0; vv = 0; } const w = isIdx || !c.v ? 1 : c.v; pv += (c.h + c.l + c.c) / 3 * w; vv += w; vw.push({ time: c.t, value: Math.round(pv / vv * 100) / 100 }); }
    const prof = profile(res === "D" ? bars.slice(-60) : bars.filter((c) => dayOf(c.t) === day), isIdx);
    const strength = (k, side) => { const key = side === "PE" ? "pe_oi" : "ce_oi", row = r.rows.find((x) => x.strike === k) || {}, top = Math.max(1, ...r.rows.map((x) => x[key] || 0)); return Math.round(100 * (row[key] || 0) / top); };
    return { symbol: u, res, candles: bars.slice(-400).map((c) => ({ time: c.t, open: c.o, high: c.h, low: c.l, close: c.c })), vwap: vw.slice(-400), profile: prof,
      spot: r.spot, max_pain: r.max_pain, call_wall: r.call_wall, put_wall: r.put_wall, ceiling: r.ceiling, floor: r.floor, band: r.band, expiry: r.expiry,
      support: { level: r.floor, score: strength(r.floor, "PE") }, resistance: { level: r.ceiling, score: strength(r.ceiling, "CE") }, bias: r.bias, conviction: r.conviction,
      index_note: isIdx ? "Index has no volume: VWAP and profile use time at price." : null };
  }

  function strength() {
    return cached("pulse:str", ttl(60, 600), async () => {
      const uni = await universe();
      let nifty = 0; try { nifty = ((await quotes([INDEX_SPOT.NIFTY]))[INDEX_SPOT.NIFTY] || {}).chp || 0; } catch (e) { nifty = 0; }
      const vals = uni.rows.map((r) => r.value || 0).sort((a, b) => a - b), med = vals[Math.floor(vals.length / 2)] || 1, out = [];
      for (const r of uni.rows) {
        const { high: h, low: l, lp, open: o } = r; if (!(h && l && lp) || h <= l) continue;
        const pos = (lp - l) / (h - l) * 2 - 1, rel = (r.chp || 0) - nifty, body = o ? (lp - o) / (h - l) : 0, act = Math.log10(Math.max(1, (r.value || 0) / med) + 1) + 0.5;
        out.push({ symbol: r.symbol, lp, chp: r.chp, rel: Math.round(rel * 100) / 100, pos: Math.round(pos * 100) / 100, score: Math.round((0.45 * rel + 1.2 * pos + 0.9 * body) * act * 100) / 100, sector: r.sector, value: r.value });
      }
      const sec = {}; out.forEach((x) => (sec[x.sector] = sec[x.sector] || []).push(x.rel + nifty));
      const sa = Object.entries(sec).filter(([, v]) => v.length >= 2).map(([k, v]) => [k, v.reduce((a, b) => a + b) / v.length]).sort((a, b) => b[1] - a[1]);
      return { as_of: stamp(), nifty_chp: nifty, bullish: out.filter((x) => x.score >= 0.5).sort((a, b) => b.score - a.score), bearish: out.filter((x) => x.score <= -0.5).sort((a, b) => a.score - b.score),
        choppy: out.filter((x) => x.score > -0.5 && x.score < 0.5).length, leading: sa.slice(0, 3), lagging: sa.slice(-3).reverse(), source: uni.source,
        method: "Score = (0.45 × move vs NIFTY + 1.2 × position in day range + 0.9 × candle body) × turnover weight." };
    });
  }

  function basis(series) {
    return cached("pulse:basis:" + series, ttl(120, 900), async () => {
      const m = await master(), pairs = [];
      for (const s of m.stocks) { const list = (m.futs[s] || []).filter((x) => x.expiry > Date.now()); const fr = list[series === "next" ? 1 : 0]; if (fr) pairs.push([s, fr]); }
      if (!pairs.length) throw new HE(503, "No stock futures in the FYERS contract list");
      const q = await quotes(pairs.flatMap(([s, fr]) => [`NSE:${s}-EQ`, fr.symbol]));
      const exp = pairs[0][1].expiry, days = Math.max(1, Math.round((exp - Date.now()) / 864e5)), carry = CARRY * days / 365 * 100, out = [];
      for (const [s, fr] of pairs) {
        const sp = (q[`NSE:${s}-EQ`] || {}).lp, fv = q[fr.symbol.toUpperCase()] || {}; if (!sp || !fv.lp) continue;
        const b = fv.lp - sp, bp = b / sp * 100;
        out.push({ symbol: s, spot: sp, fut: fv.lp, basis: Math.round(b * 100) / 100, basis_pct: Math.round(bp * 1000) / 1000, annual: Math.round(bp * 365 / days * 100) / 100,
          vs_carry: Math.round((bp - carry) * 1000) / 1000, oi: 0, doi: 0, doi_pct: null, fut_chg: fv.chp || 0, build: fv.chp >= 0 ? "Long buildup" : "Short buildup",
          bucket: Math.abs(bp) <= 0.05 ? "At par" : bp > 0 ? "Premium" : "Discount" });
      }
      out.sort((a, b) => a.basis_pct - b.basis_pct);
      const buckets = { Discount: 0, "At par": 0, Premium: 0 }; out.forEach((x) => buckets[x.bucket]++);
      return { expiry: new Date(exp + IST).toISOString().slice(0, 10), days, fair_carry_pct: Math.round(carry * 1000) / 1000, rows: out, buckets,
        note: `Fair carry at ${(CARRY * 100).toFixed(1)}% a year ≈ +${carry.toFixed(2)}% for this expiry; a discount usually means short bias or an upcoming dividend. On the phone, OI change and buildup are not available (FYERS quotes don't carry OI); "buildup" here is just the future's direction.` };
    });
  }

  function rsi(closes, n = 14) {
    if (closes.length <= n) return null;
    let g = 0, l = 0;
    for (let i = 1; i <= n; i++) { const d = closes[i] - closes[i - 1]; g += Math.max(d, 0); l += Math.max(-d, 0); }
    g /= n; l /= n;
    for (let i = n + 1; i < closes.length; i++) { const d = closes[i] - closes[i - 1]; g = (g * (n - 1) + Math.max(d, 0)) / n; l = (l * (n - 1) + Math.max(-d, 0)) / n; }
    return l === 0 ? 100 : 100 - 100 / (1 + g / l);
  }
  function resample(bars, minutes) {
    const out = new Map();
    for (const c of bars) { const t = new Date(c.t * 1000 + IST), k = minutes >= 1440 ? t.toISOString().slice(0, 10) : t.toISOString().slice(0, 10) + ":" + Math.floor((t.getUTCHours() * 60 + t.getUTCMinutes() - 555) / minutes); out.set(k, c.c); }
    return [...out.keys()].sort().map((k) => out.get(k));
  }
  function rsiScreen(top) {
    return cached("pulse:rsi:" + top, ttl(900, 3600), async () => {
      const uni = await universe(), names = uni.rows.slice().sort((a, b) => (b.value || 0) - (a.value || 0)).slice(0, top).map((r) => r.symbol);
      const targets = [["NIFTY", INDEX_SPOT.NIFTY], ["BANKNIFTY", INDEX_SPOT.BANKNIFTY]].concat(names.map((n) => [n, `NSE:${n}-EQ`])), rows = [];
      for (const [n, s] of targets) {
        try {
          const bars = await candles(s, "15", 45), r15 = rsi(bars.map((c) => c.c)), r60 = rsi(resample(bars, 60)), rd = rsi(resample(bars, 1440)), v = [r15, r60, rd].filter((x) => x != null);
          const state = v.length && Math.min(...v) >= 70 ? "Overbought all TFs" : v.length && Math.max(...v) <= 30 ? "Oversold all TFs" : v.length && Math.min(...v) >= 55 ? "Bullish aligned" : v.length && Math.max(...v) <= 45 ? "Bearish aligned" : "Mixed";
          rows.push({ symbol: n, lp: bars.length ? bars[bars.length - 1].c : null, r15, r60, rd, state });
        } catch (e) { rows.push({ symbol: n, error: e.message }); }
      }
      return { rows, as_of: stamp(), note: `Top ${top} F&O stocks by turnover; daily RSI uses the last ~45 days of 15-minute candles.` };
    });
  }

  function radar(sym) {
    const u = sym.toUpperCase();
    return cached("pulse:radar:" + u, ttl(60, 900), async () => {
      const s = spotSym(u), isIdx = !!INDEX_SPOT[u];
      const [b5, daily, ch] = await Promise.all([candles(s, "5", 5), candles(s, "D", 60).catch(() => []), chain(u, 15).catch(() => null)]);
      if (!b5.length) throw new HE(503, "No candles for " + u);
      const last = b5[b5.length - 1].c, today = dayOf(b5[b5.length - 1].t), tb = b5.filter((c) => dayOf(c.t) === today), sig = {}, why = {};
      let fr = null;
      if (!isIdx) { try { const bs = await basis("near"), row = bs.rows.find((x) => x.symbol === u); if (row) fr = row; } catch (e) { fr = null; } }
      if (fr) { sig["Futures direction"] = clip(fr.fut_chg / 1, -2, 2); why["Futures direction"] = `Future ${fr.fut_chg >= 0 ? "+" : ""}${fr.fut_chg.toFixed(2)}%, basis ${fr.basis_pct.toFixed(2)}%`; }
      else { sig["Futures direction"] = 0; why["Futures direction"] = "Futures not available"; }
      const rc = ch ? readChain(ch) : null;
      if (rc) { sig["Option chain"] = Math.round(clip(rc.score * 1.6, -2, 2) * 10) / 10; why["Option chain"] = rc.pcr ? `${rc.bias} writers, PCR ${rc.pcr.toFixed(2)}` : rc.bias; }
      else { sig["Option chain"] = 0; why["Option chain"] = "No option chain"; }
      const pr = profile(tb.length ? tb : b5.slice(-75), isIdx);
      if (pr) { sig["Volume profile"] = last > pr.vah ? 1 : last < pr.val ? -1 : last > pr.poc ? 0.5 : -0.5; why["Volume profile"] = `Price ${last > pr.poc ? "above" : "below"} POC ${pr.poc}`; }
      const r = rsi(b5.slice(-120).map((c) => c.c));
      if (r != null) { sig.RSI = r >= 55 && r < 70 ? 1 : r >= 70 ? 0.5 : r > 30 && r <= 45 ? -1 : r <= 30 ? -0.5 : 0; why.RSI = `RSI(14, 5m) ${r.toFixed(0)}`; }
      const w = b5.slice(-36), seg = []; for (let i = 0; i < w.length; i += 6) seg.push(w.slice(i, i + 6));
      const hs = seg.map((x) => Math.max(...x.map((c) => c.h))), ls = seg.map((x) => Math.min(...x.map((c) => c.l)));
      let up = 0, dn = 0; for (let i = 1; i < hs.length; i++) { up += (hs[i] > hs[i - 1]) + (ls[i] > ls[i - 1]); dn += (hs[i] < hs[i - 1]) + (ls[i] < ls[i - 1]); }
      sig["Price action"] = Math.round(clip((up - dn) / 4, -2, 2) * 10) / 10; why["Price action"] = `${up} higher swing points vs ${dn} lower (last 3 hours)`;
      let pv = 0, vv = 0; for (const c of tb) { const wt = isIdx || !c.v ? 1 : c.v; pv += (c.h + c.l + c.c) / 3 * wt; vv += wt; }
      const vwap = vv ? pv / vv : null;
      if (vwap) { const d = (last - vwap) / vwap * 100; sig.VWAP = Math.round(clip(d / 0.4, -2, 2) * 10) / 10; why.VWAP = `Last ${last.toFixed(2)} vs VWAP ${vwap.toFixed(2)} (${d >= 0 ? "+" : ""}${d.toFixed(2)}%)`; }
      const ks = Object.keys(sig), score = ks.length ? Math.round(ks.reduce((a, k) => a + sig[k], 0) / (2 * ks.length) * 100) / 10 : 0;
      let atr = null;
      if (daily.length > 15) { const a = daily.slice(-15); let t = 0; for (let i = 1; i < a.length; i++) t += Math.max(a[i].h - a[i].l, Math.abs(a[i].h - a[i - 1].c), Math.abs(a[i].l - a[i - 1].c)); atr = t / (a.length - 1); }
      const axes = {}; ks.forEach((k) => { axes[k] = Math.round(50 + 25 * sig[k]); });
      return { symbol: u, last, score, verdict: score >= 2.5 ? "Bullish" : score <= -2.5 ? "Bearish" : "Neutral", signals: sig, why, axes, atr, atr_pct: atr ? atr / last * 100 : null,
        expected: rc ? rc.band : atr ? [last - atr * 3, last + atr * 3] : null, expiry: rc ? rc.expiry : null, conviction: rc ? rc.conviction : null, vwap, profile: pr,
        levels: rc ? { support: rc.floor, resistance: rc.ceiling, max_pain: rc.max_pain } : null,
        candles: b5.slice(-150).map((c) => ({ time: c.t, open: c.o, high: c.h, low: c.l, close: c.c })), as_of: stamp("t") };
    });
  }

  function monthlyExpiry(series) {
    const now = istNow(), out = [];
    for (let i = 0; i < 6 && out.length < 3; i++) {
      const y = now.getUTCFullYear(), m = now.getUTCMonth() + i, last = new Date(Date.UTC(y, m + 1, 0));
      while (last.getUTCDay() !== 2) last.setUTCDate(last.getUTCDate() - 1);
      if (last.getTime() >= Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate())) out.push(last);
    }
    return out[Math.min(series, out.length - 1)];
  }
  function calm(series) {
    return cached("pulse:calm:" + series, ttl(600, 3600), async () => {
      const uni = await universe(), names = uni.rows.slice().sort((a, b) => (b.value || 0) - (a.value || 0)).slice(0, 40), day = dstr(Date.now());
      let store = LS.get("pulse.rv") || {};
      if (store.day !== day) store = { day, v: {} };
      for (const r of names) {
        if (store.v[r.symbol] != null) continue;
        try {
          const d = await candles(`NSE:${r.symbol}-EQ`, "D", 100), lr = [];
          for (let i = 1; i < d.length; i++) lr.push(Math.log(d[i].c / d[i - 1].c));
          const x = lr.slice(-60), mean = x.reduce((a, b) => a + b, 0) / (x.length || 1), sd = Math.sqrt(x.reduce((a, b) => a + (b - mean) ** 2, 0) / Math.max(1, x.length - 1));
          const r20 = lr.slice(-20), m20 = r20.reduce((a, b) => a + b, 0) / (r20.length || 1), sd20 = Math.sqrt(r20.reduce((a, b) => a + (b - m20) ** 2, 0) / Math.max(1, r20.length - 1));
          store.v[r.symbol] = [sd * Math.sqrt(252) * 100, sd20 * Math.sqrt(252) * 100];
        } catch (e) { /* skip this name */ }
      }
      LS.set("pulse.rv", store);
      const exp = monthlyExpiry(series), days = Math.max(1, Math.round((exp.getTime() - Date.UTC(istNow().getUTCFullYear(), istNow().getUTCMonth(), istNow().getUTCDate())) / 864e5));
      const rows = names.filter((r) => store.v[r.symbol]).map((r) => ({ symbol: r.symbol, avol: store.v[r.symbol][0], dvol: store.v[r.symbol][1] / Math.sqrt(252), lp: r.lp, chp: r.chp, sector: r.sector }));
      if (!rows.length) throw new HE(503, "Could not read daily candles from FYERS for the Calm Zone.");
      const sorted = rows.map((x) => x.avol).sort((a, b) => a - b);
      for (const x of rows) {
        const rank = sorted.filter((v) => v >= x.avol).length / sorted.length, pen = Math.min(20, Math.abs(x.chp || 0) * 4), spike = Math.max(0, x.dvol * Math.sqrt(252) - x.avol) / Math.max(x.avol, 1) * 30;
        x.calm = Math.round(Math.max(0, Math.min(100, 100 * rank - pen - spike)) * 10) / 10;
        x.sd = x.lp * x.avol / 100 * Math.sqrt(days / 365); x.put_k = x.lp - x.sd; x.call_k = x.lp + x.sd;
      }
      rows.sort((a, b) => b.calm - a.calm);
      return { file_date: day + " (FYERS, 60-day realised)", expiry: exp.toISOString().slice(0, 10), days, rows, top: rows.slice(0, 8),
        note: "Phone version: calm = how low each stock's 60-day realised volatility ranks among the 40 most traded F&O stocks (FYERS daily candles, read once a day on this device), minus today's move and any recent volatility spike. Strikes are ±1 standard deviation to expiry. Check results dates yourself." };
    });
  }

  async function symbols() {
    let stocks = []; try { stocks = (await master()).stocks; } catch (e) { stocks = Object.keys(SECTOR).sort(); }
    return { indices: Object.keys(INDEX_SPOT), stocks };
  }

  MM.pulse = async function (path, q) {
    const p = path.replace(/^\/api\/pulse/, "");
    if (p === "/overview") return overview();
    if (p === "/options") return options(q.get("symbol") || "NIFTY");
    if (p === "/oiflow") return oiflow(q.get("symbol") || "NIFTY");
    if (p === "/liquidity") return liquidity(q.get("symbol") || "NIFTY", q.get("res") || "5");
    if (p === "/strength") return strength();
    if (p === "/screeners/basis") return basis(q.get("series") || "near");
    if (p === "/screeners/oispurts") throw new HE(503, "OI spurts come from NSE's website, which phones can't read. Use the PC version for this screener.");
    if (p === "/screeners/rsi") return rsiScreen(Math.max(5, Math.min(60, +(q.get("top") || 30))));
    if (p === "/radar") return radar(q.get("symbol") || "RELIANCE");
    if (p === "/calm") return calm(+(q.get("series") || 0));
    if (p === "/symbols") return symbols();
    throw new HE(404, "Not available: " + path);
  };
})();
