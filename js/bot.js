/* MarketMiner Mobile: Auto paper bot + Telegram alerts (Signal Lab "Auto bot" tab).
 * PAPER ONLY: trades go into Signal Lab's own paper ledger (capital protection, costs, T1/T2/T3 thirds, breakeven after T1).
 * Runs while the Signal Lab page is open. Settings, the Telegram bot token and the bot's memory stay in this browser.
 *  - Entry: a confirmed signal (score >= minimum, from a CLOSED candle) that is fresh (the last closed candle).
 *  - Telegram: setups forming / watch (recommendations), entries, skipped entries, T1/T2 hits, closes, exit warnings.
 *  - In-between exit: a confirmed opposite signal on the same symbol closes the bot's open trade (can be switched off).
 */
(function () {
  "use strict";
  const CK = "mmbot.cfg", SK = "mmbot.state";
  const TFSEC = { "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800 };
  const DEF = { on: false, tf: "15m", every: 60, watchMin: 45, exitOpposite: true, tgToken: "", tgChat: "",
    notify: { reco: true, entry: true, skip: true, tp: true, exit: true, warn: true, charts: true },
    symbols: ["DELTAF:BTCUSD", "DELTAF:ETHUSD", "NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX"] };
  const load = (k, d) => { try { const v = JSON.parse(localStorage.getItem(k) || "null"); return v ? { ...d, ...v } : JSON.parse(JSON.stringify(d)); } catch (e) { return JSON.parse(JSON.stringify(d)); } };
  let cfg = load(CK, DEF); cfg.notify = { ...DEF.notify, ...(cfg.notify || {}) };
  let st = load(SK, { seen: {}, trades: {}, log: [], errs: {} });
  const saveC = () => { try { localStorage.setItem(CK, JSON.stringify(cfg)); } catch (e) { /* ignore */ } };
  const saveS = () => {
    const cut = Date.now() / 1000 - 3 * 86400;                       // forget de-dupe keys after 3 days
    for (const k in st.seen) if (st.seen[k] < cut) delete st.seen[k];
    st.log = st.log.slice(0, 150);
    try { localStorage.setItem(SK, JSON.stringify(st)); } catch (e) { /* ignore */ }
  };
  const M = () => (typeof META !== "undefined" ? META : null);   // Signal Lab settings (a top-level let in lab.html)
  const nowS = () => Math.floor(Date.now() / 1000);
  const short = (s) => String(s).split(":").pop();
  const F = (x) => (typeof f === "function" ? f(x) : String(x));
  const tgEsc = (s) => String(s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  let timer = null, busy = false, lastScan = null, wake = null, lastErr = "";

  // ------------------------------------------------------------------ Telegram
  const tgBase = () => `https://api.telegram.org/bot${encodeURIComponent(cfg.tgToken).replace(/%3A/gi, ":")}`;
  async function tg(text, photo) {
    if (!cfg.tgToken || !cfg.tgChat) return { ok: false, why: "Telegram not set up" };
    if (photo && text.length <= 1024) {
      try {
        const fd = new FormData();
        fd.append("chat_id", cfg.tgChat); fd.append("caption", text); fd.append("parse_mode", "HTML"); fd.append("photo", photo, "chart.png");
        const j = await (await fetch(`${tgBase()}/sendPhoto`, { method: "POST", body: fd })).json();
        if (j.ok) return { ok: true };
        log("err", "Telegram photo: " + (j.description || "refused") + " (sent as text)");
      } catch (e) { log("err", "Telegram photo: " + e.message + " (sent as text)"); }
    }
    const body = new URLSearchParams({ chat_id: cfg.tgChat, text, parse_mode: "HTML", disable_web_page_preview: "true" });
    try {
      const r = await fetch(`https://api.telegram.org/bot${encodeURIComponent(cfg.tgToken).replace(/%3A/gi, ":")}/sendMessage`, { method: "POST", body });
      const j = await r.json().catch(() => ({ ok: r.ok }));
      if (!j.ok) throw new Error(j.description || ("HTTP " + r.status));
      return { ok: true };
    } catch (e) {
      log("err", "Telegram: " + e.message);
      return { ok: false, why: e.message };
    }
  }
  function log(kind, text) { st.log.unshift({ t: nowS(), kind, text }); saveS(); if (paneOn()) rLog(); }
  async function event(kind, text, tgText, pic) {
    log(kind, text);
    if (cfg.notify[kind] === false || !tgText) return;
    let photo = null;
    if (pic && cfg.notify.charts !== false) { try { photo = await pic(); } catch (e) { console.warn("chart picture", e); } }
    await tg(tgText, photo);
  }

  // ------------------------------------------------------------------ chart picture with the Long/Short box (for Telegram)
  async function snap(d, pos) {
    if (!window.LightweightCharts || !window.MMPOS || !d || !d.candles || !d.candles.length) return null;
    const W = 760, H = 430, div = document.createElement("div");
    div.style.cssText = `position:fixed;left:-10000px;top:0;width:${W}px;height:${H}px`;
    document.body.appendChild(div);
    const tz = d.market === "NSE" || d.market === "FNO" || d.market === "MCX" ? 19800 : 0, T = (x) => x + tz;
    const ch = LightweightCharts.createChart(div, { width: W, height: H, layout: { background: { color: "#0d1117" }, textColor: "#cbd5e1", fontFamily: "JetBrains Mono" },
      grid: { vertLines: { color: "#161d2b" }, horzLines: { color: "#161d2b" } }, rightPriceScale: { borderColor: "#263450", scaleMargins: { top: 0.08, bottom: 0.08 } },
      timeScale: { borderColor: "#263450", timeVisible: true, secondsVisible: false, rightOffset: 18 }, crosshair: { mode: 2 },
      watermark: { visible: true, text: `${short(d.symbol)}  ${d.tf}`, color: "rgba(148,163,184,.10)", fontSize: 48 } });
    try {
      const bars = d.candles.slice(-140);
      const lo = Math.min(...bars.map((c) => c.low), pos.stop, ...pos.targets), hi = Math.max(...bars.map((c) => c.high), pos.stop, ...pos.targets);
      const s = ch.addCandlestickSeries({ upColor: "#26a69a", downColor: "#ef5350", wickUpColor: "#26a69a", wickDownColor: "#ef5350", borderVisible: false,
        autoscaleInfoProvider: () => ({ priceRange: { minValue: lo, maxValue: hi } }) });
      s.setData(bars.map((c) => ({ time: T(c.time), open: c.open, high: c.high, low: c.low, close: c.close })));
      ch.timeScale().fitContent();
      await new Promise((r) => setTimeout(r, 80));
      const cv = ch.takeScreenshot(), ctx = cv.getContext("2d"), k = cv.width / W;
      ctx.save(); ctx.scale(k, k);
      ctx.beginPath(); ctx.rect(0, 0, ch.timeScale().width(), H - ch.timeScale().height()); ctx.clip();
      MMPOS.paint(ctx, MMPOS.geometry(ch, s, pos, T, TFSEC[d.tf] || 900));
      ctx.restore();
      return await new Promise((r) => cv.toBlob(r, "image/png"));
    } finally { ch.remove(); div.remove(); }
  }
  const posOf = (s, title) => ({ direction: s.direction, entry: s.entry, stop: s.stop, targets: s.targets, rr: s.rr, time: s.time, title });
  const lv = (s) => `Entry ${F(s.entry)} · SL ${F(s.stop)}\nT1 ${F(s.targets[0])} · T2 ${F(s.targets[1])} · T3 ${F(s.targets[2])}${s.rr ? ` · ${(+s.rr).toFixed(1)}R` : ""}`;
  const arrow = (d) => (d === "LONG" ? "▲" : "▼");

  // ------------------------------------------------------------------ one scan
  async function tick() {
    if (busy || !cfg.on) return;
    busy = true; lastErr = "";
    try {
      const minS = (M() && M().protection && M().protection.min_score) || 60;
      for (const sym of cfg.symbols) {
        let d;
        try { d = await api(`/api/lab/analyze?symbol=${encodeURIComponent(sym)}&tf=${cfg.tf}&live=1`); }
        catch (e) {
          lastErr = `${short(sym)}: ${e.message}`;
          const k = "err:" + sym, t = nowS();
          if (!st.errs[k] || t - st.errs[k] > 6 * 3600) { st.errs[k] = t; await event("warn", `${short(sym)}: no data (${e.message})`, `⚠️ <b>${tgEsc(short(sym))}</b>: data unavailable, the bot skips it until it returns.\n${tgEsc(e.message.slice(0, 200))}`); }
          continue;
        }
        delete st.errs["err:" + sym];
        await handle(sym, d, minS);
      }
      await track();
      lastScan = nowS();
    } catch (e) { lastErr = e.message; log("err", e.message); }
    finally { busy = false; saveS(); if (paneOn()) rStatus(); badge(); }
  }

  const openBotTrade = (sym) => Object.entries(st.trades).find(([, t]) => t.sym === sym && t.status === "open");

  async function handle(sym, d, minS, opt) {
    const trade = !opt || opt.entries !== false;           // false: only alerts for the chart on screen, no entries/exits
    const tfs = TFSEC[d.tf] || 900, t = nowS(), setups = d.setups || [];
    const confirmed = setups.filter((s) => s.signal && !s.provisional && (!s.locked || ((s.hits || 0) === 0 && s.status !== "closed"))
      && s.time >= t - 2.5 * tfs);
    const forming = setups.filter((s) => (!s.signal || s.provisional) && s.score >= cfg.watchMin).sort((a, b) => b.score - a.score);
    const mine = openBotTrade(sym);

    // in-between exit: confirmed opposite signal closes the bot's trade
    if (trade && mine && cfg.exitOpposite) {
      const opp = confirmed.find((s) => s.direction !== mine[1].dir);
      if (opp) {
        const k = `xo|${sym}|${opp.time}`;
        if (!st.seen[k]) {
          st.seen[k] = t;
          try {
            const r = await post(`/api/lab/paper/close/${mine[0]}`, {});
            if (r.ok) {
              mine[1].status = "closing"; mine[1].reason = `opposite ${opp.direction} signal`;
              await event("exit", `#${mine[0]} ${short(sym)} closed on opposite ${opp.direction} signal at ${F(r.exit)}`,
                `🔻 <b>EXIT · opposite signal</b> #${mine[0]} ${tgEsc(short(sym))} ${d.tf}\n${arrow(mine[1].dir)} ${mine[1].dir} closed at ${F(r.exit)}\nReason: ${arrow(opp.direction)} ${opp.direction} confirmed · ${tgEsc(opp.name)} · score ${opp.score.toFixed(0)}`);
            }
          } catch (e) { log("err", `close #${mine[0]}: ${e.message}`); }
        }
      }
    }
    // exit warning: opposite setup forming against an open bot trade
    const still = openBotTrade(sym);
    if (still) {
      const w = forming.find((s) => s.direction !== still[1].dir);
      const k = w && `xw|${sym}|${w.direction}|${w.time}`;
      if (w && !st.seen[k]) {
        st.seen[k] = t;
        await event("warn", `#${still[0]} ${short(sym)}: opposite ${w.direction} setup forming (${w.score.toFixed(0)})`,
          `⚠️ <b>Exit watch</b> #${still[0]} ${tgEsc(short(sym))} ${d.tf}\nOpen ${arrow(still[1].dir)} ${still[1].dir} · an opposite ${arrow(w.direction)} ${w.direction} setup is forming (${tgEsc(w.name)}, score ${w.score.toFixed(0)}).\n${cfg.exitOpposite ? "The bot closes the trade if it confirms on candle close." : "Auto-exit on opposite signals is off."}`);
      }
    }
    // entries
    for (const s of trade ? confirmed : []) {
      const k = `en|${sym}|${d.tf}|${s.strategy}|${s.direction}|${s.time}`;
      if (st.seen[k]) continue;
      st.seen[k] = t;
      const cur = openBotTrade(sym);
      if (cur && cur[1].dir === s.direction) { log("skip", `${short(sym)} ${s.direction}: already in a bot trade (#${cur[0]})`); continue; }
      let r;
      try { r = await post("/api/lab/paper/open", { symbol: d.symbol, tf: d.tf, strategy: s.strategy, direction: s.direction, entry: s.entry, stop: s.stop, targets: s.targets, score: s.score }); }
      catch (e) { r = { ok: false, blocks: [e.message] }; }
      if (r.ok) {
        st.trades[r.id] = { sym: d.symbol, tf: d.tf, dir: s.direction, name: s.name, score: s.score, entry: r.fill, stop: s.stop, targets: s.targets,
          qty: r.qty, cur: r.currency, hit: [], status: "open", opened: t };
        await event("entry", `#${r.id} ${short(sym)} ${s.direction} filled ${F(r.fill)} (${s.name}, ${s.score.toFixed(0)})`,
          `✅ <b>PAPER ENTRY</b> #${r.id} · ${tgEsc(short(sym))} ${d.tf}\n${arrow(s.direction)} <b>${s.direction}</b> filled ${F(r.fill)} · qty ${F(r.qty)} · risk ${F(r.risk)} ${tgEsc(r.currency)}\n${lv(s)}\n${tgEsc(s.name)} · score ${s.score.toFixed(0)}`,
          () => snap(d, { ...posOf(s, `paper #${r.id}`), entry: r.fill }));
        break;                                                         // one entry per symbol per scan
      } else {
        await event("skip", `${short(sym)} ${s.direction} not entered: ${(r.blocks || []).join(" · ")}`,
          `⛔ <b>Entry skipped</b> · ${tgEsc(short(sym))} ${d.tf}\n${arrow(s.direction)} ${s.direction} · ${tgEsc(s.name)} · score ${s.score.toFixed(0)}\nWhy: ${tgEsc((r.blocks || []).join(" · "))}`);
      }
    }
    // recommendations: best forming / watch setup per symbol per candle
    const best = (trade ? forming : [...confirmed.map((s) => ({ ...s, provisional: false })), ...forming]).find((s) => !(still && s.direction !== still[1].dir));
    if (best) {
      const k = `rc|${sym}|${d.tf}|${best.direction}|${best.time}`;
      if (!st.seen[k]) {
        st.seen[k] = t;
        const why = best.provisional ? "live, candle still forming" : `watch: below the entry minimum ${minS}`;
        await event("reco", `${short(sym)} ${best.direction} setup ${best.score.toFixed(0)} (${why})`,
          `👀 <b>Setup</b> · ${tgEsc(short(sym))} ${d.tf}\n${arrow(best.direction)} <b>${best.direction}</b> · ${tgEsc(best.name)} · score ${best.score.toFixed(0)} (${why})\n${lv(best)}\n${trade ? `The bot enters only if it confirms at candle close with score ≥ ${minS}.` : "Chart on screen (not in the bot's markets): alert only, no paper entry."}`,
          () => snap(d, posOf(best, `${best.score.toFixed(0)} ${best.provisional ? "live" : "watch"}`)));
      }
    }
  }

  // ------------------------------------------------------------------ follow the bot's paper trades (T1/T2/T3, stop, closes)
  async function track() {
    const ids = Object.keys(st.trades).filter((id) => st.trades[id].status !== "closed");
    if (!ids.length) return;
    let L;
    try { L = await api("/api/lab/paper"); } catch (e) { log("err", "ledger: " + e.message); return; }
    for (const r of L.trades || []) {
      const b = st.trades[r.id];
      if (!b || b.status === "closed") continue;
      const hit = r.hit || [];
      for (const h of hit) {
        if (b.hit.includes(h)) continue;
        b.hit.push(h);
        if (h === "T3") continue;                                      // reported with the close
        const k = { T1: 0, T2: 1 }[h];
        const left = h === "T1" ? "2/3 left" : "1/3 left";
        await event("tp", `#${r.id} ${short(b.sym)} ${h} hit ${F(b.targets[k])}`,
          `🎯 <b>${h} HIT</b> #${r.id} · ${tgEsc(short(b.sym))} ${b.tf}\n${arrow(b.dir)} ${b.dir} · ${h} ${F(b.targets[k])} · booked 1/3, ${left}\n${h === "T1" ? `Stop moved to breakeven ${F(r.stop_now)}` : `Stop now ${F(r.stop_now)}`}`);
      }
      if (r.status === "closed") {
        b.status = "closed";
        const pnl = r.pnl || 0, rr = r.r_result || 0;
        const why = b.reason || r.exit_reason;
        const icon = r.exit_reason === "T3" ? "🏆" : pnl >= 0 ? "🏁" : "🛑";
        await event("exit", `#${r.id} ${short(b.sym)} closed: ${why} · ${pnl >= 0 ? "+" : ""}${F(pnl)} ${r.currency}`,
          `${icon} <b>CLOSED</b> #${r.id} · ${tgEsc(short(b.sym))} ${b.tf}\n${arrow(b.dir)} ${b.dir} · ${tgEsc(why)} · avg exit ${F(r.exit)}\nP&amp;L ${pnl >= 0 ? "+" : ""}${F(pnl)} ${tgEsc(r.currency)} · ${rr >= 0 ? "+" : ""}${rr.toFixed(2)}R (after costs)`);
      }
    }
    for (const id of ids) if (st.trades[id].status === "closing") st.trades[id].status = "open";   // wait for the ledger to show it closed
  }

  // ------------------------------------------------------------------ run / stop, screen awake
  async function keepAwake() {
    try { if (cfg.on && "wakeLock" in navigator && document.visibilityState === "visible" && !wake) { wake = await navigator.wakeLock.request("screen"); wake.addEventListener("release", () => { wake = null; if (paneOn()) rStatus(); }); } } catch (e) { wake = null; }
  }
  function schedule() { clearInterval(timer); timer = null; if (cfg.on) { timer = setInterval(tick, Math.max(30, cfg.every) * 1000); setTimeout(tick, 1500); } }
  function setOn(on) {
    cfg.on = on; saveC(); schedule();
    if (on) { keepAwake(); log("info", "Bot started"); tg(`🤖 <b>MarketMiner bot started</b>\n${cfg.tf} · ${cfg.symbols.map(short).map(tgEsc).join(", ")}\nPaper trading only.`); }
    else { try { wake && wake.release(); } catch (e) { /* ignore */ } wake = null; log("info", "Bot stopped"); }
    rBot(); badge();
  }
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && cfg.on) { keepAwake(); setTimeout(tick, 800); } });

  // ------------------------------------------------------------------ UI
  const $ = (id) => document.getElementById(id);
  (function css() { const c = document.createElement("style"); c.textContent = `
    #p-bot .bot label{display:flex;flex-direction:row;align-items:center;gap:6px;font:600 .74rem var(--mono);color:var(--t2);text-transform:none}
    #p-bot .bot label.fld{flex-direction:column;align-items:flex-start;gap:3px;font-size:.64rem;color:var(--t3);text-transform:uppercase}
    #p-bot .bot input:not([type=checkbox]),#p-bot .bot select{background:var(--bg0);border:1px solid var(--bdr2);color:var(--t1);padding:6px 9px;border-radius:6px;font:600 .8rem var(--mono);max-width:100%}
    #p-bot .bot input[type=checkbox]{width:16px;height:16px;accent-color:var(--gd);flex:none}`; document.head.appendChild(c); })();
  const paneOn = () => { const p = $("p-bot"); return p && p.classList.contains("on"); };
  const E = (s) => (typeof esc === "function" ? esc(s) : String(s));
  const when = (t) => (t ? new Date(t * 1000).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—");
  function badge() { const b = document.querySelector('.tabs button[data-k="bot"]'); if (b) b.innerHTML = `Auto bot ${cfg.on ? '<span class="up">●</span>' : ""}`; }
  function allSymbols() {
    const g = { ...((M() && M().watch) || {}) };
    for (const k of Object.keys(g)) if (/PC only/i.test(k)) delete g[k];
    if (M() && M().user_watch && M().user_watch.length) g["My watch-list"] = M().user_watch.filter((s) => !/^YF:/i.test(s));
    return g;
  }
  function rStatus() {
    const el = $("botStat"); if (!el) return;
    const next = cfg.on && lastScan ? lastScan + Math.max(30, cfg.every) : null;
    el.innerHTML = `<div class="hr" style="justify-content:space-between">
        <div><b class="${cfg.on ? "up" : "nt"}" style="font:800 1rem var(--mono)">${cfg.on ? "● RUNNING" : "○ STOPPED"}</b>
        <span class="nt" style="margin-left:8px">last scan ${when(lastScan)}${busy ? ' · <span class="spin"></span> scanning' : ""}${next ? " · next ~" + when(next).split(", ").pop() : ""}</span></div>
        <button class="btn ${cfg.on ? "" : "gold"}" onclick="MMBOT.setOn(${!cfg.on})">${cfg.on ? "■ Stop bot" : "▶ Start bot"}</button></div>
      <div class="nt" style="margin-top:8px;line-height:1.5">${cfg.on ? (wake ? "Screen kept awake. " : "") : ""}The bot works only while this page is open and the screen is on (phones pause hidden pages). After a pause it catches up: T1/T2/stop hits are worked out from 1-minute candles, but alerts arrive late and signals older than about two candles are not entered. Paper trades only, never a real order.${lastErr ? `<br><span class="am">Last problem: ${E(lastErr)}</span>` : ""}</div>`;
  }
  function rLog() {
    const el = $("botLog"); if (!el) return;
    const cls = { entry: "up", exit: "gd", tp: "up", skip: "am", warn: "am", err: "dn", reco: "cy", info: "nt" };
    el.innerHTML = st.log.length ? st.log.slice(0, 80).map((l) => `<div style="padding:4px 0;border-bottom:1px solid var(--bdr);font:500 .72rem var(--mono)"><span class="nt">${when(l.t)}</span> <span class="${cls[l.kind] || ""}">${E(l.kind.toUpperCase())}</span> ${E(l.text)}</div>`).join("") : '<div class="nt">Nothing yet.</div>';
    const ot = $("botOpen");
    if (ot) {
      const open = Object.entries(st.trades).filter(([, t]) => t.status !== "closed");
      ot.innerHTML = open.length ? open.map(([id, t]) => `<div style="padding:5px 0;border-bottom:1px solid var(--bdr);font:600 .74rem var(--mono)"><span class="${t.dir === "LONG" ? "up" : "dn"}">#${id} ${arrow(t.dir)} ${t.dir}</span> ${E(short(t.sym))} ${E(t.tf)} · entry ${F(t.entry)} · SL ${F(t.stop)} · T1 ${F(t.targets[0])} · T2 ${F(t.targets[1])} · T3 ${F(t.targets[2])}${t.hit.length ? ` · <span class="up">${t.hit.join(" ")} hit</span>` : ""}</div>`).join("") : '<div class="nt">No open bot trades.</div>';
    }
  }
  function rBot() {
    const p = $("p-bot"); if (!p) return;
    const groups = allSymbols(), minS = (M() && M().protection && M().protection.min_score) || 60;
    const tfs = Object.keys(TFSEC).filter((t) => t !== "1w");
    const chk = (k, l) => `<label style="display:flex;gap:6px;align-items:center"><input type="checkbox" ${cfg.notify[k] ? "checked" : ""} onchange="MMBOT.set('notify.${k}',this.checked)">${l}</label>`;
    p.innerHTML = `
    <div class="cd" style="margin-bottom:10px" id="botStat"></div>
    <div class="cd" style="margin-bottom:10px"><h3><span>Open bot trades</span><span class="nt">paper</span></h3><div id="botOpen"></div></div>
    <div class="cd bot" style="margin-bottom:10px"><h3><span>Rules</span></h3>
      <div class="hr" style="gap:12px;margin-bottom:10px">
        <label class="fld">Timeframe <select onchange="MMBOT.set('tf',this.value)">${tfs.map((t) => `<option ${t === cfg.tf ? "selected" : ""}>${t}</option>`).join("")}</select></label>
        <label class="fld">Scan every <select onchange="MMBOT.set('every',+this.value)">${[30, 60, 120, 300].map((s) => `<option value="${s}" ${s === cfg.every ? "selected" : ""}>${s < 60 ? s + " s" : s / 60 + " min"}</option>`).join("")}</select></label>
        <label class="fld">Alert setups from score <input type="number" min="0" max="100" style="width:70px" value="${cfg.watchMin}" onchange="MMBOT.set('watchMin',+this.value)"></label>
      </div>
      <div class="nt" style="line-height:1.55;margin-bottom:8px">Enters a paper trade on a <b>confirmed</b> signal: score ≥ <b>${minS}</b> (Settings tab) on a <b>closed</b> candle. Setups that are still forming or score below that are sent as recommendations only. Capital protection (max open trades, daily/weekly loss limits, no averaging) still applies. Exits: a third each at T1, T2, T3; stop moves to breakeven after T1.</div>
      <label style="display:flex;gap:6px;align-items:center"><input type="checkbox" ${cfg.exitOpposite ? "checked" : ""} onchange="MMBOT.set('exitOpposite',this.checked)">Close a bot trade early when an opposite signal confirms (in-between exit)</label>
      <h3 style="margin-top:14px"><span>Markets</span><span class="nt">${cfg.symbols.length} selected</span></h3>
      ${Object.entries(groups).map(([g, l]) => `<div style="margin:6px 0"><div class="nt" style="font:700 .68rem var(--mono);text-transform:uppercase;margin-bottom:4px">${E(g)}</div><div class="hr" style="gap:10px">${l.map((s) => `<label style="display:flex;gap:5px;align-items:center;font:600 .74rem var(--mono)"><input type="checkbox" ${cfg.symbols.includes(s) ? "checked" : ""} onchange="MMBOT.sym('${E(s)}',this.checked)">${E(short(s))}</label>`).join("")}</div></div>`).join("")}
      <div class="nt" style="margin-top:6px">Indian symbols need FYERS connected (renew each morning) and only trade in market hours. US/global symbols are PC-only.</div>
    </div>
    <div class="cd bot" style="margin-bottom:10px"><h3><span>Telegram alerts</span><span class="${cfg.tgToken && cfg.tgChat ? "up" : "am"}">${cfg.tgToken && cfg.tgChat ? "set up" : "not set up"}</span></h3>
      <ol class="nt" style="line-height:1.6;margin:0 0 10px 18px">
        <li>In Telegram, open <b>@BotFather</b>, send <span class="mono">/newbot</span>, pick a name. Copy the <b>token</b> it gives you.</li>
        <li>Open your new bot in Telegram and send it any message (e.g. "hi").</li>
        <li>Paste the token below and tap <b>Find my chat ID</b>, then <b>Send test</b>.</li></ol>
      <div class="hr" style="gap:8px;margin-bottom:8px">
        <input id="tgTok" type="password" placeholder="Bot token, or setup code token|chatID" style="flex:1;min-width:220px" value="${E(cfg.tgToken)}" onchange="MMBOT.set('tgToken',this.value.trim())">
        <input id="tgChat" placeholder="Chat ID" style="width:150px" value="${E(cfg.tgChat)}" onchange="MMBOT.set('tgChat',this.value.trim())">
      </div>
      <div class="hr" style="gap:8px;margin-bottom:10px"><button class="btn sm" onclick="MMBOT.findChat()">Find my chat ID</button><button class="btn sm gold" onclick="MMBOT.test()">Send test</button><span id="tgMsg" class="nt"></span></div>
      <div class="hr" style="gap:12px">${chk("reco", "Setup recommendations")}${chk("entry", "Entries")}${chk("skip", "Skipped entries")}${chk("tp", "T1 / T2 hits")}${chk("exit", "Exits & closes")}${chk("warn", "Exit warnings & data problems")}${chk("charts", "Chart picture with long/short box")}</div>
      <div class="nt" style="margin-top:8px">The token stays in this browser only. Anyone with the token can post as your bot, so don't share it.</div>
    </div>
    <div class="cd"><h3><span>Bot log</span><button class="btn sm" onclick="MMBOT.clearLog()">Clear</button></h3><div id="botLog" class="scroll" style="max-height:420px"></div></div>`;
    rStatus(); rLog();
  }

  // the chart on screen in Signal Lab: alert its setups too (entries only if it is one of the bot's markets + timeframe)
  let viewBusy = false;
  async function onView(d) {
    if (!cfg.on || viewBusy || !d || !d.symbol) return;
    viewBusy = true;
    try {
      const minS = (M() && M().protection && M().protection.min_score) || 60;
      await handle(d.symbol, d, minS, { entries: cfg.symbols.includes(d.symbol) && d.tf === cfg.tf });
      saveS();
    } finally { viewBusy = false; }
  }

  window.MMBOT = {
    setOn, render: rBot, tick, onView, snap, cfg: () => cfg, state: () => st,
    set(k, v) {
      const combo = k === "tgToken" && /^\s*(\d+:[\w-]+)\s*[|,\s]\s*(-?\d+)\s*$/.exec(v || "");   // setup code "token|chatId"
      if (combo) { cfg.tgToken = combo[1]; cfg.tgChat = combo[2]; }
      else if (k.startsWith("notify.")) cfg.notify[k.slice(7)] = v; else cfg[k] = v;
      saveC(); if (k === "every") schedule(); if (k === "tgToken" || k === "tgChat") rBot();
    },
    sym(s, on) { cfg.symbols = on ? [...new Set([...cfg.symbols, s])] : cfg.symbols.filter((x) => x !== s); saveC(); },
    clearLog() { st.log = []; saveS(); rLog(); },
    async test() {
      const m = $("tgMsg"); m.textContent = "Sending…";
      const r = await tg(`🤖 <b>MarketMiner test</b>\nAlerts will arrive here: setups, paper entries, T1/T2/T3 hits and exits.`);
      m.innerHTML = r.ok ? '<span class="up">Sent ✓ check Telegram</span>' : `<span class="dn">${E(r.why)}</span>`;
    },
    async findChat() {
      const m = $("tgMsg");
      if (!cfg.tgToken) { m.innerHTML = '<span class="am">Paste the bot token first</span>'; return; }
      m.textContent = "Looking…";
      try {
        const j = await (await fetch(`https://api.telegram.org/bot${cfg.tgToken}/getUpdates`)).json();
        if (!j.ok) throw new Error(j.description || "Telegram refused the token");
        const u = (j.result || []).reverse().find((x) => (x.message || x.channel_post || {}).chat);
        if (!u) { m.innerHTML = '<span class="am">No messages yet: send your bot a message in Telegram, then try again</span>'; return; }
        const c = (u.message || u.channel_post).chat;
        cfg.tgChat = String(c.id); saveC(); rBot();
        $("tgMsg").innerHTML = `<span class="up">Found ${E(c.first_name || c.title || "")} (${E(c.id)}) ✓</span>`;
      } catch (e) { m.innerHTML = `<span class="dn">${E(e.message)}</span>`; }
    },
  };

  // start automatically if it was running, once Signal Lab has loaded its settings
  const boot = setInterval(() => { if (M() && typeof api === "function") { clearInterval(boot); badge(); if (cfg.on) { schedule(); keepAwake(); } } }, 1000);
})();
