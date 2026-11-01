/* MarketMiner Mobile engine.
 * The desktop MarketMiner runs a Python server on the PC. This phone/tablet build has no server: this file answers the
 * pages' /api/... requests inside the browser, talking straight to FYERS, Binance, Delta Exchange and alternative.me
 * (all of which accept browser requests). Logic is ported from the desktop backend:
 *   backend/brokers/fyers/client.py  -> FY (login, PIN renew, quotes, history, option chain)
 *   backend/markets/opt_lab.py       -> sweeps / decay / board
 *   backend/markets/crypto_dash.py   -> crypto overview, trend, derivatives, tradfi, fear & greed, options, assessment
 *   backend/markets/crypto_lab.py    -> crypto CPR, OI levels, confluence, lab
 * Your FYERS app ID, secret and tokens stay in this browser's storage on this device only.
 */
(function () {
  "use strict";
  window.MM_MOBILE = true;
  const IST_MS = 19800e3;
  const realFetch = window.fetch.bind(window);

  // ---------------------------------------------------------------- helpers
  const LS = {
    get(k) { try { const v = localStorage.getItem("mm." + k); return v ? JSON.parse(v) : null; } catch (e) { return null; } },
    set(k, v) { try { v == null ? localStorage.removeItem("mm." + k) : localStorage.setItem("mm." + k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
  };
  class HE extends Error { constructor(status, msg) { super(msg); this.status = status; } }
  const json = (o, status = 200) => new Response(JSON.stringify(o), { status, headers: { "content-type": "application/json" } });
  const num = (x, d = null) => { const v = Number(x); return x !== null && x !== undefined && x !== "" && isFinite(v) ? v : d; };
  const sum = (a) => a.reduce((x, y) => x + y, 0);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  async function getJ(url, opt) {
    let r;
    try { r = await realFetch(url, opt); } catch (e) { throw new HE(502, "Network error reaching " + new URL(url).host); }
    let j = null; try { j = await r.json(); } catch (e) { /* not json */ }
    if (!r.ok && !j) throw new HE(502, "HTTP " + r.status + " from " + new URL(url).host);
    return j;
  }
  const _cache = new Map();
  function cached(key, ttlMs, fn) {
    const h = _cache.get(key);
    if (h && Date.now() - h[0] < ttlMs) return h[1];
    const p = Promise.resolve().then(fn);
    _cache.set(key, [Date.now(), p]);
    p.catch(() => _cache.delete(key));
    return p;
  }
  const istNow = () => new Date(Date.now() + IST_MS);
  const marketOpen = () => { const n = istNow(), m = n.getUTCHours() * 60 + n.getUTCMinutes(), w = n.getUTCDay(); return w >= 1 && w <= 5 && m >= 555 && m <= 930; };

  // ---------------------------------------------------------------- FYERS (client.py port)
  const FY = {};
  const API = "https://api-t1.fyers.in";
  const REFRESH_DAYS = 15;
  FY.defaultRedirect = () => new URL("fyers.html", location.href).href.split("?")[0].split("#")[0];
  FY.app = () => ({ app_id: "", secret: "", redirect_uri: FY.defaultRedirect(), ...(LS.get("fyers.app") || {}) });
  FY.tokenExpiry = (issuedMs) => {
    const local = new Date(issuedMs + IST_MS);
    let cut = Date.UTC(local.getUTCFullYear(), local.getUTCMonth(), local.getUTCDate(), 6, 30) - IST_MS;
    if (issuedMs >= cut) cut += 86400e3;
    return cut;
  };
  FY.token = () => LS.get("fyers.token") || {};
  FY.connected = () => { const t = FY.token(); return !!t.access_token && Date.now() < t.expires_at && t.app_id === FY.app().app_id; };
  async function sha256(text) {
    const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
    return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
  }
  FY.loginUrl = () => {
    const a = FY.app();
    if (!a.app_id || !a.secret) throw new HE(409, "Enter the FYERS App ID and secret key on the Connections page first.");
    const state = [...crypto.getRandomValues(new Uint8Array(18))].map((b) => b.toString(16).padStart(2, "0")).join("");
    LS.set("fyers.state", { state, at: Date.now() });
    return `${API}/api/v3/generate-authcode?` + new URLSearchParams({ client_id: a.app_id, redirect_uri: a.redirect_uri, response_type: "code", state });
  };
  function saveToken(res, keepRefresh) {
    const issued = Date.now(), a = FY.app(), old = FY.token();
    const t = { access_token: res.access_token, issued_at: issued, expires_at: FY.tokenExpiry(issued), app_id: a.app_id };
    if (res.refresh_token) { t.refresh_token = res.refresh_token; t.refresh_expires_at = issued + REFRESH_DAYS * 86400e3 - 30 * 60e3; }
    else if (keepRefresh && old.refresh_token) { t.refresh_token = old.refresh_token; t.refresh_expires_at = old.refresh_expires_at; }
    LS.set("fyers.token", t);
    return t;
  }
  FY.exchange = async (code) => {
    const a = FY.app();
    const res = await getJ(`${API}/api/v3/validate-authcode`, { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ grant_type: "authorization_code", appIdHash: await sha256(`${a.app_id}:${a.secret}`), code }) });
    if (!res || !res.access_token) {
      let m = (res && (res.message || res.error)) || "FYERS token exchange failed";
      if (/hash/i.test(m) || [-371, -413].includes(res && res.code)) m += " — check the secret key.";
      throw new HE(502, m);
    }
    saveToken(res, false);
  };
  FY.refreshStatus = () => {
    const t = FY.token();
    if (!t.refresh_token || !t.refresh_expires_at || t.app_id !== FY.app().app_id) return { can_renew: false };
    if (Date.now() >= t.refresh_expires_at) return { can_renew: false, renew_expired: true };
    return { can_renew: true, renew_until: new Date(t.refresh_expires_at).toISOString(), renew_days_left: Math.round((t.refresh_expires_at - Date.now()) / 8640e3) / 10 };
  };
  FY.renew = async (pin) => {
    if (!/^[0-9]{4,6}$/.test(pin || "")) throw new HE(422, "Enter your FYERS PIN (digits only)");
    if (!FY.refreshStatus().can_renew) throw new HE(409, "No valid refresh token: use Connect FYERS (full login) once; after that the PIN renews it for 15 days.");
    const a = FY.app(), t = FY.token();
    const res = await getJ(`${API}/api/v3/validate-refresh-token`, { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ grant_type: "refresh_token", appIdHash: await sha256(`${a.app_id}:${a.secret}`), refresh_token: t.refresh_token, pin }) });
    if (!res || !res.access_token) {
      let m = String((res && (res.message || res.error)) || "FYERS renew failed");
      if (/pin/i.test(m)) m = "FYERS rejected the PIN. Check it and try again (several wrong PINs can lock the FYERS login).";
      else if (/refresh|expired/i.test(m)) m += " — use Connect FYERS (full login) to get a fresh 15-day refresh token.";
      throw new HE(502, m);
    }
    const nt = saveToken(res, true);
    return { connected: true, message: "FYERS renewed with PIN until " + fmtIst(nt.expires_at) };
  };
  const fmtIst = (ms) => new Date(ms).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata" }) + " IST";
  FY.status = () => {
    const a = FY.app(), t = FY.token(), configured = !!(a.app_id && a.secret);
    let s;
    if (!t.access_token) s = { connected: false, message: "Not connected" };
    else if (Date.now() >= t.expires_at) s = { connected: false, token_present: true, locally_expired: true, message: "Login expired (FYERS tokens end at 06:30 IST). Renew with your PIN or Connect FYERS again." };
    else s = { connected: true, token_present: true, expires_at: new Date(t.expires_at).toISOString(), message: "Connected until " + fmtIst(t.expires_at) };
    return { provider: "FYERS", configured, ...s, ...FY.refreshStatus(), rate: { last_minute: FY.calls.filter((x) => Date.now() - x < 60e3).length, per_minute_cap: 40 } };
  };
  FY.calls = [];
  async function fyAcquire() {
    for (;;) {
      const now = Date.now();
      FY.calls = FY.calls.filter((x) => now - x < 60e3);
      if (FY.calls.filter((x) => now - x < 1000).length < 3 && FY.calls.length < 40) { FY.calls.push(now); return; }
      await sleep(250);
    }
  }
  FY.get = async (path, params) => {
    if (!FY.connected()) throw new HE(401, "FYERS is not connected. Open Connections and log in (or renew with your PIN).");
    await fyAcquire();
    const a = FY.app(), t = FY.token();
    const res = await getJ(`${API}/data${path}?` + new URLSearchParams(params), { headers: { Authorization: `${a.app_id}:${t.access_token}` } });
    if (res && res.s !== "ok" && res.s !== "no_data") {
      if ([-8, -15, -16, -17].includes(res.code)) { LS.set("fyers.token", { ...t, access_token: "", expires_at: 0 }); throw new HE(401, "FYERS rejected the login token. Renew with your PIN or Connect FYERS."); }
      throw new HE(502, "FYERS: " + (res.message || "request failed") + (res.code !== undefined ? ` (code ${res.code})` : ""));
    }
    return res;
  };
  FY.quotes = (symbols) => cached("q:" + symbols, 10e3, () => FY.get("/quotes", { symbols }));
  FY.chain = (symbol, strikecount, timestamp = "", greeks = 1) =>
    cached(`oc:${symbol}:${strikecount}:${timestamp}:${greeks}`, 20e3, () => FY.get("/options-chain-v3", { symbol, strikecount, timestamp, greeks }));
  FY.history = (p) => cached("h:" + JSON.stringify(p), marketOpen() ? 60e3 : 600e3, () => FY.get("/history", p));

  // Login return: FYERS sends the browser to fyers.html?auth_code=...&state=...
  FY.completeFromUrl = async () => {
    const q = new URLSearchParams(location.search);
    const code = q.get("auth_code"), state = q.get("state");
    if (!code && !q.has("s")) return null;
    history.replaceState(null, "", location.pathname);
    const pending = LS.get("fyers.state");
    if (!pending && code) return { handoff: `${state || ""}.${code}` };  // iPhone/iPad: login came back to Safari, not the Home Screen app
    LS.set("fyers.state", null);
    if (!pending || pending.state !== state || Date.now() - pending.at > 600e3) return { ok: false, reason: "This FYERS login link is unknown or older than 10 minutes. Start the login again." };
    if (!code) return { ok: false, reason: q.get("message") || "FYERS did not return an auth code" };
    try { await FY.exchange(code); return { ok: true }; } catch (e) { return { ok: false, reason: e.message }; }
  };
  FY.finishWithCode = async (text) => {
    const x = (text || "").trim();
    let state = null, code = null;
    if (/auth_code=/.test(x)) { const q = new URLSearchParams(x.slice(x.indexOf("?") + 1)); code = q.get("auth_code"); state = q.get("state"); }
    else { const k = x.indexOf("."); if (k > 0) { state = x.slice(0, k); code = x.slice(k + 1); } }
    if (!code) throw new HE(422, "That is not a FYERS login code. Copy it again from the Safari page.");
    const pending = LS.get("fyers.state");
    if (!pending || pending.state !== state || Date.now() - pending.at > 600e3) throw new HE(409, "This code does not match the last Connect FYERS tap in this app, or is more than 10 minutes old. Tap Connect FYERS again.");
    LS.set("fyers.state", null);
    await FY.exchange(code);
  };

  // ---------------------------------------------------------------- NSE replacements built from FYERS
  const NSE_INDEX = { "NIFTY 50": "NIFTY50", "NIFTY BANK": "NIFTYBANK", "INDIA VIX": "INDIAVIX", "NIFTY AUTO": "NIFTYAUTO", "NIFTY FMCG": "NIFTYFMCG",
    "NIFTY IT": "NIFTYIT", "NIFTY METAL": "NIFTYMETAL", "NIFTY PHARMA": "NIFTYPHARMA", "NIFTY REALTY": "NIFTYREALTY", "NIFTY HEALTHCARE": "NIFTYHEALTHCARE",
    "NIFTY OIL AND GAS": "NIFTYOILANDGAS", "NIFTY CHEMICALS": "NIFTYCHEMICALS", "NIFTY ENERGY": "NIFTYENERGY", "NIFTY INFRA": "NIFTYINFRA",
    "NIFTY SERV SECTOR": "NIFTYSERVSECTOR", "NIFTY IND DIGITAL": "NIFTYINDDIGITAL", "NIFTY INDIA MFG": "NIFTYINDIAMFG", "NIFTY IND DEFENCE": "NIFTYINDDEFENCE",
    "NIFTY IND TOURISM": "NIFTYINDTOURISM", "NIFTY CAPITAL MKT": "NIFTYCAPITALMKT", "NIFTY EV": "NIFTYEV", "NIFTY MOBILITY": "NIFTYMOBILITY",
    "NIFTY HOUSING": "NIFTYHOUSING", "NIFTY SME EMERGE": "NIFTYSMEEMERGE", "NIFTY PSU BANK": "NIFTYPSUBANK", "NIFTY PVT BANK": "NIFTYPVTBANK",
    "NIFTY MEDIA": "NIFTYMEDIA", "NIFTY FIN SERVICE": "FINNIFTY", "NIFTY MIDCAP 100": "NIFTYMIDCAP100", "NIFTY SMLCAP 100": "NIFTYSMLCAP100" };
  async function allIndices() {
    const names = Object.keys(NSE_INDEX), syms = names.map((n) => `NSE:${NSE_INDEX[n]}-INDEX`);
    const back = Object.fromEntries(names.map((n, i) => [syms[i], n]));
    const rows = [];
    for (let i = 0; i < syms.length; i += 50) rows.push(...(((await FY.quotes(syms.slice(i, i + 50).join(","))) || {}).d || []));
    const data = rows.filter((r) => r.s === "ok" && r.v && r.v.lp != null && back[String(r.n).toUpperCase()]).map((r) => {
      const v = r.v, nm = back[String(r.n).toUpperCase()];
      return { index: nm, indexSymbol: nm, last: v.lp, change: v.ch, percentChange: v.chp, previousClose: v.prev_close_price, open: v.open_price, high: v.high_price, low: v.low_price };
    });
    if (!data.length) throw new HE(502, "FYERS returned no index quotes");
    return { data, timestamp: new Date().toLocaleString("en-GB", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).replace(",", ""), source: "FYERS" };
  }
  const MCX_WANT = { GOLD: "GOLD1G", SILVER: "SILVER", ALUMINIUM: "ALUMINIUM", COPPER: "COPPER", ZINC: "ZINC", CRUDEOIL: "CRUDEOIL", NATURALGAS: "NATURALGAS" };
  async function mcxFutures() {
    const day = new Date(Date.now() + IST_MS).toISOString().slice(0, 10);
    const saved = LS.get("mcx.futs");
    if (saved && saved.day === day) return saved.map;
    const txt = await (await realFetch("https://public.fyers.in/sym_details/MCX_COM.csv")).text();
    const now = Date.now() / 1000, best = {};
    for (const line of txt.split("\n")) {
      const r = line.split(",");
      if (r.length < 17 || !(r[9] || "").endsWith("FUT")) continue;
      const u = r[13], exp = Number(r[8]);
      if (!MCX_WANT[u] || !(exp > now)) continue;
      if (!best[u] || exp < best[u].exp) best[u] = { sym: r[9], exp };
    }
    const map = Object.fromEntries(Object.entries(best).map(([u, x]) => [u, x.sym]));
    LS.set("mcx.futs", { day, map });
    return map;
  }
  async function commodities() {
    const map = await mcxFutures(), syms = Object.values(map);
    const q = ((await FY.quotes(syms.join(","))) || {}).d || [];
    const by = Object.fromEntries(Object.entries(map).map(([u, s]) => [s, u]));
    const rates = q.filter((r) => r.s === "ok" && r.v).map((r) => ({ spm_symbol: MCX_WANT[by[r.n]] || by[r.n], last_spot_price: r.v.lp, perChange: r.v.chp, dt: "" }));
    return { data: { commoditySpotRates: rates, timeStamp: "" } };
  }

  // ---------------------------------------------------------------- opt_lab.py port (sweeps, decay)
  const INDEX = { NIFTY: "NSE:NIFTY50-INDEX", BANKNIFTY: "NSE:NIFTYBANK-INDEX", FINNIFTY: "NSE:FINNIFTY-INDEX", MIDCPNIFTY: "NSE:MIDCPNIFTY-INDEX",
    NIFTYNXT50: "NSE:NIFTYNXT50-INDEX", SENSEX: "BSE:SENSEX-INDEX", BANKEX: "BSE:BANKEX-INDEX" };
  const tzDate = (t, off) => new Date(t * 1000 + off);
  const dayKey = (t, off) => tzDate(t, off).toISOString().slice(0, 10);
  function isoWeek(d) { const x = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate())); const n = x.getUTCDay() || 7; x.setUTCDate(x.getUTCDate() + 4 - n); const y = new Date(Date.UTC(x.getUTCFullYear(), 0, 1)); return x.getUTCFullYear() + "-" + Math.ceil(((x - y) / 864e5 + 1) / 7); }
  const tfKeys = (off) => ({ M: (b) => tzDate(b.t, off).toISOString().slice(0, 7), W: (b) => isoWeek(tzDate(b.t, off)), D: (b) => dayKey(b.t, off) });
  const KEYS_IST = tfKeys(IST_MS), KEYS_UTC = tfKeys(0);
  function group(bars, key) {
    const out = [];
    for (const b of bars) {
      const k = key(b), g = out[out.length - 1];
      if (g && g.k === k) { g.h = Math.max(g.h, b.h); g.l = Math.min(g.l, b.l); g.c = b.c; }
      else out.push({ k, t: b.t, o: b.o, h: b.h, l: b.l, c: b.c });
    }
    return out;
  }
  const hourKeyIst = (b) => { const d = tzDate(b.t, IST_MS); const m = d.getUTCHours() * 60 + d.getUTCMinutes() - 555; return d.toISOString().slice(0, 10) + ":" + Math.floor(m / 60); };
  function periodSweep(bars, tf, keys) {
    const g = group(bars, keys[tf]);
    if (g.length < 2) return { state: "new", text: "Contract too new for a previous " + { M: "month", W: "week", D: "day" }[tf] };
    const prev = g[g.length - 2], cur = g[g.length - 1], ev = [];
    if (cur.h > prev.h) ev.push(cur.c > prev.h ? "bsl_taken" : "bsl_swept");
    if (cur.l < prev.l) ev.push(cur.c < prev.l ? "ssl_taken" : "ssl_swept");
    const state = ev.find((e) => e.endsWith("swept")) || ev[0] || "inside";
    return { state, events: ev, bsl: prev.h, ssl: prev.l, hi: cur.h, lo: cur.l, close: cur.c, at: cur.t };
  }
  function pivot(bars, j, kind, span) {
    const w = bars.slice(j - span, j + span + 1), r = bars.slice(j + 1, j + span + 1);
    if (kind === "h") return bars[j].h === Math.max(...w.map((b) => b.h)) && r.every((b) => b.h < bars[j].h);
    return bars[j].l === Math.min(...w.map((b) => b.l)) && r.every((b) => b.l > bars[j].l);
  }
  function swingSweeps(bars, span, off) {
    if (bars.length < 2 * span + 2) return { state: "new", text: "Not enough candles yet" };
    const gaps = bars.slice(1).map((b, i) => b.t - bars[i].t).sort((a, b) => a - b), step = gaps[Math.floor(gaps.length / 2)] || 0;
    let pools = []; const sweeps = [];
    bars.forEach((b, i) => {
      const gap = i > 0 && step > 0 && b.t - bars[i - 1].t > 2 * step, j = i - span;
      if (j >= span) for (const kind of ["h", "l"]) if (pivot(bars, j, kind, span)) pools.push([kind, bars[j][kind], j, b.t]);
      const keep = [];
      for (const p of pools) {
        const [kind, price, idx, conf] = p;
        if (i - idx > 300) continue;
        if (b.t <= conf || gap) { keep.push(p); continue; }
        if (kind === "h") { if (b.c > price) continue; if (b.h > price) { sweeps.push({ side: "bsl", level: price, at: b.t, extreme: b.h }); continue; } }
        else { if (b.c < price) continue; if (b.l < price) { sweeps.push({ side: "ssl", level: price, at: b.t, extreme: b.l }); continue; } }
        keep.push(p);
      }
      pools = keep;
    });
    const day = dayKey(bars[bars.length - 1].t, off), today = sweeps.filter((s) => dayKey(s.at, off) === day), last = bars[bars.length - 1].c;
    const above = pools.filter((p) => p[0] === "h" && p[1] > last).map((p) => p[1]).sort((a, b) => a - b);
    const below = pools.filter((p) => p[0] === "l" && p[1] < last).map((p) => p[1]).sort((a, b) => b - a);
    const latest = today[today.length - 1] || null;
    return { state: latest ? latest.side + "_swept" : "none", latest, count_bsl: today.filter((s) => s.side === "bsl").length,
      count_ssl: today.filter((s) => s.side === "ssl").length, bsl: above[0] ?? null, ssl: below[0] ?? null, session: day };
  }
  function trendOf(bars, n = 8) {
    const w = bars.slice(-n); if (w.length < 4) return "—";
    let lh = 0, ll = 0, hh = 0, hl = 0;
    for (let i = 1; i < w.length; i++) { if (w[i].h < w[i - 1].h) lh++; if (w[i].l < w[i - 1].l) ll++; if (w[i].h > w[i - 1].h) hh++; if (w[i].l > w[i - 1].l) hl++; }
    const k = w.length - 1;
    return lh + ll >= 1.3 * k ? "falling" : hh + hl >= 1.3 * k ? "rising" : "sideways";
  }
  function decay(side, strike, ltp, g, spot, bars, idx, dte, off) {
    const intrinsic = side === "CE" ? Math.max(0, spot - strike) : Math.max(0, strike - spot), tv = Math.max(0, ltp - intrinsic), theta = Math.abs(num(g.theta, 0));
    const out = { ltp, intrinsic, tv, tv_pct: ltp ? (100 * tv) / ltp : 0, theta, theta_pct: ltp ? (100 * theta) / ltp : 0, tv_days: theta ? tv / theta : null, iv: g.iv };
    const day = bars.length ? dayKey(bars[bars.length - 1].t, off) : null;
    const today = day ? bars.filter((b) => dayKey(b.t, off) === day) : [], itoday = day ? idx.filter((b) => dayKey(b.t, off) === day) : [];
    let lost = null;
    if (today.length && itoday.length) {
      const s0 = itoday[0].o, i0 = side === "CE" ? Math.max(0, s0 - strike) : Math.max(0, strike - s0), tv0 = Math.max(0, today[0].o - i0);
      lost = tv0 - tv;
      Object.assign(out, { tv_open: tv0, tv_lost: lost, tv_lost_pct: tv0 ? (100 * lost) / tv0 : null, day_open: today[0].o,
        day_hi: Math.max(...today.map((b) => b.h)), day_lo: Math.min(...today.map((b) => b.l)), session: day });
    }
    const tr = trendOf(bars); out.trend = tr;
    let st, txt;
    if (ltp <= 0.5 || (intrinsic === 0 && theta && ltp < theta)) [st, txt] = ["done", "Decay done: premium is close to zero"];
    else if (intrinsic > 0 && tv < Math.max(theta * 0.5, 0.02 * ltp)) [st, txt] = ["done", "Decay done: trading at intrinsic value, moves 1:1 with the index now"];
    else if (lost !== null && lost > 0 && tr === "falling") [st, txt] = [out.theta_pct >= 8 || dte <= 1 ? "fast" : "decaying", "Time value is bleeding and the premium is making lower highs/lows"];
    else if (lost !== null && lost > 0) [st, txt] = ["decaying", "Time value is shrinking; the index move is holding the premium up"];
    else if (tr === "rising" || (lost !== null && lost <= 0)) [st, txt] = ["beating", "Price action is beating decay (time value held or grew, often IV rising)"];
    else [st, txt] = ["holding", "Little change in time value"];
    out.status = st; out.status_text = txt;
    return out;
  }
  async function fyBars(symbol) {
    const to = new Date(Date.now() + IST_MS), fr = new Date(to - 62 * 864e5), f = (d) => d.toISOString().slice(0, 10);
    const p = await FY.history({ symbol, resolution: "15", date_format: "1", range_from: f(fr), range_to: f(to), cont_flag: "1" });
    return ((p && p.candles) || []).filter((c) => c.length >= 5).map((c) => ({ t: +c[0], o: +c[1], h: +c[2], l: +c[3], c: +c[4] }));
  }
  const sweepsIst = (b15) => b15.length ? { M: periodSweep(b15, "M", KEYS_IST), W: periodSweep(b15, "W", KEYS_IST), D: periodSweep(b15, "D", KEYS_IST),
    "1H": swingSweeps(group(b15, hourKeyIst), 2, IST_MS), "15m": swingSweeps(b15, 3, IST_MS) } : {};
  async function optBoard(index, expiry) {
    index = (index || "NIFTY").toUpperCase();
    const usym = INDEX[index]; if (!usym) throw new HE(400, "Unknown index");
    return cached(`board:${index}:${expiry}`, marketOpen() ? 240e3 : 1800e3, async () => {
      let d = ((await FY.chain(usym, 3, "", 1)) || {}).data || {};
      const exps = [...(d.expiryData || [])].sort((a, b) => +a.expiry - +b.expiry);
      if (!exps.length) throw new HE(502, "FYERS returned no expiries for " + index);
      const pick = expiry === "wk" ? exps[0] : exps.find((e) => e.expiry_flag === "M") || exps[0];
      if (pick !== exps[0]) d = ((await FY.chain(usym, 3, String(pick.expiry), 1)) || {}).data || {};
      const chain = d.optionsChain || [];
      const spot = num((chain.find((r) => num(r.strike_price, 0) <= 0) || {}).ltp, 0);
      const opts = chain.filter((r) => (r.option_type === "CE" || r.option_type === "PE") && r.symbol);
      const syms = [usym, ...opts.map((r) => r.symbol)];
      const hist = await Promise.allSettled(syms.map(fyBars));
      const bars = Object.fromEntries(syms.map((s, i) => [s, hist[i].status === "fulfilled" ? hist[i].value : []]));
      const expAt = new Date((+pick.expiry) * 1000); const expIst = new Date(expAt.getTime() + IST_MS);
      const expClose = Date.UTC(expIst.getUTCFullYear(), expIst.getUTCMonth(), expIst.getUTCDate(), 15, 30) - IST_MS;
      const dte = Math.max(0, (expClose - Date.now()) / 864e5);
      const rows = opts.sort((a, b) => a.strike_price - b.strike_price || (a.option_type < b.option_type ? -1 : 1)).map((r) => {
        const b = bars[r.symbol] || [], k = +r.strike_price;
        return { symbol: r.symbol, strike: k, side: r.option_type, ltp: num(r.ltp, 0), chg_pct: num(r.ltpchp, 0), candles: b.length, sweeps: sweepsIst(b),
          decay: decay(r.option_type, k, num(r.ltp, 0), r.greeks || {}, spot, b, bars[usym], dte, IST_MS) };
      });
      const atm = opts.length ? opts.map((r) => +r.strike_price).reduce((a, k) => (Math.abs(k - spot) < Math.abs(a - spot) ? k : a)) : null;
      return { index, symbol: usym, spot, atm, expiry: pick.date, dte, index_sweeps: sweepsIst(bars[usym]), rows, built_at: Math.floor(Date.now() / 1000),
        market_open: marketOpen(), missing: syms.filter((s) => !(bars[s] || []).length) };
    });
  }

  // ---------------------------------------------------------------- crypto (crypto_dash.py + crypto_lab.py ports)
  const BN = "https://data-api.binance.vision", BF = "https://fapi.binance.com", DL = "https://api.india.delta.exchange";
  const COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "TONUSDT", "TRXUSDT", "LTCUSDT"];
  const DERIV = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"];
  const TRADFI = { XAUUSDT: "Gold", XAGUSDT: "Silver", CLUSDT: "WTI crude", BZUSDT: "Brent crude", NATGASUSDT: "Natural gas" };
  const STABLE = new Set(["USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USD1", "EUR", "AEUR", "XUSD", "USDE", "PYUSD", "RLUSD"]);
  const all24 = () => cached("spot24", 30e3, () => getJ(`${BN}/api/v3/ticker/24hr`));
  const klines = (sym, interval, limit) => cached(`kl:${sym}:${interval}:${limit}`, interval === "1d" ? 900e3 : 240e3,
    async () => ((await getJ(`${BN}/api/v3/klines?` + new URLSearchParams({ symbol: sym, interval, limit }))) || []).map((r) => ({ t: Math.floor(r[0] / 1000), o: +r[1], h: +r[2], l: +r[3], c: +r[4] })));
  const nowIso = () => new Date(Date.now() + IST_MS).toISOString().slice(0, 19) + "+05:30";

  async function overview() {
    const rows = await all24(), by = Object.fromEntries(rows.map((r) => [r.symbol, r]));
    const coins = COINS.filter((s) => by[s]).map((s) => { const r = by[s]; return { symbol: s, coin: s.slice(0, -4), price: num(r.lastPrice), chg: num(r.priceChangePercent), high: num(r.highPrice), low: num(r.lowPrice), vol_usd: num(r.quoteVolume) }; });
    const liquid = [];
    for (const [s, r] of Object.entries(by)) {
      const base = s.slice(0, -4);
      if (!s.endsWith("USDT") || STABLE.has(base) || /(UP|DOWN|BULL|BEAR)$/.test(base)) continue;
      const qv = num(r.quoteVolume, 0);
      if (qv >= 5e6 && num(r.lastPrice, 0) > 0) liquid.push({ symbol: s, coin: base, price: num(r.lastPrice), chg: num(r.priceChangePercent, 0), vol_usd: qv });
    }
    const adv = liquid.filter((r) => r.chg > 0).length, dec = liquid.filter((r) => r.chg < 0).length, srt = [...liquid].sort((a, b) => a.chg - b.chg);
    return { as_of: nowIso(), coins, breadth: { advances: adv, declines: dec, unchanged: liquid.length - adv - dec, universe: liquid.length, rule: "USDT pairs with > $5M 24h volume, stablecoins excluded" },
      gainers: [...srt].reverse().slice(0, 8), losers: srt.slice(0, 8), volume_usd: sum(liquid.map((r) => r.vol_usd)) };
  }
  function rsi(c, n = 14) {
    if (c.length < n + 1) return null;
    const g = [], l = []; for (let i = 1; i < c.length; i++) { g.push(Math.max(0, c[i] - c[i - 1])); l.push(Math.max(0, c[i - 1] - c[i])); }
    let ag = sum(g.slice(0, n)) / n, al = sum(l.slice(0, n)) / n;
    for (let i = n; i < g.length; i++) { ag = (ag * (n - 1) + g[i]) / n; al = (al * (n - 1) + l[i]) / n; }
    return al ? 100 - 100 / (1 + ag / al) : 100;
  }
  async function trend1(sym) {
    const rows = await klines(sym, "1d", 120), c = rows.slice(0, -1).map((r) => r.c), last = rows.length ? rows[rows.length - 1].c : null;
    if (c.length < 55) return null;
    const ma = (n) => sum(c.slice(-n)) / n, rets = []; for (let i = c.length - 30; i < c.length; i++) rets.push(Math.log(c[i] / c[i - 1]));
    const m = sum(rets) / rets.length, sd = Math.sqrt(sum(rets.map((v) => (v - m) ** 2)) / (rets.length - 1));
    return { symbol: sym, price: last, ma20: ma(20), ma50: ma(50), rsi: rsi(c), hi20: Math.max(...c.slice(-20)), lo20: Math.min(...c.slice(-20)),
      chg7d: (c[c.length - 1] / c[c.length - 8] - 1) * 100, chg30d: (c[c.length - 1] / c[c.length - 31] - 1) * 100, realized_vol30: sd * Math.sqrt(365) * 100 };
  }
  async function trend() { const out = await Promise.allSettled(["BTCUSDT", "ETHUSDT", "SOLUSDT"].map(trend1)); return { items: out.filter((o) => o.status === "fulfilled" && o.value).map((o) => o.value) }; }
  function readOi(p, o) {
    if (p == null || o == null) return null;
    if (Math.abs(o) < 1) return "Open interest flat: no strong new positioning";
    if (p >= 0 && o > 0) return "Price up + OI up: new longs (trend confirmation)";
    if (p < 0 && o > 0) return "Price down + OI up: new shorts (bearish pressure)";
    if (p >= 0 && o < 0) return "Price up + OI down: short covering (weaker rally)";
    return "Price down + OI down: long liquidation";
  }
  const deriv1 = (sym) => cached("deriv:" + sym, 60e3, async () => {
    const [prem, hist, ls] = await Promise.allSettled([getJ(`${BF}/fapi/v1/premiumIndex?symbol=${sym}`), getJ(`${BF}/futures/data/openInterestHist?symbol=${sym}&period=1h&limit=25`), getJ(`${BF}/futures/data/globalLongShortAccountRatio?symbol=${sym}&period=1h&limit=1`)]);
    const out = { symbol: sym, coin: sym.slice(0, -4) };
    if (prem.status === "fulfilled" && prem.value) Object.assign(out, { mark: num(prem.value.markPrice), index: num(prem.value.indexPrice), funding_pct: num(prem.value.lastFundingRate, 0) * 100, next_funding: prem.value.nextFundingTime });
    if (hist.status === "fulfilled" && Array.isArray(hist.value) && hist.value.length) { const a = num(hist.value[0].sumOpenInterestValue), b = num(hist.value[hist.value.length - 1].sumOpenInterestValue); Object.assign(out, { oi_usd: b, oi_chg24_pct: a && b ? (b / a - 1) * 100 : null }); }
    if (ls.status === "fulfilled" && Array.isArray(ls.value) && ls.value.length) { const x = ls.value[ls.value.length - 1]; Object.assign(out, { long_short: num(x.longShortRatio), long_pct: num(x.longAccount, 0) * 100 }); }
    return out;
  });
  async function derivs() {
    const rows = await all24(), by = Object.fromEntries(rows.map((r) => [r.symbol, r]));
    const items = (await Promise.allSettled(DERIV.map(deriv1))).filter((x) => x.status === "fulfilled").map((x) => x.value);
    items.forEach((it) => { it.chg24 = num((by[it.symbol] || {}).priceChangePercent); it.read = readOi(it.chg24, it.oi_chg24_pct); });
    return { items };
  }
  const tradfi = () => cached("tradfi", 60e3, async () => ({ items: (await Promise.allSettled(Object.keys(TRADFI).map(async (s) => { const r = await getJ(`${BF}/fapi/v1/ticker/24hr?symbol=${s}`); return { symbol: s, name: TRADFI[s], price: num(r.lastPrice), chg: num(r.priceChangePercent), vol_usd: num(r.quoteVolume) }; }))).filter((x) => x.status === "fulfilled").map((x) => x.value) }));
  const fng = () => cached("fng", 900e3, async () => {
    const d = ((await getJ("https://api.alternative.me/fng/?limit=8")) || {}).data || [];
    if (!d.length) throw new HE(502, "Fear & Greed index unavailable");
    return { now: +d[0].value, label: d[0].value_classification, yesterday: d[1] ? +d[1].value : null, week_ago: d[7] ? +d[7].value : null, source: "alternative.me" };
  });
  const optTickers = (coin) => cached("opt:" + coin, 60e3, () => getJ(`${DL}/v2/tickers?contract_types=call_options,put_options&underlying_asset_symbols=${coin}`));
  const expOf = (sym) => { const d = String(sym).split("-").pop(); return d && d.length === 6 ? `20${d.slice(4, 6)}-${d.slice(2, 4)}-${d.slice(0, 2)}` : null; };
  async function options(coin = "BTC", expiry = null, width = 3) {
    coin = coin.toUpperCase(); width = Math.max(1, Math.min(+width || 3, 25));
    if (!["BTC", "ETH", "XAUT"].includes(coin)) throw new HE(400, "coin must be BTC, ETH or XAUT");
    const rows = ((await optTickers(coin)) || {}).result;
    if (!rows || !rows.length) throw new HE(502, "Delta Exchange returned no option tickers");
    const today = new Date().toISOString().slice(0, 10), byExp = {}; let spot = null;
    for (const r of rows) { const e = expOf(r.symbol); if (!e || e < today) continue; (byExp[e] = byExp[e] || []).push(r); spot = spot || num(r.spot_price); }
    const exps = Object.keys(byExp).sort(), oiBy = Object.fromEntries(exps.map((e) => [e, sum(byExp[e].map((r) => num(r.oi_value_usd, 0)))]));
    const def = exps.find((e) => e > today && oiBy[e] > 0) || exps[0];
    const chosen = expiry && byExp[expiry] ? expiry : def;
    const strikes = {};
    for (const r of byExp[chosen] || []) {
      const k = num(r.strike_price), side = r.contract_type === "call_options" ? "ce" : "pe", g = r.greeks || {};
      (strikes[k] = strikes[k] || { strike: k })[side] = { oi: num(r.oi, 0), oi_usd: num(r.oi_value_usd, 0), oi_chg6h_usd: num(r.oi_change_usd_6h, 0), mark: num(r.mark_price),
        iv: (num(r.mark_vol, 0) || 0) * 100, delta: num(g.delta), mark_chg24h: num(r.mark_change_24h), symbol: r.symbol, theta: num(g.theta), gamma: num(g.gamma) };
    }
    const ks = Object.keys(strikes).map(Number).sort((a, b) => a - b);
    if (!ks.length) throw new HE(404, `No ${coin} options for ${chosen}`);
    const atm = ks.reduce((a, k) => (Math.abs(k - (spot || k)) < Math.abs(a - (spot || a)) ? k : a)), i = ks.indexOf(atm);
    const view = ks.slice(Math.max(0, i - width), i + width + 1).map((k) => strikes[k]);
    const leg = (k, s) => (strikes[k][s] || {});
    const totCe = sum(ks.map((k) => leg(k, "ce").oi || 0)), totPe = sum(ks.map((k) => leg(k, "pe").oi || 0));
    const pain = ks.reduce((best, x) => { const loss = sum(ks.map((k) => (leg(k, "ce").oi || 0) * Math.max(0, x - k) + (leg(k, "pe").oi || 0) * Math.max(0, k - x))); return loss < best[1] ? [x, loss] : best; }, [ks[0], Infinity])[0];
    const ivs = [leg(atm, "ce").iv, leg(atm, "pe").iv].filter((v) => v), iv = ivs.length ? sum(ivs) / ivs.length : null;
    const days = Math.max(1 / 24, (Date.parse(chosen + "T12:00:00Z") - Date.now()) / 864e5);
    return { coin, spot, expiries: exps.map((e) => ({ date: e, oi_usd: Math.round(oiBy[e]) })), expiry: chosen, atm, strikes: view, pcr: totCe ? totPe / totCe : null,
      oi_ce: totCe, oi_pe: totPe, max_pain: pain, atm_iv: iv, days, expected_move: spot && iv ? (spot * iv) / 100 * Math.sqrt(days / 365) : null,
      expiry_oi_usd: oiBy[chosen], source: "Delta Exchange India (oi in coins, mark IV)" };
  }
  async function assessment() {
    const safe = (p) => p.catch(() => null);
    const [ov, tr, dv, fg, ob] = await Promise.all([safe(overview()), safe(trend()), safe(derivs()), safe(fng()), safe(options("BTC"))]);
    let score = 0; const why = [], strat = [], risks = [], used = [];
    const items = (tr || {}).items || [], btc = items.find((t) => t.symbol === "BTCUSDT"), eth = items.find((t) => t.symbol === "ETHUSDT");
    const fmt = (x) => (x && x > 100 ? x.toLocaleString("en-US", { maximumFractionDigits: 0 }) : x ? x.toFixed(2) : "—");
    const sg = (v, d = 1) => (v >= 0 ? "+" : "") + v.toFixed(d);
    if (btc) {
      used.push("trend"); const p = btc.price;
      score += (p > btc.ma20 ? 15 : -15) + (p > btc.ma50 ? 15 : -15) + (btc.ma20 > btc.ma50 ? 10 : -10);
      why.push(`BTC ${fmt(p)} is ${p > btc.ma20 ? "above" : "below"} its 20-day average (${fmt(btc.ma20)}) and ${p > btc.ma50 ? "above" : "below"} the 50-day (${fmt(btc.ma50)}); 7-day change ${sg(btc.chg7d)}%.`);
      if (btc.rsi > 70) risks.push(`BTC daily RSI ${btc.rsi.toFixed(0)}: overbought, pullback risk is higher.`);
      else if (btc.rsi && btc.rsi < 30) risks.push(`BTC daily RSI ${btc.rsi.toFixed(0)}: oversold; sharp relief bounces are common, shorts get squeezed.`);
    }
    if (ov) { used.push("breadth"); const b = ov.breadth, tot = b.advances + b.declines || 1, share = b.advances / tot; score += (share - 0.5) * 40; why.push(`Breadth: ${b.advances} of ${tot} liquid coins up in 24h (${(share * 100).toFixed(0)}%).`); }
    if (dv && dv.items && dv.items.length) {
      used.push("derivatives"); const bd = dv.items.find((d) => d.coin === "BTC") || dv.items[0], fr = bd.funding_pct;
      if (fr != null) { if (fr > 0.03) { score -= 5; risks.push(`BTC funding ${fr.toFixed(3)}%/8h is high: longs are crowded and pay to hold; long-squeeze risk.`); } else if (fr < -0.01) { score += 5; risks.push(`BTC funding ${fr.toFixed(3)}%/8h is negative: shorts crowded; short-squeeze risk on good news.`); } }
      if (bd.read) { why.push(`BTC derivatives: ${bd.read} (OI 24h ${sg(bd.oi_chg24_pct || 0)}%, funding ${(fr || 0).toFixed(3)}%).`); if (bd.read.includes("new longs")) score += 10; else if (bd.read.includes("new shorts") || bd.read.includes("liquidation")) score -= 10; }
      const ls = bd.long_short; if (ls > 2) risks.push(`Long/short account ratio ${ls.toFixed(2)}: retail heavily long; contrarian downside risk.`); else if (ls && ls < 0.8) risks.push(`Long/short account ratio ${ls.toFixed(2)}: crowd is short; squeeze risk.`);
    }
    if (fg) { used.push("fear & greed"); const v = fg.now; why.push(`Fear & Greed ${v} (${fg.label}), a week ago ${fg.week_ago}.`); if (v >= 75) { score -= 5; risks.push(`Fear & Greed ${v} (extreme greed): late-cycle euphoria, tighten stops.`); } else if (v <= 25) { score += 5; risks.push(`Fear & Greed ${v} (extreme fear): capitulation phases are volatile both ways.`); } }
    if (ob) {
      used.push("options"); const pcr = ob.pcr;
      if (pcr != null) score += pcr < 0.7 ? 5 : pcr > 1.2 ? -5 : 0;
      let line = pcr != null ? `BTC options (${ob.expiry}): PCR ${pcr.toFixed(2)}, max pain ${fmt(ob.max_pain)}` : "";
      if (ob.atm_iv) line += `, ATM IV ${ob.atm_iv.toFixed(0)}% → expected move ±${fmt(ob.expected_move)} by expiry`;
      if (line) why.push(line + ".");
      if (ob.atm_iv && btc && btc.realized_vol30 && ob.atm_iv > btc.realized_vol30 * 1.3) risks.push(`Implied vol ${ob.atm_iv.toFixed(0)}% is well above 30-day realised ${btc.realized_vol30.toFixed(0)}%: the options market is pricing an event.`);
      if (ob.days < 2 && ob.expiry_oi_usd > 0) risks.push(`BTC options expiry ${ob.expiry} within 2 days ($${(ob.expiry_oi_usd / 1e6).toFixed(1)}M OI on Delta): price can gravitate to max pain ${fmt(ob.max_pain)} into expiry.`);
    }
    const wd = istNow().getUTCDay(); if (wd === 0 || wd === 6) risks.push("Weekend: thinner order books and no CME/ETF flows; gaps and wicks are more likely.");
    risks.push("Event risk: US data (CPI, jobs, FOMC), ETF flow swings, exchange or regulatory news can override every signal here.");
    score = Math.max(-100, Math.min(100, Math.round(score)));
    const view = score >= 35 ? "Bullish" : score >= 12 ? "Mildly bullish" : score <= -35 ? "Bearish" : score <= -12 ? "Mildly bearish" : "Neutral / range";
    const tone = score >= 12 ? "bull" : score <= -12 ? "bear" : "ntl";
    if (btc) {
      if (tone === "bull") { strat.push(`Trend-following: favour longs on pullbacks toward the 20-day average (${fmt(btc.ma20)}); invalidate below the 20-day low (${fmt(btc.lo20)}).`); strat.push("Intraday: take opening-range breakouts on the buy side only; skip breakdowns."); }
      else if (tone === "bear") { strat.push(`Defensive: rallies into the 20-day average (${fmt(btc.ma20)}) are sell zones; no fresh longs until BTC closes above it.`); strat.push("Intraday: favour breakdown (sell) setups, or stay flat; keep size small."); }
      else { strat.push(`Range: BTC between the 20-day low ${fmt(btc.lo20)} and high ${fmt(btc.hi20)}; buy near support, trim near resistance.`);
        if (ob && ob.expected_move && ob.spot) strat.push(`Options: with ATM IV ${ob.atm_iv.toFixed(0)}%, the 1-σ range to ${ob.expiry} is ${fmt(ob.spot - ob.expected_move)}–${fmt(ob.spot + ob.expected_move)}; defined-risk premium selling outside it (iron condor) suits a range; buy options only for breakouts.`); }
    }
    if (dv && dv.items.some((d) => (d.funding_pct || 0) > 0.03)) strat.push("Funding is rich: avoid chasing longs with leverage; wait for funding to reset or use spot.");
    if (eth && btc) { const rel = eth.chg7d - btc.chg7d; strat.push(`Relative strength: ETH ${rel > 0 ? "outperformed" : "lagged"} BTC by ${Math.abs(rel).toFixed(1)} pts over 7 days; ${rel > 2 ? "alt exposure has momentum" : rel < -2 ? "keep alt exposure light" : "no clear alt rotation"}.`); }
    strat.push("Size every trade from the stop; max 1–2% of capital at risk per idea.");
    return { as_of: nowIso(), score, view, tone, why, strategy: strat, risks, inputs: used, note: "Rule-based summary of public market data. Educational only, not investment advice." };
  }

  // crypto_lab.py
  const SPOT = { BTC: "BTCUSDT", ETH: "ETHUSDT", XAUT: "PAXGUSDT" };
  const deltaBars = (symbol, res, days) => cached(`dl:${symbol}:${res}`, 240e3, async () => {
    const end = Math.floor(Date.now() / 1000), d = await getJ(`${DL}/v2/history/candles?` + new URLSearchParams({ resolution: res, symbol: "MARK:" + symbol, start: end - days * 86400, end }));
    const rows = ((d || {}).result || []).map((r) => ({ t: +r.time, o: +r.open, h: +r.high, l: +r.low, c: +r.close })).filter((b) => isFinite(b.c));
    const m = new Map(rows.map((b) => [b.t, b])); return [...m.values()].sort((a, b) => a.t - b.t);
  });
  const sweepsUtc = (d1, h1, m15) => ({ M: periodSweep(d1, "M", KEYS_UTC), W: periodSweep(d1, "W", KEYS_UTC), D: periodSweep(d1, "D", KEYS_UTC), "1H": swingSweeps(h1, 2, 0), "15m": swingSweeps(m15, 3, 0) });
  function cprOf(d) { const p = (d.h + d.l + d.c) / 3, bc = (d.h + d.l) / 2, tc = 2 * p - bc; return { pivot: p, tc: Math.max(tc, bc), bc: Math.min(tc, bc), width: Math.abs(tc - bc), pct: p ? (100 * Math.abs(tc - bc)) / p : 0 }; }
  const cside = (x, c) => (x > c.tc ? "above" : x < c.bc ? "below" : "inside");
  function cryptoCpr(daily, m15) {
    if (daily.length < 3 || !m15.length) return null;
    const t0 = Math.floor(Date.UTC(new Date().getUTCFullYear(), new Date().getUTCMonth(), new Date().getUTCDate()) / 1000);
    const done = daily.filter((d) => d.t < t0); if (done.length < 2) return null;
    const prev = done[done.length - 1], c = cprOf(prev), hist = done.slice(-21, -1).map((d) => cprOf(d).pct);
    const rank = hist.length >= 8 ? (100 * hist.filter((x) => x < c.pct).length) / hist.length : null;
    const kind = rank != null ? (rank <= 30 ? "narrow" : rank >= 70 ? "wide" : "normal") : c.pct < 0.15 ? "narrow" : c.pct > 0.45 ? "wide" : "normal";
    const price = m15[m15.length - 1].c, today = m15.filter((b) => b.t >= t0), os = today.length ? cside(today[0].o, c) : null, tol = Math.max(c.width * 0.15, price * 0.0002);
    let setup = null; const sign = os === "above" ? 1 : os === "below" ? -1 : 0;
    if (sign) for (const x of today.slice(1)) {
      if (sign > 0) { if (x.c < c.bc) { setup = { side: "failed", t: x.t, title: "Bias failed" }; continue; } if (x.l >= c.bc - tol && x.l <= c.tc + tol && x.c > c.tc && x.c > x.o) { const en = x.c, st = c.bc - tol; setup = { side: "long", t: x.t, entry: en, stop: st, target: en + 2 * (en - st), title: "Buy: top CPR held" }; } }
      else { if (x.c > c.tc) { setup = { side: "failed", t: x.t, title: "Bias failed" }; continue; } if (x.h >= c.bc - tol && x.h <= c.tc + tol && x.c < c.bc && x.c < x.o) { const en = x.c, st = c.tc + tol; setup = { side: "short", t: x.t, entry: en, stop: st, target: en - 2 * (st - en), title: "Sell: bottom CPR rejected" }; } }
    }
    const p = c.pivot, ps = cside(price, c);
    return { ...c, kind, rank, price, price_side: ps, open_side: os, setup, R1: 2 * p - prev.l, S1: 2 * p - prev.h, R2: p + (prev.h - prev.l), S2: p - (prev.h - prev.l), from: prev,
      kind_label: { narrow: "Narrow CPR: trending day likely", wide: "Wide CPR: slow / sideways day likely" }[kind] || "Normal-width CPR",
      bias: { above: "Above CPR: buy bias", below: "Below CPR: sell bias" }[ps] || "Inside CPR: sideways" };
  }
  function oiBuild(side, dOi, dPx) {
    if (!dOi) return null; const up = (dPx || 0) > 0;
    if (dOi > 0 && up) return ["Long build-up", side === "ce" ? 1 : -1];
    if (dOi > 0) return side === "ce" ? ["Call writing", -1] : ["Put writing", 1];
    if (up) return ["Short covering", side === "ce" ? 1 : -1];
    return ["Long unwinding", side === "ce" ? -1 : 1];
  }
  function oiLevels(ch) {
    const spot = ch.spot, ks = ch.strikes, ce = (s) => s.ce || {}, pe = (s) => s.pe || {};
    const ai = ks.reduce((b, s, i) => (Math.abs(s.strike - spot) < Math.abs(ks[b].strike - spot) ? i : b), 0);
    const band = ks.slice(Math.max(0, ai - 6), ai + 7), below0 = band.filter((s) => s.strike <= spot), above0 = band.filter((s) => s.strike >= spot);
    const below = below0.length ? below0 : band, above = above0.length ? above0 : band;
    const pick = (arr, chg, oi) => { const fr = arr.filter((s) => chg(s) > 0).sort((a, b) => chg(b) - chg(a)); return fr.length ? fr : [...arr].sort((a, b) => oi(b) - oi(a)); };
    const S = pick(below, (s) => pe(s).oi_chg6h_usd || 0, (s) => pe(s).oi || 0), R = pick(above, (s) => ce(s).oi_chg6h_usd || 0, (s) => ce(s).oi || 0);
    const wallS = below.reduce((a, s) => ((pe(s).oi || 0) > (pe(a).oi || 0) ? s : a)), wallR = above.reduce((a, s) => ((ce(s).oi || 0) > (ce(a).oi || 0) ? s : a));
    let b = 0, s_ = 0;
    for (const s of ks.slice(Math.max(0, ai - 8), ai + 9)) for (const [sd, o] of [["ce", ce(s)], ["pe", pe(s)]]) { const r = oiBuild(sd, o.oi_chg6h_usd || 0, o.mark_chg24h); if (r) { if (r[1] > 0) b += Math.abs(o.oi_chg6h_usd); else s_ += Math.abs(o.oi_chg6h_usd); } }
    return { sup: S[0].strike, sup_chg: pe(S[0]).oi_chg6h_usd || 0, res: R[0].strike, res_chg: ce(R[0]).oi_chg6h_usd || 0, wallS: wallS.strike, wallR: wallR.strike,
      mp: ch.max_pain, pcr: ch.pcr, flow: Math.round((100 * (b - s_)) / (b + s_ || 1)) };
  }
  function confluence(spot, oi, cp, ix, rows, atm) {
    const tol = Math.max(spot * 0.0012, 0.01), L = [], add = (px, lbl, fam) => { if (px) L.push({ px: +px, lbl, fam }); };
    add(oi.sup, "OI support" + (oi.sup_chg > 0 ? ` (+$${Math.round(oi.sup_chg / 1e3).toLocaleString()}K PE 6h)` : ""), "OI");
    add(oi.res, "OI resistance" + (oi.res_chg > 0 ? ` (+$${Math.round(oi.res_chg / 1e3).toLocaleString()}K CE 6h)` : ""), "OI");
    if (oi.wallS !== oi.sup) add(oi.wallS, "Put OI wall", "OI"); if (oi.wallR !== oi.res) add(oi.wallR, "Call OI wall", "OI"); add(oi.mp, "Max pain", "OI");
    if (cp) for (const [k, n] of [["tc", "CPR top"], ["pivot", "CPR pivot"], ["bc", "CPR bottom"], ["R1", "Pivot R1"], ["S1", "Pivot S1"], ["R2", "Pivot R2"], ["S2", "Pivot S2"]]) add(cp[k], n, "CPR");
    const names = { M: ["Prev month high", "Prev month low"], W: ["Prev week high", "Prev week low"], D: ["Prev day high", "Prev day low"], "1H": ["1H swing-high liquidity", "1H swing-low liquidity"], "15m": ["15m swing-high liquidity", "15m swing-low liquidity"] };
    for (const [tf, [hn, ln]] of Object.entries(names)) { const x = ix[tf] || {}; if (x.state !== "new") { add(x.bsl, hn, "LIQ"); add(x.ssl, ln, "LIQ"); } }
    L.sort((a, b) => a.px - b.px); const Z = [];
    for (const l of L) { const z = Z[Z.length - 1]; if (z && l.px - z.hi <= tol && l.px - z.lo <= 2.5 * tol) { z.items.push(l); z.hi = l.px; } else Z.push({ lo: l.px, hi: l.px, items: [l] }); }
    for (const z of Z) { z.mid = (z.lo + z.hi) / 2; z.fams = [...new Set(z.items.map((i) => i.fam))].sort(); const n = z.items.length + z.fams.length - 1; z.stars = z.fams.length >= 3 || n >= 4 ? 3 : z.fams.length === 2 || n >= 3 ? 2 : 1; z.side = z.mid > spot + tol * 0.3 ? "R" : z.mid < spot - tol * 0.3 ? "S" : "AT"; z.labels = z.items.map((i) => i.lbl); delete z.items; }
    const res = Z.filter((z) => z.side === "R").sort((a, b) => a.mid - b.mid).slice(0, 3), sup = Z.filter((z) => z.side === "S").sort((a, b) => b.mid - a.mid).slice(0, 3), at = Z.filter((z) => z.side === "AT");
    const zoneOf = (px) => Z.find((z) => px >= z.lo - tol && px <= z.hi + tol && z.fams.some((f) => f !== "LIQ"));
    const ev = [];
    for (const tf of ["15m", "1H"]) { const x = (ix[tf] || {}).latest; if (!x) continue; const d = x.side === "ssl" ? 1 : -1, same = ev.find((e) => e.dir === d && Math.abs(e.px - x.level) <= tol); if (same) same.tf += " + " + tf; else { const z = zoneOf(x.level); if (z) ev.push({ dir: d, tf, px: x.level, at: x.at, zone: z }); } }
    for (const tf of ["D", "W", "M"]) { const x = ix[tf] || {}; if (x.state === "ssl_swept" && zoneOf(x.ssl)) ev.push({ dir: 1, tf, px: x.ssl, zone: zoneOf(x.ssl) }); if (x.state === "bsl_swept" && zoneOf(x.bsl)) ev.push({ dir: -1, tf, px: x.bsl, zone: zoneOf(x.bsl) }); }
    const lean = (tfs) => { let b = 0, s = 0; const one = (x, d, tf) => { if (!x) return; const st = tf === "1H" || tf === "15m" ? (x.latest ? x.latest.side + "_swept" : "") : x.state; if (st === "ssl_swept") d > 0 ? b++ : s++; if (st === "bsl_swept") d > 0 ? s++ : b++; };
      for (const tf of tfs) { one(ix[tf], 1, tf); for (const r of rows) one(r.sweeps[tf], r.side === "CE" ? 1 : -1, tf); } return [b, s, (b - s) / (b + s || 1)]; };
    const sc = [], push = (name, v, txt) => sc.push({ name, v: Math.max(-1, Math.min(1, v)), txt });
    if (cp) push("CPR", cp.price_side === "above" ? 1 : cp.price_side === "below" ? -1 : 0, `${cp.bias} · ${cp.kind_label}`);
    push("OI flow", oi.flow / 100, `OI flow ${oi.flow > 0 ? "+" : ""}${oi.flow} over 6h (${oi.flow > 25 ? "bullish" : oi.flow < -25 ? "bearish" : "neutral"})`);
    if (oi.pcr) push("PCR", oi.pcr > 1.2 ? 0.5 : oi.pcr < 0.7 ? -0.5 : 0, `PCR ${oi.pcr.toFixed(2)}`);
    const dS = spot - oi.sup, dR = oi.res - spot, fm0 = (n) => Math.round(n).toLocaleString("en-US");
    push("Room", dR > dS * 1.5 ? 0.4 : dS > dR * 1.5 ? -0.4 : 0, `${fm0(dS)} above OI support, ${fm0(dR)} below OI resistance`);
    let [b, s, v] = lean(["15m", "1H"]); push("Intraday sweeps", v, `15m + 1H: ${b} bullish vs ${s} bearish sweeps`);
    [b, s, v] = lean(["D", "W", "M"]); push("Higher-TF sweeps", v * 0.8, `Daily/weekly/monthly: ${b} bullish vs ${s} bearish`);
    for (const e of ev) push("Sweep at level", e.dir * 1.2, `${e.dir > 0 ? "Sell-side" : "Buy-side"} sweep on ${e.tf} at ${fm0(e.px)} into ${e.zone.fams.filter((f) => f !== "LIQ").join(" + ")} zone`);
    const tot = sum(sc.map((x) => x.v)), norm = (tot / Math.max(3, sc.length)) * 1.6, bias = norm > 0.3 ? "BULLISH" : norm < -0.3 ? "BEARISH" : "RANGE";
    const agree = sc.filter((x) => x.v && x.v > 0 === tot > 0).length;
    const ds = rows.filter((r) => r.strike === atm).map((r) => r.decay.status), decaying = ds.length && ds.every((x) => x === "decaying" || x === "fast"), beating = ds.length && ds.every((x) => x === "beating");
    const plan = [], [s1, s2] = sup, [r1, r2] = res, rng = (z) => (Math.abs(z.hi - z.lo) < 1e-9 ? fm0(z.mid) : `${fm0(z.lo)}–${fm0(z.hi)}`);
    if (bias === "BULLISH" && s1) plan.push(`<b>Buy the dip:</b> wait for a 15m sell-side sweep into <b>${rng(s1)}</b> that closes back above it, then a call near ${fm0(atm)}. Invalid below ${fm0(s1.lo - tol)}. Targets ${r1 ? fm0(r1.mid) : "—"}${r2 ? " then " + fm0(r2.mid) : ""}.`);
    if (bias === "BEARISH" && r1) plan.push(`<b>Sell the rally:</b> wait for a 15m buy-side sweep into <b>${rng(r1)}</b> that closes back below it, then a put near ${fm0(atm)}. Invalid above ${fm0(r1.hi + tol)}. Targets ${s1 ? fm0(s1.mid) : "—"}${s2 ? " then " + fm0(s2.mid) : ""}.`);
    if (bias === "RANGE" && s1 && r1) plan.push(`<b>Range:</b> fade the edges — long only on a sell-side sweep at ${fm0(s1.mid)}, short only on a buy-side sweep at ${fm0(r1.mid)}. ` + (decaying ? "With premiums decaying, selling premium between these zones has the edge." : "Wait for one edge to break with a close before taking a direction."));
    if (cp && cp.kind === "narrow") plan.push(`<b>Narrow CPR:</b> a trending day is likely — a clean close through ${r1 ? fm0(r1.mid) : "resistance"} or ${s1 ? fm0(s1.mid) : "support"} is more likely to run than reverse.`);
    if (cp && cp.kind === "wide") plan.push("<b>Wide CPR:</b> a slow, two-sided day is likely — favour sweeps and reversals at the zones over breakouts.");
    if (at.length) plan.push(`<b>Price is sitting on a zone</b> (${at.map((z) => fm0(z.mid)).join(", ")}): expect a reaction here first.`);
    return { bias, confidence: Math.min(100, Math.round(Math.abs(norm) * 100)), agree, signals: sc, res, sup, at, events: ev, tol, plan,
      decay: decaying ? "ATM call and put are both losing time value: the market is not paying for a move — sellers have the edge until a zone breaks."
        : beating ? "ATM call and put are both holding time value: the market is pricing a move — buyers can be paid if a zone breaks." : "ATM premiums are mixed: decay is not the deciding factor right now." };
  }
  async function cryptoLab(coin = "BTC", expiry = null) {
    coin = coin.toUpperCase(); if (!SPOT[coin]) throw new HE(400, "coin must be BTC, ETH or XAUT");
    return cached(`lab:${coin}:${expiry || ""}`, 240e3, async () => {
      const ch = await options(coin, expiry, 12), spot = ch.spot, atm = ch.atm, pair = SPOT[coin];
      const ks = ch.strikes.map((s) => s.strike), ai = ks.indexOf(atm), near = ch.strikes.slice(Math.max(0, ai - 3), ai + 4);
      const opts = []; for (const s of near) for (const sd of ["ce", "pe"]) if (s[sd] && s[sd].symbol) opts.push([s.strike, sd.toUpperCase(), s[sd]]);
      const jobs = [klines(pair, "1d", 120), klines(pair, "1h", 500), klines(pair, "15m", 500)];
      for (const [, , o] of opts) jobs.push(deltaBars(o.symbol, "1h", 40), deltaBars(o.symbol, "15m", 6));
      const res = (await Promise.allSettled(jobs)).map((r) => (r.status === "fulfilled" ? r.value : []));
      const [d1, h1, m15] = res, ix = d1.length ? sweepsUtc(d1, h1, m15) : {};
      const dte = Math.max(0, (Date.parse(ch.expiry + "T12:00:00Z") - Date.now()) / 864e5), nc = { state: "new", text: "No candles" };
      const rows = opts.map(([k, side, o], n) => {
        const oh = res[3 + 2 * n], om = res[4 + 2 * n], ltp = o.mark || 0;
        const sw = { M: oh.length ? periodSweep(oh, "M", KEYS_UTC) : nc, W: oh.length ? periodSweep(oh, "W", KEYS_UTC) : nc, D: oh.length ? periodSweep(oh, "D", KEYS_UTC) : nc,
          "1H": oh.length ? swingSweeps(oh, 2, 0) : nc, "15m": om.length ? swingSweeps(om, 3, 0) : nc };
        const dc = om.length ? decay(side, k, ltp, { theta: o.theta, iv: o.iv }, spot, om, m15, dte, 0)
          : { status: "holding", status_text: "No candles", ltp, intrinsic: 0, tv: 0, tv_pct: 0, theta: Math.abs(o.theta || 0), theta_pct: 0, tv_days: null, trend: "—" };
        return { symbol: o.symbol, strike: k, side, ltp, chg_pct: o.mark_chg24h || 0, sweeps: sw, decay: dc, days: group(oh, KEYS_UTC.D).length };
      });
      const cp = cryptoCpr(d1, m15), oi = oiLevels(ch);
      return { coin, pair, spot, atm, expiry: ch.expiry, expiries: ch.expiries, dte, index_sweeps: ix, rows, cpr: cp, oi, confluence: confluence(spot, oi, cp, ix, rows, atm),
        built_at: Math.floor(Date.now() / 1000), source: `Delta Exchange India options (MARK candles) · Binance ${pair} for the underlying` };
    });
  }

  // ---------------------------------------------------------------- crypto feed status (Connections page)
  const FEEDS = { BINANCE: "https://data-api.binance.vision", BINANCEF: "https://fapi.binance.com", SHARK: "https://api.sharkexchange.in", DELTA: "https://api.india.delta.exchange", DELTAF: "https://api.india.delta.exchange" };
  async function feedTest(m) {
    const t0 = performance.now();
    if (m === "BINANCE") { const r = await getJ(`${BN}/api/v3/ticker/price?symbols=${encodeURIComponent('["BTCUSDT","ETHUSDT","SOLUSDT"]')}`); return { ping: { ms: Math.round(performance.now() - t0) }, instruments: "—", sample: r.map((x) => ({ pair: x.symbol, price: +x.price, quote: "USDT" })) }; }
    if (m === "BINANCEF") { const r = await Promise.all(["BTCUSDT", "XAUUSDT"].map((s) => getJ(`${BF}/fapi/v1/ticker/price?symbol=${s}`))); return { ping: { ms: Math.round(performance.now() - t0) }, instruments: "—", sample: r.map((x) => ({ pair: x.symbol, price: +x.price, quote: "USDT" })) }; }
    if (m === "DELTA") { const o = await options("BTC"); return { ping: { ms: Math.round(performance.now() - t0) }, instruments: o.expiries.length + " expiries", sample: [{ pair: "BTC spot", price: o.spot, quote: "USD" }] }; }
    if (m === "DELTAF") { const r = await Promise.all(["BTCUSD", "ETHUSD"].map((x) => getJ(`${DL}/v2/tickers/${x}`))); return { ping: { ms: Math.round(performance.now() - t0) }, instruments: "perpetuals", sample: r.map((x) => ({ pair: x.result.symbol, price: +x.result.mark_price, quote: "USD" })) }; }
    throw new HE(503, "Shark Exchange is not reachable from a phone browser: the phone uses Delta Exchange perpetuals (Delta Futures) instead.");
  }

  // ---------------------------------------------------------------- router
  async function route(url, init) {
    const p = url.pathname.replace(/^.*?\/api\//, "/api/"), q = url.searchParams, method = ((init && init.method) || "GET").toUpperCase();
    const body = () => { try { return JSON.parse((init && init.body) || "{}"); } catch (e) { return {}; } };
    // FYERS
    if (p === "/api/fyers/status") return FY.status();
    if (p === "/api/fyers/app" && method === "GET") { const a = FY.app(); return { app_id: a.app_id, redirect_uri: a.redirect_uri, has_secret: !!a.secret, editable: true, source: "this device" }; }
    if (p === "/api/fyers/app" && method === "POST") {
      const b = body(), a = FY.app();
      if (!/^[A-Za-z0-9]+-[0-9]{3}$/.test(b.app_id || "")) throw new HE(422, "App ID looks wrong (e.g. ABCD1234-100)");
      if (!b.secret && !a.secret) throw new HE(422, "Enter the secret key");
      LS.set("fyers.app", { app_id: b.app_id, secret: b.secret || a.secret, redirect_uri: (b.redirect_uri || "").trim() || FY.defaultRedirect() });
      return { saved: true };
    }
    if (p === "/api/fyers/disconnect") { LS.set("fyers.token", null); return { connected: false, message: "FYERS login removed from this device" }; }
    if (p === "/api/fyers/renew") return FY.renew(body().pin);
    if (p === "/api/fyers/quotes") return { provider: "FYERS", received_at: new Date().toISOString(), data: await FY.quotes(q.get("symbols")) };
    if (p === "/api/fyers/history") return { provider: "FYERS", symbol: q.get("symbol"), received_at: new Date().toISOString(),
      data: await FY.history({ symbol: q.get("symbol"), resolution: q.get("resolution") || "5", date_format: q.get("date_format") || "1", range_from: q.get("range_from"), range_to: q.get("range_to"), cont_flag: q.get("cont_flag") || "1" }) };
    if (p === "/api/fyers/option-chain") return { provider: "FYERS", symbol: q.get("symbol"), received_at: new Date().toISOString(),
      data: await FY.chain(q.get("symbol") || "NSE:NIFTY50-INDEX", q.get("strikecount") || 10, q.get("timestamp") || "", q.get("greeks") === "0" ? 0 : 1) };
    if (p === "/api/fyers/profile") return FY.get("/profile", {});
    // analytics
    if (p === "/api/optlab/board") return optBoard(q.get("index"), q.get("expiry") || "wk");
    if (p === "/api/crypto/overview") return overview();
    if (p === "/api/crypto/trend") return trend();
    if (p === "/api/crypto/derivs") return derivs();
    if (p === "/api/crypto/tradfi") return tradfi();
    if (p === "/api/crypto/fng") return fng();
    if (p === "/api/crypto/options") return options(q.get("coin") || "BTC", q.get("expiry"), q.get("width") || 3);
    if (p === "/api/crypto/lab") return cryptoLab(q.get("coin") || "BTC", q.get("expiry"));
    if (p === "/api/crypto/assessment") return assessment();
    // crypto feeds on the Connections page
    if (p === "/api/markets/status") { const st = FY.status();
      return { FYERS: { connected: st.connected, message: st.message }, ...Object.fromEntries(Object.entries(FEEDS).map(([m, u]) => [m, { enabled: m !== "SHARK", url: u, last_error: m === "SHARK" ? "PC only" : null }])) }; }
    let m = p.match(/^\/api\/markets\/(\w+)\/test$/); if (m) return feedTest(m[1]);
    m = p.match(/^\/api\/markets\/(\w+)\/enable$/); if (m) return { ok: true };
    // NSE / Moneycontrol through the PC's proxy: rebuilt from FYERS where possible
    if (p === "/api/proxy") {
      const up = q.get("url") || "";
      if (/nseindia\.com\/api\/allIndices/.test(up)) return allIndices();
      if (/getReferenceRates/.test(up)) return commodities();
      if (/^https?:\/\/(www\.)?nseindia\.com\/?$/.test(up)) return { ok: true };
      throw new HE(503, "Not available in the phone version (needs the PC server): " + new URL(up).host);
    }
    if (p === "/api/health") return { ok: true, mode: "mobile" };
    if (p === "/api/nse/status") return { engine: "FYERS (phone)", paused_for_s: 0 };
    if (p.startsWith("/api/lab/")) { if (!window.MM.lab) throw new HE(503, "Open the Signal Lab page to use it."); return window.MM.lab(p, q, method, body()); }
    if (p.startsWith("/api/desk/")) { if (!window.MM.desk) throw new HE(503, "Open the Day Desk page to use it."); return window.MM.desk(p, q, method, body()); }
    if (p.startsWith("/api/ai/")) throw new HE(503, "AI analysis needs the PC server (it holds the API key).");
    throw new HE(404, "Not available in the phone version: " + p);
  }

  window.fetch = async function (input, init) {
    let url;
    try { url = new URL(typeof input === "string" ? input : input.url, location.href); } catch (e) { return realFetch(input, init); }
    if (url.origin !== location.origin || !/\/api\//.test(url.pathname)) return realFetch(input, init);
    try { return json(await route(url, init)); }
    catch (e) { const st = e.status || 502; return json({ detail: e.message || String(e) }, st); }
  };

  window.MM = { FY, LS, HE, getJ, cached, cryptoOptions: options, version: "2026.11.01-9" };
})();
