/* MarketMiner Mobile: the analysis engine (Pyodide) in a background thread (Web Worker).
 * Started by js/lab.js. Running Python here instead of on the page means the page never freezes while the engine
 * downloads, compiles and imports its code (this was the "slow loading", most of all in Safari on a Mac).
 * Python's data calls (FYERS, Delta, Binance...) are passed to the page, which does them with its own clients and
 * sends the answer back. The Signal Lab database is SQLite in this browser's IndexedDB, as before.
 * PAPER TRADING ONLY: nothing here can place an order.
 */
"use strict";
let PY = null, V = "", IDX = "", agentSync = null, bid = 0;
const waits = new Map();
const say = (m) => postMessage({ t: "say", m });
const sync = (populate) => new Promise((res) => { try { PY.FS.syncfs(populate, (err) => res(err)); } catch (e) { res(e); } });

// bridge: every call goes to the page (js/lab.js) and comes back as a JSON string
const NAMES = ["fyGet", "fyUsage", "resolve", "deltaGet", "binanceKlines", "cryptoOptions", "yahoo"];
const bridge = {};
for (const name of NAMES) {
  bridge[name] = (...args) => new Promise((res) => { const id = ++bid; waits.set(id, res); postMessage({ t: "bridge", id, name, args: args.map(String) }); });
}

async function boot() {
  say("Downloading the analysis engine (about 10 MB the first time, then cached)…");
  importScripts(IDX + "pyodide.js");
  const py = await loadPyodide({ indexURL: IDX });
  say("Loading SQLite…");
  await py.loadPackage("sqlite3");
  py.FS.mkdirTree("/data");
  PY = py;
  try { py.FS.mount(py.FS.filesystems.IDBFS, {}, "/data"); await sync(true); } catch (e) { console.warn("Signal Lab: device storage unavailable", e); }
  say("Loading Signal Lab code…");
  const bundle = await (await fetch(`../py/bundle.json?v=${V}`)).json();
  for (const [f, text] of Object.entries(bundle)) { const path = "/app/" + f; py.FS.mkdirTree(path.slice(0, path.lastIndexOf("/"))); py.FS.writeFile(path, text); }
  py.registerJsModule("mmbridge", bridge);
  await py.runPythonAsync("import sys\nsys.path.insert(0, '/app')\nimport mobile_api");
  say(null);
}

let ML = null;
const needML = () => (ML = ML || PY.loadPackage(["numpy", "scikit-learn"]).catch((e) => console.warn("scikit-learn", e)));

async function agent() {
  try { await PY.runPythonAsync("mobile_api.start_agent()"); } catch (e) { console.warn("agent start", e); }
  let mode = "off";
  try { mode = String(await PY.runPythonAsync("from backend.agent import ml as _ml\n_ml.cfg()['mode']")); } catch (e) { /* ignore */ }
  if (mode !== "off") needML();
  if (!agentSync) agentSync = setInterval(() => sync(false), 30000);   // the agent writes in the background
}

let n = 0, ready = null;
onmessage = async (ev) => {
  const m = ev.data || {};
  if (m.t === "bres") { const r = waits.get(m.id); waits.delete(m.id); if (r) r(m.raw); return; }
  if (m.t === "init") {
    V = m.v; IDX = m.idx;
    ready = boot().then(() => (m.agent ? agent() : null)).then(() => postMessage({ t: "ready" }))
      .catch((e) => { postMessage({ t: "fail", m: String((e && e.message) || e) }); throw e; });
    return;
  }
  if (m.t === "sync") { if (PY) await sync(false); postMessage({ t: "synced", id: m.id }); return; }
  if (m.t === "req") {
    let out;
    try {
      await ready;
      if (/^\/api\/lab\/agent\/(ml|workbench)/.test(m.path)) await needML();
      const k = "_mm_req_" + (++n);
      PY.globals.set(k, JSON.stringify({ path: m.path, query: m.query, method: m.method, body: m.body }));
      try { out = await PY.runPythonAsync(`await mobile_api.handle_json(${k})`); }
      finally { try { PY.globals.delete(k); } catch (e) { /* ignore */ } }
      postMessage({ t: "res", id: m.id, out: String(out) });
    } catch (e) {
      postMessage({ t: "res", id: m.id, err: String((e && e.message) || e) });
    }
  }
};
