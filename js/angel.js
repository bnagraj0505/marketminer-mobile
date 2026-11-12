/* MarketMiner Mobile: Angel One SmartAPI live India prices, in this browser. DATA ONLY: nothing here can place an order.
 *
 * Same idea as the PC version (backend/brokers/angel): Angel One's live stream is free (up to 1,000 instruments) and
 * does not use FYERS's 5,000 data requests a day. While a symbol has a fresh Angel price, FYERS /quotes is not asked
 * for it (MM.FY.get is wrapped below); everything else (candle history, option chains) still comes from FYERS.
 *
 * Settings (Connections page) stay on THIS device: client code, SmartAPI key, MPIN, TOTP secret. The MPIN and TOTP
 * secret are encrypted with a key this browser creates and cannot export (Web Crypto, kept in IndexedDB). The 6-digit
 * TOTP code is computed here (RFC 6238), so the daily login happens by itself.
 *
 * Whether a web page may talk to Angel One is Angel's decision (browser rules: CORS for the login, and the stream
 * taking its keys in the address). "Test" on the Connections page shows exactly what works on this device; when it
 * does not, MarketMiner simply keeps using FYERS as before.
 */
(function () {
  "use strict";
  const MM = window.MM; if (!MM || !MM.FY) return;
  const API = "https://apiconnect.angelone.in", LOGIN = "/rest/auth/angelbroking/user/v1/loginByPassword";
  const WS = "wss://smartapisocket.angelone.in/smart-stream";
  const MASTER_URLS = ["https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json",
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"];
  const SEARCH = "/rest/secure/angelbroking/order/v1/searchScrip";
  // instruments looked up one name at a time when the full list cannot be downloaded in this browser
  const QUICK = [["NFO", "NIFTY"], ["NFO", "BANKNIFTY"], ["NFO", "FINNIFTY"], ["NFO", "MIDCPNIFTY"], ["BFO", "SENSEX"], ["BFO", "BANKEX"]];
  const EXCH = { NSE: 1, NFO: 2, BSE: 3, BFO: 4, MCX: 5 };
  const INDEX = { "NSE:NIFTY50-INDEX": ["Nifty 50", "99926000"], "NSE:NIFTYBANK-INDEX": ["Nifty Bank", "99926009"],
    "NSE:FINNIFTY-INDEX": ["Nifty Fin Service", "99926037"], "NSE:MIDCPNIFTY-INDEX": ["NIFTY MID SELECT", "99926074"],
    "NSE:INDIAVIX-INDEX": ["India VIX", "99926017"], "BSE:SENSEX-INDEX": ["SENSEX", "99919000"], "BSE:BANKEX-INDEX": ["BANKEX", "99919012"] };
  const MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
  const WEEK_M = { 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8, 9: 9, O: 10, N: 11, D: 12 };
  const DERIV_RE = /^(NSE|BSE|MCX):([A-Z][A-Z&-]*?)(\d{2})(?:(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)|([1-9OND])(\d{2}))(?:(FUT)|(\d+(?:\.\d+)?)(CE|PE))$/;
  const LIVE_AGE = 15e3, MAX_SYMBOLS = 1000;
  const istDay = () => new Date(Date.now() + 19800e3).toISOString().slice(0, 10);
  const LS = { get: (k) => { try { return JSON.parse(localStorage.getItem("mm.angel." + k) || "null"); } catch (e) { return null; } },
    set: (k, v) => { try { if (v == null) localStorage.removeItem("mm.angel." + k); else localStorage.setItem("mm.angel." + k, JSON.stringify(v)); } catch (e) { /* private mode */ } } };

  // ------------------------------------------------------------ device-only encryption (non-exportable AES-GCM key)
  function idb() {
    return new Promise((res, rej) => { const r = indexedDB.open("mm-angel", 1); r.onupgradeneeded = () => r.result.createObjectStore("kv"); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
  }
  async function kv(op, k, v) {
    const db = await idb();
    return new Promise((res, rej) => { const tx = db.transaction("kv", op === "get" ? "readonly" : "readwrite"), s = tx.objectStore("kv");
      const r = op === "get" ? s.get(k) : op === "put" ? s.put(v, k) : s.delete(k); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
  }
  async function devKey() {
    let k = await kv("get", "key").catch(() => null);
    if (!k) { k = await crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]); await kv("put", "key", k); }
    return k;
  }
  const b64 = (u8) => btoa(String.fromCharCode(...new Uint8Array(u8)));
  const unb64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
  async function seal(text) {
    try { const iv = crypto.getRandomValues(new Uint8Array(12)), ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, await devKey(), new TextEncoder().encode(text));
      return "aes:" + b64(iv) + "." + b64(ct); } catch (e) { return "plain:" + btoa(text); }
  }
  async function unseal(blob) {
    if (!blob) return "";
    const [kind, rest] = [blob.slice(0, blob.indexOf(":")), blob.slice(blob.indexOf(":") + 1)];
    if (kind === "plain") return atob(rest);
    const [iv, ct] = rest.split(".");
    return new TextDecoder().decode(await crypto.subtle.decrypt({ name: "AES-GCM", iv: unb64(iv) }, await devKey(), unb64(ct)));
  }

  // ------------------------------------------------------------ settings
  const cfg = () => ({ client_code: "", api_key: "", mpin: "", totp: "", enabled: true, ...(LS.get("cfg") || {}) });
  const configured = (c = cfg()) => !!(c.client_code && c.api_key && c.mpin && c.totp);
  function publicCfg() {
    const c = cfg(), k = c.api_key;
    return { client_code: c.client_code, api_key_hint: k.length > 6 ? k.slice(0, 3) + "…" + k.slice(-2) : k ? "set" : "", mpin_set: !!c.mpin, totp_set: !!c.totp, enabled: c.enabled !== false, configured: configured(c) };
  }
  async function saveCfg(b) {
    const c = cfg();
    if (b.client_code && b.client_code.trim()) c.client_code = b.client_code.trim().toUpperCase();
    if (b.api_key && b.api_key.trim()) c.api_key = b.api_key.trim();
    if (b.mpin && b.mpin.trim()) { if (!/^\d{4,6}$/.test(b.mpin.trim())) throw new Error("The MPIN is 4 to 6 digits"); c.mpin = await seal(b.mpin.trim()); }
    if (b.totp_secret && b.totp_secret.trim()) {
      const s = b.totp_secret.replace(/\s+/g, "").toUpperCase();
      if (!/^[A-Z2-7]+=*$/.test(s)) throw new Error("That TOTP secret is not valid (it is the letters and digits behind the QR code)");
      c.totp = await seal(s);
    }
    if (typeof b.enabled === "boolean") c.enabled = b.enabled;
    LS.set("cfg", c); LS.set("sess", null);
    return publicCfg();
  }
  function forget() { LS.set("cfg", null); LS.set("sess", null); kv("del", "key").catch(() => {}); stop(); }

  // ------------------------------------------------------------ TOTP (RFC 6238, HMAC-SHA1, 30 s, 6 digits)
  function b32(s) {
    const A = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"; let bits = "", out = [];
    for (const ch of s.replace(/=+$/, "")) { const v = A.indexOf(ch); if (v < 0) throw new Error("bad base32"); bits += v.toString(2).padStart(5, "0"); }
    for (let i = 0; i + 8 <= bits.length; i += 8) out.push(parseInt(bits.slice(i, i + 8), 2));
    return new Uint8Array(out);
  }
  async function totp(secret, t = Date.now() / 1000, digits = 6) {
    const key = await crypto.subtle.importKey("raw", b32(secret.replace(/\s+/g, "").toUpperCase()), { name: "HMAC", hash: "SHA-1" }, false, ["sign"]);
    const c = Math.floor(t / 30), msg = new Uint8Array(8); new DataView(msg.buffer).setUint32(0, Math.floor(c / 2 ** 32)); new DataView(msg.buffer).setUint32(4, c >>> 0);
    const h = new Uint8Array(await crypto.subtle.sign("HMAC", key, msg)), o = h[h.length - 1] & 15;
    const n = ((h[o] & 127) << 24 | h[o + 1] << 16 | h[o + 2] << 8 | h[o + 3]) % 10 ** digits;
    return String(n).padStart(digits, "0");
  }

  // ------------------------------------------------------------ login
  const headers = (key, jwt) => ({ "Content-Type": "application/json", Accept: "application/json", "X-UserType": "USER", "X-SourceID": "WEB",
    "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1", "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": key, ...(jwt ? { Authorization: "Bearer " + jwt } : {}) });
  let loginP = null;
  function login(force) {
    const s = LS.get("sess");
    if (!force && s && s.jwt && s.day === istDay()) return Promise.resolve(s);
    return loginP || (loginP = (async () => {
      const c = cfg(); if (!configured(c)) throw new Error("Angel One is not set up (Connections page)");
      const body = { clientcode: c.client_code, password: await unseal(c.mpin), totp: await totp(await unseal(c.totp)) };
      let r;
      try { r = await fetch(API + LOGIN, { method: "POST", headers: headers(c.api_key), body: JSON.stringify(body) }); }
      catch (e) { throw new Error("This browser was not allowed to reach Angel One's login (Angel One does not accept web-page requests from here). The PC version can use it; this device keeps using FYERS."); }
      const j = await r.json().catch(() => ({})), d = j.data || {};
      if (!j.status || !d.jwtToken) throw new Error(`Angel One login refused: ${j.message || r.status} ${j.errorcode || ""}`.trim());
      const out = { jwt: String(d.jwtToken).replace(/^Bearer\s+/, ""), feed: d.feedToken, day: istDay(), at: new Date().toLocaleString("en-IN"), client_code: c.client_code, api_key: c.api_key };
      LS.set("sess", out); return out;
    })().finally(() => { loginP = null; }));
  }

  // ------------------------------------------------------------ scrip master -> compact map (kept in IndexedDB for the day)
  let MAP = null, mapP = null;
  function buildMap(rows) {
    const m = { v: 2, day: istDay(), count: rows.length, cash: {}, names: {}, deriv: {} };
    for (const r of rows) {
      const seg = r.exch_seg, tok = r.token, sym = String(r.symbol || ""), it = String(r.instrumenttype || "");
      if (!tok) continue;
      if (seg === "NSE" || seg === "BSE") {
        if (it === "AMXIDX" || it === "INDEX") { m.names[seg + "|" + sym.toUpperCase()] = tok; if (r.name) m.names[seg + "|" + String(r.name).toUpperCase()] = tok; }
        else if (/-(EQ|BE)$/.test(sym)) m.cash[seg + "|" + sym.toUpperCase()] = tok;
      } else if ((seg === "NFO" || seg === "BFO" || seg === "MCX") && /^(FUT|OPT)/.test(it)) {
        const e = String(r.expiry || ""), mm = e.match(/^(\d{2})([A-Z]{3})(\d{4})$/); if (!mm) continue;
        const kind = it.startsWith("FUT") ? "FUT" : sym.endsWith("CE") ? "CE" : sym.endsWith("PE") ? "PE" : ""; if (!kind) continue;
        const k = kind === "FUT" ? 0 : Math.round(+r.strike || 0), key = [seg, String(r.name || "").toUpperCase(), kind, k].join("|");
        (m.deriv[key] = m.deriv[key] || []).push([`${mm[3]}-${String(MON.indexOf(mm[2]) + 1).padStart(2, "0")}-${mm[1]}`, tok, +r.lotsize || 0]);
      }
    }
    return m;
  }
  function loadMap() {
    if (MAP && MAP.day === istDay() && MAP.v === 2) return Promise.resolve(MAP);
    return mapP || (mapP = (async () => {
      const saved = await kv("get", "map").catch(() => null);
      if (saved && saved.day === istDay() && saved.v === 2) return (MAP = saved);
      let rows = null;
      for (const u of MASTER_URLS) { try { rows = await (await fetch(u)).json(); if (Array.isArray(rows) && rows.length) break; rows = null; } catch (e) { rows = null; } }
      if (rows) { MAP = buildMap(rows); kv("put", "map", MAP).catch(() => {}); return MAP; }
      // Angel One's file server does not let web pages download the list: look up the index futures through the API
      // (same server as the login, which this browser can reach). Indices themselves use their fixed tokens.
      const m = { v: 2, day: istDay(), count: 0, cash: {}, names: {}, deriv: {}, quick: true };
      const got = await Promise.allSettled(QUICK.map(([seg, name]) => searchInto(m, seg, name)));
      if (!got.some((g) => g.status === "fulfilled" && g.value > 0)) {
        const why = got.map((g) => g.reason && g.reason.message).filter(Boolean)[0];
        throw new Error("Could not get Angel One's instrument list in this browser" + (why ? " (" + why + ")" : ""));
      }
      MAP = m; kv("put", "map", MAP).catch(() => {});
      return MAP;
    })().finally(() => { mapP = null; }));
  }
  // Angel One scrip search: adds the futures (and cash share) of one name to the map; returns how many were added
  async function searchInto(m, seg, name) {
    const s = await login(), c = cfg();
    const r = await fetch(API + SEARCH, { method: "POST", headers: headers(c.api_key, s.jwt), body: JSON.stringify({ exchange: seg, searchscrip: name }) });
    const j = await r.json().catch(() => ({})); if (!j.status) throw new Error(j.message || "search refused " + r.status);
    let n = 0;
    for (const x of j.data || []) {
      const ts = String(x.tradingsymbol || "").toUpperCase(), tok = String(x.symboltoken || "");
      let k = ts.match(/^([A-Z&-]+?)(\d{2})([A-Z]{3})(\d{2})FUT$/);
      if (k && k[1] === name.toUpperCase() && (seg === "NFO" || seg === "BFO" || seg === "MCX")) {
        const key = [seg, k[1], "FUT", 0].join("|"), d = `20${k[4]}-${String(MON.indexOf(k[3]) + 1).padStart(2, "0")}-${k[2]}`;
        const l = (m.deriv[key] = m.deriv[key] || []); if (!l.find((e) => e[1] === tok)) { l.push([d, tok, 0]); n++; }
      } else if ((seg === "NSE" || seg === "BSE") && /-(EQ|BE)$/.test(ts)) { m.cash[seg + "|" + ts] = tok; n++; }
    }
    m.count += n; return n;
  }
  // for a symbol the quick list does not have (a stock future), look it up once
  async function ensure(fy) {
    if (tokenOf(fy)) return true; if (!MAP || !MAP.quick) return false;
    const s = String(fy).toUpperCase(), x = s.match(DERIV_RE) || s.match(/^(NSE|BSE):([A-Z0-9&_.-]+)-(EQ|BE)$/); if (!x) return false;
    const seg = x.length > 4 ? { NSE: "NFO", BSE: "BFO", MCX: "MCX" }[x[1]] : x[1];
    try { await searchInto(MAP, seg, x[2]); kv("put", "map", MAP).catch(() => {}); } catch (e) { err(e.message); }
    return !!tokenOf(fy);
  }
  function tokenOf(fy) {
    const s = String(fy).toUpperCase(), m = MAP; if (!m) return null;
    if (INDEX[s]) { const ex = s.split(":")[0], [nm, fb] = INDEX[s]; return [EXCH[ex], m.names[ex + "|" + nm.toUpperCase()] || fb]; }
    let x = s.match(/^(NSE|BSE):([A-Z0-9&_.-]+-(?:EQ|BE))$/);
    if (x) { const t = m.cash[x[1] + "|" + x[2]]; return t ? [EXCH[x[1]], t] : null; }
    x = s.match(DERIV_RE); if (!x) return null;
    const seg = { NSE: "NFO", BSE: "BFO", MCX: "MCX" }[x[1]], kind = x[7] ? "FUT" : x[9], k = kind === "FUT" ? 0 : Math.round(+x[8] * 100);
    const c = m.deriv[[seg, x[2], kind, k].join("|")] || [], yr = 2000 + +x[3];
    let tok = null;
    if (x[4]) { const pre = `${yr}-${String(MON.indexOf(x[4]) + 1).padStart(2, "0")}`; const hit = c.filter((e) => e[0].startsWith(pre)).sort(); tok = hit.length ? hit[hit.length - 1][1] : null; }
    else { const d = `${yr}-${String(WEEK_M[x[5]]).padStart(2, "0")}-${x[6]}`; const hit = c.find((e) => e[0] === d); tok = hit ? hit[1] : null; }
    return tok ? [EXCH[seg], tok] : null;
  }

  // front-month and next futures of an index or stock, from the instrument list: [{fy, expiry, token, exch, lot}]
  async function futures(name, ex = "NSE") {
    await loadMap();
    const seg = { NSE: "NFO", BSE: "BFO", MCX: "MCX" }[ex], today = istDay();
    return (MAP.deriv[[seg, String(name).toUpperCase(), "FUT", 0].join("|")] || []).filter((e) => e[0] >= today).sort()
      .map(([d, tok, lot]) => ({ fy: `${ex}:${String(name).toUpperCase()}${d.slice(2, 4)}${MON[+d.slice(5, 7) - 1]}FUT`, expiry: d, token: tok, exch: EXCH[seg], lot: lot || null }));
  }
  function lotOf(fy) {
    const s = String(fy).toUpperCase(), x = s.match(DERIV_RE); if (!x || !MAP) return null;
    const seg = { NSE: "NFO", BSE: "BFO", MCX: "MCX" }[x[1]], kind = x[7] ? "FUT" : x[9], k = kind === "FUT" ? 0 : Math.round(+x[8] * 100);
    const c = MAP.deriv[[seg, x[2], kind, k].join("|")] || []; const e = c.find((y) => y[2]); return e ? e[2] : null;
  }

  // ------------------------------------------------------------ live stream
  const Q = new Map();           // FYERS symbol -> {t, row}  (fresh Angel quotes)
  const ASKED = new Map();       // FYERS symbol -> last time /quotes asked for it
  const ST = { state: "off", connected: false, subscribed: 0, ticks: 0, last_tick: null, errors: [], unmapped: [] };
  const err = (m) => { ST.errors = ST.errors.concat(new Date().toLocaleTimeString("en-IN") + " " + String(m).slice(0, 200)).slice(-10); };
  const DEPTH = new Map();       // FYERS symbol -> last time a page asked for its market depth (mode 3, best five)
  let subs3 = new Map();
  let ws = null, subs = new Map(), cumvol = new Map(), pingT = null, loopT = null;
  function parse(buf) {
    if (!(buf instanceof ArrayBuffer) || buf.byteLength < 51) return null;
    const v = new DataView(buf), u8 = new Uint8Array(buf), mode = u8[0], exch = u8[1];
    let token = ""; for (let i = 2; i < 27 && u8[i]; i++) token += String.fromCharCode(u8[i]);
    const q = (o) => Number(v.getBigInt64(o, true));
    const out = { mode, exch, token, t: q(35) / 1000, ltp: q(43) / 100 };
    if ((mode === 2 || mode === 3) && buf.byteLength >= 123) Object.assign(out, { volume: q(67), open: q(91) / 100, high: q(99) / 100, low: q(107) / 100, close: q(115) / 100 });
    if (mode === 3 && buf.byteLength >= 347) {
      // SnapQuote: totals (doubles) at 75/83, OI at 131, best five buy + sell at 147..347 (10 x 20 bytes: flag, qty, price, orders)
      Object.assign(out, { ltq: q(51), tbq: v.getFloat64(75, true), tsq: v.getFloat64(83, true), ltt: q(123), oi: q(131) });
      const lv = [];
      for (let i = 0; i < 10; i++) { const o = 147 + i * 20; lv.push({ flag: v.getUint16(o, true), qty: q(o + 2), price: q(o + 10) / 100, orders: v.getUint16(o + 18, true) }); }
      // the flag's meaning is not documented consistently, so the side is taken from price: the lower five are the bids
      const g0 = lv.filter((x) => x.flag === 0 && x.price > 0), g1 = lv.filter((x) => x.flag !== 0 && x.price > 0);
      const avg = (a) => a.length ? a.reduce((s, x) => s + x.price, 0) / a.length : 0;
      let bids = g0, asks = g1; if (g0.length && g1.length && avg(g0) > avg(g1)) { bids = g1; asks = g0; }
      out.bids = bids.sort((a, b) => b.price - a.price).map((x) => [x.price, x.qty, x.orders]);
      out.asks = asks.sort((a, b) => a.price - b.price).map((x) => [x.price, x.qty, x.orders]);
    }
    return out;
  }
  function toQuote(sym, p) {
    const lp = p.ltp, pc = p.close || 0, v = { symbol: sym, lp, tt: Math.floor(p.t), source: "angel" };
    if ("open" in p) { Object.assign(v, { open_price: p.open, high_price: p.high, low_price: p.low, prev_close_price: pc, volume: p.volume || 0 });
      if (pc) Object.assign(v, { ch: +(lp - pc).toFixed(2), chp: +((lp - pc) / pc * 100).toFixed(2) }); }
    return { n: sym, s: "ok", v };
  }
  function send(action, keys, mode = 2) {
    const by = {}; keys.forEach(([ex, tok]) => (by[ex] = by[ex] || []).push(tok));
    for (const [ex, toks] of Object.entries(by)) for (let i = 0; i < toks.length; i += 500)
      ws.send(JSON.stringify({ correlationID: "mm" + (Date.now() % 1e5), action, params: { mode, tokenList: [{ exchangeType: +ex, tokens: toks.slice(i, i + 500) }] } }));
  }
  function connect(s) {
    return new Promise((res, rej) => {
      const w = new WebSocket(`${WS}?clientCode=${encodeURIComponent(s.client_code)}&feedToken=${encodeURIComponent(s.feed)}&apiKey=${encodeURIComponent(s.api_key)}`);
      w.binaryType = "arraybuffer"; let done = false;
      const to = setTimeout(() => { if (!done) { done = true; try { w.close(); } catch (e) { /* ignore */ } rej(new Error("Angel One's live stream did not accept this browser within 15 s")); } }, 15e3);
      w.onopen = () => { if (done) return; done = true; clearTimeout(to); ST.connected = true; ST.state = "connected"; res(w); };
      w.onerror = () => { if (!done) { done = true; clearTimeout(to); rej(new Error("Angel One's live stream refused this browser")); } };
      w.onclose = (e) => { ST.connected = false; ST.state = "disconnected"; if (ws === w) { ws = null; subs = new Map(); subs3 = new Map(); } err(`stream closed (${e.code})`); };
      w.onmessage = (e) => {
        if (typeof e.data === "string") { if (e.data !== "pong") err(e.data); return; }
        const p = parse(e.data); if (!p || p.ltp <= 0) return;
        const sym = subs.get(p.exch + "|" + p.token); if (!sym) return;
        ST.ticks++; ST.last_tick = Date.now();
        Q.set(sym, { t: Date.now(), row: toQuote(sym, p) });
        if ("volume" in p) cumvol.set(sym, p.volume);
        if (p.bids) window.dispatchEvent(new CustomEvent("mm-angel-depth", { detail: { symbol: sym, ...p, t: Date.now() } }));
        window.dispatchEvent(new CustomEvent("mm-angel-tick", { detail: { symbol: sym, price: p.ltp, t: p.t } }));
      };
    });
  }
  const indiaOpen = () => { const d = new Date(Date.now() + 19800e3), wd = d.getUTCDay(), m = d.getUTCHours() * 60 + d.getUTCMinutes(); return wd > 0 && wd < 6 && m >= 540 && m < 1410; };
  async function tick() {
    clearTimeout(loopT);
    try {
      const c = cfg(), now = Date.now();
      for (const [s, t] of DEPTH) if (now - t > 3 * 60e3) DEPTH.delete(s);
      const want = [...ASKED].filter(([, t]) => now - t < 30 * 60e3).sort((a, b) => b[1] - a[1]).map(([s]) => s).filter((s) => /^(NSE|BSE|MCX):/.test(s));
      if (!configured(c) || c.enabled === false || !indiaOpen() || !want.length || document.visibilityState === "hidden") {
        ST.state = !configured(c) ? "off (not set up)" : c.enabled === false ? "off (switched off)" : !indiaOpen() ? "waiting: market closed" : document.visibilityState === "hidden" ? "paused (page hidden)" : "waiting: no Indian symbols in use";
        if (ws && (!indiaOpen() || c.enabled === false || !configured(c))) stop();
      } else {
        const s = await login(); await loadMap();
        if (!ws) { ws = await connect(s); clearInterval(pingT); pingT = setInterval(() => { try { ws && ws.send("ping"); } catch (e) { /* ignore */ } }, 10e3); }
        const target = new Map(), unmapped = [], deep = new Map();
        for (const sym of DEPTH.keys()) { let k = tokenOf(sym); if (!k && await ensure(sym)) k = tokenOf(sym); if (k) deep.set(k[0] + "|" + k[1], sym); else unmapped.push(sym); }
        for (const sym of want) { if (DEPTH.has(sym)) continue; const k = tokenOf(sym); if (k) { if (target.size < MAX_SYMBOLS) target.set(k[0] + "|" + k[1], sym); } else unmapped.push(sym); }
        const add3 = [...deep.keys()].filter((k) => !subs3.has(k)), drop3 = [...subs3.keys()].filter((k) => !deep.has(k));
        if (drop3.length) { send(0, drop3.map((k) => k.split("|")), 3); drop3.forEach((k) => { subs3.delete(k); if (!target.has(k)) subs.delete(k); }); }
        if (add3.length) { add3.forEach((k) => { subs3.set(k, deep.get(k)); subs.set(k, deep.get(k)); }); send(1, add3.map((k) => k.split("|")), 3); }
        deep.forEach((v, k) => target.set(k, v));
        ST.unmapped = unmapped.slice(0, 20);
        const add = [...target.keys()].filter((k) => !subs.has(k)), drop = [...subs.keys()].filter((k) => !target.has(k));
        if (drop.length) { send(0, drop.map((k) => k.split("|"))); drop.forEach((k) => subs.delete(k)); }
        const add2 = add.filter((k) => !subs3.has(k));
        if (add2.length) { add2.forEach((k) => subs.set(k, target.get(k))); send(1, add2.map((k) => k.split("|"))); }
        ST.subscribed = subs.size;
      }
    } catch (e) { err(e.message || e); ST.state = "error: " + String(e.message || e).slice(0, 120); if (/login|refused|token/i.test(String(e.message))) LS.set("sess", null); stop(); loopT = setTimeout(tick, 120e3); return; }
    loopT = setTimeout(tick, ws ? 30e3 : 20e3);
  }
  function stop() { clearInterval(pingT); if (ws) { try { ws.close(); } catch (e) { /* ignore */ } } ws = null; subs = new Map(); subs3 = new Map(); ST.connected = false; ST.subscribed = 0; }
  const active = () => !!(ST.connected && ST.last_tick && Date.now() - ST.last_tick < 60e3);

  // ------------------------------------------------------------ FYERS /quotes: answer from Angel while fresh
  const fyGet = MM.FY.get;
  MM.FY.get = async (path, params) => {
    if (path !== "/quotes" || !params || !params.symbols) return fyGet(path, params);
    const syms = String(params.symbols).split(",").map((x) => x.trim()).filter(Boolean), now = Date.now();
    syms.forEach((x) => ASKED.set(x, now));
    if (cfg().enabled !== false && configured() && !ws && !loopT) loopT = setTimeout(tick, 500);
    const live = {}; syms.forEach((x) => { const h = Q.get(x); if (h && now - h.t < LIVE_AGE) live[x] = h.row; });
    const miss = syms.filter((x) => !live[x]);
    let rest = { s: "ok", d: [] };
    if (miss.length) rest = await fyGet(path, { ...params, symbols: miss.join(",") });
    const byN = {}; (rest.d || []).forEach((d) => { byN[d.n || (d.v || {}).symbol] = d; });
    return { ...rest, s: "ok", d: syms.map((x) => live[x] || byN[x]).filter(Boolean) };
  };

  // ------------------------------------------------------------ test (Connections page)
  async function test() {
    const out = { login: null, master: null, nifty_token: null, stream: null, ok: false };
    try {
      const s = await login(true); out.login = "ok at " + s.at;
      await loadMap(); out.master = MAP.quick ? `quick list (${MAP.count} index futures looked up; full list blocked in this browser)` : MAP.count + " instruments"; out.nifty_token = tokenOf("NSE:NIFTY50-INDEX");
      const w = ws || await connect(s); const before = ST.ticks; if (!ws) { ws = w; }
      subs.set("1|99926000", "NSE:NIFTY50-INDEX"); send(1, [[1, "99926000"]]);
      await new Promise((r) => setTimeout(r, 4000));
      out.stream = ST.ticks > before ? `ok · ${ST.ticks - before} price updates in 4 s` : indiaOpen() ? "connected, no price yet (try again in a minute)" : "connected (market closed, so no prices now)";
      out.ok = true;
    } catch (e) { out.error = e.message || String(e); }
    return out;
  }

  // a page asks for live market depth of one symbol; call again at least every 2 minutes to keep it, with on=false to stop
  function depth(sym, on = true) {
    sym = String(sym).toUpperCase();
    if (!on) { DEPTH.delete(sym); return { ok: true }; }
    const c = cfg(); if (!configured(c)) return { ok: false, reason: "Angel One is not set up (Connections page)" };
    if (c.enabled === false) return { ok: false, reason: "Angel One is switched off (Connections page)" };
    const fresh = !DEPTH.has(sym); DEPTH.set(sym, Date.now()); ASKED.set(sym, Date.now());
    if (fresh || !ws) { clearTimeout(loopT); loopT = setTimeout(tick, 300); }
    return { ok: true, state: ST.state };
  }
  MM.angel = { depth, futures, lotOf, publicCfg, saveCfg, forget, test, status: () => ({ ...ST, active: active(), config: publicCfg(), last_tick_age_s: ST.last_tick ? Math.round((Date.now() - ST.last_tick) / 100) / 10 : null }), active, totp, tokenOf, buildMap, parse, toQuote, _setMap: (m) => (MAP = m) };
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && configured()) { clearTimeout(loopT); loopT = setTimeout(tick, 1000); } });
})();
