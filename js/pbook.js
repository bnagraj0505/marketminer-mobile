/* MarketMiner "Profit booking or fresh selling" block (index level). Usage: <div id="mmPbook"></div><script src="/js/pbook.js"></script>
   Data: GET /api/agent/pbook (today's 15m leg + per-minute option-chain OI snapshots). Information only. */
(function () {
  const CSS = `
.mmpb{margin:10px 16px;border:1px solid #1b2536;border-radius:10px;background:#0d1117;padding:12px;font-family:Inter,system-ui,sans-serif;color:#e2e8f0}
.mmpb h3{font:700 .72rem 'JetBrains Mono',ui-monospace,monospace;color:#8b9ab5;letter-spacing:.6px;text-transform:uppercase;margin:0 0 10px;display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap}
.mmpb .tw{overflow-x:auto}.mmpb table{width:100%;border-collapse:collapse;font:500 .72rem 'JetBrains Mono',monospace}
.mmpb th{text-align:left;color:#5b6b85;font-weight:600;padding:6px;border-bottom:1px solid #1b2536;white-space:nowrap}
.mmpb td{padding:7px 6px;border-bottom:1px solid #1b2536;vertical-align:top}
.mmpb .chip{display:inline-block;font:700 .62rem 'JetBrains Mono',monospace;padding:3px 7px;border-radius:5px;letter-spacing:.3px;white-space:nowrap}
.mmpb .TRENDING{color:#00e676;background:rgba(0,230,118,.1)}.mmpb .PROFIT_BOOKING,.mmpb .SHORT_COVERING{color:#18ffff;background:rgba(24,255,255,.08)}
.mmpb .PULLBACK{color:#8b9ab5;background:#182030}.mmpb .BOOKING_UNWIND{color:#ffab00;background:rgba(255,171,0,.1)}
.mmpb .FRESH_SELLING,.mmpb .FRESH_BUYING{color:#ff9100;background:rgba(255,145,0,.12)}.mmpb .REVERSAL_RISK,.mmpb .REVERSAL{color:#fff;background:#7f1d1d}
.mmpb .RANGE{color:#8b9ab5;background:#182030}
.mmpb .d{display:block;font:500 .6rem 'JetBrains Mono',monospace;color:#5b6b85;margin-top:3px}
.mmpb .up{color:#00e676}.mmpb .dn{color:#ff5252}.mmpb .wa{color:#ffab00}
.mmpb .na{font:500 .72rem 'JetBrains Mono',monospace;color:#5b6b85;padding:14px;text-align:center}
.mmpb .lbl{font:500 .6rem 'JetBrains Mono',monospace;color:#5b6b85;margin-top:8px;line-height:1.5}`;
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const n = (x, d = 0) => x === null || x === undefined || isNaN(x) ? "–" : Number(x).toLocaleString("en-IN", {maximumFractionDigits: d});
  const hm = t => t ? new Date(t * 1000).toLocaleTimeString("en-IN", {hour: "2-digit", minute: "2-digit", hour12: false}) : "";
  const lakh = x => x === null || x === undefined ? "–" : Math.abs(x) >= 1e5 ? n(x / 1e5, 1) + "L" : n(x, 0);
  const NAME = {TRENDING: "TRENDING", PROFIT_BOOKING: "PROFIT BOOKING", SHORT_COVERING: "SHORT COVERING", PULLBACK: "PULLBACK", BOOKING_UNWIND: "BOOKING + UNWIND",
                FRESH_SELLING: "FRESH SELLING", FRESH_BUYING: "FRESH BUYING", REVERSAL_RISK: "REVERSAL RISK", REVERSAL: "REVERSING", RANGE: "RANGE"};
  const pc = (x, good) => x === null || x === undefined ? "–" : `<span class="${Math.abs(x) < 3 ? "" : (x > 0) === good ? "up" : "dn"}">${x > 0 ? "+" : ""}${n(x, 1)}%</span>`;
  function render(el, d) {
    const rows = (d.indices || []).map(x => {
      const up = x.trend === "bullish", dn = x.trend === "bearish", o = x.oi;
      const move = up || dn ? `${up ? '<span class="up">▲ up</span>' : '<span class="dn">▼ down</span>'} ${n(x.leg_start)} → ${n(x.leg_end)}${x.leg_end_time ? " @" + hm(x.leg_end_time) : ""}
        <span class="d">now ${n(x.close, 1)} · ${n(x.depth_pct)}% given back</span><span class="d">${esc(x.zone || "")}</span>` : '<span class="d" style="margin:0">no clear move</span>';
      const oi = o ? `CE ${pc(o.ce_pct, !up)} <span class="d" style="display:inline">(${lakh(o.ce_chg)})</span><br>PE ${pc(o.pe_pct, up)} <span class="d" style="display:inline">(${lakh(o.pe_chg)})</span>
        <span class="d">PCR ${n(o.pcr_from, 2)} → ${n(o.pcr_now, 2)} · since ${hm(o.since)} · ${o.strikes} strikes</span>` : '<span class="d" style="margin:0">no OI snapshots since the extreme yet</span>';
      const f = x.fib || {};
      const lv = up || dn ? ["23.6", "38.2", "50.0", "61.8", "78.6"].map(k => f[k] !== undefined ? `<span class="d" style="margin:0${(up ? x.close < f[k] : x.close > f[k]) ? ";color:#ffab00" : ""}">${k}% ${n(f[k])}</span>` : "").join("") : "";
      return `<tr><td><b style="color:#18ffff">${esc(x.und)}</b></td>
        <td><span class="chip ${x.verdict}">${NAME[x.verdict] || esc(x.verdict)}</span><span class="d" style="color:#8b9ab5;max-width:420px">${esc(x.reading)}</span></td>
        <td>${move}</td><td>${oi}</td><td>${lv}</td></tr>`;
    }).join("");
    el.innerHTML = `<div class="mmpb"><h3><span>Profit booking or fresh selling · index level · today</span><span style="text-transform:none;font-weight:500;color:#5b6b85">price pullback + option writers since today's extreme · updates every minute</span></h3>
      ${!rows && d.updating ? `<div class="na">⏳ Fetching candles…</div>` : ""}
      ${rows ? `<div class="tw"><table><tr><th>Index</th><th>Reading</th><th>Today's move (15m)</th><th>Writers since the extreme</th><th>Pullback levels</th></tr>${rows}</table></div>` : d.updating ? "" : `<div class="na">No indices configured</div>`}
      <div class="lbl">In an up-move: price off the high with call OI not building and put OI holding = <b>profit booking</b> (longs taking profit, writers not betting against the move). Put OI falling = support leaving (deeper pullback risk). Call OI building = <b>fresh selling</b>; with put OI falling too = <b>reversal risk</b>. Mirror in a down-move (short covering vs fresh buying). Zones: 23.6–38.2% normal profit booking, 38.2–61.8% deeper, 61.8–78.6% last line, beyond 78.6% the move is reversing. Levels crossed are amber. ${esc(d.note || "")}</div></div>`;
  }
  async function load(el) {
    try {
      const r = await fetch("/api/agent/pbook", {cache: "no-store"});
      if (r.status === 404) throw new Error("MarketMiner is still running the previous version. Close the server window and start launch_marketminer.cmd again.");
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.status);
      render(el, d);
      if (d.updating && !(d.indices || []).length) setTimeout(() => load(el), 8000);
    } catch (e) {
      el.innerHTML = `<div class="mmpb"><h3>Profit booking or fresh selling</h3><div class="na">Unavailable: ${esc(e.message)}</div></div>`;
    }
  }
  function mount(el) {
    if (!el || el._mmpb) return;
    el._mmpb = true;
    if (!document.getElementById("mmpbCss")) { const s = document.createElement("style"); s.id = "mmpbCss"; s.textContent = CSS; document.head.appendChild(s); }
    el.innerHTML = `<div class="mmpb"><h3>Profit booking or fresh selling · index level</h3><div class="na">⏳ Loading…</div></div>`;
    load(el); setInterval(() => load(el), 60000);
  }
  window.MMPbook = {mount};
  const auto = () => mount(document.getElementById("mmPbook"));
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", auto); else auto();
})();
