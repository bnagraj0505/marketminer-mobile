/* MarketMiner: Market Structure Break & Order Blocks (MSB, BU-OB, BU-BB, BU-MB, BE-OB, BE-BB, BE-MB).
 * Data from /api/lab/msb and /api/lab/msb/scan (backend/lab/msb.py, closed candles only, nothing repaints).
 *
 *   MMMSB.attach(chart, series, {box, plot, bar, symbol: () => sym, tf: () => "5m", tz: () => 19800})
 *       draws the zones (boxes extending right until broken) and MSB lines on a lightweight-charts chart
 *   MMMSB.pane(el, {groups: () => ({...}), open: (sym, tf) => {}})
 *       the scanner: every symbol's structure, the zone price is in, confirmed retests; Telegram alerts through the
 *       Auto bot's Telegram settings (MMBOT.tg). Drawing and alerts only - no orders.
 */
(function () {
  "use strict";
  const COL = { bullish: [8, 153, 129], bearish: [242, 54, 69] };
  const KIND_A = { OB: 0.2, BB: 0.13, MB: 0.13 };
  const KEY = "mmmsb.on", SKEY = "mmmsb.scan", SEEN = "mmmsb.seen";
  const TFSEC = { "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400 };
  const ls = {
    get(k, d) { try { const v = JSON.parse(localStorage.getItem(k) || "null"); return v == null ? d : (typeof d === "object" && !Array.isArray(d) ? { ...d, ...v } : v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
  };
  const num = (x) => (x == null || isNaN(x) ? "—" : Number(x).toLocaleString("en-IN", { maximumFractionDigits: Math.abs(x) >= 1000 ? 1 : Math.abs(x) >= 10 ? 2 : 4 }));
  const E = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const short = (s) => (window.MMK && MMK.label ? MMK.label(s) : String(s).split(":").pop());
  const cache = {};

  async function get(path) {
    const r = await fetch(path);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || r.statusText);
    return j;
  }
  async function load(symbol, tf, force) {
    const k = symbol + "|" + tf, c = cache[k];
    if (!force && c && c.d && Date.now() - c.t < 45000) return c.d;
    if (c && c.p) return c.p;
    const p = get(`/api/lab/msb?symbol=${encodeURIComponent(symbol)}&tf=${encodeURIComponent(tf)}`)
      .then((d) => { cache[k] = { t: Date.now(), d }; return d; })
      .finally(() => { if (cache[k]) delete cache[k].p; });
    cache[k] = { ...(c || { t: 0 }), p };
    return p;
  }

  // ------------------------------------------------------------------ chart overlay
  function tag(ctx, text, x, y, bg) {
    ctx.font = "700 10px 'JetBrains Mono', ui-monospace, monospace";
    const w = ctx.measureText(text).width + 8, h = 14;
    ctx.fillStyle = bg; ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(x, y, w, h, 3); else ctx.rect(x, y, w, h);
    ctx.fill(); ctx.fillStyle = "#fff"; ctx.textBaseline = "middle"; ctx.fillText(text, x + 4, y + h / 2 + 0.5);
  }
  function paint(ctx, g) {
    const d = g.data, ts = g.chart.timeScale(), plotW = ts.width(), on = g.on;
    const X = (t) => { const x = ts.timeToCoordinate(t + g.tz); return x == null ? null : x; };
    (d.zones || []).forEach((z) => {
      const k = z.kind.split("-")[1];
      if (!on[k] || (z.state === "broken" && !on.broken)) return;
      const yt = g.series.priceToCoordinate(z.top), yb = g.series.priceToCoordinate(z.bottom);
      if (yt == null || yb == null) return;
      const y0 = Math.min(yt, yb), h = Math.max(2, Math.abs(yb - yt));
      if (y0 > g.H || y0 + h < 0) return;
      let x0 = X(z.time); if (x0 == null || x0 < 0) x0 = 0;
      let x1 = z.broken_time ? X(z.broken_time) : plotW; if (x1 == null) x1 = plotW;
      if (x1 <= 0) return;
      const [r, gg, b] = COL[z.direction], dim = z.state === "broken" ? 0.45 : 1;
      ctx.fillStyle = `rgba(${r},${gg},${b},${KIND_A[k] * dim})`;
      ctx.fillRect(x0, y0, x1 - x0, h);
      ctx.strokeStyle = `rgba(${r},${gg},${b},${0.8 * dim})`; ctx.lineWidth = 1;
      ctx.setLineDash(k === "OB" ? [] : [4, 3]); ctx.strokeRect(x0 + 0.5, y0 + 0.5, x1 - x0 - 1, h - 1); ctx.setLineDash([]);
      const txt = z.kind + (z.retests ? ` ✓${z.retests}` : "") + (z.state === "broken" ? " ✗" : "");
      tag(ctx, txt, Math.max(2, x0 + 2), z.direction === "bullish" ? Math.min(g.H - 16, y0 + h + 2) : Math.max(1, y0 - 16), `rgba(${r},${gg},${b},${0.9 * dim})`);
    });
    if (on.MSB) (d.msb || []).forEach((m) => {
      const y = g.series.priceToCoordinate(m.level), x0 = X(m.from_time), x1 = X(m.time);
      if (y == null || x1 == null) return;
      const a = x0 == null ? 0 : x0;
      const [r, gg, b] = COL[m.direction];
      ctx.strokeStyle = `rgba(${r},${gg},${b},.95)`; ctx.lineWidth = 1.5; ctx.setLineDash([6, 3]);
      ctx.beginPath(); ctx.moveTo(a, y + 0.5); ctx.lineTo(x1, y + 0.5); ctx.stroke(); ctx.setLineDash([]);
      tag(ctx, "MSB", Math.max(2, (a + x1) / 2 - 14), m.direction === "bullish" ? y - 16 : y + 2, `rgba(${r},${gg},${b},.95)`);
    });
  }
  function renderBar(st) {
    if (!st.bar) return;
    const on = st.on, d = st.data;
    const chip = (k, l) => `<button type="button" data-mb="${k}" style="font:600 .66rem 'JetBrains Mono',monospace;padding:2px 7px;border-radius:5px;cursor:pointer;border:1px solid ${on[k] ? "#6366f1" : "rgba(148,163,184,.4)"};background:${on[k] ? "rgba(99,102,241,.18)" : "transparent"};color:inherit">${l}</button>`;
    const info = st.err ? "unavailable: " + E(String(st.err).slice(0, 80)) : st.loading ? "loading…"
      : d ? `${E(d.tf)} · structure <b style="color:${d.structure === "bullish" ? "#089981" : d.structure === "bearish" ? "#f23645" : "inherit"}">${E(d.structure)}</b> · ${d.active} active zone(s)` : "";
    st.bar.innerHTML = `<span style="display:inline-flex;gap:4px;align-items:center;flex-wrap:wrap;font-size:.66rem">
      <label style="display:inline-flex;gap:4px;align-items:center;cursor:pointer"><input type="checkbox" data-mb="master" ${on.master ? "checked" : ""}>MSB / OB</label>
      ${chip("MSB", "MSB")}${chip("OB", "OB")}${chip("MB", "MB")}${chip("BB", "BB")}${chip("broken", "broken")}
      <span style="opacity:.7">${info}</span></span>`;
    st.bar.querySelectorAll("[data-mb]").forEach((el) => {
      el.onclick = (ev) => {
        const k = el.dataset.mb;
        if (k === "master") on.master = el.checked; else { on[k] = !on[k]; ev.preventDefault(); }
        ls.set(KEY, on); st.key = ""; renderBar(st);
        if (on.master && !st.data) st.sym = null;
      };
    });
  }
  function attach(chart, series, o) {
    const box = o.box, plot = o.plot || o.box;
    const cv = document.createElement("canvas");
    cv.style.cssText = "position:absolute;top:0;left:0;z-index:1;pointer-events:none";
    box.appendChild(cv);
    const st = { chart, series, bar: o.bar, on: ls.get(KEY, { master: true, MSB: true, OB: true, MB: true, BB: true, broken: false }), data: null, sym: null, key: "", err: null, loading: false, alive: true };
    const want = () => { const s = o.symbol(), t = (o.tf && o.tf()) || "5m"; return s ? s + "|" + t : null; };
    async function refresh(force) {
      const w = want(); if (!w || !st.on.master) return;
      const [s, t] = w.split("|");
      st.loading = !st.data; renderBar(st);
      try { const d = await load(s, t, force); if (want() === w) { st.data = d; st.err = null; st.key = ""; } }
      catch (e) { if (want() === w) { st.data = null; st.err = e.message; } }
      st.loading = false; renderBar(st);
    }
    function frame() {
      if (!st.alive) return;
      try {
        if (!document.body.contains(box)) { st.alive = false; return; }
        const w = want();
        if (w && w !== st.sym && st.on.master) { st.sym = w; st.data = null; refresh(false); }
        if (document.visibilityState === "visible") {
          const W = plot.clientWidth, H = Math.max(0, plot.clientHeight - chart.timeScale().height());
          const tz = (o.tz && o.tz()) || 0, vr = chart.timeScale().getVisibleLogicalRange();
          const k = [W, H, tz, JSON.stringify(st.on), vr && vr.from.toFixed(2), vr && vr.to.toFixed(2), series.coordinateToPrice(0), series.coordinateToPrice(H),
            st.data && st.data.symbol, st.data && st.data.tf, st.data && st.data.last_time].join("|");
          if (k !== st.key) {
            st.key = k;
            const r = window.devicePixelRatio || 1;
            if (st.cw !== W * r || st.ch !== H * r) { st.cw = cv.width = W * r; st.ch = cv.height = H * r; cv.style.width = W + "px"; cv.style.height = H + "px"; }
            const ctx = cv.getContext("2d"); ctx.setTransform(r, 0, 0, r, 0, 0); ctx.clearRect(0, 0, W, H);
            if (st.on.master && st.data && st.data.symbol + "|" + st.data.tf === w) {
              ctx.save(); ctx.beginPath(); ctx.rect(0, 0, chart.timeScale().width(), H); ctx.clip();
              paint(ctx, { chart, series, W, H, tz, on: st.on, data: st.data });
              ctx.restore();
            }
          }
        }
      } catch (e) { /* chart being rebuilt */ }
      requestAnimationFrame(frame);
    }
    renderBar(st);
    requestAnimationFrame(frame);
    const iv = setInterval(() => { if (!st.alive) return clearInterval(iv); if (document.visibilityState === "visible") refresh(true); }, 60000);
    return { refresh, state: st, detach() { st.alive = false; clearInterval(iv); cv.remove(); if (st.bar) st.bar.innerHTML = ""; } };
  }

  // ------------------------------------------------------------------ scanner + Telegram alerts
  const DEF = { tf: "5m", groups: ["India · Indices", "Gold & Crypto · Delta"], auto: false,
    alert: { retest: true, msb: true, broken: false }, rows: [], at: 0, log: [] };
  let cfg = ls.get(SKEY, DEF); cfg.alert = { ...DEF.alert, ...(cfg.alert || {}) };
  let seen = ls.get(SEEN, {});
  const save = () => { cfg.log = (cfg.log || []).slice(0, 80); ls.set(SKEY, cfg); };
  const saveSeen = () => { const cut = Date.now() / 1000 - 4 * 86400; for (const k in seen) if (seen[k] < cut) delete seen[k]; ls.set(SEEN, seen); };
  let P = null, busy = false, timer = null, err = "";

  function symbols() {
    const g = (P && P.groups()) || {};
    return [...new Set(cfg.groups.flatMap((k) => g[k] || []))].filter((s) => !/^YF:/i.test(s));
  }
  async function tgSend(text) {
    if (!window.MMBOT || !MMBOT.tg) return { ok: false, why: "Auto bot not loaded" };
    return MMBOT.tg(text);
  }
  async function alertRow(r) {
    if (!r || r.error) return;
    const now = Date.now() / 1000, tfs = TFSEC[r.tf] || 300;
    if (!r.last_time || now - (r.last_time + tfs) > 2.5 * tfs) return;     // stale (page was asleep): no late alerts
    const esc = window.MMBOT && MMBOT.tgEsc ? MMBOT.tgEsc : E, name = esc(short(r.symbol));
    const out = [];
    const m = r.last_msb;
    if (cfg.alert.msb && m && m.time === r.last_time) {
      const k = `msb:${r.symbol}:${r.tf}:${m.time}`;
      if (!seen[k]) { seen[k] = m.time; out.push([`MSB ${m.direction} ${short(r.symbol)} ${r.tf}`, `${m.direction === "bullish" ? "🟢" : "🔴"} <b>MSB ${m.direction}</b> · ${name} ${esc(r.tf)}\nClosed ${num(m.close)} through ${num(m.level)}. New ${m.direction === "bullish" ? "BU-OB / BU-MB demand" : "BE-OB / BE-MB supply"} zones drawn.`]); }
    }
    if (cfg.alert.retest) (r.retest_now || []).forEach((z) => {
      const k = `rt:${r.symbol}:${r.tf}:${z.kind}:${z.time}`;            // one alert per zone: no repeats while it is in play
      if (seen[k] || z.retests > 1) return;
      seen[k] = r.last_time;
      const bull = z.direction === "bullish", aligned = (bull && r.structure === "bullish") || (!bull && r.structure === "bearish");
      out.push([`${z.kind} retest held ${short(r.symbol)} ${r.tf}`, `${bull ? "🟩" : "🟥"} <b>${esc(z.kind)} retest held</b> · ${name} ${esc(r.tf)}\nZone ${num(z.bottom)}–${num(z.top)}: a closed candle came into it and closed back ${bull ? "above" : "below"} (rejection).\nPrice ${num(r.last)} · structure ${esc(r.structure)}${aligned ? " (with structure)" : " (against structure: weaker)"}.\nInvalid on a close ${bull ? "below " + num(z.bottom) : "above " + num(z.top)}.`]);
    });
    if (cfg.alert.broken) (r.broken_now || []).forEach((z) => {
      const k = `bk:${r.symbol}:${r.tf}:${z.kind}:${z.time}`;
      if (seen[k]) return; seen[k] = r.last_time;
      out.push([`${z.kind} broken ${short(r.symbol)}`, `⚪ <b>${esc(z.kind)} broken</b> · ${name} ${esc(r.tf)}\nClosed through ${num(z.bottom)}–${num(z.top)}.`]);
    });
    for (const [line, msg] of out) {
      cfg.log.unshift({ t: Math.floor(now), text: line });
      const res = await tgSend(msg + "\n<i>MarketMiner · analysis only, not a trade.</i>");
      if (!res.ok) cfg.log.unshift({ t: Math.floor(now), text: "Telegram: " + (res.why || "failed") });
    }
    if (out.length) { saveSeen(); save(); }
  }

  async function scan() {
    if (busy) return;
    const syms = symbols();
    if (!syms.length) { err = "Tick at least one market group"; render(); return; }
    busy = true; err = ""; render();
    try {
      const rows = [];
      for (let i = 0; i < syms.length; i += 8) {             // in small batches so the table fills as it goes
        const j = await get(`/api/lab/msb/scan?tf=${encodeURIComponent(cfg.tf)}&symbols=${encodeURIComponent(syms.slice(i, i + 8).join(","))}`);
        rows.push(...(j.rows || []));
        cfg.rows = rows.concat((cfg.rows || []).filter((x) => !rows.some((y) => y.symbol === x.symbol) && syms.slice(i + 8).includes(x.symbol)));
        render();
        for (const r of j.rows || []) await alertRow(r);
      }
      cfg.rows = rows; cfg.at = Math.floor(Date.now() / 1000);
    } catch (e) { err = e.message; }
    finally { busy = false; save(); render(); }
  }
  function schedule() {
    clearTimeout(timer); timer = null;
    if (!cfg.auto) return;
    const s = TFSEC[cfg.tf] || 300, now = Date.now() / 1000;
    const next = (Math.floor(now / s) + 1) * s + 25;          // 25 s after each candle closes
    timer = setTimeout(async () => { await scan(); schedule(); }, (next - now) * 1000);
  }

  function zoneCell(z, near) {
    if (!z) return '<span class="nt">—</span>';
    const c = z.direction === "bullish" ? "up" : "dn";
    return `<span class="${c}">${E(z.kind)}</span> ${num(z.bottom)}–${num(z.top)}${near && z.distance_atr != null ? ` <span class="nt">${z.at_price ? "in" : z.distance_atr + " ATR"}</span>` : ""}`;
  }
  const when = (t) => (t ? new Date(t * 1000).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—");
  function render() {
    if (!P || !P.el) return;
    const g = P.groups() || {}, rows = cfg.rows || [];
    const order = (r) => (r.error ? 9 : (r.retest_now || []).length ? 0 : (r.in_zone || []).length ? 1 : 2);
    const sorted = [...rows].sort((a, b) => order(a) - order(b));
    const heavy = cfg.groups.filter((k) => /options/i.test(k) && /India/i.test(k)).length;
    P.el.innerHTML = `
    <div class="cd" style="margin-bottom:10px"><h3><span>MSB / Order-block scanner</span><span class="nt">${cfg.at ? "last scan " + when(cfg.at) : ""}</span></h3>
      <div class="hr" style="gap:10px;align-items:flex-end;flex-wrap:wrap;margin-bottom:8px">
        <label class="nt" style="display:flex;flex-direction:column;gap:3px;font:700 .62rem var(--mono);text-transform:uppercase">Timeframe
          <select id="mbTf" style="background:var(--bg1);border:1px solid var(--bdr2);color:var(--t1);padding:6px 9px;border-radius:6px;font:600 .8rem var(--mono)">${Object.keys(TFSEC).map((t) => `<option ${t === cfg.tf ? "selected" : ""}>${t}</option>`).join("")}</select></label>
        <button class="btn gold" id="mbRun">${busy ? '<span class="spin"></span> scanning…' : "Scan now"}</button>
        <label style="display:flex;gap:6px;align-items:center;font:600 .74rem var(--mono)"><input type="checkbox" id="mbAuto" ${cfg.auto ? "checked" : ""}>Auto-scan after every ${E(cfg.tf)} close</label>
      </div>
      <div class="hr" style="gap:10px;flex-wrap:wrap;margin-bottom:6px">${Object.keys(g).filter((k) => !/PC only/i.test(k)).map((k) => `<label style="display:flex;gap:5px;align-items:center;font:600 .72rem var(--mono)"><input type="checkbox" data-g="${E(k)}" ${cfg.groups.includes(k) ? "checked" : ""}>${E(k)} <span class="nt">${(g[k] || []).length}</span></label>`).join("")}</div>
      <div class="nt" style="line-height:1.5">${symbols().length} symbols. ${heavy ? `<span class="am">Indian option strikes use the FYERS daily request budget (5,000 on the free plan): each symbol costs about one request per candle. Keep option groups for 15m or slower, or tick only the index you trade.</span>` : "Indian symbols need FYERS connected and only update in market hours."}${err ? `<br><span class="am">${E(err)}</span>` : ""}</div>
      <div class="hr" style="gap:12px;margin-top:8px;flex-wrap:wrap">
        <span class="nt" style="font:700 .62rem var(--mono);text-transform:uppercase">Telegram</span>
        <label style="display:flex;gap:5px;align-items:center;font:600 .72rem var(--mono)"><input type="checkbox" data-a="retest" ${cfg.alert.retest ? "checked" : ""}>Zone retest held (confirmed close)</label>
        <label style="display:flex;gap:5px;align-items:center;font:600 .72rem var(--mono)"><input type="checkbox" data-a="msb" ${cfg.alert.msb ? "checked" : ""}>New MSB</label>
        <label style="display:flex;gap:5px;align-items:center;font:600 .72rem var(--mono)"><input type="checkbox" data-a="broken" ${cfg.alert.broken ? "checked" : ""}>Zone broken</label>
        <span class="nt">${window.MMBOT && MMBOT.cfg && MMBOT.cfg().tgToken && MMBOT.cfg().tgChat ? "uses the Auto bot's Telegram" : '<span class="am">set up Telegram in the Auto bot tab</span>'}</span>
      </div>
    </div>
    <div class="cd" style="margin-bottom:10px"><div class="scroll" style="overflow-x:auto">
      <table style="width:100%;border-collapse:collapse;font:500 .72rem var(--mono)">
        <thead><tr style="color:var(--t3);text-align:left"><th style="padding:5px">Symbol</th><th>Structure</th><th>Last MSB</th><th>Now</th><th>Demand below</th><th>Supply above</th></tr></thead>
        <tbody>${sorted.length ? sorted.map((r) => r.error ? `<tr style="border-top:1px solid var(--bdr)"><td style="padding:6px 5px">${E(short(r.symbol))}</td><td colspan="5" class="am">${E(r.error)}</td></tr>`
          : `<tr data-s="${E(r.symbol)}" style="border-top:1px solid var(--bdr);cursor:pointer">
            <td style="padding:6px 5px"><b>${E(short(r.symbol))}</b><div class="nt">${num(r.last)}</div></td>
            <td class="${r.structure === "bullish" ? "up" : r.structure === "bearish" ? "dn" : "nt"}">${r.structure === "bullish" ? "▲ bull" : r.structure === "bearish" ? "▼ bear" : "—"}</td>
            <td>${r.last_msb ? `<span class="${r.last_msb.direction === "bullish" ? "up" : "dn"}">${num(r.last_msb.level)}</span><div class="nt">${when(r.last_msb.time + 0)}</div>` : '<span class="nt">—</span>'}</td>
            <td>${(r.retest_now || []).length ? r.retest_now.map((z) => `<span class="${z.direction === "bullish" ? "up" : "dn"}"><b>${E(z.kind)} retest ✓</b></span>`).join(" ")
              : (r.in_zone || []).length ? r.in_zone.map((z) => `<span class="am">in ${E(z.kind)}</span>`).join(" ") : '<span class="nt">—</span>'}</td>
            <td>${zoneCell(r.nearest_demand, true)}</td><td>${zoneCell(r.nearest_supply, true)}</td></tr>`).join("")
          : `<tr><td colspan="6" class="nt" style="padding:10px 5px">${busy ? "Scanning…" : "Tap Scan now."}</td></tr>`}</tbody></table></div>
      <div class="nt" style="margin-top:8px;line-height:1.5">Tap a row to open it on the chart. MSB = a close beyond the last swing by more than 0.33 × the last leg. BU-OB/BE-OB = the last opposite candle before the break; BU-BB/BE-BB when the pullback made a new extreme (breaker), BU-MB/BE-MB when it did not (mitigation). "Retest ✓" = the last closed candle came into the zone and closed back out on its side. Zones are removed when a candle closes through them.</div></div>
    <div class="cd"><h3><span>MSB alert log</span><span class="nt">this device</span></h3><div class="scroll" style="max-height:260px">${(cfg.log || []).length ? cfg.log.map((l) => `<div style="padding:4px 0;border-bottom:1px solid var(--bdr);font:500 .72rem var(--mono)"><span class="nt">${when(l.t)}</span> ${E(l.text)}</div>`).join("") : '<div class="nt">No alerts yet.</div>'}</div></div>`;
    const $ = (id) => P.el.querySelector("#" + id);
    $("mbRun").onclick = () => scan();
    $("mbTf").onchange = (e) => { cfg.tf = e.target.value; cfg.rows = []; save(); schedule(); render(); };
    $("mbAuto").onchange = (e) => { cfg.auto = e.target.checked; save(); schedule(); if (cfg.auto && !busy) scan(); };
    P.el.querySelectorAll("[data-g]").forEach((el) => { el.onchange = () => { const k = el.dataset.g; cfg.groups = el.checked ? [...new Set([...cfg.groups, k])] : cfg.groups.filter((x) => x !== k); save(); render(); }; });
    P.el.querySelectorAll("[data-a]").forEach((el) => { el.onchange = () => { cfg.alert[el.dataset.a] = el.checked; save(); }; });
    P.el.querySelectorAll("tr[data-s]").forEach((el) => { el.onclick = () => P.open && P.open(el.dataset.s, cfg.tf); });
  }
  function pane(el, o) {
    P = { el, ...o };
    render();
    if (cfg.auto && !timer) schedule();
  }

  window.MMMSB = { attach, load, pane, render: () => render(), scan, cfg: () => cfg };
})();
