/* MarketMiner Order Flow engine: order book ladder, bought / sold / pulled per price bucket, big buyers / sellers.
 *
 * Feed it book snapshots (book) and trades (trade); read it with view(). Pure maths, no network, so the same file runs
 * on the phone, on the PC and in tests (module.exports).
 *
 *   book({ t, bids: [[price, qty], ...] best first, asks: [[price, qty], ...] best first, ltp, tbq, tsq })
 *   trade({ t, price, qty, side })   side "buy" = a buyer took sellers' orders (ask side "bought"),
 *                                     side "sell" = a seller hit buyers' orders (bid side "sold")
 *
 * How "pulled" is decided: between two snapshots, a price level that is still inside the visible depth and has fewer
 * orders waiting lost them either to trades at that price (filled) or to cancellations (pulled). Trades at that price
 * since the last snapshot are counted as filled first; the rest is pulled. A level that only moved out of the visible
 * depth because the price moved is NOT counted as pulled. Levels seen earlier but now outside the visible depth are
 * kept as "last seen" (shown faded) until the price trades through them or they are 10 minutes old.
 */
(function (root) {
  "use strict";
  const MIN = 60e3;

  function create(opt) {
    const o = Object.assign({ step: 5, bigMult: 2.5, bigMin: 0, bigHoldMs: 15e3, memoryMs: 10 * MIN, keepMin: 375 }, opt || {});
    let prev = null;                 // last snapshot {bids: Map price->qty, asks: Map, lowBid, highAsk}
    let tradesAt = new Map();        // price -> {buy, sell} since the last snapshot
    const seen = { bid: new Map(), ask: new Map() };   // price -> {qty, t}  last known size for levels out of view
    const bins = new Map();          // minute -> Map(bucket -> {bought, sold, pulledB, pulledA, addB, addA})
    const bigSince = { bid: new Map(), ask: new Map() };   // bucket -> first time it qualified as big
    const S = { ltp: null, t: 0, tbq: null, tsq: null, bids: [], asks: [], updates: 0, trades: 0, last: null,
      tot: { bought: 0, sold: 0, pulledB: 0, pulledA: 0, addB: 0, addA: 0 } };

    const median = (a) => { if (!a.length) return 0; const s = [...a].sort((x, y) => x - y), i = s.length >> 1; return s.length % 2 ? s[i] : (s[i - 1] + s[i]) / 2; };
    const bucket = (p) => Math.floor(p / o.step + 1e-9) * o.step;
    function bin(t, p) {
      const m = Math.floor(t / MIN) * MIN;
      let b = bins.get(m); if (!b) { b = new Map(); bins.set(m, b); }
      const k = bucket(p); let x = b.get(k); if (!x) { x = { bought: 0, sold: 0, pulledB: 0, pulledA: 0, addB: 0, addA: 0 }; b.set(k, x); }
      return x;
    }
    function add(t, p, kind, q) { if (q > 0) { bin(t, p)[kind] += q; S.tot[kind] += q; } }

    function trade(x) {
      const p = +x.price, q = +x.qty; if (!(p > 0) || !(q > 0)) return;
      const t = x.t || Date.now(), side = x.side === "sell" ? "sell" : "buy";
      const h = tradesAt.get(p) || { buy: 0, sell: 0 }; h[side] += q; tradesAt.set(p, h);
      add(t, p, side === "buy" ? "bought" : "sold", q);
      S.trades++; S.ltp = p;
      // price traded through remembered levels: they are no longer "last seen"
      for (const [lp] of seen.bid) if (lp >= p) seen.bid.delete(lp);
      for (const [lp] of seen.ask) if (lp <= p) seen.ask.delete(lp);
    }

    // side "bid" levels are hit by sells, "ask" levels are lifted by buys
    function diffSide(side, before, after, edge, t) {
      const pulledK = side === "bid" ? "pulledB" : "pulledA", addK = side === "bid" ? "addB" : "addA", hit = side === "bid" ? "sell" : "buy";
      const inView = (p) => edge == null || (side === "bid" ? p >= edge - 1e-9 : p <= edge + 1e-9);
      for (const [p, q0] of before) {
        if (!inView(p)) continue;                       // only moved out of the visible depth
        const q1 = after.get(p) || 0, d = q0 - q1;
        if (d > 0) {
          const h = tradesAt.get(p), filled = h ? Math.min(d, h[hit]) : 0;
          if (h) h[hit] -= filled;
          add(t, p, pulledK, d - filled);
        } else if (d < 0) add(t, p, addK, -d);
      }
      for (const [p, q1] of after) if (!before.has(p) && prevCovers(side, p)) add(t, p, addK, q1);
    }
    function prevCovers(side, p) {
      if (!prev) return false;
      return side === "bid" ? p >= prev.lowBid - 1e-9 && p <= prev.highBid + 1e-9 : p <= prev.highAsk + 1e-9 && p >= prev.lowAsk - 1e-9;
    }

    function book(x) {
      const t = x.t || Date.now();
      const bids = new Map(), asks = new Map();
      (x.bids || []).forEach(([p, q]) => { if (p > 0 && q > 0) bids.set(+p, (bids.get(+p) || 0) + +q); });
      (x.asks || []).forEach(([p, q]) => { if (p > 0 && q > 0) asks.set(+p, (asks.get(+p) || 0) + +q); });
      if (!bids.size && !asks.size) return;
      const bp = [...bids.keys()], ap = [...asks.keys()];
      const cur = { bids, asks, lowBid: bp.length ? Math.min(...bp) : null, highBid: bp.length ? Math.max(...bp) : null,
        lowAsk: ap.length ? Math.min(...ap) : null, highAsk: ap.length ? Math.max(...ap) : null };
      if (prev) {
        // a full side shows only its top N levels: anything beyond the new edge simply left the view
        const full = x.levels || Math.max(bids.size, asks.size);
        diffSide("bid", prev.bids, bids, bids.size >= full ? cur.lowBid : null, t);
        diffSide("ask", prev.asks, asks, asks.size >= full ? cur.highAsk : null, t);
        for (const [p, q] of prev.bids) if (!bids.has(p) && cur.lowBid != null && p < cur.lowBid) seen.bid.set(p, { qty: q, t });
        for (const [p, q] of prev.asks) if (!asks.has(p) && cur.highAsk != null && p > cur.highAsk) seen.ask.set(p, { qty: q, t });
      }
      for (const p of bids.keys()) seen.bid.delete(p);
      for (const p of asks.keys()) seen.ask.delete(p);
      for (const m of [seen.bid, seen.ask]) for (const [p, v] of m) if (t - v.t > o.memoryMs) m.delete(p);
      // a remembered bid above the best bid (or ask below the best ask) is stale
      if (cur.highBid != null) for (const [p] of seen.bid) if (p > cur.highBid) seen.bid.delete(p);
      if (cur.lowAsk != null) for (const [p] of seen.ask) if (p < cur.lowAsk) seen.ask.delete(p);
      // typical bucket size, smoothed over a couple of minutes of snapshots (the yardstick for "big")
      const bs = new Map(); for (const [p, q] of bids) bs.set("b" + bucket(p), (bs.get("b" + bucket(p)) || 0) + q);
      for (const [p, q] of asks) bs.set("a" + bucket(p), (bs.get("a" + bucket(p)) || 0) + q);
      const md = median([...bs.values()]), dt = prev ? Math.min(1, (t - S.t) / 120e3) : 1;
      S.typical = S.typical == null ? md : S.typical + (md - S.typical) * Math.max(dt, 0.01);
      prev = cur; tradesAt = new Map();
      S.t = t; S.updates++; S.bids = [...bids]; S.asks = [...asks];
      if (x.ltp > 0) S.ltp = +x.ltp; else if (S.ltp == null && cur.highBid && cur.lowAsk) S.ltp = (cur.highBid + cur.lowAsk) / 2;
      S.tbq = x.tbq != null ? +x.tbq : null; S.tsq = x.tsq != null ? +x.tsq : null;
      const cut = Math.floor((t - o.keepMin * MIN) / MIN) * MIN; for (const m of bins.keys()) if (m < cut) bins.delete(m);
    }

    // windowMin: minutes of activity to sum (0 = everything kept); rows: buckets shown each side of the price
    function view(windowMin = 5, rows = 12, now = Date.now()) {
      const B = new Map();   // bucket -> row
      const row = (k) => { let r = B.get(k); if (!r) { r = { price: k, bid: 0, ask: 0, bidSeen: 0, askSeen: 0, bought: 0, sold: 0, pulledB: 0, pulledA: 0, addB: 0, addA: 0 }; B.set(k, r); } return r; };
      S.bids.forEach(([p, q]) => { row(bucket(p)).bid += q; });
      S.asks.forEach(([p, q]) => { row(bucket(p)).ask += q; });
      seen.bid.forEach((v, p) => { row(bucket(p)).bidSeen += v.qty; });
      seen.ask.forEach((v, p) => { row(bucket(p)).askSeen += v.qty; });
      for (const [m, b] of bins) if (!windowMin || m + MIN > now - windowMin * MIN) for (const [k, x] of b) {
        const r = row(k); for (const f in x) r[f] += x[f];
      }
      const ltp = S.ltp, mid = ltp != null ? bucket(ltp) : null;
      // big walls: waiting size well above the typical visible bucket, held for a while
      const sizes = [...B.values()].flatMap((r) => [r.bid, r.ask]).filter((x) => x > 0), med = S.typical || median(sizes);
      const thr = Math.max(o.bigMin, med * o.bigMult);
      for (const r of B.values()) {
        for (const side of ["bid", "ask"]) {
          const big = sizes.length >= 2 && r[side] >= thr && r[side] > 0, m = bigSince[side];
          if (big) { if (!m.has(r.price)) m.set(r.price, now); } else m.delete(r.price);
          r[side + "Big"] = big && now - m.get(r.price) >= o.bigHoldMs;
          r[side + "BigNew"] = big && !r[side + "Big"];
        }
      }
      for (const side of ["bid", "ask"]) for (const k of bigSince[side].keys()) if (!B.has(k)) bigSince[side].delete(k);
      let list = [...B.values()].sort((a, b) => b.price - a.price);
      if (mid != null) {
        const lo = mid - rows * o.step, hi = mid + rows * o.step;
        list = list.filter((r) => r.price >= lo - 1e-9 && r.price <= hi + 1e-9);
        for (let k = hi; k >= lo - 1e-9; k -= o.step) { const kk = +k.toFixed(8); if (!list.find((r) => Math.abs(r.price - kk) < 1e-9)) list.push(Object.assign(row(kk), {})); }
        list.sort((a, b) => b.price - a.price);
      }
      const bigB = [...B.values()].filter((r) => r.bidBig && (ltp == null || r.price <= ltp)).sort((a, b) => b.price - a.price)[0] || null;
      const bigA = [...B.values()].filter((r) => r.askBig && (ltp == null || r.price + o.step > ltp)).sort((a, b) => a.price - b.price)[0] || null;
      const vb = S.bids.reduce((s, x) => s + x[1], 0), va = S.asks.reduce((s, x) => s + x[1], 0);
      const tb = S.tbq != null && S.tsq != null && S.tbq + S.tsq > 0 ? S.tbq : vb, ta = S.tbq != null && S.tsq != null && S.tbq + S.tsq > 0 ? S.tsq : va;
      // how often waiting orders were pulled rather than traded (whole window, both sides)
      let fill = 0, pull = 0; for (const r of B.values()) { fill += r.bought + r.sold; pull += r.pulledA + r.pulledB; }
      return { ltp, mid, step: o.step, rows: list, bigBuy: bigB, bigSell: bigA, threshold: thr, median: med,
        strength: tb + ta > 0 ? tb / (tb + ta) : null, totals: { buy: tb, sell: ta, fromBook: S.tbq == null },
        pullShare: fill + pull > 0 ? pull / (fill + pull) : null, filled: fill, pulled: pull,
        visibleLevels: { bid: S.bids.length, ask: S.asks.length }, updates: S.updates, trades: S.trades, t: S.t };
    }

    function setStep(s) { o.step = s; }
    function reset() { prev = null; tradesAt = new Map(); seen.bid.clear(); seen.ask.clear(); bins.clear(); bigSince.bid.clear(); bigSince.ask.clear();
      Object.assign(S, { ltp: null, t: 0, bids: [], asks: [], updates: 0, trades: 0, typical: null }); for (const k in S.tot) S.tot[k] = 0; }
    return { book, trade, view, setStep, reset, opt: o, state: S, bucket };
  }

  // tick rule for feeds that only give the last price and cumulative volume (Angel One / FYERS):
  // traded at or above the previous best ask = buyers lifted; at or below the previous best bid = sellers hit
  function sideOf(price, bestBid, bestAsk, lastPrice, lastSide) {
    if (bestAsk && price >= bestAsk) return "buy";
    if (bestBid && price <= bestBid) return "sell";
    if (lastPrice != null && price > lastPrice) return "buy";
    if (lastPrice != null && price < lastPrice) return "sell";
    return lastSide || "buy";
  }

  const api = { create, sideOf };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.MMOF = api;
})(typeof window !== "undefined" ? window : globalThis);
