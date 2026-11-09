/* MarketMiner: chart controls shared by Signal Lab, MM Trend and AuctionFlow.
 *
 *   MMCTL.attach(chart, {box, plot, series, also: [otherChart], fsOffset: 0, pos: "mid"})   pos mid = buttons at the top centre
 *     - the chart follows its box's size (window resize, rotating a phone, full screen)
 *     - zoom with the mouse wheel / trackpad / pinch, drag to scroll, drag the price axis to stretch it,
 *       double-click the price axis to reset it
 *     - buttons on the chart:  Fit (whole history, prices auto-scaled)  +  −  ⛶ full screen (Esc to leave)
 * Display only.
 */
(function () {
  "use strict";
  const CSS = `.mmctl{position:absolute;top:6px;right:72px;z-index:5;display:flex;gap:3px;opacity:.82}
.mmctl:hover{opacity:1}
.mmctl.mid{right:auto;left:50%;transform:translateX(-50%)}
.mmctl button{font:700 12px 'JetBrains Mono',ui-monospace,monospace;min-width:28px;height:26px;padding:0 7px;border-radius:6px;cursor:pointer;
 border:1px solid rgba(148,163,184,.45);background:rgba(15,23,42,.72);color:#e2e8f0;line-height:1}
.mmctl button:hover{background:rgba(13,148,136,.85);border-color:#0d9488}
.mmfs{position:fixed!important;inset:0!important;z-index:9000!important;margin:0!important;border-radius:0!important;width:100vw!important;height:100vh!important}
.mmfs-lock{overflow:hidden!important}
@media(max-width:700px){.mmctl{right:58px}.mmctl button{min-width:32px;height:30px}}`;
  let styled = false;

  function attach(chart, o) {
    o = o || {};
    if (!styled) { const st = document.createElement("style"); st.textContent = CSS; document.head.appendChild(st); styled = true; }
    const box = o.box, plot = o.plot || o.box, charts = [chart, ...(o.also || [])];
    const opts = { autoSize: true, handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
      handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: { time: true, price: true }, axisDoubleClickReset: { time: true, price: true } },
      kineticScroll: { mouse: false, touch: true } };
    charts.forEach((c) => { try { c.applyOptions(opts); } catch (e) { /* older library */ } });

    const fit = () => {
      charts.forEach((c) => { try { c.timeScale().fitContent(); c.priceScale("right").applyOptions({ autoScale: true }); } catch (e) { /* ignore */ } });
    };
    const zoom = (f) => {
      const ts = chart.timeScale(), r = ts.getVisibleLogicalRange(); if (!r) return;
      const mid = r.to - (r.to - r.from) * 0.15, half = Math.max(5, ((r.to - r.from) * f) / 2);   // keep the latest candles in view
      ts.setVisibleLogicalRange({ from: mid - half * 1.7, to: mid + half * 0.3 });
    };
    let saved = null;
    const full = (on) => {
      const isOn = box.classList.contains("mmfs");
      if (on === undefined) on = !isOn;
      if (on === isOn) return;
      if (on) {
        saved = plot.style.height;
        box.classList.add("mmfs"); document.documentElement.classList.add("mmfs-lock");
        plot.style.height = `calc(100vh - ${o.fsOffset || 0}px)`;
      } else {
        box.classList.remove("mmfs"); document.documentElement.classList.remove("mmfs-lock");
        plot.style.height = saved || "";
      }
      fsBtn.textContent = on ? "✕" : "⛶"; fsBtn.title = on ? "Leave full screen (Esc)" : "Full screen";
      setTimeout(fit, 60);
    };

    const bar = document.createElement("div");
    bar.className = "mmctl" + (o.pos === "mid" ? " mid" : "");
    bar.innerHTML = `<button type="button" title="Fit: show all candles, prices auto-scaled">Fit</button><button type="button" title="Zoom in">+</button><button type="button" title="Zoom out">−</button><button type="button" title="Full screen">⛶</button>`;
    const [fitBtn, inBtn, outBtn, fsBtn] = bar.querySelectorAll("button");
    fitBtn.onclick = fit; inBtn.onclick = () => zoom(0.6); outBtn.onclick = () => zoom(1.6); fsBtn.onclick = () => full();
    box.appendChild(bar);
    const onKey = (e) => { if (e.key === "Escape") full(false); };
    document.addEventListener("keydown", onKey);
    return { fit, zoom, full, detach() { full(false); bar.remove(); document.removeEventListener("keydown", onKey); } };
  }

  window.MMCTL = { attach };
})();
