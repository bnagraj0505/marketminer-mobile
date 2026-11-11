/* MarketMiner · Market Pulse page (Tredye-style). Views: pulse, options, oiflow, liquidity, strength, screeners,
   radar, calm, journal, academy. Data: /api/pulse/* (backend/markets/pulse.py). */
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var NF = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 });
  var N0 = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 });
  function n(v, d) { if (v === null || v === undefined || isNaN(v)) return '—'; return d === 0 ? N0.format(v) : new Intl.NumberFormat('en-IN', { maximumFractionDigits: d === undefined ? 2 : d, minimumFractionDigits: d === undefined ? 0 : d }).format(v); }
  function sg(v, d) { if (v === null || v === undefined || isNaN(v)) return '—'; return (v > 0 ? '+' : v < 0 ? '−' : '') + n(Math.abs(v), d); }
  function cls(v) { return v > 0 ? 'up' : v < 0 ? 'dn' : 'mut'; }
  function cr(v) { if (v === null || v === undefined) return '—'; var a = Math.abs(v); return (v < 0 ? '−' : '') + '₹' + (a >= 1e7 ? n(a / 1e7, 0) + ' cr' : a >= 1e5 ? n(a / 1e5, 1) + ' L' : n(a, 0)); }
  function kfmt(v) { if (v === null || v === undefined) return '—'; var a = Math.abs(v); return (v < 0 ? '−' : '') + (a >= 1e7 ? n(a / 1e7, 2) + ' Cr' : a >= 1e5 ? n(a / 1e5, 2) + ' L' : a >= 1e3 ? n(a / 1e3, 1) + ' K' : n(a, 0)); }
  function esc(s) { return String(s === null || s === undefined ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function css(v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }
  function api(path) {
    return fetch(path).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error((j && (j.detail || j.message)) || ('HTTP ' + r.status));
        return j;
      });
    });
  }
  function err(id, e) { var el = $(id); if (el) el.innerHTML = e ? '<div class="err">' + esc(e.message || e) + '</div>' : ''; }
  function icons(root) { (root || document).querySelectorAll('[data-ic]').forEach(function (el) { if (!el.innerHTML && window.MMShell) el.innerHTML = MMShell.icon(el.getAttribute('data-ic')); }); }
  function mopen() { var d = new Date(new Date().toLocaleString('en-US', { timeZone: 'Asia/Kolkata' })), m = d.getHours() * 60 + d.getMinutes(); return d.getDay() > 0 && d.getDay() < 6 && m >= 555 && m <= 930; }

  /* ---------------- routing ---------------- */
  var VIEWS = ['pulse', 'news', 'options', 'oiflow', 'liquidity', 'strength', 'screeners', 'radar', 'calm', 'journal', 'academy'];
  var SYM = { options: 'NIFTY', oiflow: 'NIFTY', liquidity: 'NIFTY', radar: 'RELIANCE' };
  var PICKS = {
    options: ['NIFTY', 'BANKNIFTY', 'SENSEX', 'FINNIFTY', 'MIDCPNIFTY', 'RELIANCE', 'HDFCBANK', 'ICICIBANK', 'INFY', 'SBIN'],
    oiflow: ['NIFTY', 'BANKNIFTY', 'SENSEX', 'FINNIFTY', 'RELIANCE', 'HDFCBANK', 'SBIN', 'INFY'],
    liquidity: ['NIFTY', 'BANKNIFTY', 'SENSEX', 'RELIANCE', 'HDFCBANK', 'SBIN', 'ICICIBANK'],
    radar: ['RELIANCE', 'HDFCBANK', 'ICICIBANK', 'INFY', 'TCS', 'SBIN', 'ITC', 'NIFTY', 'BANKNIFTY']
  };
  try { var saved = JSON.parse(localStorage.getItem('mm.pulse.sym') || '{}'); Object.keys(saved).forEach(function (k) { SYM[k] = saved[k]; }); } catch (e) {}
  var cur = null, timer = null;
  function route() {
    var h = (location.hash || '#pulse').slice(1), parts = h.split('?'), v = VIEWS.indexOf(parts[0]) >= 0 ? parts[0] : 'pulse';
    var q = new URLSearchParams(parts[1] || '');
    if (q.get('s') && SYM[v] !== undefined) SYM[v] = q.get('s').toUpperCase();
    VIEWS.forEach(function (x) { $('v-' + x).classList.toggle('on', x === v); });
    cur = v;
    document.title = 'MarketMiner · ' + ({ pulse: 'Market Pulse', options: 'Option Pulse', oiflow: 'OI Flow', liquidity: 'Liquidity Map', strength: 'Strength Boards', screeners: 'Screeners', radar: 'Stock Radar', calm: 'Calm Zone', journal: 'Journal', academy: 'F&O Academy', news: 'Market News' }[v]);
    symbar(v);
    load(v);
    clearInterval(timer);
    var every = { news: 300, pulse: 60, options: 60, oiflow: 60, liquidity: 90, strength: 60, radar: 90 }[v];
    if (every) timer = setInterval(function () { if (mopen() && !document.hidden) load(v, true); }, every * 1000);
  }
  function symbar(v) {
    var seg = document.querySelector('.sym-seg[data-for="' + v + '"]');
    if (!seg) return;
    var list = PICKS[v].slice();
    if (list.indexOf(SYM[v]) < 0) list.push(SYM[v]);
    seg.innerHTML = list.map(function (s) { return '<button class="' + (s === SYM[v] ? 'on' : '') + '" data-s="' + s + '">' + s + '</button>'; }).join('');
  }
  function setSym(v, s) {
    SYM[v] = s.toUpperCase();
    try { var o = {}; Object.keys(SYM).forEach(function (k) { o[k] = SYM[k]; }); localStorage.setItem('mm.pulse.sym', JSON.stringify(o)); } catch (e) {}
    history.replaceState(null, '', '#' + v + '?s=' + encodeURIComponent(SYM[v]));
    symbar(v); load(v);
  }
  document.addEventListener('click', function (e) {
    var b = e.target.closest('.sym-seg button');
    if (b) setSym(b.parentNode.getAttribute('data-for'), b.getAttribute('data-s'));
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && e.target.classList && e.target.classList.contains('sym-in') && e.target.value.trim()) {
      setSym(e.target.getAttribute('data-for'), e.target.value.trim()); e.target.value = '';
    }
  });
  var SKEL = { pulse: ['pRot', 'pMood', 'pIdx0', 'pIdx1', 'pIdx2', 'pVix', 'pMap'], options: ['oOI', 'oPCR', 'oSmile', 'oRange'], oiflow: ['fFlow', 'fMom', 'fNext', 'fHot'],
               liquidity: ['lScores'], strength: ['sPic', 'sBull', 'sBear'], radar: ['rRadar', 'rScore', 'rSigs'], calm: ['cMap', 'cTop'], news: ['nList'] }, seen = {};
  function skel(v) {
    if (seen[v]) return; seen[v] = 1;
    (SKEL[v] || []).forEach(function (id) { var el = $(id); if (el) el.innerHTML = '<div class="skel-line big"></div><div class="skel-line"></div><div class="skel-line w60"></div><div class="skel-line w40"></div>'; });
  }
  function load(v, quiet) {
    document.documentElement.classList.toggle('mm-still', !!quiet);
    if (!quiet) { skel(v); MMShell.stagger($('v-' + v)); }
    var f = { pulse: loadPulse, options: loadOptions, oiflow: loadFlow, liquidity: loadLiq, strength: loadStrength, screeners: loadScreeners,
              radar: loadRadar, calm: loadCalm, journal: loadJournal, academy: loadAcademy, news: loadNews }[v];
    if (f) f(quiet);
  }

  /* ---------------- small SVG helpers ---------------- */
  function gauge(val, min, max, opts) {
    opts = opts || {};
    var w = opts.w || 200, h = opts.h || 120, cx = w / 2, cy = h - 14, r = Math.min(cx - 12, cy - 6), t = (Math.max(min, Math.min(max, val)) - min) / (max - min);
    function pt(f, rr) { var a = Math.PI * (1 - f); return [cx + rr * Math.cos(a), cy - rr * Math.sin(a)]; }
    function arc(f0, f1, col, wd) { var a = pt(f0, r), b = pt(f1, r); return '<path d="M' + a[0] + ' ' + a[1] + ' A' + r + ' ' + r + ' 0 0 1 ' + b[0] + ' ' + b[1] + '" stroke="' + col + '" stroke-width="' + wd + '" fill="none" stroke-linecap="round"/>'; }
    var segs = opts.segs || [[0, .33, css('--tb-dn')], [.33, .66, css('--tb-mut2')], [.66, 1, css('--tb-up')]];
    var s = '<svg viewBox="0 0 ' + w + ' ' + h + '" width="100%" style="max-width:' + w + 'px">';
    segs.forEach(function (g) { s += arc(g[0] + 0.005, g[1] - 0.005, g[2], 9).replace('stroke-width', 'opacity=".28" stroke-width'); });
    var on = segs.filter(function (g) { return t >= g[0]; }).pop() || segs[0];
    s += arc(Math.max(on[0], 0.005), Math.max(on[0] + 0.01, Math.min(t, on[1])), on[2], 9).replace('<path ', '<path pathLength="1" class="draw" ');
    var ang = (t - 0.5) * 180;     // needle drawn pointing up, then rotated into place (animated sweep)
    s += '<g class="needle" style="--a:' + ang.toFixed(1) + 'deg;transform:rotate(' + ang.toFixed(1) + 'deg);transform-origin:' + cx + 'px ' + cy + 'px"><line x1="' + cx + '" y1="' + cy + '" x2="' + cx + '" y2="' + (cy - r + 16) + '" stroke="' + css('--tb-fg') + '" stroke-width="3" stroke-linecap="round"/></g><circle cx="' + cx + '" cy="' + cy + '" r="5" fill="' + css('--tb-fg') + '"/>';
    if (opts.lo) s += '<text x="8" y="' + (h - 1) + '" font-size="9" fill="' + css('--tb-mut2') + '">' + opts.lo + '</text>';
    if (opts.hi) s += '<text x="' + (w - 8) + '" y="' + (h - 1) + '" font-size="9" text-anchor="end" fill="' + css('--tb-mut2') + '">' + opts.hi + '</text>';
    return s + '</svg>';
  }
  function spark(vals, w, h, col) {
    if (!vals || vals.length < 2) return '';
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals), d = (hi - lo) || 1;
    var pts = vals.map(function (v, i) { return (i / (vals.length - 1) * w).toFixed(1) + ',' + (h - 3 - (v - lo) / d * (h - 6)).toFixed(1); });
    return '<svg viewBox="0 0 ' + w + ' ' + h + '" width="100%" height="' + h + '" preserveAspectRatio="none"><polyline pathLength="1" class="draw" points="' + pts.join(' ') + '" fill="none" stroke="' + (col || css('--tb-brand')) + '" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>';
  }
  function rangeBar(lo, hi, spot, labels) {
    if (!lo || !hi || !spot) return '';
    var a = Math.min(lo, spot), b = Math.max(hi, spot), pad = (b - a) * 0.12 || 1, A = a - pad, B = b + pad;
    function x(v) { return ((v - A) / (B - A) * 100).toFixed(2) + '%'; }
    return '<div style="position:relative;height:44px;margin:16px 4px 4px">' +
      '<div style="position:absolute;left:0;right:0;top:20px;height:6px;border-radius:6px;background:var(--tb-soft)"></div>' +
      '<div style="position:absolute;left:' + x(lo) + ';width:calc(' + x(hi) + ' - ' + x(lo) + ');top:20px;height:6px;border-radius:6px;background:var(--tb-brand)" class="grow-l"></div>' +
      '<div style="position:absolute;left:' + x(lo) + ';top:12px;width:2px;height:22px;background:var(--tb-up)"></div>' +
      '<div style="position:absolute;left:' + x(hi) + ';top:12px;width:2px;height:22px;background:var(--tb-dn)"></div>' +
      '<div style="position:absolute;left:' + x(spot) + ';top:-6px;transform:translateX(-50%);background:var(--tb-fg);color:var(--tb-card);font-size:11px;font-weight:600;padding:1px 6px;border-radius:6px">' + n(spot, 0) + '</div>' +
      '<div style="position:absolute;left:' + x(spot) + ';top:14px;width:2px;height:18px;background:var(--tb-fg);transform:translateX(-1px)"></div>' +
      '<div style="position:absolute;left:' + x(lo) + ';top:34px;transform:translateX(-50%);font-size:11px;color:var(--tb-mut)">' + (labels ? labels[0] : n(lo, 0)) + '</div>' +
      '<div style="position:absolute;left:' + x(hi) + ';top:34px;transform:translateX(-50%);font-size:11px;color:var(--tb-mut)">' + (labels ? labels[1] : n(hi, 0)) + '</div></div>';
  }
  function heat(c) {      // −4..+4 % → Tredye-like red/green tint
    var t = Math.max(-1, Math.min(1, (c || 0) / 4)), L = MMShell.theme() === 'light';
    if (Math.abs(t) < 0.04) return L ? '#e9e9ef' : '#2a2a38';
    var a = 0.18 + 0.72 * Math.abs(t);
    return t > 0 ? 'rgba(22,163,74,' + a + ')' : 'rgba(220,38,38,' + a + ')';
  }

  /* ---------------- PULSE ---------------- */
  var pulseData = null, pSub = 'ov';
  $('pSeg').addEventListener('click', function (e) {
    var b = e.target.closest('button'); if (!b) return;
    pSub = b.getAttribute('data-v');
    this.querySelectorAll('button').forEach(function (x) { x.classList.toggle('on', x === b); });
    ['ov', 'mf', 'of'].forEach(function (k) { $('p-' + k).style.display = k === pSub ? '' : 'none'; });
    if (pSub === 'of') loadSpurts();
    if (pulseData) drawPulse(pulseData);
  });
  function loadPulse(quiet) {
    if (!quiet && !pulseData) $('pHead').textContent = 'Reading the market…';
    api('/api/pulse/overview').then(function (d) { pulseData = d; err('pErr'); drawPulse(d); })
      .catch(function (e) { err('pErr', e); $('pHead').textContent = 'Market data is not available right now.'; });
  }
  function drawPulse(d) {
    $('pAsOf').textContent = d.as_of + (d.market_open ? ' · live' : ' · market closed');
    $('pSrc').textContent = 'Source: ' + d.source;
    var m = d.mood || {}, b = d.breadth || {};
    $('pHead').innerHTML = 'Market mood is <em>' + esc(m.label) + '</em> (' + m.score + '/100): ' + b.adv + ' of ' + b.total + ' F&amp;O stocks up, money ' +
      (d.flow.net >= 0 ? 'arriving' : 'leaving') + ' (' + cr(d.flow.net) + ' est.).';
    $('pMood').innerHTML = '<div>' + gauge(m.score, 0, 100, { lo: 'Risk-off', hi: 'Risk-on' }) + '<div style="text-align:center;margin-top:-6px"><span class="big-n" data-k="mood" data-roll="' + m.score + '"></span> <span class="mut">/ 100</span></div></div>' +
      '<div><div class="mid-n" style="margin-bottom:8px">' + esc(m.label) + '</div>' + (m.parts || []).map(function (p) {
        return '<div class="kv"><span class="mut">' + p.name + ' <span class="mut2">' + Math.round(p.weight * 100) + '%</span></span><b class="' + cls(p.value) + '">' + sg(p.value, 2) + '</b></div>';
      }).join('') + '</div>';
    $('pBreadth').innerHTML = 'Breadth <b>' + b.adv + ':' + b.dec + '</b> · unchanged ' + b.unch + ' · net flow <b class="' + cls(d.flow.net) + '">' + cr(d.flow.net) + '</b> <span class="mut2">(' + esc(d.flow.note) + ')</span>';
    (d.indices || []).forEach(function (x, i) {
      var el = $('pIdx' + i); if (!el) return;
      var bias = x.bias || '—', bc = bias === 'Bullish' ? 'up' : bias === 'Bearish' ? 'dn' : '';
      el.innerHTML = '<div class="ch"><h2>' + x.name + '</h2><span class="pill ' + bc + '">' + bias + '</span></div>' +
        '<div class="big-n" data-k="idx' + x.name + '" data-roll="' + n(x.lp, 2) + '"></div><span class="pill ' + cls(x.chp) + '" style="margin-top:6px">' + sg(x.ch, 2) + ' (' + sg(x.chp, 2) + '%)</span>' +
        (x.range ? rangeBar(x.range[0], x.range[1], x.lp) + '<div class="note">Expected <b style="color:var(--tb-brand)">' + n(x.range[0], 0) + ' – ' + n(x.range[1], 0) + '</b> by ' + esc(x.expiry || 'expiry') +
          (x.iv ? ' · ATM IV ' + n(x.iv, 1) + '%' : '') + '</div>' : '<div class="note" style="margin-top:12px">' + esc(x.error || 'Option chain not available') + '</div>') +
        (x.high && x.low ? '<div class="note">Day range ' + n(x.low, 0) + ' – ' + n(x.high, 0) + (x.pcr ? ' · PCR ' + n(x.pcr, 2) : '') + '</div>' : '');
    });
    drawRotation(d.sectors || []);
    drawTreemap(d.sectors || []);
    $('pLead').innerHTML = '<span class="up">↗ Leaders</span> ' + (d.leaders || []).map(function (l) { return esc(l[0]) + ' ' + sg(l[1], 1) + '%'; }).join(', ') +
      ' &nbsp; <span class="dn">↘ Laggards</span> ' + (d.laggards || []).map(function (l) { return esc(l[0]) + ' ' + sg(l[1], 1) + '%'; }).join(', ');
    var v = d.vix || {};
    $('pVix').innerHTML = '<div style="text-align:center">' + gauge(v.pct1y || 0, 0, 100, { w: 220, h: 128, segs: [[0, .33, css('--tb-up')], [.33, .66, css('--tb-mut2')], [.66, 1, css('--tb-dn')]] }) +
      '<div class="big-n" style="margin-top:-8px" data-k="vix" data-roll="' + n(v.lp, 2) + '"></div><span class="pill ' + cls(-(v.ch || 0)) + '">' + sg(v.ch, 2) + ' (' + sg(v.chp, 2) + '%)</span> <span class="pill">' + esc(v.regime || '—') + '</span></div>' +
      '<div style="margin:14px 0 4px">' + spark(v.spark, 240, 46) + '</div><div class="kv"><span class="mut">30 sessions</span><span class="mut">1y ' + n(v.lo1y, 1) + ' – ' + n(v.hi1y, 1) + '</span></div>' +
      '<div class="note" style="margin-top:8px">' + (v.pct1y !== null && v.pct1y !== undefined ? '<b>' + n(v.pct1y, 0) + 'th percentile</b> of the past year · ' : '') + (v.daily_move ? 'NIFTY daily move ±' + n(v.daily_move, 2) + '%' : '') + '</div>';
    if (pSub === 'mf') drawMoneyTable(d.sectors || []);
    MMShell.rollAll($('v-pulse'));
  }
  function drawRotation(secs) {
    var s = secs.slice().sort(function (a, b) { return a.flow - b.flow; });
    var leave = s.filter(function (x) { return x.flow < 0; }).slice(0, 6), arrive = s.filter(function (x) { return x.flow > 0; }).reverse().slice(0, 6);
    var mx = Math.max.apply(null, s.map(function (x) { return Math.abs(x.flow); }).concat([1]));
    var rows = Math.max(leave.length, arrive.length), h = '';
    for (var i = 0; i < rows; i++) {
      var L = leave[i], A = arrive[i];
      h += '<div class="flowrow"><div class="l">' + (L ? '<span class="mut" style="margin-right:8px">' + esc(L.sector) + ' <b class="dn">' + cr(L.flow) + '</b></span><span class="b grow-r" style="width:' + (Math.abs(L.flow) / mx * 46) + '%;background:var(--tb-dn);opacity:.75"></span>' : '') + '</div>' +
        '<div class="m">' + (i === 0 ? '<b style="color:var(--tb-brand)">' + cr(s.reduce(function (a, x) { return a + x.flow; }, 0)) + '</b>' : '') + '</div>' +
        '<div>' + (A ? '<span class="b grow-l" style="display:inline-block;vertical-align:middle;width:' + (A.flow / mx * 46) + '%;background:var(--tb-up);opacity:.75"></span><span class="mut" style="margin-left:8px">' + esc(A.sector) + ' <b class="up">+' + cr(A.flow).replace('−', '') + '</b></span>' : '') + '</div></div>';
    }
    $('pRot').innerHTML = h || '<div class="loading">No sector data yet.</div>';
    $('pRotN').textContent = 'Each sector\'s traded value, tilted by how its stocks moved today. Left: sectors money is leaving. Right: sectors it is arriving in. Middle: net.';
  }
  // squarified treemap: sectors sized by turnover, stocks inside coloured by % change
  function squarify(items, x, y, w, h) {
    var out = [], tot = items.reduce(function (a, b) { return a + b.v; }, 0);
    if (!tot || w <= 0 || h <= 0) return out;
    var scale = w * h / tot, rest = items.map(function (it) { return { it: it, a: it.v * scale }; }), X = x, Y = y, W = w, H = h;
    function worst(row, side) { var s = row.reduce(function (a, b) { return a + b.a; }, 0), mx = Math.max.apply(null, row.map(function (r) { return r.a; })), mn = Math.min.apply(null, row.map(function (r) { return r.a; })); return Math.max(side * side * mx / (s * s), s * s / (side * side * mn)); }
    while (rest.length) {
      var side = Math.min(W, H), row = [rest[0]], i = 1;
      while (i < rest.length && worst(row.concat([rest[i]]), side) <= worst(row, side)) { row.push(rest[i]); i++; }
      rest = rest.slice(i);
      var s = row.reduce(function (a, b) { return a + b.a; }, 0);
      if (W >= H) { var cw = s / H, cy = Y; row.forEach(function (r) { var ch = r.a / cw; out.push({ it: r.it, x: X, y: cy, w: cw, h: ch }); cy += ch; }); X += cw; W -= cw; }
      else { var rh = s / W, cx = X; row.forEach(function (r) { var cwi = r.a / rh; out.push({ it: r.it, x: cx, y: Y, w: cwi, h: rh }); cx += cwi; }); Y += rh; H -= rh; }
    }
    return out;
  }
  function drawTreemap(secs) {
    var box = $('pMap'), W = box.clientWidth || 800, H = box.clientHeight || 430;
    var top = secs.filter(function (s) { return s.value > 0; }).slice(0, 14).map(function (s) { return { v: s.value, s: s }; });
    var h = '', k = 0;
    squarify(top, 0, 0, W, H).forEach(function (r) {
      var s = r.it.s, hdr = r.h > 40 ? 18 : 0;
      h += '<div class="tm-sec" style="left:' + r.x + 'px;top:' + r.y + 'px;width:' + r.w + 'px">' + esc(s.sector) + ' <span class="' + cls(s.chg) + '" style="font-weight:500">' + sg(s.chg, 1) + '%</span></div>';
      var st = (s.stocks || []).filter(function (x) { return x.v > 0; }).map(function (x) { return { v: x.v, x: x }; });
      squarify(st, r.x + 1, r.y + hdr + 1, r.w - 2, r.h - hdr - 2).forEach(function (c) {
        var big = c.w > 58 && c.h > 30;
        h += '<div class="tm-cell pop" title="' + esc(c.it.x.s) + ' ' + sg(c.it.x.c, 2) + '% · ' + cr(c.it.x.v) + '" style="animation-delay:' + Math.min(900, (k++) * 6) + 'ms;left:' + c.x + 'px;top:' + c.y + 'px;width:' + c.w + 'px;height:' + c.h + 'px;background:' + heat(c.it.x.c) + ';color:' + (Math.abs(c.it.x.c) > 2.2 ? '#fff' : 'var(--tb-fg)') + '">' +
          (big ? '<b>' + esc(c.it.x.s) + '</b>' + sg(c.it.x.c, 1) + '%' : '') + '</div>';
      });
    });
    box.innerHTML = h || '<div class="loading">No sector data.</div>';
  }
  function drawMoneyTable(secs) {
    $('pMfT').innerHTML = '<tr><th>Sector</th><th class="r">Stocks</th><th class="r">Turnover</th><th class="r">Wtd change</th><th class="r">Est. net flow</th><th>Top names</th></tr>' +
      secs.slice().sort(function (a, b) { return b.flow - a.flow; }).map(function (s) {
        return '<tr><td><b>' + esc(s.sector) + '</b></td><td class="r">' + s.n + '</td><td class="r">' + cr(s.value) + '</td><td class="r ' + cls(s.chg) + '">' + sg(s.chg, 2) + '%</td><td class="r ' + cls(s.flow) + '">' + cr(s.flow) + '</td><td class="mut">' +
          (s.stocks || []).slice(0, 4).map(function (x) { return esc(x.s) + ' <span class="' + cls(x.c) + '">' + sg(x.c, 1) + '%</span>'; }).join(', ') + '</td></tr>';
      }).join('');
  }
  function loadSpurts() {
    if (pulseData) {
      var all = [];
      (pulseData.sectors || []).forEach(function (s) { (s.stocks || []).forEach(function (x) { all.push({ s: x.s, c: x.c, v: x.v, lp: x.lp, sec: s.sector }); }); });
      all.sort(function (a, b) { return b.v - a.v; });
      $('pOfT').innerHTML = '<tr><th>Symbol</th><th>Sector</th><th class="r">Last</th><th class="r">Change</th><th class="r">Turnover</th></tr>' + all.slice(0, 40).map(function (x) {
        return '<tr><td><a href="#radar?s=' + x.s + '">' + esc(x.s) + '</a></td><td class="mut">' + esc(x.sec) + '</td><td class="r">' + n(x.lp, 2) + '</td><td class="r ' + cls(x.c) + '">' + sg(x.c, 2) + '%</td><td class="r">' + cr(x.v) + '</td></tr>';
      }).join('');
    }
    api('/api/pulse/screeners/oispurts').then(function (d) { $('pSpT').innerHTML = spurtsTable(d.rows.slice(0, 40)); })
      .catch(function (e) { $('pSpT').innerHTML = '<tr><td class="mut">' + esc(e.message) + '</td></tr>'; });
  }
  function spurtsTable(rows) {
    return '<tr><th>Symbol</th><th class="r">Last</th><th class="r">OI</th><th class="r">OI change</th><th class="r">OI %</th><th class="r">Volume</th></tr>' + rows.map(function (r) {
      return '<tr><td><a href="#radar?s=' + esc(r.symbol) + '">' + esc(r.symbol) + '</a></td><td class="r">' + n(r.lp, 2) + '</td><td class="r">' + kfmt(r.oi) + '</td><td class="r ' + cls(r.doi) + '">' + kfmt(r.doi) + '</td><td class="r ' + cls(r.doi_pct) + '">' + sg(r.doi_pct, 1) + '%</td><td class="r">' + kfmt(r.volume) + '</td></tr>';
    }).join('');
  }

  /* ---------------- OPTIONS ---------------- */
  function loadOptions(quiet) {
    var s = SYM.options;
    if (!quiet) { $('oHead').textContent = 'Reading the ' + s + ' option chain…'; }
    api('/api/pulse/options?symbol=' + encodeURIComponent(s)).then(function (d) { err('oErr'); drawOptions(d); })
      .catch(function (e) { err('oErr', e); $('oHead').textContent = s + ': option chain not available.'; });
  }
  function drawOptions(d) {
    $('oAsOf').textContent = d.as_of;
    $('oExp').textContent = 'Expiry ' + (d.expiry || '—') + ' · ' + n(d.days, 1) + ' days';
    var bc = d.bias === 'Bullish' ? 'up' : d.bias === 'Bearish' ? 'dn' : '';
    $('oHead').innerHTML = d.symbol + ' writers lean <em>' + d.bias.toLowerCase() + '</em>: floor ' + n(d.floor, 0) + ', ceiling ' + n(d.ceiling, 0) + '.';
    $('oSpotL').textContent = d.symbol + ' spot';
    MMShell.roll($('oSpot'), n(d.spot, 2), 'oSpot' + d.symbol);
    $('oLot').textContent = 'ATM ' + n(d.atm, 0) + ' · ' + (d.source || '');
    MMShell.roll($('oPain'), n(d.max_pain, 0), 'oPain' + d.symbol);
    $('oPainN').textContent = sg((d.max_pain - d.spot) / d.spot * 100, 2) + '% from spot, ' + (d.max_pain > d.spot ? 'above' : 'below');
    MMShell.roll($('oIV'), d.atm_iv ? n(d.atm_iv, 1) + '%' : '—', 'oIV' + d.symbol);
    $('oStr').textContent = 'Straddle ₹' + n(d.straddle, 1) + ' · ±' + n(d.sigma / d.spot * 100, 1) + '% by expiry' + (d.iv_rank && d.iv_rank.available ? ' · IV rank ' + n(d.iv_rank.rank, 0) : '');
    $('oBias').innerHTML = '<span class="' + bc + '">' + (d.bias === 'Bullish' ? '↗ ' : d.bias === 'Bearish' ? '↘ ' : '— ') + d.bias + '</span>';
    $('oConvN').textContent = d.conviction + '%'; $('oConv').style.width = d.conviction + '%';
    // OI butterfly
    var rows = d.rows.slice().reverse(), mx = Math.max.apply(null, rows.map(function (r) { return Math.max(r.ce_oi, r.pe_oi); }).concat([1]));
    var h = '<div style="display:grid;grid-template-columns:1fr 64px 1fr;gap:3px 6px;font-size:11.5px">';
    rows.forEach(function (r) {
      var atm = r.strike === d.atm, ps = r.strike === d.put_wall || r.strike === d.floor, cs = r.strike === d.call_wall || r.strike === d.ceiling;
      h += '<div style="display:flex;justify-content:flex-end;align-items:center;gap:4px" title="PE OI ' + kfmt(r.pe_oi) + ' (' + sg(r.pe_chg, 0) + ')">' +
        '<span class="mut2" style="white-space:nowrap;flex:0 0 auto">' + (r.pe_chg ? kfmt(r.pe_chg) : '') + '</span><span class="grow-r" style="position:relative;height:13px;flex:0 0 auto;width:' + (r.pe_oi / mx * 80) + '%;background:var(--tb-up);opacity:' + (ps ? 1 : .55) + ';border-radius:4px 0 0 4px">' +
        (r.pe_chg > 0 ? '<i style="position:absolute;left:0;top:0;bottom:0;width:' + Math.min(100, r.pe_chg / (r.pe_oi || 1) * 100) + '%;background:var(--tb-brand);opacity:.7;border-radius:4px 0 0 4px"></i>' : '') + '</span></div>' +
        '<div style="text-align:center;font-weight:' + (atm ? 700 : 500) + ';' + (atm ? 'background:var(--tb-brand-soft);color:var(--tb-brand);border-radius:6px' : '') + '">' + n(r.strike, 0) + '</div>' +
        '<div style="display:flex;align-items:center;gap:4px" title="CE OI ' + kfmt(r.ce_oi) + ' (' + sg(r.ce_chg, 0) + ')"><span class="grow-l" style="position:relative;height:13px;flex:0 0 auto;width:' + (r.ce_oi / mx * 80) + '%;background:var(--tb-dn);opacity:' + (cs ? 1 : .55) + ';border-radius:0 4px 4px 0">' +
        (r.ce_chg > 0 ? '<i style="position:absolute;right:0;top:0;bottom:0;width:' + Math.min(100, r.ce_chg / (r.ce_oi || 1) * 100) + '%;background:var(--tb-brand);opacity:.7;border-radius:0 4px 4px 0"></i>' : '') + '</span><span class="mut2" style="white-space:nowrap">' + (r.ce_chg ? kfmt(r.ce_chg) : '') + '</span></div>';
    });
    $('oOI').innerHTML = h + '</div>';
    // PCR
    $('oPCR').innerHTML = '<div style="text-align:center">' + gauge(d.pcr || 0, 0.4, 1.8, { w: 210, h: 120, lo: 'Call-heavy', hi: 'Put-heavy', segs: [[0, .33, css('--tb-dn')], [.33, .66, css('--tb-mut2')], [.66, 1, css('--tb-up')]] }) +
      '<div class="big-n" style="margin-top:-6px" data-k="pcr' + d.symbol + '" data-roll="' + n(d.pcr, 2) + '"></div></div><div class="note" style="text-align:center">' + (d.pcr > 1.2 ? 'Put-heavy positioning' : d.pcr < 0.8 ? 'Call-heavy positioning' : 'Balanced positioning') + (d.dpcr ? ' · Δ-PCR ' + n(d.dpcr, 2) : '') + '</div>';
    // IV smile
    var sm = d.smile.filter(function (r) { return r.ce_iv || r.pe_iv; });
    if (sm.length > 2) {
      var W = 320, H = 130, ivs = []; sm.forEach(function (r) { if (r.ce_iv) ivs.push(r.ce_iv); if (r.pe_iv) ivs.push(r.pe_iv); });
      var lo = Math.min.apply(null, ivs), hi = Math.max.apply(null, ivs), k0 = sm[0].strike, k1 = sm[sm.length - 1].strike;
      function X(k) { return 8 + (k - k0) / ((k1 - k0) || 1) * (W - 16); } function Y(v) { return H - 18 - (v - lo) / ((hi - lo) || 1) * (H - 30); }
      function line(key, col) { var p = sm.filter(function (r) { return r[key]; }).map(function (r) { return X(r.strike).toFixed(1) + ',' + Y(r[key]).toFixed(1); }); return '<polyline pathLength="1" class="draw" points="' + p.join(' ') + '" fill="none" stroke="' + col + '" stroke-width="2"/>'; }
      $('oSmile').innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%">' + line('pe_iv', css('--tb-up')) + line('ce_iv', css('--tb-dn')) +
        '<line x1="' + X(d.spot) + '" x2="' + X(d.spot) + '" y1="4" y2="' + (H - 16) + '" stroke="' + css('--tb-mut2') + '" stroke-dasharray="3 3"/>' +
        '<text x="' + X(d.spot) + '" y="' + (H - 3) + '" font-size="10" text-anchor="middle" fill="' + css('--tb-mut') + '">spot ' + n(d.spot, 0) + '</text>' +
        '<text x="4" y="12" font-size="10" fill="' + css('--tb-mut2') + '">' + n(hi, 1) + '%</text><text x="4" y="' + (H - 20) + '" font-size="10" fill="' + css('--tb-mut2') + '">' + n(lo, 1) + '%</text></svg>' +
        '<div class="note">ATM IV ' + n(d.atm_iv, 1) + '% · straddle ₹' + n(d.straddle, 1) + ' · max pain ' + n(d.max_pain, 0) + '</div>';
    } else $('oSmile').innerHTML = '<div class="note">IV not available for this chain.</div>';
    $('oRange').innerHTML = '<div class="note">Writers expect, by ' + esc(d.expiry || 'expiry') + '</div><div class="mid-n">' + n(d.floor, 0) + ' – ' + n(d.ceiling, 0) + '</div>' + rangeBar(d.floor, d.ceiling, d.spot) +
      '<div class="kv"><span class="mut">Writers\' walls</span><b>' + n(d.put_wall, 0) + ' – ' + n(d.call_wall, 0) + '</b></div><div class="kv"><span class="mut">IV ±1σ band</span><b>' + n(d.band[0], 0) + ' – ' + n(d.band[1], 0) + '</b></div><div class="kv"><span class="mut">Conviction</span><b>' + d.conviction + '%</b></div>';
    MMShell.rollAll($('v-options'));
    $('oNotes').innerHTML = d.notes.map(function (x) { return '<li style="margin:4px 0">' + esc(x) + '</li>'; }).join('');
  }

  /* ---------------- OI FLOW ---------------- */
  var BC = { 'Long buildup': '#16a34a', 'Short buildup': '#dc2626', 'Short covering': '#0d9488', 'Long unwinding': '#ea580c' };
  function loadFlow(quiet) {
    var s = SYM.oiflow;
    if (!quiet) $('fHead').textContent = 'Reading the ' + s + ' option chain…';
    api('/api/pulse/oiflow?symbol=' + encodeURIComponent(s)).then(function (d) { err('fErr'); drawFlow(d); })
      .catch(function (e) { err('fErr', e); $('fHead').textContent = s + ': OI flow not available.'; });
  }
  function drawFlow(d) {
    $('fAsOf').textContent = d.as_of + ' · expiry ' + (d.expiry || '—');
    $('fHead').innerHTML = d.symbol + ': ' + esc(d.next) + '.';
    var by = {}; d.flows.forEach(function (f) { (by[f.strike] = by[f.strike] || {})[f.side] = f; });
    var ks = Object.keys(by).map(Number).sort(function (a, b) { return b - a; });
    var mx = Math.max.apply(null, d.flows.map(function (f) { return Math.abs(f.chg); }).concat([1]));
    var h = '<div style="display:grid;grid-template-columns:1fr 64px 1fr;gap:3px 6px;font-size:11.5px"><div class="mut2" style="text-align:right">PUT OI change · writers = floor</div><div></div><div class="mut2">CALL OI change · writers = ceiling</div>';
    ks.forEach(function (k) {
      var p = by[k].PE || { chg: 0 }, c = by[k].CE || { chg: 0 }, at = Math.abs(k - d.spot) === Math.min.apply(null, ks.map(function (x) { return Math.abs(x - d.spot); }));
      h += '<div style="display:flex;justify-content:flex-end;align-items:center;gap:4px" title="' + (p.build || '') + '"><span class="mut2" style="white-space:nowrap;flex:0 0 auto">' + (p.chg ? kfmt(p.chg) : '') + '</span><span class="grow-r" style="height:13px;flex:0 0 auto;border-radius:4px 0 0 4px;width:' + (Math.abs(p.chg) / mx * 80) + '%;background:' + (BC[p.build] || 'var(--tb-soft)') + '"></span></div>' +
        '<div style="text-align:center;font-weight:' + (at ? 700 : 500) + ';' + (at ? 'background:var(--tb-brand-soft);color:var(--tb-brand);border-radius:6px' : '') + '">' + n(k, 0) + '</div>' +
        '<div style="display:flex;align-items:center;gap:4px" title="' + (c.build || '') + '"><span class="grow-l" style="height:13px;flex:0 0 auto;border-radius:0 4px 4px 0;width:' + (Math.abs(c.chg) / mx * 80) + '%;background:' + (BC[c.build] || 'var(--tb-soft)') + '"></span><span class="mut2" style="white-space:nowrap">' + (c.chg ? kfmt(c.chg) : '') + '</span></div>';
    });
    $('fFlow').innerHTML = h + '</div>';
    var sn = d.snapshots || [];
    $('fSnapN').textContent = sn.length ? '(' + sn.length + ' today, kept while this page or the Options page is open)' : '';
    if (sn.length > 1) {
      var W = 600, H = 90, sp = sn.map(function (s) { return s.spot; }), lo = Math.min.apply(null, sp.concat(sn.map(function (s) { return s.floor; }))), hi = Math.max.apply(null, sp.concat(sn.map(function (s) { return s.ceiling; })));
      function X(i) { return i / (sn.length - 1) * (W - 10) + 5; } function Y(v) { return H - 14 - (v - lo) / ((hi - lo) || 1) * (H - 24); }
      function pl(f, col, dash) { return '<polyline pathLength="1" class="draw" points="' + sn.map(function (s, i) { return X(i).toFixed(1) + ',' + Y(f(s)).toFixed(1); }).join(' ') + '" fill="none" stroke="' + col + '" stroke-width="' + (dash ? 1.5 : 2) + '"' + (dash ? ' stroke-dasharray="4 3"' : '') + '/>'; }
      $('fTL').innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" preserveAspectRatio="none">' + pl(function (s) { return s.ceiling; }, css('--tb-dn'), 1) + pl(function (s) { return s.floor; }, css('--tb-up'), 1) + pl(function (s) { return s.spot; }, css('--tb-brand')) +
        '<text x="5" y="' + (H - 2) + '" font-size="10" fill="' + css('--tb-mut2') + '">' + sn[0].t + '</text><text x="' + (W - 5) + '" y="' + (H - 2) + '" font-size="10" text-anchor="end" fill="' + css('--tb-mut2') + '">' + sn[sn.length - 1].t + '</text></svg>' +
        '<div class="legend"><span><i style="background:var(--tb-brand)"></i>Spot</span><span><i style="background:var(--tb-dn)"></i>Ceiling</span><span><i style="background:var(--tb-up)"></i>Floor</span></div>';
    } else $('fTL').innerHTML = '<div class="note">Snapshots build up minute by minute while the market is open.</div>';
    $('fMom').innerHTML = '<div class="lbl">FLOW MOMENTUM</div><div class="mid-n ' + cls(d.momentum) + '" data-k="mom' + d.symbol + '" data-roll="' + sg(d.momentum, 0) + '"></div><div class="bar" style="margin:6px 0 2px;position:relative"><i style="margin-left:' + (50 + Math.min(0, d.momentum) / 2) + '%;width:' + Math.abs(d.momentum) / 2 + '%;background:' + (d.momentum >= 0 ? 'var(--tb-up)' : 'var(--tb-dn)') + '"></i></div><div class="note">−100 bearish · +100 bullish</div>' +
      '<div class="lbl" style="margin-top:12px">SPEED</div><div class="mid-n">' + (d.speed !== null && d.speed !== undefined ? kfmt(d.speed) + '<span class="mut" style="font-size:13px"> /min</span>' : '—') + '</div><div class="note">OI contracts changing per minute near spot</div>' +
      (d.futures ? '<div class="lbl" style="margin-top:12px">FUTURES</div><div><b>' + n(d.futures.lp, 2) + '</b> <span class="' + cls(d.futures.chp) + '">' + sg(d.futures.chp, 2) + '%</span></div>' : '');
    $('fNext').innerHTML = '<div class="mid-n">' + esc(d.next) + '</div><div class="kv"><span class="mut">PCR</span><b>' + n(d.pcr, 2) + '</b></div><div class="kv"><span class="mut">Ceiling</span><b class="dn">' + n(d.ceiling, 0) + '</b></div><div class="kv"><span class="mut">Floor</span><b class="up">' + n(d.floor, 0) + '</b></div><div class="kv"><span class="mut">Conviction</span><b>' + d.conviction + '%</b></div>';
    setTimeout(function () { MMShell.rollAll($('v-oiflow')); }, 0);
    $('fHot').innerHTML = d.hot.map(function (f) { return '<div class="kv"><span><b>' + n(f.strike, 0) + ' ' + f.side + '</b> <span class="mut" style="font-size:12px">' + f.build + '</span></span><b style="color:' + BC[f.build] + '">' + kfmt(f.chg) + '</b></div>'; }).join('');
  }

  /* ---------------- LIQUIDITY ---------------- */
  var lRes = '5', lChart = null, lSeries = null, lVwap = null, lLines = [];
  $('lRes').addEventListener('click', function (e) { var b = e.target.closest('button'); if (!b) return; lRes = b.getAttribute('data-r'); this.querySelectorAll('button').forEach(function (x) { x.classList.toggle('on', x === b); }); loadLiq(); });
  function loadLiq(quiet) {
    var s = SYM.liquidity;
    if (!quiet) $('lHead').textContent = 'Finding where the big money sits in ' + s + '…';
    api('/api/pulse/liquidity?symbol=' + encodeURIComponent(s) + '&res=' + lRes).then(function (d) { err('lErr'); drawLiq(d); })
      .catch(function (e) { err('lErr', e); $('lHead').textContent = s + ': liquidity map not available.'; });
  }
  function drawLiq(d) {
    var pos = d.ceiling > d.floor ? (d.spot - d.floor) / (d.ceiling - d.floor) : 0.5;
    $('lAsOf').textContent = 'expiry ' + (d.expiry || '—');
    $('lHead').innerHTML = d.symbol + ' at ' + n(d.spot, 1) + ' sits ' + (pos < 0.33 ? 'near the <em>put wall</em> (support)' : pos > 0.66 ? 'near the <em>call wall</em> (resistance)' : '<em>mid-range</em>') + ' between ' + n(d.floor, 0) + ' and ' + n(d.ceiling, 0) + '.';
    function sc(t, v, s, l, c) { return '<div class="c" style="display:flex;gap:14px;align-items:center"><div style="width:62px;height:62px;border-radius:50%;display:grid;place-items:center;font-weight:600;font-size:18px;background:conic-gradient(' + c + ' ' + s * 3.6 + 'deg,var(--tb-soft) 0)" class="pop"><span style="width:50px;height:50px;border-radius:50%;background:var(--tb-card);display:grid;place-items:center">' + s + '</span></div><div><div class="lbl">' + t + '</div><div class="mid-n">' + v + '</div><div class="note">' + l + '</div></div></div>'; }
    $('lScores').innerHTML = sc('SUPPORT', n(d.support.level, 0), d.support.score, 'Put OI strength vs the biggest wall', 'var(--tb-up)') + sc('RESISTANCE', n(d.resistance.level, 0), d.resistance.score, 'Call OI strength vs the biggest wall', 'var(--tb-dn)') +
      sc('WRITERS', '<span class="' + (d.bias === 'Bullish' ? 'up' : d.bias === 'Bearish' ? 'dn' : '') + '">' + d.bias + '</span>', d.conviction, 'Option writers\' lean', 'var(--tb-brand)');
    var el = $('lChart');
    if (!lChart) {
      lChart = LightweightCharts.createChart(el, { autoSize: true, timeScale: { timeVisible: true, secondsVisible: false }, localization: { timeFormatter: istTime } });
      lSeries = lChart.addCandlestickSeries({ upColor: '#16a34a', downColor: '#dc2626', borderVisible: false, wickUpColor: '#16a34a', wickDownColor: '#dc2626' });
      lVwap = lChart.addLineSeries({ color: '#2563eb', lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
    }
    lSeries.setData(d.candles.map(shiftT));
    lVwap.setData(d.vwap.map(function (p) { return { time: p.time + 19800, value: p.value }; }));
    lLines.forEach(function (l) { lSeries.removePriceLine(l); }); lLines = [];
    function pl(p, col, title, style) { if (p) lLines.push(lSeries.createPriceLine({ price: p, color: col, lineWidth: 1, lineStyle: style === undefined ? 0 : style, axisLabelVisible: true, title: title })); }
    pl(d.put_wall, '#16a34a', 'Put wall'); pl(d.call_wall, '#dc2626', 'Call wall'); pl(d.max_pain, '#7c3aed', 'Max pain', 2);
    if (d.floor !== d.put_wall) pl(d.floor, '#16a34a', 'Floor', 2); if (d.ceiling !== d.call_wall) pl(d.ceiling, '#dc2626', 'Ceiling', 2);
    pl(d.band[0], '#94a3b8', '−1σ', 3); pl(d.band[1], '#94a3b8', '+1σ', 3);
    if (d.profile) { pl(d.profile.poc, '#d97706', 'POC', 0); pl(d.profile.vah, '#d97706', 'VAH', 1); pl(d.profile.val, '#d97706', 'VAL', 1); }
    lChart.timeScale().fitContent();
    if (d.profile) {
      var bins = d.profile.bins.slice().reverse(), mx = Math.max.apply(null, bins.map(function (b) { return b[1]; }).concat([1]));
      $('lProf').innerHTML = '<div class="lbl" style="margin-bottom:4px">Profile</div>' + bins.map(function (b) {
        var poc = Math.abs(b[0] - d.profile.poc) < 1e-6 || b[0] === d.profile.poc, inva = b[0] <= d.profile.vah && b[0] >= d.profile.val;
        return '<div title="' + n(b[0], 1) + '" style="height:6px;margin:1px 0;border-radius:3px;width:' + (b[1] / mx * 100) + '%;background:' + (poc ? '#d97706' : inva ? 'rgba(217,119,6,.45)' : 'var(--tb-soft2)') + '"></div>';
      }).join('');
    } else $('lProf').innerHTML = '';
    $('lNote').textContent = [d.index_note, 'Walls = biggest put/call OI; floor/ceiling = where writers added most today; ±1σ from ATM IV to expiry.'].filter(Boolean).join(' ');
  }
  function shiftT(c) { return { time: c.time + 19800, open: c.open, high: c.high, low: c.low, close: c.close }; }   // show IST on the time axis
  function istTime(t) { var d = new Date(t * 1000); return ('0' + d.getUTCHours()).slice(-2) + ':' + ('0' + d.getUTCMinutes()).slice(-2); }

  /* ---------------- STRENGTH ---------------- */
  function loadStrength() {
    api('/api/pulse/strength').then(function (d) {
      err('sErr');
      $('sAsOf').textContent = d.as_of + ' · ' + d.source;
      $('sPic').innerHTML = '<div><div class="lbl">NIFTY</div><div class="mid-n ' + cls(d.nifty_chp) + '" data-k="sNifty" data-roll="' + sg(d.nifty_chp, 2) + '%"></div></div>' +
        '<div><div class="lbl">Leading</div>' + d.leading.map(function (x) { return '<div class="kv"><span>' + esc(x[0]) + '</span><b class="' + cls(x[1]) + '">' + sg(x[1], 2) + '%</b></div>'; }).join('') + '</div>' +
        '<div><div class="lbl">Lagging</div>' + d.lagging.map(function (x) { return '<div class="kv"><span>' + esc(x[0]) + '</span><b class="' + cls(x[1]) + '">' + sg(x[1], 2) + '%</b></div>'; }).join('') + '</div>' +
        '<div><div class="kv"><span class="up">▲ Bullish</span><b>' + d.bullish.length + '</b></div><div class="kv"><span class="dn">▼ Bearish</span><b>' + d.bearish.length + '</b></div><div class="kv"><span class="mut">Choppy</span><b>' + d.choppy + '</b></div></div>';
      $('sBn').textContent = d.bullish.length; $('sRn').textContent = d.bearish.length;
      function board(list, col) {
        var mx = Math.max.apply(null, list.map(function (x) { return Math.abs(x.score); }).concat([1]));
        return list.slice(0, 25).map(function (x, i) {
          return '<div class="rank"><span class="n">' + ('0' + (i + 1)).slice(-2) + '</span><div><a class="s" href="#radar?s=' + x.symbol + '" style="color:var(--tb-fg);text-decoration:none">' + esc(x.symbol) + '</a><small>' + n(x.lp, 2) + ' · <span class="' + cls(x.chp) + '">' + sg(x.chp, 2) + '%</span> · ' + esc(x.sector) + '</small>' +
            '<div class="sbar"><i class="grow-l" style="width:' + Math.abs(x.score) / mx * 100 + '%;background:' + col + '"></i></div></div><span class="sc" style="color:' + col + '">' + sg(x.score, 2) + '</span></div>';
        }).join('') || '<div class="note">None right now.</div>';
      }
      $('sBull').innerHTML = board(d.bullish, 'var(--tb-up)'); $('sBear').innerHTML = board(d.bearish, 'var(--tb-dn)');
      $('sMeth').textContent = d.method;
      MMShell.rollAll($('v-strength'));
    }).catch(function (e) { err('sErr', e); });
  }

  /* ---------------- SCREENERS ---------------- */
  var scView = 'basis', scSeries = 'near', basisRows = [], basisSort = { k: 'basis_pct', d: 1 };
  $('scSeg').addEventListener('click', function (e) {
    var b = e.target.closest('button'); if (!b) return; scView = b.getAttribute('data-v');
    this.querySelectorAll('button').forEach(function (x) { x.classList.toggle('on', x === b); });
    ['basis', 'spurts', 'rsi'].forEach(function (k) { $('sc-' + k).style.display = k === scView ? '' : 'none'; });
    $('scSer').style.display = scView === 'basis' ? '' : 'none';
    $('scHead').textContent = { basis: 'Futures basis across the F&O universe', spurts: 'Open interest spurts across F&O stocks', rsi: 'RSI on three timeframes' }[scView];
    loadScreeners();
  });
  $('scSer').addEventListener('click', function (e) { var b = e.target.closest('button'); if (!b) return; scSeries = b.getAttribute('data-s'); this.querySelectorAll('button').forEach(function (x) { x.classList.toggle('on', x === b); }); loadScreeners(); });
  $('scFilter').addEventListener('input', drawBasisTable);
  $('scRun').addEventListener('click', function () {
    var b = this; b.disabled = true; b.textContent = 'Scanning…';
    api('/api/pulse/screeners/rsi').then(function (d) {
      err('scErr'); $('scRsiN').textContent = d.note + ' · ' + d.as_of;
      function c(v) { return v === null || v === undefined ? '<td class="r mut">—</td>' : '<td class="r" style="font-weight:600;color:' + (v >= 70 ? 'var(--tb-dn)' : v <= 30 ? 'var(--tb-up)' : v >= 55 ? 'var(--tb-up)' : v <= 45 ? 'var(--tb-dn)' : 'var(--tb-fg)') + '">' + n(v, 1) + '</td>'; }
      $('scRsi').innerHTML = '<tr><th>Symbol</th><th class="r">Last</th><th class="r">RSI 15m</th><th class="r">RSI 1h</th><th class="r">RSI 1D</th><th>Reading</th></tr>' + d.rows.map(function (r) {
        return r.error ? '<tr><td>' + esc(r.symbol) + '</td><td colspan="5" class="mut">' + esc(r.error) + '</td></tr>' :
          '<tr><td><a href="#radar?s=' + esc(r.symbol) + '">' + esc(r.symbol) + '</a></td><td class="r">' + n(r.lp, 2) + '</td>' + c(r.r15) + c(r.r60) + c(r.rd) + '<td><span class="pill">' + esc(r.state) + '</span></td></tr>';
      }).join('');
    }).catch(function (e) { err('scErr', e); }).then(function () { b.disabled = false; b.textContent = 'Run scan'; });
  });
  function loadScreeners() {
    if (scView === 'basis') {
      api('/api/pulse/screeners/basis?series=' + scSeries).then(function (d) {
        err('scErr'); basisRows = d.rows; $('scNote').textContent = d.note + ' Expiry ' + d.expiry + ' (' + d.days + ' days).';
        var tot = d.rows.length || 1, bk = d.buckets;
        function top(list, label, col) { return '<div class="lbl" style="margin-top:12px">' + label + '</div>' + list.map(function (r) { return '<div class="kv"><span>' + esc(r.symbol) + '</span><b style="color:' + col + '">' + sg(r.basis_pct, 2) + '%</b></div>'; }).join(''); }
        $('scBk').innerHTML = [['Discount', 'fut < spot · bearish tilt or dividend', 'var(--tb-dn)'], ['At par', 'within ±0.05%', 'var(--tb-mut2)'], ['Premium', 'fut > spot · normal carry / bullish', 'var(--tb-up)']].map(function (b) {
          return '<div style="margin-bottom:10px"><div class="kv"><span><b>' + b[0] + '</b> <span class="mut" style="font-size:12px">' + b[1] + '</span></span><b>' + (bk[b[0]] || 0) + '</b></div><div class="bar"><i class="grow-l" style="width:' + ((bk[b[0]] || 0) / tot * 100) + '%;background:' + b[2] + '"></i></div></div>';
        }).join('') + top(d.rows.slice(0, 4), 'Deepest discounts', 'var(--tb-dn)') + top(d.rows.slice(-4).reverse(), 'Richest premiums', 'var(--tb-up)');
        // basis map: x = basis %, y = OI change %, size = OI
        var W = 680, H = 260, xs = d.rows.map(function (r) { return r.basis_pct; }), lo = Math.min.apply(null, xs.concat([-0.5])), hi = Math.max.apply(null, xs.concat([1.5]));
        var ys = d.rows.map(function (r) { return Math.max(-30, Math.min(30, r.doi_pct || 0)); }), mo = Math.max.apply(null, d.rows.map(function (r) { return r.oi; }).concat([1]));
        function X(v) { return 30 + (v - lo) / ((hi - lo) || 1) * (W - 40); } function Y(v) { return H / 2 - v / 30 * (H / 2 - 16); }
        var s = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%"><line x1="' + X(0) + '" x2="' + X(0) + '" y1="6" y2="' + (H - 14) + '" stroke="' + css('--tb-mut2') + '" stroke-dasharray="3 3"/><line x1="' + X(d.fair_carry_pct) + '" x2="' + X(d.fair_carry_pct) + '" y1="6" y2="' + (H - 14) + '" stroke="' + css('--tb-brand') + '" stroke-dasharray="2 4"/><line x1="30" x2="' + W + '" y1="' + Y(0) + '" y2="' + Y(0) + '" stroke="' + css('--tb-line2') + '"/>';
        d.rows.forEach(function (r, i) { var col = r.bucket === 'Discount' ? css('--tb-dn') : r.bucket === 'Premium' ? css('--tb-up') : css('--tb-mut2'); s += '<circle class="pop" style="transform-box:fill-box;transform-origin:center;animation-delay:' + Math.min(800, i * 3) + 'ms" cx="' + X(r.basis_pct).toFixed(1) + '" cy="' + Y(ys[i]).toFixed(1) + '" r="' + (2 + Math.sqrt(r.oi / mo) * 6).toFixed(1) + '" fill="' + col + '" fill-opacity=".45" stroke="' + col + '"><title>' + esc(r.symbol) + ' basis ' + r.basis_pct + '% · OI ' + sg(r.doi_pct, 1) + '% · ' + r.build + '</title></circle>'; });
        s += '<text x="' + X(d.fair_carry_pct) + '" y="12" font-size="10" fill="' + css('--tb-brand') + '" text-anchor="middle">fair carry</text><text x="32" y="' + (H - 2) + '" font-size="10" fill="' + css('--tb-mut2') + '">x = basis % · y = OI change % · size = OI</text></svg>';
        $('scMap').innerHTML = s;
        drawBasisTable();
      }).catch(function (e) { err('scErr', e); });
    } else if (scView === 'spurts') {
      api('/api/pulse/screeners/oispurts').then(function (d) { err('scErr'); $('scSp').innerHTML = spurtsTable(d.rows); }).catch(function (e) { err('scErr', e); });
    } else err('scErr');
  }
  function drawBasisTable() {
    var f = ($('scFilter').value || '').toUpperCase(), k = basisSort.k, dd = basisSort.d;
    var rows = basisRows.filter(function (r) { return !f || r.symbol.indexOf(f) >= 0; }).sort(function (a, b) { return ((a[k] > b[k]) - (a[k] < b[k])) * dd; });
    var cols = [['symbol', 'Symbol'], ['spot', 'Spot'], ['fut', 'Future'], ['basis', 'Basis ₹'], ['basis_pct', 'Basis %'], ['annual', 'Annualised'], ['vs_carry', 'Vs carry'], ['oi', 'Fut OI'], ['doi_pct', 'Δ OI'], ['fut_chg', 'Fut chg'], ['build', 'Buildup'], ['bucket', 'Bucket']];
    $('scT').innerHTML = '<tr>' + cols.map(function (c) { return '<th data-k="' + c[0] + '" class="' + (c[0] === 'symbol' || c[0] === 'build' || c[0] === 'bucket' ? '' : 'r') + '">' + c[1] + (k === c[0] ? (dd > 0 ? ' ↑' : ' ↓') : '') + '</th>'; }).join('') + '</tr>' +
      rows.map(function (r) {
        return '<tr><td><a href="#radar?s=' + esc(r.symbol) + '"><b>' + esc(r.symbol) + '</b></a></td><td class="r">' + n(r.spot, 2) + '</td><td class="r">' + n(r.fut, 2) + '</td><td class="r ' + cls(r.basis) + '">' + sg(r.basis, 2) + '</td><td class="r ' + cls(r.basis_pct) + '">' + sg(r.basis_pct, 3) + '%</td><td class="r">' + sg(r.annual, 1) + '%</td><td class="r ' + cls(r.vs_carry) + '">' + sg(r.vs_carry, 2) + '%</td><td class="r">' + kfmt(r.oi) + '</td><td class="r ' + cls(r.doi) + '">' + sg(r.doi_pct, 1) + '%</td><td class="r ' + cls(r.fut_chg) + '">' + sg(r.fut_chg, 2) + '%</td><td><span style="color:' + BC[r.build] + ';font-weight:600">' + r.build + '</span></td><td><span class="pill ' + (r.bucket === 'Premium' ? 'up' : r.bucket === 'Discount' ? 'dn' : '') + '">' + r.bucket + '</span></td></tr>';
      }).join('');
  }
  $('scT').addEventListener('click', function (e) { var th = e.target.closest('th'); if (!th) return; var k = th.getAttribute('data-k'); basisSort = { k: k, d: basisSort.k === k ? -basisSort.d : 1 }; drawBasisTable(); });

  /* ---------------- RADAR ---------------- */
  var rChart = null, rSeries = null, rLines = [];
  function loadRadar(quiet) {
    var s = SYM.radar;
    if (!quiet) $('rHead').textContent = 'Reading ' + s + ' signals…';
    api('/api/pulse/radar?symbol=' + encodeURIComponent(s)).then(function (d) { err('rErr'); drawRadar(d); })
      .catch(function (e) { err('rErr', e); $('rHead').textContent = s + ': radar not available.'; });
  }
  function drawRadar(d) {
    $('rAsOf').textContent = d.as_of;
    var vc = d.verdict === 'Bullish' ? 'up' : d.verdict === 'Bearish' ? 'dn' : '';
    $('rHead').innerHTML = d.symbol + ' reads <em>' + d.verdict.toLowerCase() + '</em> (' + sg(d.score, 1) + ' on −10…+10)' + (d.expected ? ', expected ' + n(d.expected[0], 0) + ' – ' + n(d.expected[1], 0) + (d.expiry ? ' by ' + esc(d.expiry) : '') : '') + '.';
    $('rSpotL').textContent = d.symbol + ' last';
    MMShell.roll($('rSpot'), n(d.last, 2), 'rSpot' + d.symbol);
    $('rVwap').textContent = d.vwap ? 'VWAP ' + n(d.vwap, 2) : '';
    $('rExp').textContent = d.expected ? n(d.expected[0], 0) + ' – ' + n(d.expected[1], 0) : '—';
    $('rExpN').textContent = d.expiry ? 'expected by ' + d.expiry + (d.conviction ? ' · conviction ' + d.conviction + '%' : '') : 'from ATR (no option chain)';
    $('rConv').style.width = (d.conviction || 0) + '%';
    MMShell.roll($('rAtr'), d.atr ? '±' + n(d.atr, 1) : '—', 'rAtr' + d.symbol);
    $('rAtrN').textContent = d.atr_pct ? n(d.atr_pct, 2) + '% of price per day' : '';
    $('rVerd').className = 'pill ' + vc; $('rVerd').textContent = d.verdict.toLowerCase();
    // spider chart
    var ax = Object.keys(d.axes), N = ax.length, W = 340, H = 300, cx = W / 2, cy = H / 2 + 6, R = 105;
    function P(i, v) { var a = -Math.PI / 2 + i / N * 2 * Math.PI; return [cx + Math.cos(a) * R * v / 100, cy + Math.sin(a) * R * v / 100]; }
    var s = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" style="max-width:420px;display:block;margin:auto">';
    [25, 50, 75, 100].forEach(function (lv) { s += '<polygon points="' + ax.map(function (_, i) { return P(i, lv).join(','); }).join(' ') + '" fill="none" stroke="' + css('--tb-line2') + '"' + (lv === 50 ? ' stroke-dasharray="3 3"' : '') + '/>'; });
    ax.forEach(function (a, i) { var p = P(i, 100), q = P(i, 122); s += '<line x1="' + cx + '" y1="' + cy + '" x2="' + p[0] + '" y2="' + p[1] + '" stroke="' + css('--tb-line') + '"/><text x="' + q[0] + '" y="' + q[1] + '" font-size="11" text-anchor="middle" fill="' + css('--tb-mut') + '">' + esc(a) + '</text><text x="' + q[0] + '" y="' + (q[1] + 13) + '" font-size="11" font-weight="600" text-anchor="middle" fill="' + (d.axes[a] > 50 ? css('--tb-up') : d.axes[a] < 50 ? css('--tb-dn') : css('--tb-mut')) + '">' + d.axes[a] + '</text>'; });
    var col = d.score > 0 ? css('--tb-up') : d.score < 0 ? css('--tb-dn') : css('--tb-brand');
    s += '<polygon class="pop" style="transform-box:fill-box;transform-origin:center" points="' + ax.map(function (a, i) { return P(i, Math.max(4, d.axes[a])).join(','); }).join(' ') + '" fill="' + col + '" fill-opacity=".22" stroke="' + col + '" stroke-width="2"/></svg>';
    $('rRadar').innerHTML = s;
    $('rScore').innerHTML = '<div style="text-align:center">' + gauge(d.score, -10, 10, { w: 230, h: 126, lo: 'Bearish', hi: 'Bullish' }) + '<div class="big-n" style="margin-top:-6px" data-k="rScore' + d.symbol + '" data-roll="' + sg(d.score, 1) + '"></div><span class="pill ' + vc + '">' + d.verdict.toUpperCase() + '</span><div class="note">Composite of ' + N + ' signals, scaled −10 to +10</div></div>';
    MMShell.rollAll($('v-radar'));
    $('rSigs').innerHTML = ax.map(function (a) { var v = d.signals[a]; return '<div class="kv" style="border-top:1px solid var(--tb-line);margin-top:4px;padding-top:8px"><span><b>' + esc(a) + '</b><br><span class="mut" style="font-size:12px">' + esc(d.why[a] || '') + '</span></span><b class="' + cls(v) + '">' + sg(v, 1) + '</b></div>'; }).join('');
    if (!rChart) {
      rChart = LightweightCharts.createChart($('rChart'), { autoSize: true, timeScale: { timeVisible: true, secondsVisible: false }, localization: { timeFormatter: istTime } });
      rSeries = rChart.addCandlestickSeries({ upColor: '#16a34a', downColor: '#dc2626', borderVisible: false, wickUpColor: '#16a34a', wickDownColor: '#dc2626' });
    }
    rSeries.setData(d.candles.map(shiftT));
    rLines.forEach(function (l) { rSeries.removePriceLine(l); }); rLines = [];
    function pl(p, c, t, st) { if (p) rLines.push(rSeries.createPriceLine({ price: p, color: c, lineWidth: 1, lineStyle: st || 0, axisLabelVisible: true, title: t })); }
    if (d.levels) { pl(d.levels.support, '#16a34a', 'Support'); pl(d.levels.resistance, '#dc2626', 'Resistance'); pl(d.levels.max_pain, '#7c3aed', 'Max pain', 2); }
    pl(d.vwap, '#2563eb', 'VWAP', 2); if (d.profile) pl(d.profile.poc, '#d97706', 'POC', 1);
    rChart.timeScale().fitContent();
  }

  /* ---------------- CALM ---------------- */
  var cSeries = 0;
  $('cSer').addEventListener('click', function (e) { var b = e.target.closest('button'); if (!b) return; cSeries = +b.getAttribute('data-s'); this.querySelectorAll('button').forEach(function (x) { x.classList.toggle('on', x === b); }); loadCalm(); });
  function loadCalm() {
    api('/api/pulse/calm?series=' + cSeries).then(function (d) {
      err('cErr');
      var t = d.top.slice(0, 3).map(function (x) { return x.symbol; });
      $('cHead').innerHTML = 'Calmest F&amp;O stocks for ' + esc(d.expiry) + ' strangles (' + d.days + ' days): <em>' + t.join(', ') + '</em>';
      $('cFile').textContent = 'NSE volatility file ' + d.file_date; $('cDays').textContent = d.days + ' days to expiry'; $('cN').textContent = d.rows.length + ' stocks';
      $('cTop').innerHTML = d.top.map(function (x, i) { return '<div class="rank"><span class="n">' + (i + 1) + '</span><div><a class="s" style="color:var(--tb-fg);text-decoration:none" href="#options?s=' + x.symbol + '">' + esc(x.symbol) + '</a> <span class="mut" style="font-size:12px">calm ' + n(x.calm, 0) + '</span><small>Sell ' + n(x.put_k, 0) + ' PE / ' + n(x.call_k, 0) + ' CE (±1σ)</small><div class="sbar"><i class="grow-l" style="width:' + x.calm + '%;background:var(--tb-brand)"></i></div></div><span class="pill brand">' + n(x.avol, 0) + '% vol</span></div>'; }).join('');
      var W = 640, H = 300, xs = d.rows.map(function (r) { return r.avol; }), lo = Math.min.apply(null, xs), hi = Math.min(Math.max.apply(null, xs), 90);
      function X(v) { return 34 + (Math.min(v, hi) - lo) / ((hi - lo) || 1) * (W - 44); } function Y(v) { return H - 20 - v / 100 * (H - 30); }
      var s = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%"><rect x="34" y="' + Y(100) + '" width="' + (X(lo + (hi - lo) * .3) - 34) + '" height="' + (Y(75) - Y(100)) + '" rx="8" fill="' + css('--tb-up') + '" fill-opacity=".10"/><text x="42" y="' + (Y(100) + 16) + '" font-size="11" font-weight="600" fill="' + css('--tb-up') + '">CALM ZONE</text>';
      [0, 25, 50, 75, 100].forEach(function (v) { s += '<line x1="34" x2="' + W + '" y1="' + Y(v) + '" y2="' + Y(v) + '" stroke="' + css('--tb-line') + '"/><text x="28" y="' + (Y(v) + 3) + '" font-size="10" text-anchor="end" fill="' + css('--tb-mut2') + '">' + v + '</text>'; });
      d.rows.forEach(function (r, i) { var top = i < 8, lab = i < 3; s += '<circle class="pop" style="transform-box:fill-box;transform-origin:center;animation-delay:' + Math.min(800, i * 3) + 'ms" cx="' + X(r.avol).toFixed(1) + '" cy="' + Y(r.calm).toFixed(1) + '" r="' + (top ? 5 : 3.2) + '" fill="' + (top ? css('--tb-brand') : css('--tb-mut2')) + '" fill-opacity="' + (top ? .9 : .45) + '"><title>' + esc(r.symbol) + ' · calm ' + r.calm + ' · vol ' + n(r.avol, 1) + '%</title></circle>' + (lab ? '<text x="' + (X(r.avol) + 7) + '" y="' + (Y(r.calm) + 4) + '" font-size="10" fill="' + css('--tb-fg') + '">' + esc(r.symbol) + '</text>' : ''); });
      $('cMap').innerHTML = s + '<text x="34" y="' + (H - 4) + '" font-size="10" fill="' + css('--tb-mut2') + '">' + n(lo, 0) + '% vol</text><text x="' + W + '" y="' + (H - 4) + '" font-size="10" text-anchor="end" fill="' + css('--tb-mut2') + '">' + n(hi, 0) + '%+</text></svg>';
      $('cT').innerHTML = '<tr><th>#</th><th>Symbol</th><th class="r">Calm</th><th class="r">Annual vol</th><th class="r">Today vol (ann.)</th><th class="r">Last</th><th class="r">Today</th><th class="r">±1σ to expiry</th><th class="r">PE strike</th><th class="r">CE strike</th><th>Sector</th></tr>' +
        d.rows.map(function (r, i) { return '<tr><td class="mut2">' + (i + 1) + '</td><td><a href="#options?s=' + esc(r.symbol) + '"><b>' + esc(r.symbol) + '</b></a></td><td class="r"><b>' + n(r.calm, 1) + '</b></td><td class="r">' + n(r.avol, 1) + '%</td><td class="r">' + n(r.dvol * Math.sqrt(252), 1) + '%</td><td class="r">' + n(r.lp, 2) + '</td><td class="r ' + cls(r.chp) + '">' + sg(r.chp, 2) + '%</td><td class="r">±' + n(r.sd, 1) + '</td><td class="r up">' + n(r.put_k, 0) + '</td><td class="r dn">' + n(r.call_k, 0) + '</td><td class="mut">' + esc(r.sector || '') + '</td></tr>'; }).join('');
      $('cNote').textContent = d.note;
    }).catch(function (e) { err('cErr', e); $('cHead').textContent = 'Calm Zone not available right now.'; });
  }

  /* ---------------- JOURNAL (browser only) ---------------- */
  var JK = 'mm.journal.v1', MIST = ['FOMO entry', 'No stop', 'Moved stop', 'Oversized', 'Exited early', 'Held loser', 'Revenge trade', 'Against trend', 'News gamble'];
  function jget() { try { return JSON.parse(localStorage.getItem(JK) || '[]'); } catch (e) { return []; } }
  function jset(a) { try { localStorage.setItem(JK, JSON.stringify(a)); } catch (e) { alert('Could not save in this browser.'); } }
  function pnl(t) { if (t.exit === '' || t.exit === null || t.exit === undefined || isNaN(+t.exit)) return null; return ((+t.exit - +t.entry) * (t.side === 'Short' ? -1 : 1)) * +t.qty - (+t.fees || 0); }
  function rmult(t) { var p = pnl(t); if (p === null || !+t.stop) return null; var risk = Math.abs(+t.entry - +t.stop) * +t.qty; return risk ? p / risk : null; }
  var jInit = false;
  function loadJournal() {
    if (!jInit) {
      jInit = true;
      $('jMist').innerHTML = MIST.map(function (m) { return '<label><input type="checkbox" value="' + m + '">' + m + '</label>'; }).join('');
      $('jForm').date.value = new Date().toISOString().slice(0, 10);
      $('jAdd').onclick = function () {
        var f = $('jForm'); if (!f.reportValidity()) return;
        var t = { id: Date.now(), date: f.date.value, sym: f.sym.value.trim().toUpperCase(), side: f.side.value, qty: f.qty.value, entry: f.entry.value, exit: f.exit.value, stop: f.stop.value, fees: f.fees.value || 0,
                  setup: f.setup.value.trim(), mkt: f.mkt.value, notes: f.notes.value.trim(), mist: [].slice.call(document.querySelectorAll('#jMist input:checked')).map(function (x) { return x.value; }) };
        var a = jget(); a.push(t); jset(a);
        ['sym', 'qty', 'entry', 'exit', 'stop', 'notes'].forEach(function (k) { f[k].value = ''; }); document.querySelectorAll('#jMist input').forEach(function (x) { x.checked = false; });
        loadJournal();
      };
      $('jExp').onclick = function () {
        var a = jget(), keys = ['date', 'sym', 'side', 'qty', 'entry', 'exit', 'stop', 'fees', 'setup', 'mkt', 'mist', 'notes'];
        var csv = keys.join(',') + '\n' + a.map(function (t) { return keys.map(function (k) { var v = k === 'mist' ? (t.mist || []).join('|') : (t[k] === undefined ? '' : t[k]); return '"' + String(v).replace(/"/g, '""') + '"'; }).join(','); }).join('\n');
        var b = new Blob([csv], { type: 'text/csv' }), u = URL.createObjectURL(b), l = document.createElement('a'); l.href = u; l.download = 'marketminer-journal.csv'; l.click(); setTimeout(function () { URL.revokeObjectURL(u); }, 1000);
      };
      $('jImp').onchange = function () {
        var file = this.files[0]; if (!file) return;
        file.text().then(function (txt) {
          var lines = txt.split(/\r?\n/).filter(Boolean), head = parse(lines.shift()), a = jget();
          lines.forEach(function (ln, i) { var v = parse(ln), t = { id: Date.now() + i }; head.forEach(function (h, j) { t[h] = v[j]; }); t.mist = t.mist ? String(t.mist).split('|') : []; a.push(t); });
          jset(a); loadJournal();
        });
        function parse(s) { var out = [], cur = '', q = false; for (var i = 0; i < s.length; i++) { var c = s[i]; if (q) { if (c === '"' && s[i + 1] === '"') { cur += '"'; i++; } else if (c === '"') q = false; else cur += c; } else if (c === '"') q = true; else if (c === ',') { out.push(cur); cur = ''; } else cur += c; } out.push(cur); return out; }
      };
      $('jT').addEventListener('click', function (e) { var b = e.target.closest('[data-del]'); if (!b) return; if (!confirm('Delete this trade from the journal?')) return; jset(jget().filter(function (t) { return String(t.id) !== b.getAttribute('data-del'); })); loadJournal(); });
    }
    var a = jget().sort(function (x, y) { return x.date < y.date ? -1 : x.date > y.date ? 1 : x.id - y.id; });
    var closed = a.filter(function (t) { return pnl(t) !== null; }), wins = closed.filter(function (t) { return pnl(t) > 0; }), tot = closed.reduce(function (s, t) { return s + pnl(t); }, 0);
    var rs = closed.map(rmult).filter(function (x) { return x !== null; }), gw = wins.reduce(function (s, t) { return s + pnl(t); }, 0), gl = closed.filter(function (t) { return pnl(t) < 0; }).reduce(function (s, t) { return s + pnl(t); }, 0);
    $('jStats').innerHTML = [['Net P&L', '<span class="' + cls(tot) + '">' + (tot < 0 ? '−' : '') + '₹' + n(Math.abs(tot), 0) + '</span>', closed.length + ' closed · ' + (a.length - closed.length) + ' open'],
      ['Win rate', closed.length ? n(wins.length / closed.length * 100, 0) + '%' : '—', wins.length + ' wins'], ['Average R', rs.length ? sg(rs.reduce(function (s, x) { return s + x; }, 0) / rs.length, 2) + 'R' : '—', rs.length + ' trades with a stop'],
      ['Profit factor', gl ? n(gw / Math.abs(gl), 2) : '—', 'gross win ÷ gross loss']].map(function (k) { return '<div class="c"><div class="lbl">' + k[0] + '</div><div class="big-n">' + k[1] + '</div><div class="note">' + k[2] + '</div></div>'; }).join('');
    MMShell.stagger($('v-journal'));
    var eq = [0]; closed.forEach(function (t) { eq.push(eq[eq.length - 1] + pnl(t)); });
    $('jCurve').innerHTML = closed.length ? spark(eq, 600, 140, tot >= 0 ? css('--tb-up') : css('--tb-dn')) : '<div class="note">Add closed trades to see your equity curve.</div>';
    var by = {}; closed.forEach(function (t) { var k = t.setup || '(no setup)'; by[k] = by[k] || { n: 0, p: 0, w: 0 }; by[k].n++; by[k].p += pnl(t); if (pnl(t) > 0) by[k].w++; });
    var ms = {}; closed.forEach(function (t) { (t.mist || []).forEach(function (m) { ms[m] = ms[m] || { n: 0, p: 0 }; ms[m].n++; ms[m].p += pnl(t); }); });
    $('jBy').innerHTML = Object.keys(by).sort(function (x, y) { return by[y].p - by[x].p; }).map(function (k) { return '<div class="kv"><span>' + esc(k) + ' <span class="mut" style="font-size:12px">' + by[k].n + ' trades · ' + n(by[k].w / by[k].n * 100, 0) + '% win</span></span><b class="' + cls(by[k].p) + '">' + (by[k].p < 0 ? '−' : '') + '₹' + n(Math.abs(by[k].p), 0) + '</b></div>'; }).join('') +
      (Object.keys(ms).length ? '<div class="lbl" style="margin-top:12px">Cost of mistakes</div>' + Object.keys(ms).sort(function (x, y) { return ms[x].p - ms[y].p; }).map(function (k) { return '<div class="kv"><span>' + esc(k) + ' <span class="mut" style="font-size:12px">×' + ms[k].n + '</span></span><b class="' + cls(ms[k].p) + '">' + (ms[k].p < 0 ? '−' : '') + '₹' + n(Math.abs(ms[k].p), 0) + '</b></div>'; }).join('') : '') || '<div class="note">Nothing yet.</div>';
    $('jSetups').innerHTML = Object.keys(by).map(function (k) { return '<option value="' + esc(k) + '">'; }).join('');
    $('jT').innerHTML = '<tr><th>Date</th><th>Symbol</th><th>Side</th><th class="r">Qty</th><th class="r">Entry</th><th class="r">Exit</th><th class="r">P&amp;L</th><th class="r">R</th><th>Setup</th><th>Mistakes</th><th>Notes</th><th></th></tr>' +
      a.slice().reverse().map(function (t) { var p = pnl(t), r = rmult(t); return '<tr><td>' + esc(t.date) + '</td><td><b>' + esc(t.sym) + '</b></td><td>' + esc(t.side) + '</td><td class="r">' + esc(t.qty) + '</td><td class="r">' + esc(t.entry) + '</td><td class="r">' + esc(t.exit || 'open') + '</td><td class="r ' + cls(p) + '">' + (p === null ? '—' : (p < 0 ? '−' : '') + '₹' + n(Math.abs(p), 0)) + '</td><td class="r">' + (r === null ? '—' : sg(r, 2)) + '</td><td>' + esc(t.setup) + '</td><td class="mut">' + esc((t.mist || []).join(', ')) + '</td><td class="mut" style="white-space:normal;max-width:260px">' + esc(t.notes) + '</td><td><button class="btn2 ghost" style="padding:3px 9px" data-del="' + t.id + '">✕</button></td></tr>'; }).join('');
  }

  /* ---------------- ACADEMY ---------------- */
  var LESSONS = [
    ['Futures vs options', 'Start here', '<p>A <b>future</b> is an agreement to buy or sell the underlying at a fixed price on expiry. Profit and loss move one-for-one with the underlying, and both sides post margin.</p><p>An <b>option</b> gives the buyer the right, not the obligation, to buy (call) or sell (put) at the strike. The buyer pays a premium and can lose only that premium. The seller (writer) collects the premium and carries the open-ended risk, so brokers ask writers for margin.</p><div class="ex">Example: NIFTY at 24,000. A 24,200 CE costing ₹90 needs NIFTY above 24,290 at expiry to profit. The writer keeps the ₹90 if NIFTY stays below 24,200.</div><p>In MarketMiner: <a href="#options">Option Pulse</a> and <a href="#screeners">Futures basis</a>.</p>'],
    ['Open interest and buildups', 'OI Flow', '<p><b>Open interest (OI)</b> counts contracts still open. Volume counts trades; OI counts positions. Reading OI change together with price change tells you who is acting:</p><ul><li><b>Long buildup</b>: price up, OI up. New buyers.</li><li><b>Short buildup</b>: price down, OI up. New sellers.</li><li><b>Short covering</b>: price up, OI down. Sellers exiting.</li><li><b>Long unwinding</b>: price down, OI down. Buyers exiting.</li></ul><p>For options, heavy <b>put writing</b> below the price acts as a floor and heavy <b>call writing</b> above acts as a ceiling, because writers defend those strikes.</p><p>In MarketMiner: <a href="#oiflow">OI Flow</a>, <a href="#liquidity">Liquidity Map</a>.</p>'],
    ['PCR and max pain', 'Options', '<p><b>Put-call ratio (PCR)</b> = total put OI ÷ total call OI. Above about 1.2, put writers dominate (often read as support). Below about 0.8, call writers dominate (resistance). It is a positioning gauge, not a timing signal.</p><p><b>Max pain</b> is the strike where option buyers, in total, would lose the most at expiry. Price often drifts toward it in the last days of a quiet expiry, but trends can ignore it.</p><p>In MarketMiner: <a href="#options">Option Pulse</a>.</p>'],
    ['Option Greeks', 'Options', '<ul><li><b>Delta</b>: how much the premium moves for a 1-point move in the underlying. ATM options are near 0.5.</li><li><b>Theta</b>: premium lost per day from time decay. It speeds up close to expiry and hurts buyers, helps writers.</li><li><b>Vega</b>: premium change for a 1-point change in implied volatility.</li><li><b>Gamma</b>: how fast delta changes. High near expiry for ATM strikes, which is why expiry-day moves feel violent.</li></ul><div class="ex">A buyer needs the move to come quickly (fighting theta). A writer wants time to pass quietly (collecting theta) and fears gamma.</div>'],
    ['Implied vs realised volatility', 'Calm Zone', '<p><b>Implied volatility (IV)</b> is the move the option market is pricing in. <b>Realised volatility (RV)</b> is how much the stock actually moved. When IV is well above RV, options are rich and selling tends to have an edge; when IV is below RV, buying is cheaper.</p><p>A one-standard-deviation range to expiry is price × IV × √(days ÷ 365). About 68% of the time price stays inside it, if volatility holds.</p><p>In MarketMiner: <a href="#calm">Calm Zone</a>, <a href="#pulse">expected ranges on Pulse</a>.</p>'],
    ['Futures basis and carry', 'Screeners', '<p><b>Basis</b> = future price − spot price. Normally a future trades at a small premium that reflects interest (cost of carry). A <b>discount</b> often means short positioning or an upcoming dividend; an unusually rich premium can mean aggressive longs.</p><p>Fair carry ≈ spot × rate × days ÷ 365. MarketMiner uses 6.5% a year.</p><p>In MarketMiner: <a href="#screeners">Futures basis screener</a>.</p>'],
    ['Risk management', 'Most important', '<ul><li>Decide the stop before the entry, and size the trade from it: quantity = money you accept to lose ÷ (entry − stop).</li><li>Risk a small, fixed share of capital per trade (many traders use 0.5–2%).</li><li>Set a daily loss limit and stop trading when it is hit.</li><li>Prefer defined-risk option structures (spreads) over naked selling.</li><li>Keep a journal and review the mistakes that cost the most.</li></ul><p>In MarketMiner: <a href="#journal">Journal</a>, <a href="desk.html">Day Desk</a> sizing, <a href="agent.html">AI Agent</a> risk rules.</p>']
  ];
  var aInit = false;
  function loadAcademy() {
    if (aInit) return; aInit = true;
    $('aList').innerHTML = LESSONS.map(function (l, i) { return '<details class="lesson"' + (i === 0 ? ' open' : '') + '><summary>' + (i + 1) + '. ' + l[0] + '<small>' + l[1] + '</small></summary><div class="body">' + l[2] + '</div></details>'; }).join('');
  }

  /* ---------------- NEWS (publisher RSS through /api/proxy) ---------------- */
  var FEEDS = {
    in: [['Economic Times', 'https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms'], ['Mint', 'https://www.livemint.com/rss/markets'],
         ['Business Standard', 'https://www.business-standard.com/rss/markets-106.rss']],
    gl: [['CNBC', 'https://www.cnbc.com/id/100003114/device/rss/rss.html'], ['MarketWatch', 'https://feeds.content.dowjones.io/public/rss/mw_topstories'],
         ['Yahoo Finance', 'https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EGSPC,%5EDJI,%5EIXIC,%5ENSEI,CL%3DF,GC%3DF,DX-Y.NYB&region=US&lang=en-US'],
         ['BBC Business', 'https://feeds.bbci.co.uk/news/business/rss.xml'], ['FT', 'https://www.ft.com/markets?format=rss']],
    fx: [['FXStreet', 'https://www.fxstreet.com/rss/news'], ['CNBC Commodities', 'https://www.cnbc.com/id/20910258/device/rss/rss.html']]
  };
  var nCat = 'in', nItems = [];
  $('nSeg').addEventListener('click', function (e) { var b = e.target.closest('button'); if (!b) return; nCat = b.getAttribute('data-v'); this.querySelectorAll('button').forEach(function (x) { x.classList.toggle('on', x === b); }); loadNews(); });
  $('nFilter').addEventListener('input', drawNews);
  $('nRef').addEventListener('click', function () { loadNews(); });
  function strip(h) { var d = document.createElement('div'); d.innerHTML = h || ''; return (d.textContent || '').replace(/\s+/g, ' ').trim(); }
  function ago(t) { if (!t) return ''; var m = Math.round((Date.now() - t) / 60000); return m < 1 ? 'just now' : m < 60 ? m + 'm ago' : m < 1440 ? Math.round(m / 60) + 'h ago' : Math.round(m / 1440) + 'd ago'; }
  function loadNews() {
    var feeds = FEEDS[nCat], fails = [];
    $('nList').innerHTML = '<div class="loading">Loading headlines…</div>';
    Promise.all(feeds.map(function (f) {
      if (window.MM_MOBILE) {     // phone: no PC proxy, so the public rss2json service turns each feed into JSON
        return fetch('https://api.rss2json.com/v1/api.json?rss_url=' + encodeURIComponent(f[1])).then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }).then(function (j) {
          if (j.status !== 'ok') throw new Error(j.message || 'feed error');
          return (j.items || []).map(function (i) { return { src: f[0], title: strip(i.title), link: (i.link || '').trim(), t: Date.parse((i.pubDate || '').replace(' ', 'T') + 'Z') || 0, sum: strip(i.description).slice(0, 220) }; });
        }).catch(function () { fails.push(f[0]); return []; });
      }
      return fetch('/api/proxy?url=' + encodeURIComponent(f[1])).then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.text(); }).then(function (txt) {
        var x = new DOMParser().parseFromString(txt, 'text/xml'), out = [];
        x.querySelectorAll('item, entry').forEach(function (it) {
          var g = function (q) { var e = it.querySelector(q); return e ? e.textContent : ''; };
          var link = g('link') || ((it.querySelector('link') || {}).getAttribute ? it.querySelector('link').getAttribute('href') : '');
          var t = Date.parse(g('pubDate') || g('updated') || g('published')) || 0;
          if (g('title')) out.push({ src: f[0], title: strip(g('title')), link: link.trim(), t: t, sum: strip(g('description') || g('summary')).slice(0, 220) });
        });
        return out;
      }).catch(function () { fails.push(f[0]); return []; });
    })).then(function (lists) {
      var seen = {}; nItems = [].concat.apply([], lists).filter(function (i) { var k = i.title.toLowerCase(); if (seen[k]) return false; seen[k] = 1; return true; }).sort(function (a, b) { return b.t - a.t; });
      $('nAsOf').textContent = nItems.length + ' headlines';
      err('nErr', fails.length ? 'Not reachable right now: ' + fails.join(', ') : null);
      drawNews();
    });
  }
  function drawNews() {
    var f = ($('nFilter').value || '').toLowerCase();
    var list = nItems.filter(function (i) { return !f || (i.title + ' ' + i.sum).toLowerCase().indexOf(f) >= 0; }).slice(0, 60);
    $('nList').innerHTML = list.map(function (i) {
      var safe = /^https?:\/\//.test(i.link) ? i.link : '#';
      return '<a class="c news" href="' + esc(safe) + '" target="_blank" rel="noopener noreferrer"><div class="lbl"><span class="pill brand">' + esc(i.src) + '</span><span class="mut2">' + ago(i.t) + '</span></div><h3>' + esc(i.title) + '</h3>' + (i.sum ? '<p>' + esc(i.sum) + '</p>' : '') + '</a>';
    }).join('') || '<div class="loading">No headlines.</div>';
    MMShell.stagger($('nList'));
  }

  /* ---------------- "What am I looking at?" guides ---------------- */
  var HELP = {
    pulse: ['One-screen read of the whole F&O market.', ['<b>Market mood</b> blends breadth (stocks up vs down), estimated money flow, NIFTY\'s own move and the change in India VIX into a 0–100 score.', '<b>Index cards</b> show today\'s move and the range the option market expects by expiry (ATM IV ±1σ).', '<b>Money rotation</b> and the <b>sector map</b> show where turnover is going; box size is turnover, colour is % change.', '<b>India VIX</b> shows fear level and where today sits in the past year.']],
    news: ['Headlines from Indian and global market publishers.', ['Pick India, Global or FX & commodities, filter by a word, and open any headline on the publisher\'s site.']],
    options: ['How option writers are positioned for one underlying.', ['<b>Max pain</b>: the strike where option buyers lose most at expiry.', '<b>Put OI</b> (green) below price is support; <b>call OI</b> (red) above is resistance; purple is OI added today.', '<b>Writers\' range</b>: the floor and ceiling where writers added most today.', '<b>PCR</b> above ~1.2 leans put-heavy (supportive), below ~0.8 call-heavy.']],
    oiflow: ['Who is acting in the option chain today, strike by strike.', ['OI up + premium up = <b>long buildup</b>; OI up + premium down = <b>short buildup</b> (writing).', 'OI down + premium up = <b>short covering</b>; OI down + premium down = <b>long unwinding</b>.', 'Snapshots are kept every minute while this page is open, so you can see the floor and ceiling move during the day.']],
    liquidity: ['Where the big money sits around the current price.', ['Put and call <b>walls</b> (largest OI), <b>max pain</b>, the writers\' floor and ceiling and the IV ±1σ band are drawn on the chart.', '<b>VWAP</b> and the <b>volume profile</b> (POC and value area) show where most trading happened. Indices have no volume, so time at price is used.']],
    strength: ['Every F&O stock ranked by intraday strength.', ['Score = move vs NIFTY + where price sits in the day\'s range + candle body, weighted by turnover.', 'Bullish board: strongest names. Bearish board: weakest. Click a name to open its Radar.']],
    screeners: ['Scans across the whole F&O universe.', ['<b>Futures basis</b>: future vs spot; a discount often means short positioning or a dividend, a rich premium aggressive longs.', '<b>OI spurts</b>: biggest open-interest changes today.', '<b>RSI</b>: 15-minute, hourly and daily RSI side by side (runs on demand to save FYERS requests).']],
    radar: ['Six signals combined into one score for a symbol.', ['Futures OI, option chain, volume profile, RSI, price action and VWAP each score −2…+2; the total is scaled to −10…+10.', 'The radar chart shows which signals support longs (outside 50) or shorts (inside 50).']],
    calm: ['Stocks that have been moving least, for option selling.', ['Calm score ranks NSE\'s annualised volatility across F&O stocks, minus today\'s move and any volatility spike.', 'Suggested strikes are ±1 standard deviation to the chosen expiry. Always check results dates and news before selling.']],
    journal: ['Your private trade log, stored only in this browser.', ['Log entry, exit, stop and mistakes. You get win rate, average R, profit factor and what each mistake cost. Export to CSV to back it up.']],
    academy: ['Short lessons behind every chart in MarketMiner.', ['Each lesson links to the page where you can see the idea live.']]
  };
  function helpUI() {
    VIEWS.forEach(function (v) {
      var sec = $('v-' + v), k = sec && sec.querySelector('.kick'), h = HELP[v];
      if (!k || !h || k.querySelector('.helpbtn')) return;
      var b = document.createElement('button'); b.className = 'helpbtn'; b.textContent = 'What am I looking at?';
      var box = document.createElement('div'); box.className = 'helpbox';
      box.innerHTML = '<b>' + h[0] + '</b><ul>' + h[1].map(function (x) { return '<li>' + x + '</li>'; }).join('') + '</ul>';
      b.onclick = function () { box.classList.toggle('on'); };
      k.appendChild(b); k.parentNode.insertBefore(box, k.nextSibling.nextSibling);
    });
  }

  /* ---------------- boot ---------------- */
  icons();
  helpUI();
  window.addEventListener('hashchange', route);
  document.addEventListener('mm-theme', function () { if (cur) load(cur, true); });
  window.addEventListener('resize', function () { if (cur === 'pulse' && pulseData) drawTreemap(pulseData.sectors || []); });
  route();
})();
