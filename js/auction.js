/* MarketMiner AuctionFlow: auction-market structure drawn over a lightweight-charts chart, plus a "how to trade here"
 * card. Data: /api/lab/auction (backend/lab/auction.py). Drawing and reading only: no orders.
 *
 *   MMAF.attach(chart, series, {box, plot, bar, read, symbol: () => "NSE:NIFTY50-INDEX", tz: () => 19800})
 *     box   element the overlay canvas goes in (position:relative)      plot  element with the chart's size
 *     bar   element for the layer toggles (optional)                     read  element for the trading read (optional)
 */
(function () {
  "use strict";
  const LAYERS = [["va", "Value (VAH/VAL/POC)", true], ["dev", "Developing", true], ["bal", "Balance areas", true], ["imb", "Imbalance", true],
    ["fail", "Failed auctions", true], ["nodes", "HVN / LVN", true], ["proj", "Next balance", true], ["prof", "Profile", true], ["conf", "Confluence", false]];
  const KEY = "mmaf.on";
  const C = { vah: "37,99,235", val: "37,99,235", poc: "217,119,6", bal: "99,102,241", dev: "13,148,136", bull: "8,153,129", bear: "242,54,69",
    hvn: "234,88,12", lvn: "147,51,234", proj: "14,165,233", fail: "220,38,38", conf: "202,138,4", tpo: "100,116,139" };
  const cache = {};
  const num = (x) => (x == null || isNaN(x) ? "—" : Number(x).toLocaleString("en-IN", { maximumFractionDigits: Math.abs(x) >= 1000 ? 1 : Math.abs(x) >= 10 ? 2 : 4 }));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function loadOn() {
    const d = { master: true, src: "vp" };
    LAYERS.forEach(([k, , v]) => (d[k] = v));
    try { const v = JSON.parse(localStorage.getItem(KEY) || "null"); if (v && typeof v === "object") return { ...d, ...v }; } catch (e) { /* private mode */ }
    return d;
  }
  function saveOn(on) { try { localStorage.setItem(KEY, JSON.stringify(on)); } catch (e) { /* ignore */ } }

  async function load(symbol, force, params) {
    const key = symbol + (params || "");
    const c = cache[key];
    if (!force && c && c.d && Date.now() - c.t < 120000) return c.d;
    if (c && c.p) return c.p;
    const p = fetch("/api/lab/auction?symbol=" + encodeURIComponent(symbol) + (params || "")).then(async (r) => {
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || r.statusText);
      cache[key] = { t: Date.now(), d: j };
      return j;
    }).finally(() => { if (cache[key]) delete cache[key].p; });
    cache[key] = { ...(c || { t: 0 }), p };
    return p;
  }

  // time -> x, for any epoch second (the auction data may be on another timeframe than the chart)
  function xMapper(chart, series, tz) {
    const ts = chart.timeScale();
    let data = [];
    try { data = series.data() || []; } catch (e) { data = []; }
    const T = data.map((d) => (typeof d.time === "number" ? d.time : null)).filter((x) => x != null);
    const step = T.length > 1 ? T[T.length - 1] - T[T.length - 2] : 300;
    return (t) => {
      if (!T.length) return null;
      const u = t + tz;
      let lo = 0, hi = T.length - 1;
      if (u >= T[hi]) return ts.logicalToCoordinate(hi + (u - T[hi]) / step);
      if (u <= T[0]) return ts.logicalToCoordinate((u - T[0]) / step);
      while (hi - lo > 1) { const m = (lo + hi) >> 1; if (T[m] <= u) lo = m; else hi = m; }
      const f = Math.min(1, (u - T[lo]) / Math.max(1, T[hi] - T[lo]));
      return ts.logicalToCoordinate(lo + f);
    };
  }

  function tag(ctx, text, x, y, bg, align) {
    ctx.font = "600 10px 'JetBrains Mono', ui-monospace, monospace";
    const w = ctx.measureText(text).width + 8, h = 14, x0 = align === "right" ? x - w : x;
    ctx.fillStyle = bg; ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(x0, y - h / 2, w, h, 3); else ctx.rect(x0, y - h / 2, w, h);
    ctx.fill(); ctx.fillStyle = "#fff"; ctx.textBaseline = "middle"; ctx.fillText(text, x0 + 4, y + 0.5);
  }
  function hline(ctx, x0, x1, y, rgb, a, w, dash) {
    ctx.strokeStyle = `rgba(${rgb},${a})`; ctx.lineWidth = w; ctx.setLineDash(dash || []);
    ctx.beginPath(); ctx.moveTo(x0, Math.round(y) + 0.5); ctx.lineTo(x1, Math.round(y) + 0.5); ctx.stroke(); ctx.setLineDash([]);
  }

  function paint(ctx, g) {
    const d = g.data, on = g.on, S = g.series, W = g.plotW, H = g.H;
    const X = xMapper(g.chart, S, g.tz), Y = (p) => S.priceToCoordinate(p);
    const src = on.src === "tpo" ? "tpo" : "vp";
    const lab = [];                                       // right-edge labels, collision-checked at the end
    const clampX = (x, dflt) => (x == null || isNaN(x) ? dflt : Math.max(-5, Math.min(W + 5, x)));

    // multi-day + intraday balance areas
    if (on.bal) {
      (d.multiday || []).forEach((b) => {
        const lv = b[src] || b; if (!lv || lv.vah == null) return;
        const x0 = clampX(X(b.start), 0), x1 = b.status === "developing" ? W : clampX(X(b.end), W), y0 = Y(lv.vah), y1 = Y(lv.val);
        if (y0 == null || y1 == null || x1 < 0 || x0 > W) return;
        ctx.strokeStyle = `rgba(${C.bal},.55)`; ctx.lineWidth = 1.5; ctx.setLineDash([6, 4]); ctx.strokeRect(x0, y0, x1 - x0, y1 - y0); ctx.setLineDash([]);
        if (x0 > 0 && x0 < W - 60) tag(ctx, `${b.sessions}-day balance`, x0 + 2, y0 + 9, `rgba(${C.bal},.75)`);
      });
      (d.balances || []).forEach((b) => {
        const lv = b[src] || b; if (!lv || lv.vah == null) return;
        const dev = b.status === "developing";
        const x0 = clampX(X(b.start), 0), x1 = dev ? W : clampX(X(b.end), W);
        if (x1 < 0 || x0 > W) return;
        const yh = Y(lv.vah), yl = Y(lv.val), yp = Y(lv.poc), yH = Y(b.high), yL = Y(b.low);
        if (yh == null || yl == null) return;
        ctx.fillStyle = `rgba(${dev ? C.dev : C.bal},${dev ? 0.13 : 0.08})`; ctx.fillRect(x0, yh, x1 - x0, yl - yh);
        ctx.strokeStyle = `rgba(${dev ? C.dev : C.bal},${dev ? 0.8 : 0.45})`; ctx.lineWidth = dev ? 1.5 : 1; ctx.strokeRect(x0, yh, x1 - x0, yl - yh);
        if (yH != null && yL != null) { hline(ctx, x0, x1, yH, dev ? C.dev : C.bal, 0.35, 1, [2, 3]); hline(ctx, x0, x1, yL, dev ? C.dev : C.bal, 0.35, 1, [2, 3]); }
        if (yp != null) hline(ctx, x0, x1, yp, C.poc, dev ? 0.9 : 0.55, dev ? 1.5 : 1);
        if (dev) {
          lab.push({ y: yh, t: `Balance VAH ${num(lv.vah)}`, c: `rgba(${C.dev},.9)`, r: 1 }, { y: yl, t: `Balance VAL ${num(lv.val)}`, c: `rgba(${C.dev},.9)`, r: 1 },
            { y: yp, t: `Balance POC ${num(lv.poc)}`, c: `rgba(${C.poc},.9)`, r: 1 });
        }
      });
    }
    // session value areas (historical) and the developing value of the current session
    const ss = d.sessions || [];
    if (on.va) {
      ss.forEach((s, i) => {
        const lv = s[src + "_lv"]; if (!lv) return;
        const cur = i === ss.length - 1;
        if (cur && on.dev) return;                          // the developing lines show today's value
        const x0 = clampX(X(s.start), 0), x1 = cur ? W : clampX(X(s.last_t), W);
        if (x1 < 0 || x0 > W) return;
        hline(ctx, x0, x1, Y(lv.vah), C.vah, 0.6, 1, [5, 3]); hline(ctx, x0, x1, Y(lv.val), C.val, 0.6, 1, [5, 3]);
        hline(ctx, x0, x1, Y(lv.poc), C.poc, s.naked_poc ? 0.95 : 0.7, s.naked_poc ? 2 : 1.25);
        if (s.naked_poc) { hline(ctx, x1, W, Y(lv.poc), C.poc, 0.6, 1, [2, 4]); lab.push({ y: Y(lv.poc), t: `naked POC ${s.date.slice(5)} ${num(lv.poc)}`, c: `rgba(${C.poc},.85)`, r: 3 }); }
        if (i === ss.length - 2) {
          lab.push({ y: Y(lv.vah), t: `prev VAH ${num(lv.vah)}`, c: `rgba(${C.vah},.85)`, r: 2 }, { y: Y(lv.val), t: `prev VAL ${num(lv.val)}`, c: `rgba(${C.val},.85)`, r: 2 },
            { y: Y(lv.poc), t: `prev POC ${num(lv.poc)}`, c: `rgba(${C.poc},.85)`, r: 2 });
        }
      });
    }
    if (on.dev && ss.length) {
      const s = ss[ss.length - 1], dv = s.developing || [];
      ["vah", "val", "poc"].forEach((k) => {
        ctx.strokeStyle = `rgba(${k === "poc" ? C.poc : C.vah},${k === "poc" ? 0.95 : 0.8})`; ctx.lineWidth = k === "poc" ? 2 : 1.25; ctx.beginPath();
        let started = false, py = null;
        dv.forEach((p) => {
          const lv = p[src]; if (!lv) return;
          const x = X(p.t), y = Y(lv[k]); if (x == null || y == null) return;
          if (!started) { ctx.moveTo(x, y); started = true; } else { ctx.lineTo(x, py); ctx.lineTo(x, y); }
          py = y;
        });
        ctx.stroke();
        const lv = s[src + "_lv"];
        if (lv) lab.push({ y: Y(lv[k]), t: `${k === "poc" ? "dPOC" : k === "vah" ? "dVAH" : "dVAL"} ${num(lv[k])}`, c: `rgba(${k === "poc" ? C.poc : C.vah},.95)`, r: 0 });
      });
      // initial balance (first hour)
      const ib = (s.tpo || {}).ib;
      if (ib) { const x0 = clampX(X(s.start), 0); hline(ctx, x0, W, Y(ib[0]), C.tpo, 0.5, 1, [1, 3]); hline(ctx, x0, W, Y(ib[1]), C.tpo, 0.5, 1, [1, 3]); }
    }
    // imbalance legs
    if (on.imb) {
      (d.imbalances || []).forEach((x) => {
        const x0 = X(x.start), x1 = x.status === "developing" ? W : X(x.end);
        if (x0 == null || x1 == null || x1 < 0 || x0 > W) return;
        const rgb = x.dir === "bull" ? C.bull : C.bear, y0 = Y(x.high), y1 = Y(x.low);
        if (y0 == null || y1 == null) return;
        ctx.fillStyle = `rgba(${rgb},.07)`; ctx.fillRect(x0, y0, Math.max(3, x1 - x0), y1 - y0);
        const ya = Y(x.frm), yb = Y(x.to);
        ctx.strokeStyle = `rgba(${rgb},.75)`; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(x0, ya); ctx.lineTo(x1, yb); ctx.stroke();
        const ang = Math.atan2(yb - ya, x1 - x0);
        ctx.beginPath(); ctx.moveTo(x1, yb); ctx.lineTo(x1 - 8 * Math.cos(ang - 0.5), yb - 8 * Math.sin(ang - 0.5)); ctx.lineTo(x1 - 8 * Math.cos(ang + 0.5), yb - 8 * Math.sin(ang + 0.5));
        ctx.closePath(); ctx.fillStyle = `rgba(${rgb},.85)`; ctx.fill();
        if (x.size_atr >= 2 && x1 - x0 > 30) tag(ctx, `${x.dir === "bull" ? "▲ bullish" : "▼ bearish"} imbalance ${x.size_atr}×ATR`, x0 + 2, x.dir === "bull" ? y0 + 9 : y1 - 9, `rgba(${rgb},.8)`);
      });
      // single prints of the current and previous session (initiative)
      ss.slice(-2).forEach((s) => ((s.tpo || {}).single_prints || []).forEach(([lo, hi]) => {
        const x0 = clampX(X(s.start), 0), y0 = Y(hi), y1 = Y(lo); if (y0 == null || y1 == null) return;
        ctx.fillStyle = "rgba(202,138,4,.10)"; ctx.fillRect(x0, y0, W - x0, y1 - y0);
      }));
    }
    // HVN / LVN
    if (on.nodes) {
      (d.hvn || []).forEach((h) => { const y = Y(h.price); if (y == null || y < 0 || y > H) return; hline(ctx, 0, W, y, C.hvn, 0.45, 1); tag(ctx, `HVN ${num(h.price)}`, 4, y, `rgba(${C.hvn},.7)`); });
      (d.lvn || []).forEach((h) => { const y = Y(h.price); if (y == null || y < 0 || y > H) return; hline(ctx, 0, W, y, C.lvn, 0.55, 1, [3, 3]); tag(ctx, `LVN ${num(h.price)}`, 4, y, `rgba(${C.lvn},.7)`); });
    }
    // failed auctions
    if (on.fail) {
      (d.failed || []).forEach((f) => {
        const x = X(f.t_probe), y = Y(f.price); if (x == null || y == null || x < 0 || x > W) return;
        const up = f.side === "high";
        ctx.fillStyle = `rgba(${C.fail},.9)`; ctx.beginPath();
        if (up) { ctx.moveTo(x - 6, y - 12); ctx.lineTo(x + 6, y - 12); ctx.lineTo(x, y - 3); } else { ctx.moveTo(x - 6, y + 12); ctx.lineTo(x + 6, y + 12); ctx.lineTo(x, y + 3); }
        ctx.closePath(); ctx.fill();
        ctx.font = "700 9px 'JetBrains Mono', monospace"; ctx.fillStyle = `rgba(${C.fail},1)`; ctx.textBaseline = "middle";
        ctx.fillText("FA", x + 8, up ? y - 8 : y + 8);
      });
    }
    // next balance projections (to the right of the last candle)
    if (on.proj) {
      const xl = X(d.as_of - 1) || W * 0.8;
      (d.projections || []).forEach((p) => {
        const y0 = Y(p.hi), y1 = Y(p.lo); if (y0 == null || y1 == null) return;
        const x0 = Math.max(0, Math.min(xl, W - 40));
        ctx.fillStyle = `rgba(${C.proj},.12)`; ctx.fillRect(x0, y0, W - x0, y1 - y0);
        ctx.strokeStyle = `rgba(${C.proj},.8)`; ctx.lineWidth = 1.25; ctx.setLineDash([5, 3]); ctx.strokeRect(x0, y0, W - x0, y1 - y0); ctx.setLineDash([]);
        lab.push({ y: (y0 + y1) / 2, t: `next balance ${p.dir === "up" ? "↑" : "↓"} ${num(p.lo)}–${num(p.hi)}`, c: `rgba(${C.proj},.9)`, r: 1 });
      });
    }
    // confluence zones
    if (on.conf) {
      (d.confluence || []).slice(0, 8).forEach((z) => {
        const y0 = Y(z.hi), y1 = Y(z.lo); if (y0 == null || y1 == null) return;
        const h = Math.max(3, y1 - y0);
        ctx.fillStyle = `rgba(${C.conf},${z.vp_tpo ? 0.22 : 0.12})`; ctx.fillRect(W - 14, y0 - (h === 3 ? 1 : 0), 14, h);
        lab.push({ y: y0 + h / 2, t: `★ ${z.vp_tpo ? "VP+TPO " : ""}${num(z.mid)} (${z.members.length})`, c: `rgba(${C.conf},.9)`, r: 2 });
      });
    }
    // profile histograms: composite (wide, faint) and current session (narrow, strong) on the right
    if (on.prof && d.profile) {
      const drawP = (rows, maxW, rgb, a, idx) => {
        const mx = Math.max(...rows.map((r) => r[idx]), 1e-9), st = d.profile.step;
        rows.forEach((r) => {
          const y0 = Y(r[0] + st), y1 = Y(r[0]); if (y0 == null || y1 == null || y1 < 0 || y0 > H) return;
          const w = (r[idx] / mx) * maxW;
          ctx.fillStyle = `rgba(${rgb},${a})`; ctx.fillRect(W - w, y0 + 0.5, w, Math.max(1, y1 - y0 - 1));
        });
      };
      const idx = src === "tpo" ? 2 : 1;
      drawP(d.profile.composite || [], Math.min(160, W * 0.18), C.tpo, 0.18, idx);
      drawP(d.profile.session || [], Math.min(90, W * 0.1), src === "tpo" ? C.tpo : C.vah, 0.35, idx);
    }
    // labels on the right, most important first, no overlaps
    lab.sort((a, b) => a.r - b.r);
    const used = [];
    lab.forEach((l) => {
      if (l.y == null || l.y < 7 || l.y > H - 7 || used.some((u) => Math.abs(u - l.y) < 14)) return;
      used.push(l.y);
      tag(ctx, l.t, W - 4, l.y, l.c, "right");
    });
  }

  function renderBar(st) {
    if (!st.bar) return;
    const on = st.on, d = st.data;
    const chip = (k, l) => `<button type="button" data-af="${k}" style="font:600 .66rem 'JetBrains Mono',monospace;padding:2px 7px;border-radius:5px;cursor:pointer;border:1px solid ${on[k] ? "#0d9488" : "rgba(148,163,184,.4)"};background:${on[k] ? "rgba(13,148,136,.15)" : "transparent"};color:inherit">${l}</button>`;
    st.bar.innerHTML = `<span style="display:inline-flex;gap:4px;align-items:center;flex-wrap:wrap;font-size:.66rem">
      <label style="display:inline-flex;gap:4px;align-items:center;cursor:pointer;font-weight:700"><input type="checkbox" data-af="master" ${on.master ? "checked" : ""}>AuctionFlow</label>
      <span style="display:inline-flex;border:1px solid rgba(148,163,184,.5);border-radius:5px;overflow:hidden">${["vp", "tpo"].map((s) => `<button type="button" data-afsrc="${s}" style="font:700 .64rem 'JetBrains Mono',monospace;padding:2px 7px;border:0;cursor:pointer;background:${on.src === s ? "rgba(13,148,136,.2)" : "transparent"};color:inherit">${s.toUpperCase()}</button>`).join("")}</span>
      ${LAYERS.map(([k, l]) => chip(k, l)).join("")}
      <span style="opacity:.65">${st.err ? "unavailable: " + esc(String(st.err).slice(0, 90)) : st.loading ? "loading…" : d ? `${d.tf} · bin ${num(d.bin)} · volume: ${esc(d.volume_source)}` : ""}</span></span>`;
    st.bar.querySelectorAll("[data-af]").forEach((el) => {
      el.onclick = (ev) => {
        const k = el.dataset.af;
        if (k === "master") on.master = el.checked; else { on[k] = !on[k]; ev.preventDefault(); }
        saveOn(on); st.key = ""; renderBar(st);
      };
    });
    st.bar.querySelectorAll("[data-afsrc]").forEach((el) => { el.onclick = () => { on.src = el.dataset.afsrc; saveOn(on); st.key = ""; renderBar(st); renderRead(st); }; });
  }

  function renderRead(st) {
    const el = st.read; if (!el) return;
    const d = st.data;
    if (!st.on.master || !d) { el.innerHTML = st.err ? `<div style="font:500 .72rem 'JetBrains Mono',monospace;opacity:.7">AuctionFlow unavailable: ${esc(st.err)}</div>` : ""; return; }
    const s = d.state || {}, ss = d.sessions || [], cur = ss[ss.length - 1] || {}, lv = cur[st.on.src + "_lv"] || {}, plv = (ss[ss.length - 2] || {})[st.on.src + "_lv"] || {};
    const chip = (t, c) => `<span style="display:inline-block;font:700 .66rem 'JetBrains Mono',monospace;padding:3px 7px;border-radius:5px;border:1px solid ${c};color:${c};margin:0 4px 4px 0">${t}</span>`;
    const phaseC = s.phase === "balance" ? "#0d9488" : s.phase === "imbalance" ? (s.imbalance && s.imbalance.dir === "bull" ? "#089981" : "#f23645") : "#64748b";
    const row = (p) => `<tr><td style="padding:5px 6px;font-weight:700">${esc(p.setup)}</td><td style="padding:5px 6px">${esc(p.bias)}</td><td style="padding:5px 6px">${esc(p.entry)}</td>
      <td style="padding:5px 6px">${esc(p.stop)}</td><td style="padding:5px 6px">${(p.targets || []).map(num).join(" → ") || "—"}</td><td style="padding:5px 6px;opacity:.75;white-space:normal;min-width:220px">${esc(p.why)}</td></tr>`;
    el.innerHTML = `<div style="border:1px solid rgba(148,163,184,.35);border-radius:8px;padding:10px;margin-top:8px;font:500 .72rem 'JetBrains Mono',monospace">
      <div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:6px"><b style="font-size:.78rem">AuctionFlow · how to trade here</b>
        <span style="opacity:.6">${esc(d.symbol)} · ${esc(d.tf)} · ${st.on.src.toUpperCase()} levels · analysis only</span></div>
      <div>${chip(String(s.phase || "?").toUpperCase(), phaseC)}${chip("today: " + (s.today || "?"), "#2563eb")}${chip("vs yesterday: " + (s.vs_prev_day || "?"), "#2563eb")}${chip(s.open_vs_prev || "?", "#64748b")}
        ${s.poor_high ? chip("poor high (unfinished)", "#d97706") : ""}${s.poor_low ? chip("poor low (unfinished)", "#d97706") : ""}${(s.failed_today || []).length ? chip(`${s.failed_today.length} failed auction(s) today`, "#dc2626") : ""}</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:6px;margin:6px 0 8px">
        ${[["Today POC", lv.poc], ["Today VAH", lv.vah], ["Today VAL", lv.val], ["Prev POC", plv.poc], ["Prev VAH", plv.vah], ["Prev VAL", plv.val]].map(([k, v]) => `<div style="border:1px solid rgba(148,163,184,.25);border-radius:6px;padding:5px 7px"><div style="opacity:.6;font-size:.6rem;text-transform:uppercase">${k}</div><b>${num(v)}</b></div>`).join("")}
        ${s.balance ? `<div style="border:1px solid rgba(13,148,136,.5);border-radius:6px;padding:5px 7px"><div style="opacity:.6;font-size:.6rem;text-transform:uppercase">Balance (${s.balance.bars} candles)</div><b>${num(s.balance.val)} – ${num(s.balance.vah)}</b> POC ${num(s.balance.poc)}</div>` : ""}
        ${s.imbalance ? `<div style="border:1px solid rgba(148,163,184,.5);border-radius:6px;padding:5px 7px"><div style="opacity:.6;font-size:.6rem;text-transform:uppercase">Imbalance</div><b>${s.imbalance.dir === "bull" ? "▲" : "▼"} ${num(s.imbalance.frm)} → ${num(s.imbalance.to)}</b> (${s.imbalance.size_atr}×ATR)</div>` : ""}
      </div>
      ${(d.plan || []).length ? `<div style="overflow:auto"><table style="width:100%;border-collapse:collapse;font-size:.7rem"><tr style="opacity:.6;text-align:left"><th style="padding:4px 6px">Setup</th><th style="padding:4px 6px">Bias</th><th style="padding:4px 6px">Entry</th><th style="padding:4px 6px">Stop</th><th style="padding:4px 6px">Targets</th><th style="padding:4px 6px">Why</th></tr>${d.plan.map(row).join("")}</table></div>` : '<div style="opacity:.7">No clean auction setup right now.</div>'}
      <div style="margin-top:6px;opacity:.75">Next balance: ${(d.projections || []).map((p) => `<b>${p.dir === "up" ? "↑" : "↓"} ${num(p.lo)}–${num(p.hi)}</b> (${esc(p.basis)}; ${esc(p.trigger)})`).join(" · ") || "—"}</div>
      <div style="margin-top:4px;opacity:.75">Confluence: ${(d.confluence || []).slice(0, 4).map((z) => `<b>${num(z.mid)}</b>${z.vp_tpo ? " VP+TPO" : ""} <span style="opacity:.7">(${z.members.slice(0, 4).map(esc).join(", ")}${z.members.length > 4 ? "…" : ""})</span>`).join(" · ") || "—"}</div>
      <div style="margin-top:6px;opacity:.55;line-height:1.5">Rules of thumb: inside value → fade edges toward POC, never trade the middle · 3 closes outside value + beyond the balance range = acceptance → trade the retest toward the next balance · a probe outside that comes straight back = failed auction → trade back through value · poor highs/lows and naked POCs get revisited. Paper trade it first.</div></div>`;
  }

  function attach(chart, series, o) {
    const box = o.box, plot = o.plot || o.box;
    const cv = document.createElement("canvas");
    cv.style.cssText = "position:absolute;top:0;left:0;z-index:1;pointer-events:none";
    box.appendChild(cv);
    const st = { chart, series, bar: o.bar, read: o.read, on: loadOn(), data: null, sym: null, key: "", err: null, loading: false, alive: true };
    async function refresh(force) {
      const s = o.symbol(); if (!s) return;
      st.loading = !st.data || st.sym !== s; renderBar(st);
      try { const d = await load(s, force, o.params ? o.params() : ""); if (o.symbol() === s) { st.data = d; st.err = null; st.key = ""; if (o.onData) o.onData(d); } }
      catch (e) { if (o.symbol() === s) { st.data = null; st.err = e.message; } }
      st.loading = false; renderBar(st); renderRead(st);
    }
    function frame() {
      if (!st.alive) return;
      try {
        if (!document.body.contains(box)) { st.alive = false; return; }
        const s = o.symbol();
        const sk = s + (o.params ? o.params() : "");
        if (s && sk !== st.sym) { st.sym = sk; st.data = null; refresh(false); }
        if (document.visibilityState === "visible") {
          const W = plot.clientWidth, H = Math.max(0, plot.clientHeight - chart.timeScale().height());
          const tz = (o.tz && o.tz()) || 0, vr = chart.timeScale().getVisibleLogicalRange();
          const k = [W, H, tz, JSON.stringify(st.on), vr && vr.from.toFixed(2), vr && vr.to.toFixed(2), series.coordinateToPrice(0), series.coordinateToPrice(H), st.data && st.data.as_of, st.data && st.data.symbol].join("|");
          if (k !== st.key) {
            st.key = k;
            const r = window.devicePixelRatio || 1;
            cv.width = W * r; cv.height = H * r; cv.style.width = W + "px"; cv.style.height = H + "px";
            const ctx = cv.getContext("2d"); ctx.setTransform(r, 0, 0, r, 0, 0); ctx.clearRect(0, 0, W, H);
            if (st.on.master && st.data && !st.data.error) {
              const plotW = chart.timeScale().width();
              ctx.save(); ctx.beginPath(); ctx.rect(0, 0, plotW, H); ctx.clip();
              paint(ctx, { chart, series, plotW, H, tz, on: st.on, data: st.data });
              ctx.restore();
            }
          }
        }
      } catch (e) { /* chart being rebuilt */ }
      requestAnimationFrame(frame);
    }
    renderBar(st);
    requestAnimationFrame(frame);
    const iv = setInterval(() => { if (!st.alive) return clearInterval(iv); if (document.visibilityState === "visible" && st.on.master) refresh(true); }, 300000);
    return { refresh, state: st, detach() { st.alive = false; clearInterval(iv); cv.remove(); if (st.bar) st.bar.innerHTML = ""; if (st.read) st.read.innerHTML = ""; } };
  }

  window.MMAF = { attach, load, paint };
})();
