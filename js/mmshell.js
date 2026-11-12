/* MarketMiner shell: Tredye-style top navigation, light/dark theme, Ctrl+K search and data-status banner.
   Loaded in <head> of every page. Classic pages keep working as before; this only re-skins them:
   - colour variables come from /css/mm-tredye.css
   - hard-coded neon colours in their <style> blocks, inline styles and SVG are mapped to readable ones
   - Lightweight Charts get the theme's background, text and grid colours. */
(function () {
  'use strict';
  var KEY = 'mm.theme';
  var theme = 'light';
  try { theme = localStorage.getItem(KEY) || 'light'; } catch (e) {}
  var root = document.documentElement;
  root.setAttribute('data-mm-theme', theme);

  if (!document.querySelector('link[href*="mm-tredye.css"]')) {
    var l = document.createElement('link');
    l.rel = 'stylesheet'; l.href = 'css/mm-tredye.css?v=2026.11.12';
    document.head.appendChild(l);
  }

  /* ---------------- colour mapping for the classic (dark-designed) pages ---------------- */
  var LIGHT = {
    '#06080f': '#f6f6f8', '#0d1117': '#ffffff', '#131a27': '#ffffff', '#182030': '#f1f1f5', '#141b29': '#f1f1f5',
    '#1b2536': '#e7e7ee', '#263450': '#d9d9e4', '#e2e8f0': '#0b0b10', '#cbd5e1': '#334155', '#d7dee8': '#334155',
    '#eef2f7': '#0b0b10', '#8b9ab5': '#5b6474', '#94a3b8': '#64748b', '#5b6b85': '#8a90a2', '#4f5f78': '#8a90a2',
    '#00e676': '#15803d', '#69f0ae': '#16a34a', '#66bb6a': '#16a34a', '#b9ffd9': '#166534', '#ff5252': '#dc2626',
    '#ef5350': '#dc2626', '#ff80ab': '#be185d', '#ffb3b3': '#b91c1c', '#18ffff': '#0e7490', '#40c4ff': '#0369a1',
    '#4fc3f7': '#0369a1', '#ffab00': '#b45309', '#ffd740': '#a16207', '#ffd700': '#a16207', '#ffd8a8': '#9a3412',
    '#ffd27a': '#a16207', '#ff9100': '#c2410c', '#448aff': '#4338ca', '#b388ff': '#6d28d9', '#ea80fc': '#a21caf',
    '#f7931a': '#c2410c', '#5a1018': '#fde2e2', '#7f1d1d': '#fecaca'
  };
  var LIGHT_RGBA = [
    [/rgba\(\s*255\s*,\s*255\s*,\s*255\s*,/gi, 'rgba(15,15,35,'], [/rgba\(\s*0\s*,\s*230\s*,\s*118\s*,/gi, 'rgba(21,128,61,'],
    [/rgba\(\s*255\s*,\s*82\s*,\s*82\s*,/gi, 'rgba(220,38,38,'], [/rgba\(\s*24\s*,\s*255\s*,\s*255\s*,/gi, 'rgba(14,116,144,'],
    [/rgba\(\s*255\s*,\s*215\s*,\s*0\s*,/gi, 'rgba(161,98,7,'], [/rgba\(\s*255\s*,\s*171\s*,\s*0\s*,/gi, 'rgba(180,83,9,'],
    [/rgba\(\s*79\s*,\s*195\s*,\s*247\s*,/gi, 'rgba(3,105,161,'], [/rgba\(\s*179\s*,\s*136\s*,\s*255\s*,/gi, 'rgba(109,40,217,']
  ];
  var DARK = {
    '#06080f': '#0b0b12', '#0d1117': '#14141d', '#131a27': '#14141d', '#182030': '#1d1d29', '#141b29': '#1b1b27',
    '#1b2536': '#262636', '#263450': '#34344a'
  };
  var HEX = /#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b/g;
  function mapText(s) {
    if (!s) return s;
    var m = theme === 'light' ? LIGHT : DARK;
    var out = s.replace(HEX, function (h) { return m[h.toLowerCase()] || h; });
    if (theme === 'light') LIGHT_RGBA.forEach(function (p) { out = out.replace(p[0], p[1]); });
    out = out.replace(/'JetBrains Mono'\s*,\s*(ui-monospace\s*,\s*)?monospace/g, "'Outfit',ui-sans-serif,sans-serif");
    return out;
  }
  var ORIG = new WeakMap();
  function fixStyleEl(el) {
    if (el.id === 'mmx-own' || (el.href && el.href.indexOf('mm-tredye') >= 0)) return;
    if (!ORIG.has(el)) ORIG.set(el, el.textContent);
    var t = mapText(ORIG.get(el));
    if (t !== el.textContent) el.textContent = t;
  }
  var ATTRS = ['style', 'fill', 'stroke', 'stop-color', 'color'];
  function fixEl(el) {
    if (!el || el.nodeType !== 1 || el.closest && el.closest('.mmx-nav,.mmx-banner,.mmx-pal,[data-mm-keep]')) return;
    for (var i = 0; i < ATTRS.length; i++) {
      var a = ATTRS[i], v = el.getAttribute(a);
      if (!v || (v.indexOf('#') < 0 && v.indexOf('rgba') < 0 && v.indexOf('JetBrains') < 0)) continue;
      var key = 'data-mm-o-' + a, o = el.getAttribute(key);
      if (o === null) { o = v; el.setAttribute(key, v); }
      else if (v !== mapTextFor(o, 'light') && v !== mapTextFor(o, 'dark')) { o = v; el.setAttribute(key, v); }  // page changed it
      var n = mapText(o);
      if (n !== v) el.setAttribute(a, n);
    }
  }
  function mapTextFor(s, th) { var k = theme; theme = th; var r = mapText(s); theme = k; return r; }
  function fixTree(node) {
    if (!node || node.nodeType !== 1) return;
    if (node.tagName === 'STYLE') { fixStyleEl(node); return; }
    fixEl(node);
    var list = node.querySelectorAll('[style],[fill],[stroke],[stop-color],style');
    for (var i = 0; i < list.length; i++) list[i].tagName === 'STYLE' ? fixStyleEl(list[i]) : fixEl(list[i]);
  }
  var pending = [], scheduled = false;
  function flush() { scheduled = false; var p = pending; pending = []; for (var i = 0; i < p.length; i++) fixTree(p[i]); }
  function queue(n) { pending.push(n); if (!scheduled) { scheduled = true; (window.requestAnimationFrame || setTimeout)(flush); } }
  var classic = !document.documentElement.hasAttribute('data-mm-native');
  if (classic) {
    var mo = new MutationObserver(function (recs) {
      for (var i = 0; i < recs.length; i++) {
        var r = recs[i];
        if (r.type === 'attributes') { if (!r.attributeName || r.attributeName.indexOf('data-mm-o-') !== 0) queue(r.target); }
        else for (var j = 0; j < r.addedNodes.length; j++) {
          var nd = r.addedNodes[j];
          if (nd.nodeType === 1) queue(nd); else if (nd.nodeType === 3 && nd.parentNode && nd.parentNode.tagName === 'STYLE') queue(nd.parentNode);
        }
      }
    });
    mo.observe(document.documentElement, { subtree: true, childList: true, attributes: true, attributeFilter: ATTRS });
  }

  /* ---------------- Lightweight Charts theming ---------------- */
  var charts = [];
  function chartColors() {
    var L = theme === 'light';
    return { layout: { background: { type: 'solid', color: L ? '#ffffff' : '#14141d' }, textColor: L ? '#5b6474' : '#a3a6bb',
               fontFamily: "'Outfit',ui-sans-serif,sans-serif" },
             grid: { vertLines: { color: L ? 'rgba(15,15,35,.05)' : 'rgba(255,255,255,.05)' },
                     horzLines: { color: L ? 'rgba(15,15,35,.05)' : 'rgba(255,255,255,.05)' } },
             rightPriceScale: { borderColor: L ? 'rgba(15,15,35,.10)' : 'rgba(255,255,255,.10)' },
             timeScale: { borderColor: L ? 'rgba(15,15,35,.10)' : 'rgba(255,255,255,.10)' } };
  }
  function merge(a, b) { var o = {}, k; for (k in a) o[k] = a[k]; for (k in b) o[k] = (b[k] && typeof b[k] === 'object' && !Array.isArray(b[k]) && a && typeof a[k] === 'object') ? merge(a[k], b[k]) : b[k]; return o; }
  // The library's exports are read-only, so it is wrapped in a copy instead of being changed in place.
  function patchLWC(lib) {
    if (!lib || lib.__mmPatched || typeof lib.createChart !== 'function') return lib;
    var o = {}, k;
    try { Object.getOwnPropertyNames(lib).forEach(function (n) { try { o[n] = lib[n]; } catch (e) {} }); } catch (e) {}
    for (k in lib) { try { if (!(k in o)) o[k] = lib[k]; } catch (e) {} }
    var orig = lib.createChart;
    o.createChart = function (el, opts) {
      var ch = orig.call(lib, el, merge(opts || {}, chartColors()));
      // later applyOptions calls from the page (e.g. Signal Lab's own light/dark chart button) still win
      charts.push(ch);
      return ch;
    };
    o.__mmPatched = true;
    return o;
  }
  var _lwc;
  try {
    if (window.LightweightCharts) _lwc = window.LightweightCharts;
    Object.defineProperty(window, 'LightweightCharts', { configurable: true, enumerable: true,
      get: function () { try { _lwc = patchLWC(_lwc); } catch (e) {} return _lwc; },   // UMD builds fill exports after assigning
      set: function (v) { _lwc = v; } });
  } catch (e) {}

  function setTheme(t) {
    root.classList.add('mm-theming'); setTimeout(function () { root.classList.remove('mm-theming'); }, 450);
    theme = t;
    try { localStorage.setItem(KEY, t); } catch (e) {}
    root.setAttribute('data-mm-theme', t);
    if (classic) fixTree(document.documentElement);
    charts.forEach(function (c) { try { c.applyOptions(chartColors()); } catch (e) {} });
    document.dispatchEvent(new CustomEvent('mm-theme', { detail: t }));
    var b = document.getElementById('mmxTheme'); if (b) b.innerHTML = ic(t === 'light' ? 'sun' : 'moon');
  }
  /* rolling digits: MMShell.roll(el, '25,338.60') rolls each digit from the value it showed before */
  var LAST = {};
  function roll(el, text, key) {
    if (!el) return;
    text = String(text === null || text === undefined ? '—' : text);
    key = key || el.getAttribute('data-k') || el.id;
    var prev = (key && LAST[key]) || el.getAttribute('data-prev') || '';
    var pn = parseFloat(prev.replace(/[^0-9.\-]/g, '')), nn = parseFloat(text.replace(/[^0-9.\-]/g, ''));
    if (key) LAST[key] = text;
    el.setAttribute('data-prev', text);
    if (window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches) { el.textContent = text; return; }
    var pad = prev.length === text.length ? prev : '', h = '<span class="roll" aria-label="' + text.replace(/"/g, '') + '">';
    for (var i = 0; i < text.length; i++) {
      var ch = text[i];
      if (/[0-9]/.test(ch)) {
        var from = /[0-9]/.test(pad[i] || '') ? +pad[i] : 0;
        h += '<span class="rd" aria-hidden="true"><span data-to="' + ch + '" style="transform:translateY(-' + from * 10 + '%)"><i>0</i><i>1</i><i>2</i><i>3</i><i>4</i><i>5</i><i>6</i><i>7</i><i>8</i><i>9</i></span></span>';
      } else h += '<span class="rc" aria-hidden="true">' + ch.replace(/</g, '&lt;') + '</span>';
    }
    el.innerHTML = h + '</span>';
    var cols = el.querySelectorAll('.rd>span');
    requestAnimationFrame(function () { requestAnimationFrame(function () {
      for (var j = 0; j < cols.length; j++) cols[j].style.transform = 'translateY(-' + (+cols[j].getAttribute('data-to')) * 10 + '%)';
    }); });
    if (isFinite(pn) && isFinite(nn) && pn !== nn) {
      el.classList.remove('flash-up', 'flash-dn'); void el.offsetWidth; el.classList.add(nn > pn ? 'flash-up' : 'flash-dn');
    }
  }
  function rollAll(scope) {
    var list = (scope || document).querySelectorAll('[data-roll]');
    for (var i = 0; i < list.length; i++) { var e = list[i]; roll(e, e.getAttribute('data-roll'), e.getAttribute('data-k')); e.removeAttribute('data-roll'); }
  }
  function stagger(scope) {
    var cs = (scope || document).querySelectorAll('.c,.lesson');
    for (var i = 0; i < cs.length; i++) cs[i].style.setProperty('--i', Math.min(i, 12));
  }
  window.MMShell = { roll: roll, rollAll: rollAll, stagger: stagger, setTheme: setTheme, theme: function () { return theme; }, chartColors: chartColors,
                     icon: function (n) { return P[n] ? ic(n) : ''; } };

  /* ---------------- navigation ---------------- */
  var P = {
    pulse: 'M3 12h4l3-8 4 16 3-8h4', news: 'M4 5h13v14H6a2 2 0 0 1-2-2zM17 9h3v8a2 2 0 0 1-2 2M8 9h5M8 13h5',
    options: 'M12 3 3 8l9 5 9-5zM3 13l9 5 9-5', radar: 'M12 12m-9 0a9 9 0 1 0 18 0a9 9 0 1 0-18 0M12 12m-5 0a5 5 0 1 0 10 0a5 5 0 1 0-10 0M12 12l6-6',
    calm: 'M5 19c8 0 14-6 14-14C11 5 5 11 5 19zM5 19l7-7', crypto: 'M9 7h5a2.5 2.5 0 0 1 0 5H9zM9 12h6a2.5 2.5 0 0 1 0 5H9zM9 7v10M11 5v2M11 17v2',
    liquidity: 'M12 3s6 6.5 6 11a6 6 0 0 1-12 0c0-4.5 6-11 6-11z', flow: 'M11 5H4M11 9H6M11 13H3M13 11h6M13 15h8M13 19h4M12 3v18', more: 'M6 9l6 6 6-6',
    oi: 'M4 19V9M10 19V5M16 19v-7M22 19H2', strength: 'M4 18l5-6 4 3 7-9M15 6h5v5', screen: 'M4 5h16M7 12h10M10 19h4',
    journal: 'M6 3h11a1 1 0 0 1 1 1v16l-3-2-3 2-3-2-3 2V4a1 1 0 0 1 1-1zM9 8h6M9 12h6', academy: 'M2 9l10-5 10 5-10 5zM6 11v5c3 2 9 2 12 0v-5',
    dash: 'M4 4h7v7H4zM13 4h7v4h-7zM13 10h7v10h-7zM4 13h7v7H4z', desk: 'M12 12m-9 0a9 9 0 1 0 18 0a9 9 0 1 0-18 0M12 7v5l3 2',
    lab: 'M9 3h6M10 3v6L4 19a1 1 0 0 0 1 2h14a1 1 0 0 0 1-2l-6-10V3', auction: 'M4 20V10M9 20V4M14 20v-8M19 20v-5',
    agent: 'M12 3a4 4 0 0 1 4 4v1a4 4 0 0 1-8 0V7a4 4 0 0 1 4-4zM5 21a7 7 0 0 1 14 0', pnl: 'M3 3v18h18M7 15l4-4 3 3 6-7',
    trend: 'M3 17c3-6 6-6 9-2s6 4 9-6', link: 'M10 14a4 4 0 0 0 6 0l3-3a4 4 0 0 0-6-6l-1 1M14 10a4 4 0 0 0-6 0l-3 3a4 4 0 0 0 6 6l1-1',
    sun: 'M12 12m-4 0a4 4 0 1 0 8 0a4 4 0 1 0-8 0M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4',
    moon: 'M21 13A9 9 0 1 1 11 3a7 7 0 0 0 10 10z', search: 'M11 11m-7 0a7 7 0 1 0 14 0a7 7 0 1 0-14 0M21 21l-5-5',
    refresh: 'M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7'
  };
  function ic(n) { return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="' + P[n] + '"/></svg>'; }
  var MOBILE = !!window.MM_MOBILE || /\.github\.io$/i.test(location.hostname);
  var MAIN = [
    ['pulse', 'Pulse', 'pulse.html#pulse'], ['news', 'News', 'pulse.html#news'], ['options', 'Options', 'pulse.html#options'],
    ['radar', 'Radar', 'pulse.html#radar'], ['calm', 'Calm', 'pulse.html#calm'], ['crypto', 'Crypto', 'crypto.html'],
    ['liquidity', 'Liquidity', 'pulse.html#liquidity'], ['flow', 'Order Flow', 'orderflow.html']
  ];
  var MORE = [
    ['lbl', 'Market tools'], ['oi', 'OI Flow', 'pulse.html#oiflow'], ['strength', 'Strength', 'pulse.html#strength'],
    ['screen', 'Screeners', 'pulse.html#screeners'], ['journal', 'Journal', 'pulse.html#journal'], ['academy', 'F&O Academy', 'pulse.html#academy'],
    ['hr'], ['lbl', 'MarketMiner'], ['dash', 'Dashboard', 'index.html'], ['desk', 'Day Desk', 'desk.html'], ['lab', 'Signal Lab', 'lab.html'],
    ['auction', 'AuctionFlow', 'auction.html'], ['agent', 'AI Agent', 'agent.html'], ['pnl', 'Paper P&L', 'perf.html'],
    ['trend', 'MM Trend', 'trend.html']
  ].concat(MOBILE ? [['radar', 'SmartEntry Intel', 'intel.html'], ['trend', 'TradingView', 'tv.html']] : []).concat([['link', 'Connections', 'fyers.html']]);
  function page() { var f = location.pathname.split('/').pop(); return f || 'index.html'; }
  function here(href) {
    var h = location.hash || '', u = href.split('#');
    if (u[0] !== page()) return false;
    if (u[1]) return (h.replace('#', '').split('?')[0] || (u[0] === 'pulse.html' ? 'pulse' : 'news-x')) === u[1];
    return !h;
  }
  function build() {
    if (document.querySelector('.mmx-nav')) return;
    var ban = document.createElement('div');
    ban.className = 'mmx-banner'; ban.id = 'mmxBanner';
    ban.innerHTML = '<span><span class="mmx-dot"></span><b id="mmxBanT">Checking data connection…</b> <span id="mmxBanS"></span></span><a href="fyers.html" id="mmxBanA">Connect broker</a>';
    var nav = document.createElement('nav');
    nav.className = 'mmx-nav';
    var h = '<a class="mmx-logo" href="pulse.html#pulse"><i>' + ic('pulse').replace('stroke="currentColor"', 'stroke="#fff"') + '</i><b>MarketMiner</b></a><div class="mmx-links">';
    MAIN.forEach(function (m) { h += '<a class="mmx-a' + (here(m[2]) ? ' on' : '') + '" href="' + m[2] + '">' + ic(m[0]) + m[1] + '</a>'; });
    var moreOn = MORE.some(function (m) { return m[2] && here(m[2]); });
    h += '<button class="mmx-a' + (moreOn ? ' on' : '') + '" id="mmxMore">More ' + ic('more') + '</button></div>';
    h += '<div class="mmx-right"><button class="mmx-search" id="mmxSearch">' + ic('search') + '<span>Search</span> <kbd>Ctrl K</kbd></button>' +
         '<button class="mmx-ic" id="mmxTheme" title="Light / dark">' + ic(theme === 'light' ? 'sun' : 'moon') + '</button></div>';
    nav.innerHTML = h;
    var menu = document.createElement('div');
    menu.className = 'mmx-menu'; menu.id = 'mmxMenu';
    menu.innerHTML = MORE.map(function (m) {
      if (m[0] === 'hr') return '<hr>';
      if (m[0] === 'lbl') return '<div class="lbl">' + m[1] + '</div>';
      return '<a href="' + m[2] + '">' + ic(m[0]) + m[1] + '</a>';
    }).join('');
    document.body.insertBefore(menu, document.body.firstChild);
    document.body.insertBefore(nav, document.body.firstChild);
    document.body.insertBefore(ban, document.body.firstChild);
    // phones: app-style bottom tab bar; "More" opens every page in a sheet
    var TABS = [['pulse', 'Pulse', 'pulse.html#pulse'], ['options', 'Options', 'pulse.html#options'], ['radar', 'Radar', 'pulse.html#radar'], ['dash', 'Dashboard', 'index.html']];
    var bar = document.createElement('nav');
    bar.className = 'mmx-tabbar'; bar.id = 'mmxTabbar';
    bar.innerHTML = TABS.map(function (t) { return '<a href="' + t[2] + '" class="' + (here(t[2]) ? 'on' : '') + '">' + ic(t[0]) + '<span>' + t[1] + '</span></a>'; }).join('') +
      '<button id="mmxTabMore">' + ic('screen') + '<span>More</span></button>';
    document.body.appendChild(bar);
    var sheet = document.createElement('div');
    sheet.className = 'mmx-sheet'; sheet.id = 'mmxSheet';
    sheet.innerHTML = '<div class="pan"><i class="grab"></i>' + MAIN.concat(MORE).map(function (m) {
      if (m[0] === 'hr') return '';
      if (m[0] === 'lbl') return '<div class="lbl">' + m[1] + '</div>';
      return '<a href="' + m[2] + '" class="' + (here(m[2]) ? 'on' : '') + '">' + ic(m[0]) + '<span>' + m[1] + '</span></a>';
    }).join('') + '</div>';
    document.body.appendChild(sheet);
    document.getElementById('mmxTabMore').onclick = function (e) { e.stopPropagation(); sheet.classList.add('open'); };
    sheet.onclick = function (e) { if (e.target === sheet || e.target.closest('a')) sheet.classList.remove('open'); };
    var pal = document.createElement('div');
    pal.className = 'mmx-pal'; pal.id = 'mmxPal';
    pal.innerHTML = '<div class="box"><input id="mmxQ" placeholder="Search pages, indices and F&O stocks…" autocomplete="off"><div class="res" id="mmxRes"></div></div>';
    document.body.appendChild(pal);
    document.getElementById('mmxMore').onclick = function (e) {
      e.stopPropagation();
      var r = this.getBoundingClientRect();
      menu.style.top = (r.bottom + 6) + 'px';
      menu.style.left = Math.max(8, Math.min(window.innerWidth - 246, r.left)) + 'px';
      menu.classList.toggle('open');
    };
    document.addEventListener('click', function () { menu.classList.remove('open'); });
    window.addEventListener('scroll', function () { menu.classList.remove('open'); }, { passive: true });
    document.getElementById('mmxTheme').onclick = function () { setTheme(theme === 'light' ? 'dark' : 'light'); };
    document.getElementById('mmxSearch').onclick = openPal;
    window.addEventListener('hashchange', function () {
      nav.querySelectorAll('.mmx-a[href]').forEach(function (a) { a.classList.toggle('on', here(a.getAttribute('href'))); });
      document.querySelectorAll('#mmxTabbar a,#mmxSheet a').forEach(function (a) { a.classList.toggle('on', here(a.getAttribute('href'))); });
      document.getElementById('mmxMore').classList.toggle('on', MORE.some(function (m) { return m[2] && here(m[2]); }));
    });
    status();
    setInterval(status, 120000);
  }
  function goNews() {
    var t = document.getElementById('nt');
    if (t) { var c = t.closest('.cd') || t; c.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
  }
  function status() {
    fetch('api/fyers/status').then(function (r) { return r.json(); }).then(function (j) {
      var f = j.fyers || j || {}, b = document.getElementById('mmxBanner');
      if (!b) return;
      b.classList.toggle('mmx-live', !!f.connected);
      document.getElementById('mmxBanT').textContent = f.connected ? 'Live data · FYERS connected' : (MOBILE ? 'FYERS not connected on this device' : 'FYERS not connected · NSE public data only');
      document.getElementById('mmxBanS').textContent = f.connected ? (f.message || '') : '· option chains, candles and quotes need your broker login';
      var a = document.getElementById('mmxBanA');
      a.textContent = f.connected ? 'Connections' : 'Connect broker';
      a.style.background = f.connected ? 'transparent' : '';
      a.style.color = f.connected ? 'inherit' : '';
      a.style.border = f.connected ? '1px solid currentColor' : '';
    }).catch(function () {
      var t = document.getElementById('mmxBanT'); if (t) t.textContent = MOBILE ? 'Could not read the FYERS status' : 'MarketMiner server not reachable';
    });
  }

  /* ---------------- Ctrl+K search ---------------- */
  var syms = null, sel = 0;
  function items(q) {
    q = (q || '').trim().toUpperCase();
    var pages = MAIN.concat(MORE.filter(function (m) { return m[2]; })).map(function (m) { return { t: m[1], s: 'Page', h: m[2] }; });
    var out = pages.filter(function (p) { return !q || p.t.toUpperCase().indexOf(q) >= 0; });
    if (syms && q) {
      syms.indices.concat(syms.stocks).filter(function (s) { return s.indexOf(q) === 0; }).slice(0, 12).forEach(function (s) {
        out.push({ t: s + ' · Radar', s: 'Symbol', h: 'pulse.html#radar?s=' + encodeURIComponent(s) });
        out.push({ t: s + ' · Options', s: 'Symbol', h: 'pulse.html#options?s=' + encodeURIComponent(s) });
      });
    }
    return out.slice(0, 30);
  }
  function render() {
    var q = document.getElementById('mmxQ').value, list = items(q);
    sel = Math.min(sel, Math.max(0, list.length - 1));
    document.getElementById('mmxRes').innerHTML = list.map(function (x, i) {
      return '<a href="' + x.h + '" class="' + (i === sel ? 'sel' : '') + '">' + x.t + '<small>' + x.s + '</small></a>';
    }).join('') || '<div style="padding:14px;color:var(--tb-mut)">Nothing found</div>';
  }
  function openPal() {
    var p = document.getElementById('mmxPal'); p.classList.add('open'); sel = 0;
    var q = document.getElementById('mmxQ'); q.value = ''; render(); q.focus();
    if (!syms) fetch('api/pulse/symbols').then(function (r) { return r.json(); }).then(function (j) { syms = j; render(); }).catch(function () {});
  }
  function closePal() { var p = document.getElementById('mmxPal'); if (p) p.classList.remove('open'); }
  document.addEventListener('keydown', function (e) {
    if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) { e.preventDefault(); openPal(); return; }
    var p = document.getElementById('mmxPal');
    if (!p || !p.classList.contains('open')) return;
    if (e.key === 'Escape') closePal();
    else if (e.key === 'ArrowDown') { sel++; render(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { sel = Math.max(0, sel - 1); render(); e.preventDefault(); }
    else if (e.key === 'Enter') { var a = document.querySelector('#mmxRes a.sel'); if (a) { closePal(); location.href = a.getAttribute('href'); } }
  });
  document.addEventListener('input', function (e) { if (e.target && e.target.id === 'mmxQ') { sel = 0; render(); } });
  document.addEventListener('click', function (e) {
    if (e.target && e.target.id === 'mmxPal') closePal();
    var a = e.target.closest && e.target.closest('#mmxRes a'); if (a) closePal();
  });

  function boot() {
    build();
    if (classic) fixTree(document.documentElement);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
