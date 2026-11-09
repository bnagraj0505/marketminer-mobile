/* MarketMiner "Retracement or reversal" block. Usage: <div id="mmRetrace"></div> (or <div id="mmRetrace" data-market="crypto">) <script src="/js/retrace.js"></script>
   Data: GET /api/agent/retrace (5m / 15m with levels from today only; 1h trend context; Signal Lab swing + structure engine). Information only. */
(function () {
  const CSS = `
.mmrt{margin:10px 16px;border:1px solid #1b2536;border-radius:10px;background:#0d1117;padding:12px;font-family:Inter,system-ui,sans-serif;color:#e2e8f0}
.mmrt h3{font:700 .72rem 'JetBrains Mono',ui-monospace,monospace;color:#8b9ab5;letter-spacing:.6px;text-transform:uppercase;margin:0 0 10px;display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap}
.mmrt .tw{overflow-x:auto}.mmrt table{width:100%;border-collapse:collapse;font:500 .72rem 'JetBrains Mono',monospace}
.mmrt th{text-align:left;color:#5b6b85;font-weight:600;padding:6px;border-bottom:1px solid #1b2536;white-space:nowrap}
.mmrt td{padding:7px 6px;border-bottom:1px solid #1b2536;vertical-align:top}.mmrt tr.r{cursor:pointer}.mmrt tr.r:hover td{background:#131a27}
.mmrt .chip{display:inline-block;font:700 .62rem 'JetBrains Mono',monospace;padding:3px 7px;border-radius:5px;letter-spacing:.3px;white-space:nowrap}
.mmrt .TRENDING{color:#00e676;background:rgba(0,230,118,.1)}.mmrt .RETRACEMENT{color:#18ffff;background:rgba(24,255,255,.08)}
.mmrt .DEEP_RETRACEMENT{color:#ffab00;background:rgba(255,171,0,.1)}.mmrt .POSSIBLE_REVERSAL{color:#ff9100;background:rgba(255,145,0,.12)}
.mmrt .REVERSAL{color:#fff;background:#7f1d1d}.mmrt .RANGE{color:#8b9ab5;background:#182030}.mmrt .UNAVAILABLE{color:#5b6b85;background:#131a27}
.mmrt .d{display:block;font:500 .6rem 'JetBrains Mono',monospace;color:#5b6b85;margin-top:3px}
.mmrt .up{color:#00e676}.mmrt .dn{color:#ff5252}
.mmrt .det{background:#06080f;border:1px solid #1b2536;border-radius:7px;padding:9px;display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:10px}
.mmrt .det>div{min-width:0;overflow-wrap:anywhere}.mmrt .det b{display:block;margin-bottom:3px;font:700 .66rem 'JetBrains Mono',monospace;color:#8b9ab5}.mmrt .det li{font:500 .66rem Inter,sans-serif;color:#8b9ab5;margin-left:14px;line-height:1.45}
.mmrt .na{font:500 .72rem 'JetBrains Mono',monospace;color:#5b6b85;padding:14px;text-align:center}
.mmrt .lbl{font:500 .6rem 'JetBrains Mono',monospace;color:#5b6b85;margin-top:8px}`;
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const n = (x, d = 1) => x === null || x === undefined || isNaN(x) ? "–" : Number(x).toLocaleString("en-IN", {maximumFractionDigits: d});
  const NAME = {TRENDING: "TRENDING", RETRACEMENT: "RETRACING", DEEP_RETRACEMENT: "DEEP PULLBACK", POSSIBLE_REVERSAL: "REVERSAL RISK", REVERSAL: "REVERSING", RANGE: "RANGE", UNAVAILABLE: "NO DATA"};
  const open = {};
  const tfCell = v => {
    if (!v || !v.status) return `<span class="chip UNAVAILABLE">NO DATA</span>`;
    const up = v.trend === "bullish", dn = v.trend === "bearish", s = up ? "<" : ">";
    const dir = up ? '<span class="up">▲ up today</span>' : dn ? '<span class="dn">▼ down today</span>' : "range";
    const lv = v.protected !== undefined && v.protected !== null
      ? `<span class="d">pullback ${s} ${n(v.warning, v.warning > 1000 ? 0 : 2)} <span style="opacity:.7">(61.8%)</span></span><span class="d" style="color:${v.status === "REVERSAL" ? "#ff5252" : "#ffab00"}">reversal ${s} ${n(v.protected, v.protected > 1000 ? 0 : 2)} <span style="opacity:.7">(78.6%)</span></span><span class="d">leg ${n(v.leg_start, v.leg_start > 1000 ? 0 : 2)} → ${n(v.leg_end, v.leg_end > 1000 ? 0 : 2)}</span>`
      : v.range_high ? `<span class="d">today ${n(v.range_low, 0)}–${n(v.range_high, 0)}</span>` : "";
    return `<span class="chip ${v.status}">${NAME[v.status] || v.status}</span><span class="d">${dir}${v.depth_pct !== null && v.depth_pct !== undefined ? " · " + n(v.depth_pct, 0) + "% back" : ""}</span>${lv}`;
  };
  const ctxCell = v => !v ? `<span class="chip UNAVAILABLE">NO DATA</span>` :
    `<span class="d" style="margin:0">${v.trend === "bullish" ? '<span class="up">▲ up</span>' : v.trend === "bearish" ? '<span class="dn">▼ down</span>' : "neutral"}${v.rsi !== null && v.rsi !== undefined ? " · RSI " + n(v.rsi, 0) : ""}</span>`;
  function detail(x) {
    return `<div class="det">${["5m", "15m", "1h"].map(tf => {
      const v = (x.timeframes || {})[tf];
      if (!v) return `<div><b>${tf}</b><ul><li>${esc((x.errors || {})[tf] || "no data")}</li></ul></div>`;
      const fib = v.fib ? Object.entries(v.fib).map(([k, p]) => `${k}% ${n(p, 0)}`).join(" · ") : "";
      return `<div><b>${tf}${tf === "1h" ? " (context)" : " today"} · ${esc(v.label)}</b><ul>${(v.evidence || []).map(e => `<li>${esc(e)}</li>`).join("")}${fib ? `<li>retracement levels of today's leg: ${esc(fib)}</li>` : ""}</ul></div>`;
    }).join("")}</div>`;
  }
  function render(el, d) {
    const rows = (d.indices || []).map(x => {
      const T = x.timeframes || {}, m = T["15m"] || T["5m"] || {};
      return `<tr class="r" data-u="${esc(x.und)}"><td><b style="color:#18ffff">${esc(x.und)}</b><span class="d">${n(m.close, m.close > 1000 ? 1 : 2)}</span></td>
        <td><span class="chip ${x.verdict}">${NAME[x.verdict] || x.verdict}</span><span class="d" style="color:#8b9ab5">${esc(x.summary)}</span></td>
        <td>${tfCell(T["15m"])}</td><td>${tfCell(T["5m"])}</td><td>${ctxCell(T["1h"])}</td></tr>
        ${open[x.und] ? `<tr><td colspan="5">${detail(x)}</td></tr>` : ""}`;
    }).join("");
    el.innerHTML = `<div class="mmrt"><h3><span>Retracement or reversal · Fibonacci · intraday · ${el.dataset.market === "crypto" ? "crypto (day from 05:30 IST / 00:00 UTC)" : "all indices"}</span><span style="text-transform:none;font-weight:500;color:#5b6b85">click ${el.dataset.market === "crypto" ? "a coin" : "an index"} for the evidence · updates every minute</span></h3>
      ${!rows && d.updating ? `<div class="na">⏳ ${esc(d.note || "Fetching candles...")}</div>` : ""}
      ${rows ? `<div class="tw"><table><tr><th>${el.dataset.market === "crypto" ? "Coin" : "Index"}</th><th>Verdict (today)</th><th>15m swing</th><th>5m timing</th><th>1h trend (context)</th></tr>${rows}</table></div>` : d.updating ? "" : `<div class="na">No indices configured</div>`}
      <div class="lbl">Levels are Fibonacci retracements of TODAY's leg (closed candles): the leg runs from today's latest higher low (after a break of the prior high; the day's low until a swing forms) to today's high; mirror in a down-move. Pullback &lt; 61.8%: beyond it the pullback is deep. Reversal &lt; 78.6%: a close beyond it = REVERSING. Under 23.6% given back = trending. 15m = the swing, 5m = timing, 1h = trend context only. Information only.</div></div>`;
    el.querySelectorAll("tr.r").forEach(tr => tr.onclick = () => { open[tr.dataset.u] = !open[tr.dataset.u]; render(el, d); });
  }
  async function load(el) {
    try {
      const r = await fetch("/api/agent/retrace" + (el.dataset.market === "crypto" ? "?market=crypto" : ""), {cache: "no-store"});
      if (r.status === 404) throw new Error("MarketMiner is still running the previous version. Close the server window and start launch_marketminer.cmd again.");
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.status);
      if ((el.dataset.market || "india") !== (d.market || "india") && (d.indices || []).length)
        throw new Error("MarketMiner is still running the previous version (it sent index data). Close the server window, start launch_marketminer.cmd again, then press Ctrl+F5.");
      el._d = d; render(el, d);
      if (d.updating && !(d.indices || []).length) setTimeout(() => load(el), 8000);   // first fill after start-up
    } catch (e) {
      el.innerHTML = `<div class="mmrt"><h3>Retracement or reversal</h3><div class="na">Unavailable: ${esc(e.message)}</div></div>`;
    }
  }
  function mount(el) {
    if (!el || el._mmrt) return;
    el._mmrt = true;
    if (!document.getElementById("mmrtCss")) { const s = document.createElement("style"); s.id = "mmrtCss"; s.textContent = CSS; document.head.appendChild(s); }
    el.innerHTML = `<div class="mmrt"><h3>Retracement or reversal · ${el.dataset.market === "crypto" ? "crypto" : "all indices"}</h3><div class="na">⏳ Loading…</div></div>`;
    load(el); setInterval(() => load(el), 60000);
  }
  window.MMRetrace = {mount};
  const auto = () => mount(document.getElementById("mmRetrace"));
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", auto); else auto();
})();
