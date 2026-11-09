/* MarketMiner AI Agent: "Where it's failing" tab. Reads /api/agent/diagnosis (backend/agent/diagnose.py). PAPER ONLY.
 * Shows what the agent has learned from every trade AND every setup it skipped, which checks protect or cost it,
 * exit problems, data blackouts, ML/research status, and the learned filters it applies (you can lift any). */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const E = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const R = (x, d = 2) => (x == null || isNaN(x) ? "—" : (x > 0 ? "+" : "") + (+x).toFixed(d) + "R");
  const P = (x) => (x == null ? "—" : (+x).toFixed(0) + "%");
  const cls = (x) => (x > 0 ? "up" : x < 0 ? "dn" : "");
  const when = (t) => (t ? new Date(t * 1000).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—");
  let D = null, dim = "strategy", busy = false, lastAt = 0;
  const GRADE = { "reliable weakness": ["dn", "RELIABLE WEAKNESS"], "leaning weak": ["wa", "leaning weak"], "reliable strength": ["up", "RELIABLE STRENGTH"],
    "leaning strong": ["up", "leaning strong"], unclear: ["mu", "unclear"], "no data": ["mu", "—"] };
  const SEV = { high: ["dn", "HIGH"], medium: ["wa", "MEDIUM"], low: ["mu", "LOW"], info: ["mu", "INFO"] };

  async function api(p, body) {
    const r = await fetch("/api/agent" + p, body === undefined ? { cache: "no-store" } : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || r.statusText);
    return j;
  }
  async function load(fresh) {
    if (busy) return; busy = true;
    try { D = await api("/diagnosis" + (fresh ? "?fresh=1" : "")); lastAt = Date.now(); render(); }
    catch (e) { const el = $("diag"); if (el) el.innerHTML = `<div class="cd"><div class="note dn">Diagnosis not available: ${E(e.message)}. Restart MarketMiner with the launcher after the update.</div></div>`; }
    finally { busy = false; }
  }
  const ci = (s) => (s && s.n > 1 && s.ci_lo != null ? `<span class="mu">[${R(s.ci_lo)} … ${R(s.ci_hi)}]</span>` : "");
  const stat = (label, s, sub) => `<div class="metric"><span>${label}</span><b class="${cls(s && s.mean_r)}">${s && s.n ? R(s.mean_r) : "—"}</b><div class="note">${s && s.n ? `${s.n} outcomes · win ${P(s.win_rate)} ${ci(s)}` : "no outcomes yet"}${sub ? "<br>" + sub : ""}</div></div>`;

  function render() {
    const el = $("diag"); if (!el || !D) return;
    if (!document.getElementById("mmDiagCss")) { const c = document.createElement("style"); c.id = "mmDiagCss";
      c.textContent = "#diag .g2,#diag .grid{min-width:0}#diag .g2>*{min-width:0}@media(max-width:760px){#diag .g2{grid-template-columns:1fr}}#diag h3 .hr{flex-wrap:wrap}"; document.head.appendChild(c); }
    const o = D.overview, st = D.settings;
    let h = `<div class="cd"><h3><span>Where the agent is failing · what it has learned</span><span class="hr">
        <span class="note">last ${D.window_days} days · updated ${when(D.generated)}</span><button class="btn sm" onclick="MMDiag.load(true)">↻ Re-analyse now</button></span></h3>
      <div class="note" style="margin-bottom:10px;line-height:1.6">Uses every closed trade <b>and every setup the agent skipped</b> (what price did next: T1 or the stop first). Skipped setups are <b>estimates</b>: stop = −1R, T1 = up to ${st.shadow_cap_r}R, minus ${st.cost_haircut_r}R costs; a setup re-flagged on the next candles counts once. A finding is <b>reliable</b> only with ≥ ${st.min_n} outcomes and a 95% range entirely on one side of zero; everything else is “leaning” and is never acted on.</div>
      <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(190px,1fr))">
        <div class="metric"><span>Decisions</span><b>${o.decisions}</b><div class="note">${o.executed} traded · ${o.rejected_guardian} vetoed by risk checks · ${o.declined_strategy} declined by strategy rules</div></div>
        ${stat("Real trades (after costs)", o.real, `${o.trades_closed} closed`)}
        ${stat("Vetoed setups, if taken", o.shadow_taken_would, "positive = the risk checks blocked winners")}
        ${stat("Declined setups, if taken", o.shadow_declined_would, "negative = the strategy rules were right")}
      </div></div>`;

    // suggestions
    h += `<div class="cd"><h3><span>What to fix · in order</span><span class="note">only filters are applied automatically; anything that loosens risk needs you</span></h3>
      ${D.suggestions.length ? D.suggestions.map((s) => `<div style="padding:9px 0;border-bottom:1px solid var(--bdr)">
        <div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap"><span class="chip ${s.severity === "high" ? "badc" : ""}" style="padding:2px 6px"><b class="${SEV[s.severity][0]}">${SEV[s.severity][1]}</b></span>
        <b style="font:700 .8rem var(--mono)">${E(s.title)}</b>${s.auto ? '<span class="chip okc" style="padding:2px 6px">auto</span>' : ""}</div>
        <div class="note" style="margin-top:4px">${E(s.detail)}</div><div class="note" style="margin-top:3px;color:var(--t2)">→ ${E(s.action)}</div></div>`).join("") : '<div class="note">Nothing stands out yet.</div>'}</div>`;

    // weaknesses / strengths
    const wsRow = (w) => `<tr><td>${E(w.label)}</td><td><b>${E(w.value)}</b></td><td>${w.n}<span class="mu"> (${w.trades}T/${w.shadow}S)</span></td><td>${P(w.win_rate)}</td><td class="${cls(w.mean_r)}">${R(w.mean_r)}</td><td>${ci(w)}</td><td class="${GRADE[w.grade][0]}">${GRADE[w.grade][1]}</td></tr>`;
    const wsTable = (L, empty) => L.length ? `<div style="overflow:auto;max-height:340px"><table><tr><th>Dimension</th><th>Value</th><th>N</th><th>Win</th><th>R each</th><th>95% range</th><th>Verdict</th></tr>${L.map(wsRow).join("")}</table></div>` : `<div class="note">${empty}</div>`;
    h += `<div class="g2"><div class="cd"><h3><span>Weak spots</span><span class="note">T = real trades, S = skipped setups</span></h3>${wsTable(D.weaknesses, "No weak spot yet.")}</div>
      <div class="cd"><h3><span>Strong spots</span></h3>${wsTable(D.strengths, "No strong spot yet.")}</div></div>`;

    // veto audit
    const vrow = (v) => `<tr><td class="mu">${E(v.by)}</td><td><b>${E(v.check)}</b></td><td>${v.n}</td><td>${P(v.win_rate)}</td><td class="${cls(v.mean_r)}">${R(v.mean_r)}</td><td>${ci(v)}</td>
      <td class="${v.verdict === "protecting you" ? "up" : v.verdict === "costing trades" ? "dn" : "mu"}">${E(v.verdict)}</td></tr>`;
    h += `<div class="cd"><h3><span>Are its “no”s right? · each check and rule, if the agent had ignored it</span></h3>
      ${D.vetoes.length ? `<div style="overflow:auto;max-height:360px"><table><tr><th>By</th><th>Check / reason</th><th>Stopped</th><th>Would win</th><th>R each if taken</th><th>95% range</th><th>Verdict</th></tr>${D.vetoes.map(vrow).join("")}</table></div>` : '<div class="note">No skipped setup has a result yet.</div>'}
      <div class="note" style="margin-top:6px">“Protecting you” = the setups it stopped mostly lost. “Costing trades” = they mostly won: worth reviewing, but a small sample can mislead.</div></div>`;

    // segment explorer
    const dims = Object.keys(D.segments);
    const rows = D.segments[dim] || [];
    h += `<div class="cd"><h3><span>Explore every segment</span><select class="btn sm" onchange="MMDiag.dim(this.value)">${dims.map((d) => `<option value="${d}" ${d === dim ? "selected" : ""}>${E(D.dim_labels[d] || d)}</option>`).join("")}</select></h3>
      ${rows.length ? `<div style="overflow:auto;max-height:360px"><table><tr><th>${E(D.dim_labels[dim] || dim)}</th><th>N</th><th>Trades</th><th>Skipped</th><th>Win</th><th>Win 95%</th><th>R each</th><th>95% range</th><th>Total R</th><th>Verdict</th></tr>
      ${rows.map((r) => `<tr><td><b>${E(r.value)}</b></td><td>${r.n}</td><td>${r.trades}</td><td>${r.shadow}</td><td>${P(r.win_rate)}</td><td class="mu">${P(r.win_lo)}–${P(r.win_hi)}</td><td class="${cls(r.mean_r)}">${R(r.mean_r)}</td><td>${ci(r)}</td><td class="${cls(r.total_r)}">${R(r.total_r, 1)}</td><td class="${GRADE[r.grade][0]}">${GRADE[r.grade][1]}</td></tr>`).join("")}</table></div>` : '<div class="note">No outcomes yet.</div>'}</div>`;

    // exits + lot blocks
    const ex = D.exits, lb = D.lot_blocks;
    h += `<div class="g2"><div class="cd"><h3><span>How trades end · exit quality</span></h3>${ex.n ? `
      <div class="chips" style="margin-bottom:8px"><span class="chip">closed <b>${ex.n}</b></span><span class="chip ${ex.giveback_trades ? "badc" : ""}">gave back ≥1R <b>${ex.giveback_trades}</b>${ex.giveback_trades ? ` (avg ${ex.avg_giveback_r}R)` : ""}</span>
      <span class="chip">stop exits <b>${ex.stop_exits}</b>${ex.avg_stop_r != null ? ` avg ${R(ex.avg_stop_r)}` : ""}</span><span class="chip ${ex.bad_stop_fills ? "badc" : ""}">stops worse than −1.25R <b>${ex.bad_stop_fills}</b></span>
      <span class="chip ${ex.stopped_then_t1 ? "badc" : ""}">stopped, then T1 hit <b>${ex.stopped_then_t1}</b></span><span class="chip">time stops <b>${ex.time_stops}</b></span>
      <span class="chip">entry slippage <b>${ex.avg_entry_slippage_r != null ? ex.avg_entry_slippage_r + "R" : "—"}</b></span></div>
      <div style="overflow:auto;max-height:260px"><table><tr><th>#</th><th>Symbol</th><th>Strategy</th><th>Result</th><th>Best</th><th>Worst</th><th>Exit</th><th>Problems</th></tr>
      ${ex.trades.map((t) => `<tr><td>${t.id}</td><td>${E(String(t.symbol).split(":").pop())}</td><td>${E(t.strategy)}</td><td class="${cls(t.r)}">${R(t.r)}</td><td class="up">${t.mfe != null ? "+" + t.mfe + "R" : "—"}</td><td class="dn">${t.mae != null ? "−" + t.mae + "R" : "—"}</td><td>${E(t.exit)}</td><td class="wa">${t.flags.map(E).join("<br>") || '<span class="mu">—</span>'}</td></tr>`).join("")}</table></div>` : '<div class="note">No closed trades yet.</div>'}</div>
      <div class="cd"><h3><span>Option lots vs AI risk</span></h3>${lb.n ? `<div class="note" style="line-height:1.6">${lb.n} option setup(s) rejected because one lot risks about <b>₹${(+lb.median_lot_risk).toLocaleString("en-IN")}</b> (max ₹${(+lb.max_lot_risk).toLocaleString("en-IN")}) and the AI allows <b>₹${(+lb.ai_risk).toLocaleString("en-IN")}</b> per trade.<br>
        Underlyings: ${lb.symbols.map((s) => E(s.split(":").pop())).join(", ")}<br>If taken: ${lb.shadow.n ? `${lb.shadow.n} resolved · win ${P(lb.shadow.win_rate)} · <b class="${cls(lb.shadow.mean_r)}">${R(lb.shadow.mean_r)}</b> each (estimate)` : "no results yet"}.</div>` : '<div class="note">No option setup was blocked by lot size.</div>'}</div></div>`;

    // data health + ML + research
    const dh = D.data_health;
    h += `<div class="g2"><div class="cd"><h3><span>Could it see the market? · data health</span></h3>
      <div style="overflow:auto"><table><tr><th>Day</th><th>Candles analysed</th><th>Decisions</th><th>Data gaps</th><th>FYERS limit hit</th><th>Errors</th></tr>
      ${dh.days.map((d) => `<tr><td>${E(d.day)}</td><td>${d.analysed}</td><td>${d.decisions}</td><td class="${d.data_gaps ? "wa" : ""}">${d.data_gaps}</td><td class="${d.quota_at ? "dn" : "mu"}">${d.quota_at ? `at ${E(d.quota_at)} · ~${d.blind_min} min blind` : "no"}</td><td class="${d.errors ? "dn" : ""}">${d.errors}</td></tr>`).join("")}</table></div></div>
      <div class="cd"><h3><span>Its learning engines</span></h3>
      <div class="note" style="line-height:1.6"><b>ML model:</b> ${D.ml.models ? `${D.ml.models} training run(s), ${D.ml.approved} approved. Latest #${D.ml.latest.id} (${E(D.ml.latest.status)}, ${D.ml.latest.samples} samples):<br>${(D.ml.latest.failed_gates || []).map((g) => `<span class="dn">✗</span> ${E(g)}`).join("<br>")}${(D.ml.latest.passed_gates || []).map((g) => `<br><span class="up">✓</span> ${E(g)}`).join("")}` : "not trained yet."}</div>
      <div class="note" style="line-height:1.6;margin-top:8px"><b>Research engine:</b> ${D.research.experiments ? `${D.research.experiments} experiments (${Object.entries(D.research.by_status).map(([k, v]) => `${v} ${E(k)}`).join(", ")}). Most common reasons they failed:<br>${D.research.top_fail_reasons.map(([r, n]) => `${E(r)} <span class="mu">×${n}</span>`).join("<br>")}` : "no experiments in this window."}</div></div></div>`;

    // filters + settings
    const F = D.filters;
    h += `<div class="cd"><h3><span>Learned filters · what it now refuses to trade</span><span class="hr">
        <label class="note" style="display:flex;gap:5px;align-items:center"><input type="checkbox" ${st.auto_filters ? "checked" : ""} onchange="MMDiag.set({auto_filters:this.checked})"> learn filters automatically</label>
        <label class="note">min outcomes <input type="number" min="4" max="50" value="${st.min_n}" style="width:52px" class="btn sm" onchange="MMDiag.set({min_n:+this.value})"></label>
        <label class="note">max filters <input type="number" min="0" max="20" value="${st.max_filters}" style="width:52px" class="btn sm" onchange="MMDiag.set({max_filters:+this.value})"></label>
        <label class="note">trial days <input type="number" min="3" max="60" value="${st.lift_days}" style="width:52px" class="btn sm" onchange="MMDiag.set({lift_days:+this.value})"></label></span></h3>
      ${F.active.length ? `<div style="overflow:auto"><table><tr><th>Blocking</th><th>Since</th><th>Evidence</th><th>Blocked since</th><th></th></tr>${F.active.map((f) => `<tr><td><b>${E(f.label)}</b></td><td>${when(f.t)}</td><td class="note">${E(f.why)}</td>
        <td class="note">${f.blocked && f.blocked.n ? `${f.blocked.n} setups · would have ${R(f.blocked.mean_r)} each` : "none yet"}</td><td><button class="btn sm" onclick="MMDiag.lift('${E(f.id)}')">Lift</button></td></tr>`).join("")}</table></div>`
        : `<div class="note">No filter active${st.auto_filters ? ": nothing is a reliable weakness yet." : " (automatic filters are switched off)."}</div>`}
      ${F.lifted.length ? `<div class="note" style="margin-top:8px"><b>Lifted:</b> ${F.lifted.map((f) => `${E(f.label)} (${E(f.lift_why)}, ${when(f.lifted_t)})`).join(" · ")}</div>` : ""}
      <div class="note" style="margin-top:8px;line-height:1.55">A filter is added only for a reliable weakness, never covers more than ${Math.round(st.max_cover * 100)}% of recent setups, keeps being tracked while it blocks, and is lifted when the blocked setups would have made money or after the trial period. Filters only ever make the agent more selective.</div></div>`;
    el.innerHTML = h;
  }

  window.MMDiag = {
    load,
    dim: (d) => { dim = d; render(); },
    set: async (o) => { try { D = await api("/diagnosis/settings", o); render(); } catch (e) { alert(e.message); } },
    lift: async (id) => { if (!confirm("Lift this filter? The agent may trade these setups again.")) return; try { D = await api("/diagnosis/filters/lift", { id }); render(); } catch (e) { alert(e.message); } },
    maybe: () => { if (Date.now() - lastAt > 60000) load(false); },
  };
})();
