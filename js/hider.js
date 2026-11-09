/* MarketMiner: minimise any block on the page. Each block gets a ▾ arrow in its header: click it and the block folds
   down to its title bar (the title stays on the page); click ▸ to open it again. Blocks without a header get a slim
   title bar with the same arrow. Remembered per page in this browser only.
   Usage: <script src="/js/hider.js"></script> (after the page's own markup). Works with blocks that render later. */
(function () {
  const page = (location.pathname.split("/").pop() || "index.html").replace(/\.html$/, "") || "index";
  const KEY = "mmFold:" + page;
  const SEL = {
    index: ["#gfWrap", "#mmRetrace", "#mmPbook", "#mmStraddle", ".dash > .cd", "#pnlScreener .cd", "#pnlHoldings > .cd"],
    agent: ["#mmRetrace", "#mmPbook", "#mmStraddle", "#mmBookings", "section .cd"],
    crypto: [".assess", "#majors", "#mmRetrace", "main .grid > section.card", "main > section.card"],
  }[page] || [".cd", "section.card", ".card"];
  const NAMES = {gfWrap: "Today's assessment · Greed & Fear", mmRetrace: "Retracement or reversal", mmPbook: "Profit booking or fresh selling",
                 mmStraddle: "Straddle monitor", mmBookings: "Profit booking tracker", majors: "Coin price tiles"};
  const CSS = `.mmh-off{display:none!important}
.mmh-x{background:none;border:1px solid #263450;color:#8b9ab5;font:700 .72rem 'JetBrains Mono',ui-monospace,monospace;cursor:pointer;width:22px!important;height:20px!important;min-height:0!important;min-width:0!important;box-sizing:border-box;flex:0 0 auto!important;padding:0!important;
  border-radius:5px;margin-left:8px;align-self:center;line-height:1;text-transform:none;letter-spacing:0;display:inline-flex;align-items:center;justify-content:center}
.mmh-x:hover{color:#18ffff;border-color:#18ffff}
.mmh-min .mmh-x{color:#18ffff;border-color:rgba(24,255,255,.45)}
.mmh-bar{display:flex;justify-content:space-between;align-items:center;margin:6px 16px;padding:6px 10px;border:1px solid #1b2536;border-radius:8px;
  background:#0d1117;font:700 .68rem 'JetBrains Mono',ui-monospace,monospace;color:#8b9ab5;letter-spacing:.5px;text-transform:uppercase}`;
  let mins = {};
  try {
    ["mmHidden:" + page, "mmMin:" + page].forEach(k => localStorage.removeItem(k));   // blocks removed with the first version: all back, open
    mins = JSON.parse(localStorage.getItem(KEY) || "{}") || {};
  } catch (e) { mins = {}; }
  document.querySelectorAll(".mmh-hidden").forEach(el => el.classList.remove("mmh-hidden"));
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(mins)); } catch (e) { /* private window: this session only */ } };

  function title(el) {
    if (el.id && NAMES[el.id]) return NAMES[el.id];
    if (el.classList.contains("assess")) return "Market view · strategy · risk factors";
    const h = el.querySelector(".cd-t, h3, .gf-title, .cd-h");
    if (!h) return null;
    const own = [...h.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(" ").trim();   // the heading's own words
    const first = own || (h.firstElementChild && !h.firstElementChild.classList.contains("mmh-x") ? h.firstElementChild.textContent : "") || h.textContent;
    return first.trim().replace(/\s+/g, " ").slice(0, 70) || null;
  }
  const keyOf = t => t.replace(/\d+/g, "").replace(/[^\w\s·&/()+-]/g, "").replace(/\s+/g, " ").trim().toLowerCase();   // counts in titles do not change the key
  const esc = s => String(s).replace(/[&<>]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;"}[c]));

  function arrow(k, on) {
    const b = document.createElement("button");
    b.className = "mmh-x";
    b.type = "button";
    b.dataset.k = k;
    b.textContent = on ? "▸" : "▾";
    b.title = on ? "Expand this block" : "Minimise this block (the title stays)";
    b.onclick = ev => { ev.stopPropagation(); ev.preventDefault(); if (mins[k]) delete mins[k]; else mins[k] = 1; save(); scan(); };
    return b;
  }
  function fold(el, head, on) {
    // hide everything in the block except the header and the elements that contain it
    let node = head;
    while (node && node !== el) {
      for (const sib of node.parentElement.children) if (sib !== node && !sib.classList.contains("mmh-x")) sib.classList.toggle("mmh-off", on);
      node = node.parentElement;
    }
  }

  let busy = false;
  function scan() {
    if (busy) return;
    busy = true;
    try {
      const seen = {};
      const blocks = [];
      SEL.forEach(s => document.querySelectorAll(s).forEach(el => { if (!blocks.includes(el)) blocks.push(el); }));
      blocks.filter(el => !blocks.some(o => o !== el && o.contains(el))).forEach(el => {   // outermost blocks only
        const t = title(el);
        if (!t) return;
        let k = keyOf(t);
        seen[k] = (seen[k] || 0) + 1;
        if (seen[k] > 1) k += "#" + seen[k];
        const on = !!mins[k];
        el.classList.toggle("mmh-min", on);
        const useBar = el.matches(".assess, #majors, #gfWrap");      // blocks without a header of their own
        const head = useBar ? null : el.querySelector(".cd-h, h3");
        if (!useBar && !head) return;                                // still rendering: its header arrives with the content
        const stale = el.previousElementSibling;
        if (head && stale && stale.classList.contains("mmh-bar")) stale.remove();
        if (head) {
          let b = [...head.querySelectorAll(":scope > .mmh-x")].find(x => x.dataset.k === k);
          if (!b || b.textContent !== (on ? "▸" : "▾")) { if (b) b.remove(); head.appendChild(arrow(k, on)); }
          fold(el, head, on);
        } else {                                     // no header: a slim title bar before the block carries the arrow
          let bar = el.previousElementSibling;
          if (!bar || !bar.classList.contains("mmh-bar") || bar.dataset.k !== k) {
            bar = document.createElement("div");
            bar.className = "mmh-bar";
            bar.dataset.k = k;
            el.parentElement.insertBefore(bar, el);
          }
          const want = `<span>${esc(t)}</span>`;
          if (!bar.firstElementChild || bar.firstElementChild.outerHTML !== want || !bar.querySelector(".mmh-x") || bar.querySelector(".mmh-x").textContent !== (on ? "▸" : "▾")) {
            bar.innerHTML = want;
            bar.appendChild(arrow(k, on));
          }
          el.classList.toggle("mmh-off", on);
          const show = on || getComputedStyle(el).display !== "none" ? "" : "none";   // a block the page itself has not shown yet gets no bar
          if (bar.style.display !== show) bar.style.display = show;
        }
      });
    } finally { busy = false; }
  }
  function start() {
    if (!document.getElementById("mmhCss")) { const s = document.createElement("style"); s.id = "mmhCss"; s.textContent = CSS; document.head.appendChild(s); }
    scan();
    new MutationObserver(() => { if (!busy) scan(); }).observe(document.body, {childList: true, subtree: true, attributes: true, attributeFilter: ["style"]});
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
