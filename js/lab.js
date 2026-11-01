/* MarketMiner Mobile: Signal Lab engine.
 * The PC runs Signal Lab as Python (backend/lab). The phone runs the SAME Python files in the browser with Pyodide
 * (Python compiled to WebAssembly, downloaded once from cdn.jsdelivr.net and then cached by the browser).
 * This file loads it, gives Python a bridge to the phone's data clients (FYERS, Delta Exchange, Binance) and answers
 * /api/lab/* requests. The Signal Lab database (journal, paper trades, alerts, backtests) is SQLite stored in this
 * browser's IndexedDB, on this device only. PAPER TRADING ONLY: nothing here can place an order.
 */
(function () {
  "use strict";
  const MM = window.MM, HE = MM.HE;
  const V = MM.version;
  const PYV = "0.26.4", IDX = `https://cdn.jsdelivr.net/pyodide/v${PYV}/full/`;
  const DL = "https://api.india.delta.exchange", BN = "https://data-api.binance.vision";

  // ---------------------------------------------------------------- bridge: Python -> phone data clients (JSON in, JSON out)
  const wrap = (fn) => async (...args) => {
    try { return JSON.stringify({ ok: true, data: await fn(...args.map((a) => JSON.parse(String(a)))) }); }
    catch (e) { return JSON.stringify({ ok: false, status: e.status || 502, message: e.message || String(e) }); }
  };
  const bridge = {
    fyGet: wrap((path, params) => MM.FY.get(path, params)),
    resolve: wrap(async (sym) => { if (!MM.deskResolve) throw new HE(503, "Contract resolver not loaded"); return MM.deskResolve(sym); }),
    deltaGet: wrap((path, params) => MM.getJ(DL + path + "?" + new URLSearchParams(params))),
    binanceKlines: wrap(async (pair, interval, limit, endMs) => {
      const p = { symbol: pair, interval, limit }; if (endMs != null) p.endTime = endMs;
      const r = await MM.getJ(`${BN}/api/v3/klines?` + new URLSearchParams(p));
      if (!Array.isArray(r)) throw new HE(502, "Binance returned an unexpected klines payload");
      return r.map((x) => [Math.floor(x[0] / 1000), +x[1], +x[2], +x[3], +x[4], +x[5]]);
    }),
    cryptoOptions: wrap((coin, expiry, width) => MM.cryptoOptions(coin, expiry, width)),
    yahoo: wrap(async (sym, iv, rng) => {
      try { return await MM.getJ(`https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(sym)}?interval=${iv}&range=${rng}&includePrePost=false`); }
      catch (e) { throw new HE(503, "Yahoo Finance does not accept requests from a phone browser. US, global and forex symbols work in the PC version."); }
    }),
  };

  // ---------------------------------------------------------------- loader
  const say = (m) => window.dispatchEvent(new CustomEvent("mm-lab-status", { detail: m }));
  const script = (src) => new Promise((res, rej) => { const s = document.createElement("script"); s.src = src; s.onload = res;
    s.onerror = () => rej(new HE(503, "Could not download the analysis engine (Pyodide). Check the internet connection and reload.")); document.head.appendChild(s); });
  const sync = (py, populate) => new Promise((res) => { try { py.FS.syncfs(populate, (err) => res(err)); } catch (e) { res(e); } });
  let boot = null, PY = null, n = 0, persistT = null;

  function start() {
    if (boot) return boot;
    boot = (async () => {
      say("Downloading the analysis engine (about 10 MB the first time, then cached)…");
      if (!window.loadPyodide) await script(IDX + "pyodide.js");
      const py = await window.loadPyodide({ indexURL: IDX });
      say("Loading SQLite…");
      await py.loadPackage("sqlite3");
      py.FS.mkdirTree("/data");
      try { py.FS.mount(py.FS.filesystems.IDBFS, {}, "/data"); await sync(py, true); } catch (e) { console.warn("Signal Lab: device storage unavailable, data lasts until the page closes", e); }
      say("Loading Signal Lab code…");
      const files = await (await fetch(`py/files.json?v=${V}`, { cache: "no-cache" })).json();
      await Promise.all(files.map(async (f) => {
        const r = await fetch(`py/${f}?v=${V}`, { cache: "no-cache" });
        if (!r.ok) throw new HE(503, "Missing Signal Lab file " + f);
        const path = "/app/" + f; py.FS.mkdirTree(path.slice(0, path.lastIndexOf("/"))); py.FS.writeFile(path, await r.text());
      }));
      py.registerJsModule("mmbridge", bridge);
      await py.runPythonAsync("import sys\nsys.path.insert(0, '/app')\nimport mobile_api");
      PY = py; say(null);
      return py;
    })();
    boot.catch((e) => { boot = null; say("Signal Lab engine failed to start: " + e.message); });
    return boot;
  }
  function persist() { clearTimeout(persistT); persistT = setTimeout(() => PY && sync(PY, false), 1200); }

  async function route(p, q, method, body) {
    const py = await start();
    const k = "_mm_req_" + (++n);
    py.globals.set(k, JSON.stringify({ path: p, query: Object.fromEntries(q), method, body }));
    let out;
    try { out = JSON.parse(await py.runPythonAsync(`await mobile_api.handle_json(${k})`)); }
    finally { try { py.globals.delete(k); } catch (e) { /* ignore */ } }
    persist();
    if (out.status >= 400) throw new HE(out.status, out.body.detail);
    return out.body;
  }
  MM.lab = route;
  MM.labStart = start;

  // ---------------------------------------------------------------- recorder: runs while the Signal Lab page is open
  let recT = null;
  async function recorderTick() {
    try {
      const m = await route("/api/lab/meta", new URLSearchParams(), "GET", {});
      if (m.recorder && m.recorder.enabled) await route("/api/lab/recorder/run", new URLSearchParams(), "POST", {});
      clearTimeout(recT); recT = setTimeout(recorderTick, Math.max(5, (m.recorder && m.recorder.interval_min) || 15) * 60e3);
    } catch (e) { clearTimeout(recT); recT = setTimeout(recorderTick, 15 * 60e3); }
  }
  setTimeout(() => start().then(() => { recT = setTimeout(recorderTick, 60e3); }).catch(() => {}), 0);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden" && PY) sync(PY, false); });
})();
