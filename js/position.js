/* MarketMiner: TradingView-style Long / Short position box, drawn on a 2D canvas over a lightweight-charts chart.
 * Green zone = entry → targets (T1, T2, T3 dashed inside it), red zone = entry → stop. Used by Signal Lab's chart
 * (live overlay) and by the Auto bot (chart picture attached to Telegram alerts). Drawing only: no orders. */
(function () {
  "use strict";
  const pct = (a, b) => ((b - a) / a) * 100;
  const num = (x) => (x == null || isNaN(x) ? "—" : Number(x).toLocaleString("en-IN", { maximumFractionDigits: Math.abs(x) >= 1000 ? 2 : Math.abs(x) >= 1 ? 2 : 6 }));
  function tag(ctx, text, x, y, bg, fg, align) {
    ctx.font = "600 11px 'JetBrains Mono', ui-monospace, monospace";
    const w = ctx.measureText(text).width + 10, h = 17;
    let bx = align === "right" ? x - w : x;
    bx = Math.max(2, bx);
    ctx.fillStyle = bg; ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(bx, y - h / 2, w, h, 3); else ctx.rect(bx, y - h / 2, w, h);
    ctx.fill(); ctx.fillStyle = fg; ctx.textBaseline = "middle"; ctx.fillText(text, bx + 5, y + 0.5);
  }
  /** p: {x0, x1, yE, yS, yT:[y1,y2,y3], dir, entry, stop, targets, rr, title, faded, height} (CSS pixels). */
  function paint(ctx, p) {
    if (p.yE == null || p.yS == null || !p.yT || p.yT.some((v) => v == null)) return;
    const L = p.dir === "LONG", x0 = Math.max(0, p.x0), x1 = Math.max(x0 + 40, p.x1), w = x1 - x0;
    const yT = p.yT[2], a = p.faded ? 0.1 : 0.2, b = p.faded ? 0.45 : 0.85;
    ctx.save();
    // zones
    ctx.fillStyle = `rgba(8,153,129,${a})`; ctx.fillRect(x0, Math.min(p.yE, yT), w, Math.abs(yT - p.yE));
    ctx.fillStyle = `rgba(242,54,69,${a})`; ctx.fillRect(x0, Math.min(p.yE, p.yS), w, Math.abs(p.yS - p.yE));
    ctx.lineWidth = 1;
    ctx.strokeStyle = `rgba(8,153,129,${b})`; ctx.strokeRect(x0 + 0.5, Math.min(p.yE, yT) + 0.5, w - 1, Math.abs(yT - p.yE));
    ctx.strokeStyle = `rgba(242,54,69,${b})`; ctx.strokeRect(x0 + 0.5, Math.min(p.yE, p.yS) + 0.5, w - 1, Math.abs(p.yS - p.yE));
    // T1 / T2 inside the profit zone
    ctx.setLineDash([4, 3]); ctx.strokeStyle = `rgba(8,153,129,${b})`;
    [0, 1].forEach((i) => { ctx.beginPath(); ctx.moveTo(x0, p.yT[i] + 0.5); ctx.lineTo(x1, p.yT[i] + 0.5); ctx.stroke(); });
    ctx.setLineDash([]);
    // entry line
    ctx.strokeStyle = p.faded ? "rgba(148,163,184,.7)" : "rgba(148,163,184,1)"; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(x0, p.yE + 0.5); ctx.lineTo(x1, p.yE + 0.5); ctx.stroke();
    // labels
    const green = "rgba(8,153,129,.95)", red = "rgba(242,54,69,.95)", ink = "#fff";
    const off = L ? -11 : 11;
    tag(ctx, `Target ${num(p.targets[2])} (${pct(p.entry, p.targets[2]) >= 0 ? "+" : ""}${pct(p.entry, p.targets[2]).toFixed(2)}%)${p.rr ? ` · ${(+p.rr).toFixed(1)}R` : ""}`, x0 + 4, yT + off, green, ink);
    tag(ctx, `Stop ${num(p.stop)} (${pct(p.entry, p.stop) >= 0 ? "+" : ""}${pct(p.entry, p.stop).toFixed(2)}%)`, x0 + 4, p.yS - off, red, ink);
    tag(ctx, "T1", x1 - 4, p.yT[0], "rgba(8,153,129,.75)", ink, "right");
    tag(ctx, "T2", x1 - 4, p.yT[1], "rgba(8,153,129,.75)", ink, "right");
    tag(ctx, `${L ? "▲ LONG" : "▼ SHORT"} ${num(p.entry)}${p.title ? " · " + p.title : ""}`, x0 + 4, p.yE, p.faded ? "rgba(71,85,105,.85)" : "rgba(30,41,59,.95)", ink);
    ctx.restore();
  }
  /** Pixel geometry for one position on a lightweight-charts chart. T: chart-time mapper (adds the exchange offset). */
  function geometry(chart, series, pos, T, tfSec) {
    const ts = chart.timeScale(), paneW = ts.width();
    let x0 = ts.timeToCoordinate(T(Math.floor(pos.time / tfSec) * tfSec));
    if (x0 == null) {                                   // bar not loaded (older than the chart): start at the left edge
      const first = ts.getVisibleLogicalRange && ts.getVisibleLogicalRange();
      x0 = first ? 0 : 0;
    }
    const y = (v) => series.priceToCoordinate(v);
    return { x0, x1: paneW, yE: y(pos.entry), yS: y(pos.stop), yT: pos.targets.map(y), dir: pos.direction, entry: pos.entry,
      stop: pos.stop, targets: pos.targets, rr: pos.rr, title: pos.title, faded: pos.faded };
  }
  window.MMPOS = { paint, geometry };
})();
