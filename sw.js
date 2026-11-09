// MarketMiner Mobile: network first, cached copy when offline. Market data is never cached here.
const CACHE = 'mm-mobile-v24', ENGINE = 'mm-engine-pyodide-0.26.4';   // engine files never change for a version: kept across app updates
const SHELL = ['./', './index.html', './crypto.html', './fyers.html', './js/engine.js', './desk.html', './js/desk.js', './lab.html', './js/lab.js', './js/labworker.js', './js/bot.js', './js/position.js', './tv.html', './trend.html', './js/mmtrend.js', './intel.html', './js/smartentry.js', './perf.html', './js/auction.js', './js/markets.js', './js/chartctl.js', './js/srzones.js', './js/hider.js', './js/retrace.js', './js/straddle.js', './js/pbook.js', './agent.html', './auction.html', './js/diagnose.js', './js/mlbench.js', './js/bookings.js', './manifest.webmanifest', './icons/icon-192.png'];
self.addEventListener('install', e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {})); self.skipWaiting(); });
self.addEventListener('activate', e => { e.waitUntil(caches.keys().then(k => Promise.all(k.filter(x => x !== CACHE && x !== ENGINE).map(x => caches.delete(x))))); self.clients.claim(); });
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method === 'GET' && u.host === 'cdn.jsdelivr.net' && u.pathname.startsWith('/pyodide/v0.26.4/')) {   // analysis engine: saved on this device after the first download
    e.respondWith(caches.open(ENGINE).then(c => c.match(e.request).then(h => h || fetch(e.request).then(r => { if (r.ok || r.type === 'opaque') c.put(e.request, r.clone()); return r; }))));
    return;
  }
  if (e.request.method !== 'GET' || u.origin !== self.location.origin) return;
  // newest version from the network, but if the network is slow (over 1.5 s) the copy saved on this device is shown
  // at once and the network answer still updates the saved copy for next time
  const net = fetch(e.request, { cache: 'no-cache' }).then(r => { if (r.ok) { const c = r.clone(); caches.open(CACHE).then(x => x.put(e.request, c)); } return r; });
  const slow = new Promise(res => setTimeout(res, 1500)).then(() => caches.match(e.request)).then(h => h || net);
  e.respondWith(Promise.race([net, slow]).catch(() => caches.match(e.request).then(h => h || caches.match('./index.html'))));
});
