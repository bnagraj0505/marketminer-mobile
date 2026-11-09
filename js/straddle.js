/* MarketMiner Straddle Monitor block. Usage: <div id="mmStraddle"></div><script src="/js/straddle.js"></script>
   Data: GET /api/agent/straddle (recorded every minute while NSE is open). Information only. */
(function () {
  const CSS = `
.mmsd{margin:10px 16px;border:1px solid #1b2536;border-radius:10px;background:#0d1117;padding:12px;font-family:Inter,system-ui,sans-serif;color:#e2e8f0}
.mmsd h3{font:700 .72rem 'JetBrains Mono',ui-monospace,monospace;color:#8b9ab5;letter-spacing:.6px;text-transform:uppercase;margin:0 0 10px;display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.mmsd .g{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:10px}
.mmsd .c{border:1px solid #263450;border-radius:9px;background:#06080f;padding:10px;min-width:0}
.mmsd .hd{display:flex;justify-content:space-between;align-items:baseline;gap:6px;margin-bottom:8px}
.mmsd .hd b{font:800 1rem 'JetBrains Mono',monospace;color:#18ffff}.mmsd .hd span{font:600 .64rem 'JetBrains Mono',monospace;color:#5b6b85}
.mmsd .tag{font:700 .58rem 'JetBrains Mono',monospace;padding:2px 6px;border-radius:4px;margin-left:6px}
.mmsd .live{color:#00e676;background:rgba(0,230,118,.1)}.mmsd .old{color:#ffab00;background:rgba(255,171,0,.1)}
.mmsd .m{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
.mmsd .k{background:#131a27;border:1px solid #1b2536;border-radius:6px;padding:6px 7px}
.mmsd .k span{display:block;font:600 .56rem 'JetBrains Mono',monospace;color:#5b6b85;text-transform:uppercase;letter-spacing:.4px}
.mmsd .k b{font:700 .86rem 'JetBrains Mono',monospace}.mmsd .k i{font:500 .6rem 'JetBrains Mono',monospace;color:#8b9ab5;font-style:normal;display:block}
.mmsd .up{color:#00e676}.mmsd .dn{color:#ff5252}.mmsd .fl{color:#8b9ab5}.mmsd .wa{color:#ffab00}
.mmsd svg{width:100%;height:70px;display:block;margin-top:6px}
.mmsd .lbl{font:600 .56rem 'JetBrains Mono',monospace;color:#5b6b85;margin-top:6px;display:flex;justify-content:space-between}
.mmsd ul{margin:8px 0 0 14px;padding:0}.mmsd li{font:500 .68rem Inter,sans-serif;color:#8b9ab5;line-height:1.45;margin-bottom:2px}
.mmsd .btn{background:#131a27;border:1px solid #263450;color:#8b9ab5;padding:4px 9px;border-radius:6px;cursor:pointer;font:600 .66rem 'JetBrains Mono',monospace}
.mmsd .btn:hover{color:#18ffff;border-color:#18ffff}.mmsd .na{font:500 .72rem 'JetBrains Mono',monospace;color:#5b6b85;padding:14px;text-align:center}
@media(max-width:700px){.mmsd .m{grid-template-columns:repeat(2,1fr)}}`;
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const n = (x, d = 1) => x === null || x === undefined || isNaN(x) ? "–" : Number(x).toLocaleString("en-IN", {maximumFractionDigits: d});
  const tm = t => t ? new Date(t * 1000).toLocaleTimeString("en-IN", {hour: "2-digit", minute: "2-digit", hour12: false}) : "–";
  const arrow = k => k > 0 ? `<span class="up">▲${k}</span>` : k < 0 ? `<span class="dn">▼${-k}</span>` : `<span class="fl">=</span>`;
  const pc = x => x === null || x === undefined ? "–" : `<span class="${x > 0 ? "up" : x < 0 ? "dn" : "fl"}">${x > 0 ? "+" : ""}${n(x, 1)}%</span>`;

  function line(vals, w, h, col, mn, mx, step) {
    if (vals.length < 2) return "";
    mn = mn ?? Math.min(...vals); mx = mx ?? Math.max(...vals);
    const y = v => (h - 4 - (mx === mn ? 0.5 : (v - mn) / (mx - mn)) * (h - 8)).toFixed(1);
    let d = "";
    vals.forEach((v, i) => {
      const x = (i / (vals.length - 1) * w).toFixed(1);
      d += i ? (step ? ` H${x} V${y(v)}` : ` L${x},${y(v)}`) : `M${x},${y(v)}`;
    });
    return `<path d="${d}" fill="none" stroke="${col}" stroke-width="1.6"/>`;
  }
  function charts(x) {
    const s = x.series || [];
    if (s.length < 2) return `<div class="lbl"><span>charts appear after two snapshots</span></div>`;
    const W = 300, H = 70;
    const prem = s.map(p => p.straddle);
    const lvl = s.flatMap(p => [p.centre, p.spot]);
    const mn = Math.min(...lvl), mx = Math.max(...lvl);
    return `<div class="lbl"><span>ATM straddle premium</span><span>${n(Math.min(...prem))} – ${n(Math.max(...prem))}</span></div>
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${line(prem, W, H, "#FFD700")}</svg>
      <div class="lbl"><span><span style="color:#18ffff">━ centre (cheapest straddle)</span> <span style="color:#8b9ab5">━ index</span></span><span>${tm(s[0].t)} → ${tm(s[s.length - 1].t)}</span></div>
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${line(s.map(p => p.spot), W, H, "#8b9ab5", mn, mx)}${line(s.map(p => p.centre), W, H, "#18ffff", mn, mx, true)}</svg>`;
  }
  function curve(x) {
    const c = x.curve || [];
    if (!c.length) return "";
    const mx = Math.max(...c.map(p => p.straddle)), mn = Math.min(...c.map(p => p.straddle));
    const W = 300, H = 46, bw = W / c.length;
    const bars = c.map((p, i) => {
      const h = 6 + (mx === mn ? 0.5 : (p.straddle - mn) / (mx - mn)) * (H - 10);
      const col = p.strike === x.centre ? "#18ffff" : p.strike === x.atm ? "#FFD700" : "#263450";
      return `<rect x="${(i * bw + 1).toFixed(1)}" y="${(H - h).toFixed(1)}" width="${(bw - 2).toFixed(1)}" height="${h.toFixed(1)}" fill="${col}"><title>${p.strike}: ${p.straddle}</title></rect>`;
    }).join("");
    return `<div class="lbl"><span>straddle by strike (<span style="color:#18ffff">centre</span> · <span style="color:#FFD700">ATM</span>)</span><span>${n(c[0].strike, 0)} – ${n(c[c.length - 1].strike, 0)}</span></div>
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:46px">${bars}</svg>`;
  }
  function card(x) {
    if (!x.available) return `<div class="c"><div class="hd"><b>${esc(x.und)}</b></div><div class="na">${esc(x.note)}</div></div>`;
    const so = x.since_open || {}, m15 = x.last_15m || {};
    return `<div class="c">
      <div class="hd"><div><b>${esc(x.und)}</b><span class="tag ${x.live ? "live" : "old"}">${x.live ? "LIVE" : "SESSION " + esc(x.session)}</span></div>
        <span>expiry ${esc(x.expiry)} · ${tm(x.as_of)} · ${x.snapshots} snaps</span></div>
      <div class="m">
        <div class="k"><span>Index</span><b>${n(x.spot, 1)}</b><i>${pc(so.spot_chg_pct)} since open</i></div>
        <div class="k"><span>ATM ${n(x.atm, 0)} straddle</span><b style="color:#FFD700">${n(x.atm_straddle, 1)}</b><i>CE ${n(x.ce)} + PE ${n(x.pe)}</i></div>
        <div class="k"><span>Straddle change</span><b>${pc(so.straddle_chg_pct)}</b><i>15m ${pc(m15.straddle_chg_pct)}</i></div>
        <div class="k"><span>Centre (cheapest)</span><b style="color:#18ffff">${n(x.centre, 0)}</b><i>open ${n((x.open || {}).centre, 0)} ${arrow(so.centre_shift_strikes || 0)}</i><i>15m ${arrow(m15.centre_shift_strikes || 0)} strikes</i></div>
        <div class="k"><span>Breakevens</span><b style="font-size:.72rem;white-space:nowrap">${n(x.be_low, 0)}–${n(x.be_high, 0)}</b><i>exp. move ±${n(x.expected_move, 0)}</i></div>
        <div class="k"><span>ATM IV</span><b>${n(x.iv, 1)}%</b><i>forward ${n(x.forward, 1)}</i></div>
      </div>
      ${charts(x)}${curve(x)}
      <ul>${(x.readings || []).map(r => `<li>${esc(r)}</li>`).join("")}</ul></div>`;
  }
  async function load(el, refresh) {
    try {
      const r = await fetch("/api/agent/straddle" + (refresh ? "/refresh" : ""), {method: refresh ? "POST" : "GET", cache: "no-store"});
      if (r.status === 404) throw new Error("MarketMiner is still running the previous version. Close the MarketMiner server window and start launch_marketminer.cmd again to load the straddle monitor.");
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.status);
      const st = d.status || {};
      const errs = Object.entries(st.errors || {}).map(([k, v]) => `${k}: ${v}`).join(" · ");
      el.innerHTML = `<div class="mmsd"><h3><span>Straddle monitor · where the straddle is and where it is shifting</span>
        <span><span style="color:#5b6b85;font-weight:500;text-transform:none">${st.market_open ? "recording every minute" : "market closed: last session shown"}${st.last ? " · last " + tm(st.last) : ""}</span>
        <button class="btn" id="mmsdRef">↻ Snapshot now</button></span></h3>
        <div class="g">${(d.indices || []).map(card).join("")}</div>
        ${errs ? `<div class="lbl"><span class="wa">${esc(errs)}</span></div>` : ""}
        <div class="lbl"><span>Source: FYERS option chain, nearest expiry, mid of bid/ask. Centre = strike with the lowest call+put premium. Information only.</span></div></div>`;
      const b = el.querySelector("#mmsdRef");
      if (b) b.onclick = () => { b.textContent = "…"; load(el, true); };
    } catch (e) {
      el.innerHTML = `<div class="mmsd"><h3>Straddle monitor</h3><div class="na">Straddle data unavailable: ${esc(e.message)}</div></div>`;
    }
  }
  function mount(el) {
    if (!el || el._mmsd) return;
    el._mmsd = true;
    if (!document.getElementById("mmsdCss")) {
      const s = document.createElement("style"); s.id = "mmsdCss"; s.textContent = CSS; document.head.appendChild(s);
    }
    load(el);
    setInterval(() => load(el), 60000);
  }
  window.MMStraddle = {mount};
  const auto = () => mount(document.getElementById("mmStraddle"));
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", auto); else auto();
})();
