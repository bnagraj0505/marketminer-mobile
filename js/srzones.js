/* MarketMiner: multi-timeframe support / resistance ZONES (weekly, daily, 4h, 1h, 15m) drawn as shaded bands on a
 * lightweight-charts chart. Zones come from /api/lab/sr (backend/lab/srmtf.py). Drawing only: no orders.
 *
 *   MMSR.attach(chart, series, {box, plot, bar, symbol: () => "NSE:NIFTY50-INDEX", tz: () => 19800})
 *     box   element the overlay canvas is placed in (position:relative)
 *     plot  element whose size is the chart's (defaults to box)
 *     bar   element that receives the W / D / 4H / 1H / 15m toggles (optional)
 */
(function () {
  "use strict";
  const TFS = [["1w", "W"], ["1d", "D"], ["4h", "4H"], ["1h", "1H"], ["15m", "15m"]];
  const ALPHA = { "1w": 0.2, "1d": 0.16, "4h": 0.12, "1h": 0.09, "15m": 0.07 };
  const EDGE = { "1w": 0.85, "1d": 0.75, "4h": 0.6, "1h": 0.5, "15m": 0.42 };
  const LW = { "1w": 2, "1d": 1.5, "4h": 1, "1h": 1, "15m": 1 };
  const COL = { support: [8, 153, 129], resistance: [242, 54, 69], "at price": [217, 119, 6] };
  const KEY = "mmsr.on2";                                // v2: lighter defaults (weekly + daily)
  const cache = {};
  const num = (x) => (x == null || isNaN(x) ? "—" : Number(x).toLocaleString("en-IN", { maximumFractionDigits: Math.abs(x) >= 1000 ? 0 : Math.abs(x) >= 10 ? 2 : 4 }));

  function loadOn() {
    try { const v = JSON.parse(localStorage.getItem(KEY) || "null"); if (v && typeof v === "object") return v; } catch (e) { /* private mode */ }
    return { "1w": true, "1d": true, "4h": false, "1h": false, "15m": false, master: true };
  }
  function saveOn(on) { try { localStorage.setItem(KEY, JSON.stringify(on)); } catch (e) { /* ignore */ } }

  async function load(symbol, force) {
    const c = cache[symbol];
    if (!force && c && Date.now() - c.t < 90000) return c.d;
    if (c && c.p) return c.p;
    const p = fetch("/api/lab/sr?symbol=" + encodeURIComponent(symbol)).then(async (r) => {
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || r.statusText);
      cache[symbol] = { t: Date.now(), d: j };
      return j;
    }).finally(() => { if (cache[symbol]) delete cache[symbol].p; });
    cache[symbol] = { ...(c || { t: 0 }), p };
    return p;
  }

  function tag(ctx, text, xr, y, bg) {
    ctx.font = "600 10px 'JetBrains Mono', ui-monospace, monospace";
    const w = ctx.measureText(text).width + 8, h = 15, x = Math.max(2, xr - w);
    ctx.fillStyle = bg; ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(x, y - h / 2, w, h, 3); else ctx.rect(x, y - h / 2, w, h);
    ctx.fill(); ctx.fillStyle = "#fff"; ctx.textBaseline = "middle"; ctx.fillText(text, x + 4, y + 0.5);
  }

  /** Paint the enabled timeframes' zones. g: {chart, series, W, H, tz, on, data}. Higher timeframes are painted last (on top). */
  function paint(ctx, g) {
    const tfs = (g.data && g.data.timeframes) || {};
    const ts = g.chart.timeScale(), plotW = ts.width();
    const labels = [], drawn = [];
    // higher timeframes first: a lower-timeframe zone mostly inside an already drawn higher one is not drawn again
    // (the higher zone's label lists it as confluence)
    const keep = [];
    TFS.forEach(([tf]) => {
      if (!g.on[tf] || !tfs[tf]) return;
      (tfs[tf].zones || []).forEach((z) => {
        const w = z.top - z.bottom || 1e-9;
        const dup = drawn.some((d) => (Math.min(d.top, z.top) - Math.max(d.bottom, z.bottom)) / w > 0.5);
        if (!dup) { drawn.push(z); keep.push([tf, z]); }
      });
    });
    keep.reverse().forEach(([tf, z]) => {
      {
        const yt = g.series.priceToCoordinate(z.top), yb = g.series.priceToCoordinate(z.bottom);
        if (yt == null || yb == null) return;
        const y0 = Math.min(yt, yb), h = Math.max(2, Math.abs(yb - yt));
        if (y0 > g.H || y0 + h < 0) return;
        let x0 = ts.timeToCoordinate(z.first_time + g.tz);
        if (x0 == null || x0 < 0) x0 = 0;
        const [r, gg, b] = COL[z.role] || COL["at price"];
        ctx.fillStyle = `rgba(${r},${gg},${b},${ALPHA[tf]})`;
        ctx.fillRect(x0, y0, plotW - x0, h);
        ctx.strokeStyle = `rgba(${r},${gg},${b},${EDGE[tf]})`;
        ctx.lineWidth = LW[tf];
        ctx.setLineDash(tf === "1w" || tf === "1d" ? [] : [4, 3]);
        ctx.beginPath();
        ctx.moveTo(x0, y0 + 0.5); ctx.lineTo(plotW, y0 + 0.5);
        ctx.moveTo(x0, y0 + h - 0.5); ctx.lineTo(plotW, y0 + h - 0.5);
        ctx.stroke();
        ctx.setLineDash([]);
        const role = z.role === "support" ? "S" : z.role === "resistance" ? "R" : "•";
        const conf = z.confluence && z.confluence.length ? " +" + z.confluence.join(",") : "";
        labels.push({ y: y0 + h / 2, tf, text: `${tfs[tf].label} ${role}${z.flip ? "↔" : ""} ${num(z.bottom)}–${num(z.top)} ×${z.touches}${conf}`,
          bg: `rgba(${r},${gg},${b},${Math.min(0.95, EDGE[tf] + 0.1)})`, rank: TFS.findIndex((x) => x[0] === tf) });
      }
    });
    labels.sort((a, b) => a.rank - b.rank);                    // weekly labels win a collision
    const used = [];
    labels.forEach((l) => {
      if (l.y < 8 || l.y > g.H - 8 || used.some((u) => Math.abs(u - l.y) < 15)) return;
      used.push(l.y);
      tag(ctx, l.text, plotW - 4, l.y, l.bg);
    });
  }

  function renderBar(st) {
    if (!st.bar) return;
    const on = st.on, d = st.data, err = st.err;
    const chip = (k, l) => {
      const z = d && d.timeframes && d.timeframes[k], n = z ? (z.zones || []).length : 0, e = z && z.error;
      return `<button type="button" data-sr="${k}" title="${e ? "unavailable: " + String(e).replace(/"/g, "'") : n + " zone(s)"}" style="font:600 .66rem 'JetBrains Mono',monospace;padding:2px 7px;border-radius:5px;cursor:pointer;border:1px solid ${on[k] ? "#6366f1" : "rgba(148,163,184,.4)"};background:${on[k] ? "rgba(99,102,241,.18)" : "transparent"};color:inherit;opacity:${e ? 0.5 : 1}">${l}${e ? " ✗" : ""}</button>`;
    };
    st.bar.innerHTML = `<span style="display:inline-flex;gap:4px;align-items:center;flex-wrap:wrap;font-size:.66rem">
      <label style="display:inline-flex;gap:4px;align-items:center;cursor:pointer"><input type="checkbox" data-sr="master" ${on.master ? "checked" : ""}>S/R zones</label>
      ${TFS.map(([k, l]) => chip(k, l)).join("")}
      <span style="opacity:.65">${err ? "unavailable: " + String(err).slice(0, 80) : st.loading ? "loading…" : d ? "<span style='color:#089981'>■</span> support <span style='color:#f23645'>■</span> resistance · ↔ flip · +W,D = confluence" : ""}</span></span>`;
    st.bar.querySelectorAll("[data-sr]").forEach((el) => {
      el.onclick = (ev) => {
        const k = el.dataset.sr;
        if (k === "master") on.master = el.checked; else { on[k] = !on[k]; ev.preventDefault(); }
        saveOn(on); st.key = ""; renderBar(st);
      };
    });
  }

  function attach(chart, series, o) {
    const box = o.box, plot = o.plot || o.box;
    const cv = document.createElement("canvas");
    cv.style.cssText = "position:absolute;top:0;left:0;z-index:1;pointer-events:none";
    box.appendChild(cv);
    const st = { chart, series, bar: o.bar, on: loadOn(), data: null, sym: null, key: "", err: null, loading: false, alive: true };
    async function refresh(force) {
      const s = o.symbol();
      if (!s) return;
      st.loading = !st.data || st.sym !== s; renderBar(st);
      try { const d = await load(s, force); if (o.symbol() === s) { st.data = d; st.err = null; st.key = ""; } }
      catch (e) { if (o.symbol() === s) { st.data = null; st.err = e.message; } }
      st.loading = false; renderBar(st);
    }
    function frame() {
      if (!st.alive) return;
      try {
        if (!document.body.contains(box)) { st.alive = false; return; }
        const s = o.symbol();
        if (s && s !== st.sym) { st.sym = s; st.data = null; refresh(false); }
        if (document.visibilityState === "visible") {
          const W = plot.clientWidth, H = Math.max(0, plot.clientHeight - chart.timeScale().height());
          const tz = (o.tz && o.tz()) || 0;
          const vr = chart.timeScale().getVisibleLogicalRange();
          const k = [W, H, tz, st.on.master, JSON.stringify(st.on), vr && vr.from.toFixed(2), vr && vr.to.toFixed(2),
            series.coordinateToPrice(0), series.coordinateToPrice(H), st.data && st.data.symbol, st.data && (st.data.timeframes["15m"] || {}).as_of].join("|");
          if (k !== st.key) {
            st.key = k;
            const r = window.devicePixelRatio || 1;
            if (st.cw !== W * r || st.ch !== H * r) { st.cw = cv.width = W * r; st.ch = cv.height = H * r; cv.style.width = W + "px"; cv.style.height = H + "px"; }
            const ctx = cv.getContext("2d"); ctx.setTransform(r, 0, 0, r, 0, 0); ctx.clearRect(0, 0, W, H);
            if (st.on.master && st.data) {
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
    const iv = setInterval(() => { if (!st.alive) return clearInterval(iv); if (document.visibilityState === "visible") refresh(true); }, 120000);
    return { refresh, state: st, detach() { st.alive = false; clearInterval(iv); cv.remove(); if (st.bar) st.bar.innerHTML = ""; } };
  }

  window.MMSR = { attach, load, paint, TFS };
})();
