/* MarketMiner: the market picker's lists, shared by Signal Lab, MM Trend and AuctionFlow (PC and phone).
 *
 *   India  -> FYERS            indices, futures, options ATM and 4 strikes either side (front expiry, rolls by itself:
 *                              NSE:NIFTY-ATMU2-CE = 2 strikes above ATM, NSE:NIFTY-ATMD3-PE = 3 below), stocks, MCX
 *   Global -> Delta Exchange India, one source for gold (XAUT, Tether Gold, tracks spot gold / XAUUSD), every crypto
 *             perpetual and the options on BTC and ETH (ATM and 4 strikes either side) and gold (ATM); nearest
 *             expiry, rolls by itself (DELTA:BTC-ATMU2-CE = 2 strikes above ATM)
 *
 *   MMK.GROUPS                 fixed groups (always there)
 *   await MMK.loadDelta()      adds "Gold & Crypto · all Delta perpetuals" and "Options · all Delta coins" from Delta's
 *                              live product list (new listings appear by themselves), and fills the option groups with
 *                              the real contracts (ATM and 4 strikes either side, nearest expiry) from the live option
 *                              chain: "NIFTY 25100 CE · 11 Nov". Returns the groups added / filled.
 *                              Until the chain answers (or if it cannot), the option groups hold rolling names.
 */
