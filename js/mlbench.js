/* MarketMiner "ML & Workbench" tab (AI Agent page). Data: /api/agent/ml*, /api/agent/workbench*. Uses the page's styles.
   The model can only filter, rank and size DOWN; your capital rules stay in the Risk Guardian. */
(function () {
  const API = "/api/agent";
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const n = (x, d = 2) => x === null || x === undefined || isNaN(x) ? "–" : Number(x).toLocaleString("en-IN", {maximumFractionDigits: d});
  const pct = x => x === null || x === undefined || isNaN(x) ? "–" : n(x * 100, 0) + "%";
  const R = x => x === null || x === undefined || isNaN(x) ? "–" : `<span class="${x > 0 ? "up" : x < 0 ? "dn" : "mu"}">${x > 0 ? "+" : ""}${n(x, 3)}R</span>`;
  const tm = t => t ? new Date(t * 1000).toLocaleString("en-IN", {day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false}) : "–";
  const S = {ml: null, wb: null, openModel: null, edit: null, bt: null, btBusy: false, poll: null};
  async function get(p) { const r = await fetch(API + p, {cache: "no-store"}); const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.detail || r.status); return j; }
  async function post(p, b) { const r = await fetch(API + p, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(b || {})}); const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.detail || r.status); return j; }
  const say = (m, bad) => (window.toast ? window.toast(m, bad) : alert(m));
  const root = () => document.getElementById("mlbench");

  // ------------------------------------------------------------ ML model
  function mlHtml() {
    const d = S.ml;
    if (!d) return `<div class="cd"><h3>Machine learning</h3><div class="empty">Loading…</div></div>`;
    const c = d.config || {}, lib = d.library || {}, tr = d.training || {};
    const seg = ["off", "shadow", "active"].map(m => `<button class="${c.mode === m ? "on" : ""}" onclick="MMBench.mode('${m}')">${m.toUpperCase()}</button>`).join("");
    const samples = (d.samples || []).map(s => `${esc(s.tf)} ${esc(s.source)}: <b>${n(s.n, 0)}</b> (${pct(s.pos / s.n)} reached 1:2)`).join(" · ") || "none yet";
    const models = (d.models || []).map(m => {
      const x = m.metrics || {}, h = x.held_out || {};
      const gp = (x.gates || []).filter(g => g.ok).length, gt = (x.gates || []).length;
      const act = m.status === "eligible" ? `<button class="btn sm ok" onclick="event.stopPropagation();MMBench.approve(${m.id})">Approve</button> <button class="btn sm" onclick="event.stopPropagation();MMBench.reject(${m.id})">Reject</button>`
        : m.status === "approved" ? `<button class="btn sm bad" onclick="event.stopPropagation();MMBench.reject(${m.id})">Withdraw</button>` : "";
      const tag = {approved: "ap", eligible: "ob", rejected: "rj", rejected_by_user: "rj", retired: "dc"}[m.status] || "dc";
      return `<tr class="clk" onclick="MMBench.openModel(${m.id})"><td>#${m.id}${c.model_id === m.id ? ' <span class="tag ob">IN USE</span>' : ""}</td><td>${tm(m.t)}</td><td>${esc(m.tf)}</td>
        <td>${n(x.samples, 0)}</td><td>${n(h.auc, 3)}</td><td>${pct(h.kept_share)}</td><td>${R(h.expectancy_all)} → ${R(h.expectancy_kept)}</td>
        <td class="${gp === gt ? "up" : "dn"}">${gp}/${gt}</td><td><span class="tag ${tag}">${esc(m.status.toUpperCase())}</span></td><td>${act}</td></tr>
        ${S.openModel === m.id ? `<tr><td colspan="10">${modelDetail(m)}</td></tr>` : ""}`;
    }).join("");
    const lv = d.live || {}, ba = lv.by_action || {};
    const liveTxt = lv.resolved ? `${n(lv.resolved, 0)} live predictions resolved: reached 1:2 ${pct(lv.hit_rate)} (model said ${pct(lv.mean_p)}). ` +
      ["full", "half", "skip"].map(a => `${a}: ${n((ba[a] || {}).n, 0)} · ${pct((ba[a] || {}).hit_rate)} · ${R((ba[a] || {}).expectancy)}`).join(" | ") : "No live predictions resolved yet.";
    const preds = (d.predictions || []).slice(0, 25).map(p => `<tr><td>${tm(p.t)}</td><td>${esc(p.symbol)}</td><td>${esc(p.strategy)}</td><td>${esc(p.side)}</td>
      <td>${pct(p.p)}</td><td><span class="tag ${p.action === "full" ? "ap" : p.action === "half" ? "ob" : "rj"}">${esc(p.action.toUpperCase())}</span></td><td class="mu">${esc(p.mode)}</td>
      <td>${p.y === null ? '<span class="mu">pending</span>' : p.y ? '<span class="up">1:2 first</span>' : '<span class="dn">no</span>'}</td></tr>`).join("");
    return `<div class="cd"><h3>Machine learning · setup quality model <span class="seg">${seg}</span></h3>
      <div class="note" style="margin-bottom:8px">The model scores every setup the rules find: the chance of reaching lot 1's 1:2 before the stop. <b>SHADOW</b> records predictions and changes nothing.
      <b>ACTIVE</b> (only with a model you approved) can: <b>skip</b> low-probability setups, <b>rank</b> setups on the same candle, trade <b>half</b> risk on medium ones (crypto / cash; F&amp;O and options stay at your 2 lots),
      and keep the last slot of the part's risk budget for a full-confidence setup. It can never add a trade, size up or loosen a Risk Guardian rule. If live predictions turn worse than the base rate it drops back to shadow by itself.</div>
      <div class="chips" style="margin-bottom:8px"><span class="chip ${lib.ok ? "okc" : "badc"}">${esc(lib.detail)}</span><span class="chip">training data: ${samples}</span>
        ${d.drift && d.drift.checked ? `<span class="chip">live Brier ${n(d.drift.brier, 4)} vs base ${n(d.drift.brier_base, 4)}</span>` : ""}</div>
      <div class="hr" style="margin-bottom:8px"><input id="mlSyms" class="btn" style="min-width:280px" placeholder="symbols (blank = watch-list + options desk)">
        <button class="btn pri" ${tr.training || !lib.ok ? "disabled" : ""} onclick="MMBench.train()">${tr.training ? "⏳ " + esc(tr.progress || "training…") : "Train on Signal Lab history"}</button>
        ${tr.last ? `<span class="note">last run ${tm(tr.last.t)}: ${tr.last.ok ? "model #" + tr.last.model_id : esc(tr.last.error || "failed")}</span>` : ""}</div>
      ${models ? `<div class="tw"><table><tr><th>Model</th><th>Trained</th><th>TF</th><th>Samples</th><th>Held-out AUC</th><th>Kept</th><th>Held-out expectancy (all → kept)</th><th>Gates</th><th>Status</th><th></th></tr>${models}</table></div>`
                : `<div class="empty">No model yet. Press Train: it labels every setup in your Signal Lab history (about 100 days of 15m candles per symbol) and tests the model walk-forward.</div>`}
      <h3 style="margin-top:12px">Live record</h3><div class="note">${liveTxt}</div>
      ${preds ? `<div class="tw"><table><tr><th>Time</th><th>Symbol</th><th>Strategy</th><th>Side</th><th>P(1:2)</th><th>Action</th><th>Mode</th><th>Outcome</th></tr>${preds}</table></div>` : ""}</div>`;
  }
  function modelDetail(m) {
    const x = m.metrics || {}, h = x.held_out || {}, th = x.thresholds || {};
    const gates = (x.gates || []).map(g => `<div><span class="${g.ok ? "up" : "dn"}">${g.ok ? "✓" : "✗"}</span> ${esc(g.text)}</div>`).join("");
    const folds = (x.folds || []).map(f => `fold ${f.fold}: ${f.error ? esc(f.error) : `train ${f.train}, test ${f.test}, AUC ${n(f.auc, 3)}`}`).join(" · ");
    const cal = (x.calibration || []).map(c => `<tr><td>${esc(c.bin)}</td><td>${n(c.n, 0)}</td><td>${pct(c.predicted)}</td><td>${pct(c.actual)}</td></tr>`).join("");
    const ba = h.by_action || {};
    const acts = ["full", "half", "skip"].map(a => `<tr><td>${a}</td><td>${n((ba[a] || {}).n, 0)}</td><td>${pct((ba[a] || {}).hit_rate)}</td><td>${R((ba[a] || {}).expectancy)}</td></tr>`).join("");
    const mx = Math.max(1e-6, ...(x.importance || []).map(i => i.importance));
    const imp = (x.importance || []).slice(0, 12).map(i => `<div style="display:grid;grid-template-columns:150px 1fr 60px;gap:6px;align-items:center" title="${esc(i.describe)}">
      <span>${esc(i.feature)}</span><div class="bar"><i style="width:${Math.max(0, i.importance / mx * 100)}%"></i></div><span>${n(i.importance, 4)}</span></div>`).join("");
    const reg = Object.entries(x.importance_by_regime || {}).map(([k, v]) => `<div><b>${esc(k)}</b> (${v.n}): ${v.top.map(t => esc(t.feature)).join(", ")}</div>`).join("") || "<div class='mu'>not enough held-out setups per regime</div>";
    const bs = Object.entries(x.held_out_by_strategy || {}).sort((a, b) => b[1].n - a[1].n).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v.n}</td><td>${pct(v.hit_rate)}</td><td>${R(v.expectancy)}</td></tr>`).join("");
    return `<div class="det"><div class="grid">
      <div><b class="note">PROMOTION GATES (held-out block, used once)</b><div class="checks">${gates}</div>
        <div class="note">Label: ${esc(x.label || "")}. ${n(x.samples, 0)} setups from ${tm(x.from)} to ${tm(x.to)} on ${esc((x.symbols || []).join(", "))}. Base rate ${pct(x.base_rate)}.</div>
        <div class="note">Walk-forward: ${folds}</div>
        <div class="note">Thresholds: skip below ${pct(th.skip_below)}, full size from ${pct(th.full_from)} (chosen on blocks 1-3, not on the test block).</div></div>
      <div><b class="note">HELD-OUT RESULT BY ACTION</b><table><tr><th>Action</th><th>n</th><th>Reached 1:2</th><th>Expectancy</th></tr>${acts}</table>
        <b class="note">CALIBRATION (predicted vs actual)</b><table><tr><th>p</th><th>n</th><th>Predicted</th><th>Actual</th></tr>${cal}</table></div>
      <div><b class="note">WHAT THE MODEL USES (held-out permutation importance)</b><div class="note">${imp}</div><b class="note">TOP FEATURES BY REGIME</b><div class="note">${reg}</div></div>
      <div><b class="note">HELD-OUT BY STRATEGY</b><table><tr><th>Strategy</th><th>n</th><th>1:2 first</th><th>Exp.</th></tr>${bs}</table></div></div></div>`;
  }

  // ------------------------------------------------------------ workbench
  const blank = () => ({name: "", description: "", direction: "both", conditions: [{field: "ema50_dir", op: ">", value: 0}], stop: {type: "swing", atr_mult: 1.5},
                         stop_buffer_atr: 0.1, min_rr: 1.5, regimes: [], key: null});
  function condRow(c, k) {
    const w = S.wb;
    const f = w.fields.map(x => `<option value="${x.key}" ${x.key === c.field ? "selected" : ""} title="${esc(x.describe)}">${esc(x.key)}</option>`).join("");
    const o = w.ops.map(x => `<option ${x === c.op ? "selected" : ""}>${esc(x)}</option>`).join("");
    const v = Array.isArray(c.value) ? c.value.join(",") : c.value;
    const ds = (w.fields.find(x => x.key === c.field) || {}).describe || "";
    return `<div style="display:grid;grid-template-columns:150px 90px 160px 30px 1fr;gap:6px;align-items:center;margin-bottom:4px">
      <select onchange="MMBench.cond(${k},'field',this.value)">${f}</select><select onchange="MMBench.cond(${k},'op',this.value)">${o}</select>
      <input value="${esc(v)}" placeholder="${c.op === "between" ? "low,high" : c.op.includes("in") ? "a,b,c" : "number"}" onchange="MMBench.cond(${k},'value',this.value)">
      <button class="btn sm bad" onclick="MMBench.delCond(${k})">✕</button><span class="note">${esc(ds)}</span></div>`;
  }
  function editor() {
    const e = S.edit, w = S.wb;
    if (!e) return "";
    const regs = w.regimes.map(r => `<label style="flex-direction:row;align-items:center;gap:4px"><input type="checkbox" ${e.regimes.includes(r.key) ? "checked" : ""} onchange="MMBench.reg('${r.key}',this.checked)"> ${esc(r.label)}</label>`).join("");
    return `<div class="det"><div class="set">
      <label>Name<input value="${esc(e.name)}" onchange="MMBench.ed('name',this.value)"></label>
      <label>Direction<select onchange="MMBench.ed('direction',this.value)">${["both", "long", "short"].map(x => `<option ${x === e.direction ? "selected" : ""}>${x}</option>`).join("")}</select></label>
      <label>Stop at<select onchange="MMBench.edStop('type',this.value)"><option value="swing" ${e.stop.type === "swing" ? "selected" : ""}>latest minor swing</option><option value="atr" ${e.stop.type === "atr" ? "selected" : ""}>ATR multiple</option></select></label>
      <label>ATR multiple (ATR stop)<input type="number" step="0.1" value="${e.stop.atr_mult}" onchange="MMBench.edStop('atr_mult',+this.value)"></label>
      <label>Stop buffer (ATR)<input type="number" step="0.05" value="${e.stop_buffer_atr}" onchange="MMBench.ed('stop_buffer_atr',+this.value)"></label>
      <label>Minimum reward:risk to T2<input type="number" step="0.1" value="${e.min_rr}" onchange="MMBench.ed('min_rr',+this.value)"></label></div>
      <label class="note" style="display:block;margin:8px 0 4px">Description<textarea onchange="MMBench.ed('description',this.value)">${esc(e.description)}</textarea></label>
      <div class="note" style="margin:6px 0">Regimes it suits (the agent declines it in others):</div><div class="chips">${regs}</div>
      <div class="note" style="margin:8px 0 4px">Conditions (all must hold on the closed candle; fields are measured in the trade's direction, so one rule set works long and short):</div>
      ${e.conditions.map(condRow).join("")}
      <div class="hr" style="margin-top:6px"><button class="btn sm" onclick="MMBench.addCond()">+ condition</button>
        <button class="btn ok" onclick="MMBench.save()">Save as draft</button><button class="btn" onclick="MMBench.cancel()">Cancel</button></div></div>`;
  }
  function btHtml() {
    const b = S.bt;
    if (S.btBusy) return `<div class="det note">⏳ Backtesting (Signal Lab: next-open entries, costs, 60/20/20 split, walk-forward, Monte Carlo)…</div>`;
    if (!b) return "";
    if (b.error) return `<div class="det note dn">${esc(b.error)}</div>`;
    const st = (k, x) => `<div class="metric"><span>${k}</span><b>${n((x || {}).trades, 0)} · ${R((x || {}).expectancy_r)}</b></div>`;
    const vs = Object.entries(b.validation_status || {}).filter(([k]) => k === b._key)[0];
    const wf = (b.walk_forward || []).map(f => `fold ${f.fold}: ${n(f.trades, 0)} trades ${R(f.expectancy_r)}`).join(" · ");
    const mc = b.monte_carlo || {};
    return `<div class="det"><b class="note">${esc(b._key)} on ${esc(b.symbol)} ${esc(b.tf)} · ${n(b.candles, 0)} candles ${tm(b.from)} → ${tm(b.to)}</b>
      <div class="mgrid" style="margin:6px 0">${st("All", b.overall)}${st("Train", b.train)}${st("Validation", b.validation)}${st("Test", b.test)}
        <div class="metric"><span>Win rate</span><b>${n((b.overall || {}).win_rate, 1)}%</b></div><div class="metric"><span>Max DD</span><b>${n((b.overall || {}).max_drawdown_r, 2)}R</b></div>
        <div class="metric"><span>MC negative</span><b>${mc.available ? n(mc.prob_negative, 0) + "%" : "–"}</b></div></div>
      <div class="note">Walk-forward: ${wf || "–"}</div>
      <div class="note">Status: ${vs ? `<b class="${vs[1].status === "validated" ? "up" : "wa"}">${esc(vs[1].status)}</b> (validation ${R(vs[1].val_expectancy)}, test ${R(vs[1].test_expectancy)}, ${vs[1].val_trades + vs[1].test_trades} out-of-sample trades)` : "–"}</div>
      <div class="note">${(b.notes || []).map(esc).join(" ")}</div></div>`;
  }
  function wbHtml() {
    const w = S.wb;
    if (!w) return `<div class="cd"><h3>Strategy workbench</h3><div class="empty">Loading…</div></div>`;
    const rows = (w.strategies || []).map(s => {
      const bt = (w.backtests || {})[s.key];
      return `<tr><td><b>${esc(s.name)}</b><br><span class="mu">${esc(s.key)} · ${esc(s.direction)} · stop ${s.stop.type === "atr" ? s.stop.atr_mult + "×ATR" : "swing"} · min RR ${s.min_rr}</span></td>
        <td class="mu">${s.conditions.map(c => esc(`${c.field} ${c.op} ${Array.isArray(c.value) ? c.value.join(",") : c.value}`)).join("<br>")}</td>
        <td class="mu">${esc((s.regimes || []).join(", ") || "none")}</td>
        <td>${bt ? `${esc(bt.symbol)} ${esc(bt.tf)}: ${n((bt.overall || {}).trades, 0)} trades ${R((bt.overall || {}).expectancy_r)}<br><span class="mu">test ${R((bt.test || {}).expectancy_r)} · ${esc((bt.status || {}).status || "")}</span>` : '<span class="mu">not backtested</span>'}</td>
        <td><span class="tag ${s.status === "approved" ? "ap" : "dc"}">${s.status.toUpperCase()}</span></td>
        <td style="white-space:nowrap"><button class="btn sm" onclick="MMBench.editKey('${s.key}')">Edit</button> <button class="btn sm pri" onclick="MMBench.bt('${s.key}')">Backtest</button>
          ${s.status === "approved" ? `<button class="btn sm" onclick="MMBench.status('${s.key}','draft')">To draft</button>` : `<button class="btn sm ok" onclick="MMBench.status('${s.key}','approved')">Approve</button>`}
          <button class="btn sm bad" onclick="MMBench.del('${s.key}')">Delete</button></td></tr>`;
    }).join("");
    const bi = (w.builtin || []).map(b => `<option value="${b.key}">${esc(b.name)}</option>`).join("");
    return `<div class="cd"><h3>Strategy workbench <span class="note">your rules → backtest → approve → the agent trades them (still through the ML filter and the Risk Guardian)</span></h3>
      <div class="hr" style="margin-bottom:8px"><button class="btn ok" onclick="MMBench.newRule()">+ New strategy</button>
        <input id="wbSym" class="btn" value="${esc(S.btSym || "NSE:NIFTY50-INDEX")}" style="min-width:200px" title="symbol for backtests">
        <select id="wbTf" class="btn">${["5m", "15m", "30m", "1h"].map(x => `<option ${x === (S.btTf || "15m") ? "selected" : ""}>${x}</option>`).join("")}</select>
        <select id="wbBuiltin" class="btn"><option value="">backtest a built-in strategy…</option>${bi}</select></div>
      ${editor()}${btHtml()}
      ${rows ? `<div class="tw"><table><tr><th>Strategy</th><th>Conditions</th><th>Regimes</th><th>Last backtest</th><th>Status</th><th></th></tr>${rows}</table></div>`
             : `<div class="empty">No strategies of your own yet. Build one from the same fields the ML model uses, backtest it on any symbol, and approve it when the out-of-sample numbers hold up.</div>`}
      <div class="note" style="margin-top:6px">A draft is only backtested. Editing an approved strategy puts it back to draft. Approving lets the agent use it in paper (regime fit, ML filter, every Guardian check and your 2-lot rule still apply).
      Approve only with enough out-of-sample trades (20+) and positive validation <b>and</b> test expectancy. The ML model learns from your strategies too after the next training.</div></div>`;
  }
  const CSS = `#mlbench select,#mlbench input:not([type=checkbox]){background:var(--bg0);border:1px solid var(--bdr2);color:var(--t1);padding:5px 7px;border-radius:6px;font:600 .74rem var(--mono)}
#mlbench .chips label{font:600 .68rem var(--mono);color:var(--t2);display:flex;border:1px solid var(--bdr2);border-radius:6px;padding:4px 7px;background:var(--bg2)}`;
  function render() {
    const el = root();
    if (!el) return;
    if (!document.getElementById("mlbenchCss")) { const st = document.createElement("style"); st.id = "mlbenchCss"; st.textContent = CSS; document.head.appendChild(st); }
    el.innerHTML = mlHtml() + wbHtml();
    const b = document.getElementById("wbBuiltin");
    if (b) b.onchange = () => { if (b.value) MMBench.bt(b.value); };
  }
  async function load() {
    try { [S.ml, S.wb] = await Promise.all([get("/ml"), get("/workbench")]); }
    catch (e) { const el = root(); if (el) el.innerHTML = `<div class="cd"><h3>ML &amp; workbench</h3><div class="empty">Unavailable: ${esc(e.message)}${/404|Not Found/.test(e.message) ? " — restart launch_marketminer.cmd to load the new version" : ""}</div></div>`; return; }
    render();
    clearTimeout(S.poll);
    if ((S.ml.training || {}).training) S.poll = setTimeout(load, 5000);   // follow training progress
  }
  const act = fn => async (...a) => { try { await fn(...a); } catch (e) { say(e.message, true); } };
  window.MMBench = {
    load,
    mode: act(async m => { if (m === "active" && !confirm("ACTIVE: the approved model may skip, rank and halve setups (never add or size up). Continue?")) return; await post("/ml/mode", {mode: m}); await load(); }),
    train: act(async () => { const v = (document.getElementById("mlSyms") || {}).value || ""; const r = await post("/ml/train", {symbols: v.split(/[\s,]+/).filter(Boolean)}); say(`Training started on ${r.symbols.length} symbol(s). This takes a few minutes (FYERS paces history requests).`); await load(); }),
    approve: act(async id => { if (!confirm(`Approve model #${id}? It passed every held-out gate. It acts only when the ML mode is ACTIVE.`)) return; await post(`/ml/models/${id}/approve`); await load(); }),
    reject: act(async id => { await post(`/ml/models/${id}/reject`); await load(); }),
    openModel: id => { S.openModel = S.openModel === id ? null : id; render(); },
    newRule: () => { S.edit = blank(); render(); },
    editKey: k => { const s = (S.wb.strategies || []).find(x => x.key === k); S.edit = JSON.parse(JSON.stringify(s)); render(); },
    cancel: () => { S.edit = null; render(); },
    ed: (k, v) => { S.edit[k] = v; },
    edStop: (k, v) => { S.edit.stop[k] = v; },
    reg: (k, on) => { S.edit.regimes = on ? [...new Set([...S.edit.regimes, k])] : S.edit.regimes.filter(x => x !== k); },
    cond: (i, k, v) => {
      const c = S.edit.conditions[i];
      if (k === "value") c.value = c.op === "between" || c.op.includes("in") ? String(v).split(",").map(x => x.trim()).map(x => (c.op === "between" ? +x : x)) : (isNaN(+v) || ["regime", "volatility", "market"].includes(c.field) ? v : +v);
      else { c[k] = v; render(); }
    },
    addCond: () => { S.edit.conditions.push({field: "rsi_dir", op: ">", value: 0}); render(); },
    delCond: i => { S.edit.conditions.splice(i, 1); render(); },
    save: act(async () => { const s = await post("/workbench/save", S.edit); S.edit = null; say(`Saved ${s.name} as a draft. Backtest it next.`); await load(); }),
    status: act(async (k, st) => { if (st === "approved" && !confirm("Approve this strategy? The agent may then trade it in paper (through regime fit, the ML filter and every Risk Guardian check).")) return; await post(`/workbench/${encodeURIComponent(k)}/status`, {status: st}); await load(); }),
    del: act(async k => { if (!confirm("Delete this strategy?")) return; await post(`/workbench/${encodeURIComponent(k)}/delete`); await load(); }),
    bt: async k => {
      S.btSym = (document.getElementById("wbSym") || {}).value || S.btSym; S.btTf = (document.getElementById("wbTf") || {}).value || S.btTf;
      S.btBusy = true; S.bt = null; render();
      try { S.bt = {...(await post("/workbench/backtest", {key: k, symbol: S.btSym, tf: S.btTf})), _key: k}; }
      catch (e) { S.bt = {error: e.message}; }
      S.btBusy = false; S.wb = await get("/workbench").catch(() => S.wb); render();
    },
  };
})();
