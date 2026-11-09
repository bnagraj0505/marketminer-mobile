/* MarketMiner "Profit booking tracker" (AI Agent → Overview). Data: GET /api/agent/bookings. Read-only.
   Usage: <div id="mmBookings"></div><script src="/js/bookings.js"></script> (uses the agent page's styles). */
(function () {
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const n = (x, d = 2) => x === null || x === undefined || isNaN(x) ? "–" : Number(x).toLocaleString("en-IN", {maximumFractionDigits: d});
  const p = x => x === null || x === undefined ? "–" : Math.abs(x) >= 1000 ? n(x, 1) : Math.abs(x) >= 1 ? n(x, 2) : n(x, 5);
  const R = x => x === null || x === undefined || isNaN(x) ? "–" : `<span class="${x > 0 ? "up" : x < 0 ? "dn" : "mu"}">${x > 0 ? "+" : ""}${n(x, 2)}R</span>`;
  const tm = t => t ? new Date(t * 1000).toLocaleString("en-IN", {day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false}) : "–";
  const KIND = {entry: "Entry", lot1: "Lot 1 booked (1:2)", breakeven: "Lot 2 stop → breakeven", trail: "Trail stop moved", exit: "Exit"};
  const open = {};
  function evRow(e, opt) {
    const px = opt ? e.value : e.price;
    const bits = [];
    if (px !== undefined) bits.push((opt ? "value " : "@ ") + p(px));
    if (e.stop !== undefined) bits.push("stop " + p(e.stop));
    if (e.tp1 !== undefined && e.tp1 !== null) bits.push("lot 1 target " + p(e.tp1));
    if (e.locked_r !== undefined) bits.push("locks " + R(e.locked_r));
    if (e.r !== undefined) bits.push(R(e.r));
    if (e.lots) bits.push(esc(e.lots));
    if (e.reason) bits.push(esc(e.reason));
    return `<div><span class="mu">${tm(e.t)}</span> <b>${KIND[e.kind] || esc(e.kind)}</b> · ${bits.join(" · ")}</div>`;
  }
  function render(el, d) {
    const s = d.summary || {};
    const m = (k, v, cls) => `<div class="metric"><span>${k}</span><b class="${cls || ""}">${v}</b></div>`;
    const rows = (d.trades || []).map(v => {
      const opt = v.kind === "options";
      const l1 = v.lot1 ? `<span class="up">✓ ${R(v.lot1.r)}</span><br><span class="mu">${tm(v.lot1.at)}</span>` : v.status === "open" ? `<span class="mu">waiting · ${p(v.lot1_target)}</span>` : `<span class="dn">✗ not reached</span>`;
      const l2 = v.status === "open"
        ? (v.lot1 ? `stop ${p(v.stop_now)} (locks ${R(v.locked_r)})<br><span class="mu">best ${p(v.best)} = ${R(v.best_r)}</span>` : `stop ${p(v.stop_now)}`)
        : (v.lot2_r !== null ? `${R(v.lot2_r)} <span class="mu">${esc(v.exit_reason || "")}</span>` : "–");
      return `<tr class="clk" data-id="${v.id}"><td>#${v.id} <b>${esc(v.label)}</b><br><span class="mu">${esc(v.strategy)} · ${tm(v.opened_at)}</span></td>
        <td><span class="tag ${v.status === "open" ? "ob" : "dc"}">${v.status.toUpperCase()}</span></td>
        <td>${p(v.entry)}${opt ? ' <span class="mu">/unit</span>' : ""}</td><td>${l1}</td><td>${l2}</td>
        <td>${v.giveback_r !== null ? R(-v.giveback_r) : "–"}</td><td>${v.trail_extra_r !== null ? R(v.trail_extra_r) : "–"}</td>
        <td>${v.trade_r !== null && v.trade_r !== undefined ? R(v.trade_r) : "–"}${v.pnl !== null && v.pnl !== undefined ? `<br><span class="mu">${n(v.pnl, 0)} ${esc(v.currency || "")}</span>` : ""}</td>
        <td class="mu">${esc(v.outcome)}</td></tr>
        ${open[v.id] ? `<tr><td colspan="9"><div class="det note">${(v.events || []).map(e => evRow(e, opt)).join("") || "No booking events recorded (position opened before the tracker existed)."}</div></td></tr>` : ""}`;
    }).join("");
    el.innerHTML = `<div class="cd"><h3>Profit booking tracker <span class="note">lot 1 books at 1:2 · lot 2 to breakeven, then trails · click a row for the stop's path</span></h3>
      <div class="mgrid" style="margin-bottom:10px">
        ${m("Closed trades", n(s.closed, 0))}${m("Lot 1 booked", s.lot1_booked_pct === null || s.lot1_booked_pct === undefined ? "–" : `${n(s.lot1_booked, 0)} · ${n(s.lot1_booked_pct, 0)}%`)}
        ${m("Stopped before 1:2", n(s.both_stopped, 0), s.both_stopped ? "dn" : "")}${m("Lot 2 at breakeven", n(s.lot2_breakeven, 0))}
        ${m("Lot 2 beyond 1:2", n(s.lot2_beyond_2r, 0), s.lot2_beyond_2r ? "up" : "")}${m("Avg lot 2", s.avg_lot2_r === null ? "–" : R(s.avg_lot2_r))}
        ${m("Trail vs booking at 1:2", s.avg_trail_extra_r === null ? "–" : R(s.avg_trail_extra_r))}${m("Avg give-back from peak", s.avg_giveback_r === null ? "–" : R(-s.avg_giveback_r))}
        ${m("Best runner", s.best_runner_r === null ? "–" : R(s.best_runner_r))}${m("Avg trade (after fees)", s.avg_trade_r_after_fees === null ? "–" : R(s.avg_trade_r_after_fees))}
      </div>
      ${s.verdict ? `<div class="note" style="margin-bottom:8px">${esc(s.verdict)}</div>` : ""}
      ${rows ? `<div class="tw"><table><tr><th>Position</th><th></th><th>Entry</th><th>Lot 1 (1:2)</th><th>Lot 2 (trail)</th><th>Give-back</th><th>Trail vs 1:2</th><th>Trade</th><th>Outcome</th></tr>${rows}</table></div>`
              : `<div class="empty">No 2-lot positions yet. When the agent trades, each booking (lot 1 at 1:2, breakeven, every trail step, the exit) shows here.</div>`}
      <div class="note" style="margin-top:6px">${esc(d.note || "")}</div></div>`;
    el.querySelectorAll("tr.clk").forEach(tr => tr.onclick = () => { open[tr.dataset.id] = !open[tr.dataset.id]; render(el, d); });
  }
  async function load(el) {
    el = el || document.getElementById("mmBookings");
    if (!el) return;
    try {
      const r = await fetch("/api/agent/bookings", {cache: "no-store"});
      if (r.status === 404) throw new Error("MarketMiner is still running the previous version: restart launch_marketminer.cmd");
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.status);
      render(el, d);
    } catch (e) {
      el.innerHTML = `<div class="cd"><h3>Profit booking tracker</h3><div class="empty">Unavailable: ${esc(e.message)}</div></div>`;
    }
  }
  window.MMBookings = {load};
})();
