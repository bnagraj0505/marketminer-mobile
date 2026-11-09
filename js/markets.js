/* MarketMiner: the market picker's lists, shared by Signal Lab, MM Trend and AuctionFlow (PC and phone).
 *
 *   India  -> FYERS            indices, futures, ATM options (front expiry, rolls by itself), stocks, MCX
 *   Global -> Delta Exchange India, one source for gold (XAUT, Tether Gold, tracks spot gold / XAUUSD), every crypto
 *             perpetual and the options on BTC, ETH and gold (ATM call / put, nearest expiry, rolls by itself)
 *
 *   MMK.GROUPS                 fixed groups (always there)
 *   await MMK.loadDelta()      adds "Gold & Crypto · all Delta perpetuals" and "Options · all Delta coins" from Delta's
 *                              live product list (new listings appear by themselves); returns the groups added
 */
(function () {
  "use strict";
  const DL = "https://api.india.delta.exchange";
  const GROUPS = {
    "India · Indices": ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX", "NSE:FINNIFTY-INDEX", "NSE:MIDCPNIFTY-INDEX", "BSE:SENSEX-INDEX", "BSE:BANKEX-INDEX"],
    "India · Index options (ATM)": ["NSE:NIFTY-ATM-CE", "NSE:NIFTY-ATM-PE", "NSE:BANKNIFTY-ATM-CE", "NSE:BANKNIFTY-ATM-PE", "NSE:FINNIFTY-ATM-CE",
      "NSE:FINNIFTY-ATM-PE", "BSE:SENSEX-ATM-CE", "BSE:SENSEX-ATM-PE"],
    "India · Index futures": ["NSE:NIFTY-FUT", "NSE:BANKNIFTY-FUT", "NSE:FINNIFTY-FUT", "NSE:MIDCPNIFTY-FUT", "BSE:SENSEX-FUT"],
    "India · Stocks": ["NSE:RELIANCE-EQ", "NSE:HDFCBANK-EQ", "NSE:ICICIBANK-EQ", "NSE:SBIN-EQ", "NSE:INFY-EQ", "NSE:TCS-EQ"],
    "India · MCX": ["MCX:GOLD-FUT", "MCX:SILVER-FUT", "MCX:CRUDEOIL-FUT", "MCX:NATURALGAS-FUT"],
    "Gold & Crypto · Delta": ["DELTAF:XAUTUSD", "DELTAF:BTCUSD", "DELTAF:ETHUSD", "DELTAF:SOLUSD", "DELTAF:XRPUSD", "DELTAF:BNBUSD", "DELTAF:DOGEUSD"],
    "Options · Delta (ATM)": ["DELTA:BTC-ATM-CE", "DELTA:BTC-ATM-PE", "DELTA:ETH-ATM-CE", "DELTA:ETH-ATM-PE", "DELTA:XAUT-ATM-CE", "DELTA:XAUT-ATM-PE"],
  };
  const ALL_PERPS = "Gold & Crypto · all Delta perpetuals", ALL_OPTS = "Options · all Delta coins (ATM)";
  const NAMES = { XAUTUSD: "Gold (XAUT)", XAUT: "Gold (XAUT)" };
  /** Short readable name for a symbol: "NIFTY50 · NSE", "Gold (XAUT) · Delta", "BTC ATM call · Delta". */
  function label(s) {
    const [ex, rest = ""] = String(s).split(":");
    const src = { DELTAF: "Delta", DELTA: "Delta options", NSE: "NSE", BSE: "BSE", MCX: "MCX" }[ex] || ex;
    let r = NAMES[rest] || rest.replace(/-INDEX$/, "").replace(/-EQ$/, "");
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
      Object.assign(GROUPS, out);
      return out;
    })());
  }
  window.MMK = { GROUPS, loadDelta, label, ALL_PERPS, ALL_OPTS };
})();