(function () {
  "use strict";
  const DL = "https://api.india.delta.exchange";
  const GROUPS = {
    "India · Indices": ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX", "NSE:FINNIFTY-INDEX", "NSE:MIDCPNIFTY-INDEX", "BSE:SENSEX-INDEX", "BSE:BANKEX-INDEX"],
    "India · Index futures": ["NSE:NIFTY-FUT", "NSE:BANKNIFTY-FUT", "NSE:FINNIFTY-FUT", "NSE:MIDCPNIFTY-FUT", "BSE:SENSEX-FUT"],
    "India · Stocks": ["NSE:RELIANCE-EQ", "NSE:HDFCBANK-EQ", "NSE:ICICIBANK-EQ", "NSE:SBIN-EQ", "NSE:INFY-EQ", "NSE:TCS-EQ"],
    "India · MCX": ["MCX:GOLD-FUT", "MCX:SILVER-FUT", "MCX:CRUDEOIL-FUT", "MCX:NATURALGAS-FUT"],
    "Gold & Crypto · Delta": ["DELTAF:XAUTUSD", "DELTAF:BTCUSD", "DELTAF:ETHUSD", "DELTAF:SOLUSD", "DELTAF:XRPUSD", "DELTAF:BNBUSD", "DELTAF:DOGEUSD"],
  };
  // India index options: ATM and 4 strikes either side, calls and puts, lowest strike first
  const OFFS = [-4, -3, -2, -1, 0, 1, 2, 3, 4];
  const strikes = (ex, und) => OFFS.flatMap((k) => { const a = k === 0 ? "ATM" : `ATM${k > 0 ? "U" : "D"}${Math.abs(k)}`; return [`${ex}:${und}-${a}-CE`, `${ex}:${und}-${a}-PE`]; });
  const OPT = {};
  const CHAINS = [["FNO", "NSE", "NIFTY"], ["FNO", "NSE", "BANKNIFTY"], ["FNO", "NSE", "FINNIFTY"], ["FNO", "BSE", "SENSEX"],
    ["DELTA", "DELTA", "BTC"], ["DELTA", "DELTA", "ETH"], ["DELTA", "DELTA", "XAUT"]];
  const gname = (ex, und) => (ex === "DELTA" ? `${und === "XAUT" ? "Gold" : und} options · Delta` : `India · ${und} options`);
  CHAINS.filter(([m]) => m === "FNO").forEach(([, ex, und]) => (OPT[gname(ex, und)] = strikes(ex, und)));
  const ord = Object.keys(GROUPS), at = ord.indexOf("India · Index futures");      // option groups right after the indices
  const G2 = {}; ord.slice(0, at).forEach((k) => (G2[k] = GROUPS[k])); Object.assign(G2, OPT); ord.slice(at).forEach((k) => (G2[k] = GROUPS[k]));
  Object.keys(GROUPS).forEach((k) => delete GROUPS[k]); Object.assign(GROUPS, G2);
  CHAINS.filter(([m]) => m === "DELTA").forEach(([, ex, und]) => (GROUPS[gname(ex, und)] = strikes(ex, und)));
  const LABELS = {};             // real contract -> "NIFTY 25100 CE · 11 Nov" (from the option chain)
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const shortDate = (e) => { const m = String(e || "").match(/^(\d{4})-(\d{2})-(\d{2})/); return m ? `${+m[3]} ${MON[+m[2] - 1]}` : String(e || ""); };
  const fmtK = (k) => Number(k).toLocaleString("en-IN", { maximumFractionDigits: 2 }).replace(/,/g, "");
  /** One underlying's chain -> its 18 real contracts around ATM (lowest strike first, CE then PE). */
  async function chainGroup([market, ex, und]) {
    const r = await fetch(`/api/desk/derivs/chain?market=${market}&underlying=${encodeURIComponent(market === "DELTA" ? und : ex + ":" + und)}`);
    const ch = await r.json(); if (!r.ok) throw new Error(ch.detail || r.statusText);
    const rows = (ch.strikes || []).filter((x) => x.CE || x.PE).sort((a, b) => a.strike - b.strike);
    if (!rows.length) throw new Error("empty chain");
    const ref = ch.atm != null ? ch.atm : ch.spot;
    const i = rows.reduce((bi, x, k) => (Math.abs(x.strike - ref) < Math.abs(rows[bi].strike - ref) ? k : bi), 0);
    const name = und === "XAUT" ? "Gold" : und, exp = shortDate(ch.expiry), out = [];
    rows.slice(Math.max(0, i - 4), i + 5).forEach((x) => ["CE", "PE"].forEach((side) => {
      if (!x[side]) return;
      out.push(x[side]); LABELS[x[side]] = `${name} ${fmtK(x.strike)} ${side}${exp ? " · " + exp : ""}`;
    }));
    return out;
  }
  const ALL_PERPS = "Gold & Crypto · all Delta perpetuals", ALL_OPTS = "Options · all Delta coins (ATM)";
  const NAMES = { XAUTUSD: "Gold (XAUT)", XAUT: "Gold (XAUT)" };
  /** Short readable name for a symbol: "NIFTY50 · NSE", "Gold (XAUT) · Delta", "BTC ATM call · Delta". */
  function label(s) {
    if (LABELS[s]) return LABELS[s];
    const [ex, rest = ""] = String(s).split(":");
    const src = { DELTAF: "Delta", DELTA: "Delta options", NSE: "NSE", BSE: "BSE", MCX: "MCX" }[ex] || ex;
    let r = NAMES[rest] || rest.replace(/-INDEX$/, "").replace(/-EQ$/, "");
    const am = r.match(/^(.+)-ATM(?:([UD])([1-9]))?-(CE|PE)$/);
    if (am) {                                // "NIFTY ATM+2 call (OTM) · NSE", "BTC ATM-1 put (OTM) · Delta options"
      const off = am[2] ? (am[2] === "U" ? 1 : -1) * +am[3] : 0, call = am[4] === "CE";
      const money = off === 0 ? "" : (off > 0) === call ? " (OTM)" : " (ITM)";
      return `${NAMES[am[1]] || am[1]} ATM${off > 0 ? "+" + off : off < 0 ? off : ""} ${am[4]}${money} · ${src}`;   // only until the live chain answers
    }
    r = r.replace(/-ATM-CE$/, " ATM call").replace(/-ATM-PE$/, " ATM put").replace(/-FUT$/, " fut");
    if (ex === "DELTA") { const c = rest.split("-")[0]; r = (NAMES[c] || c) + r.slice(c.length); }
    return `${r} · ${src}`;
  }
  let loading = null;
  function loadDelta() {
    return loading || (loading = (async () => {
      const out = {};
      try {
        const j = await (await fetch(`${DL}/v2/products?contract_types=perpetual_futures&states=live&page_size=500`)).json();
        const l = [...new Set((j.result || []).map((p) => p.symbol).filter((x) => /^[A-Z0-9]+USD$/.test(x)))];
        l.sort((a, b) => (a === "XAUTUSD" ? -1 : b === "XAUTUSD" ? 1 : a.localeCompare(b)));     // gold first
        if (l.length) out[ALL_PERPS] = l.map((x) => "DELTAF:" + x);
      } catch (e) { console.warn("Delta perpetuals", e); }
      try {
        const j = await (await fetch(`${DL}/v2/products?contract_types=call_options&states=live&page_size=1000`)).json();
        const coins = [...new Set((j.result || []).map((p) => (p.underlying_asset || {}).symbol || String(p.symbol || "").split("-")[1]).filter(Boolean))].sort();
        if (coins.length) out[ALL_OPTS] = coins.flatMap((c) => [`DELTA:${c}-ATM-CE`, `DELTA:${c}-ATM-PE`]);
      } catch (e) { console.warn("Delta options", e); }
      await Promise.all(CHAINS.map(async (c) => {           // real strikes around ATM from each live option chain
        try { const l = await chainGroup(c); if (l.length) out[gname(c[1], c[2])] = l; } catch (e) { console.warn("option chain", c[2], e.message || e); }
      }));
      Object.assign(GROUPS, out);
      return out;
    })());
  }
  window.MMK = { GROUPS, loadDelta, label, ALL_PERPS, ALL_OPTS };
})();
