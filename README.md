# MarketMiner Mobile

MarketMiner for iPhone, iPad and Android. It runs entirely in the browser (GitHub Pages), so it does **not** need the Windows PC.

Your FYERS app ID, secret key and login stay in the browser on each device. The app talks straight to FYERS, Binance, Delta Exchange and the news wires. Nothing passes through any server of ours.

## What's in it

| Page | What it shows | Data |
|---|---|---|
| **Dashboard** (`index.html`) | NIFTY/BANK/VIX and sector indices, MCX commodities, options-chain heatmap with OI change in each bar, option greeks and gamma-blast picks, OI support/resistance, OI change tracker, CPR and intraday range, confluence / liquidity sweeps / time decay, India and global news | FYERS, news via rss2json |
| **Crypto** (`crypto.html`) | Market view and strategy, Fear & Greed, perps and funding, TradFi perps, Delta options heatmap, OI change tracker, confluence / sweeps / time decay | Binance, Delta Exchange India, alternative.me |
| **Day Desk** (`desk.html`) | The 7-step paper-trading desk: scan, entry timer, exit plan, Time Machine, sizing, risk check, paper orders. Covers NSE, index F&O, MCX, Binance spot and futures, Delta perpetual futures (in place of Shark) and Delta options. Paper trades and limits are kept on each device | FYERS, Binance, Delta |
| **Signal Lab** (`lab.html`) | Chart with smart-money/ICT overlays, regime, 0–100 scored setups from 12 strategies with entry/stop/T1–T3 and size, option-chain and perpetual intelligence, 9-timeframe bias, backtests with costs and walk-forward checks, paper trades with capital protection, journal and alerts. Runs the PC's own Python code in the browser (Pyodide, about 10 MB on first open, then cached). Data, journal and paper trades stay on each device. US/global/forex symbols are PC-only (Yahoo blocks phone browsers) | FYERS, Delta, Binance |
| **Auto bot** (Signal Lab → Auto bot tab) | Paper-trades confirmed signals (score ≥ minimum, closed candle) on the markets you tick: crypto, Indian indices, F&O, stocks, MCX. Sends Telegram alerts for setups forming, entries, skipped entries, T1/T2 hits, exit warnings, opposite-signal exits and closes with P&L. Runs while the Signal Lab page is open and the screen is on | Signal Lab paper ledger, Telegram |
| **TradingView** (`tv.html`) | Full TradingView chart, plus **Open in TradingView** to use your own account and its Paper Trading panel. Shows the bot's open trades to mirror there (TradingView has no API for a bot to trade your paper account) | TradingView |
| **Connections** (`fyers.html`) | FYERS app details, login, daily PIN renewal, quotes and option-chain test | FYERS |

These stay on the PC version, because they need NSE's website, Moneycontrol or the PC server: pre-open, FII/DII, large deals, OI spurts, volume shockers, events, seasonal and broker picks, the screener and holdings tabs, Shark Exchange (the phone uses Delta Exchange perpetuals instead), and AI analysis.

## One-time setup

1. **Turn on GitHub Pages.** In this repo go to Settings → Pages, choose source "Deploy from a branch", branch `main`, folder `/ (root)`. After about a minute the app is at `https://<your-user>.github.io/<repo>/`.
2. **Point a FYERS app at it.** In the FYERS API dashboard, create a second app (keep the PC app as it is), or edit an app you don't use on the PC. Set its redirect URL to exactly
   `https://<your-user>.github.io/<repo>/fyers.html`
3. **On each device**, open `https://<your-user>.github.io/<repo>/fyers.html`:
   - Enter the App ID and secret key and tap Save. The redirect URL is filled in for you.
   - Tap **Connect FYERS** and log in.
4. **Add it to the Home Screen.**
   - iPhone/iPad: Safari → Share → Add to Home Screen.
   - Android: Chrome → ⋮ → Install app / Add to Home screen.

### iPhone / iPad login note

Sometimes the FYERS login returns to Safari instead of the Home Screen app. Safari can't see the app's saved settings, so it shows a one-time code instead. Copy the code, open MarketMiner from the Home Screen, go to Connections and tap **Paste login code**.

## Daily use

The FYERS login ends every day at 06:30 IST.

- Each morning, open Connections, type your FYERS PIN in **Quick renew** and tap Renew. No OTP or FYERS website is needed. The PIN is sent once to FYERS and never saved.
- Every 15 days, do one full **Connect FYERS** to get a fresh refresh token.

## Updating from the desktop version

The pages are generated from the desktop MarketMiner pages:

```
python tools/build_mobile.py C:\MarketMiner-FYERS\static
```

`js/engine.js` and `js/desk.js` stand in for the desktop Python server. It ports `backend/brokers/fyers/client.py`, `backend/markets/opt_lab.py`, `crypto_dash.py`, `crypto_lab.py`, `desk/engine.py`, `desk/routes.py` and `fyers_master.py` to JavaScript. The Day Desk maths gives the same entries, exits, sizes, risk checks and history replays as the PC. Time Machine odds differ by a point or so, because the browser uses a different random-number generator. If those Python files change, update `engine.js` to match.

Signal Lab is different: it is not a port. `tools/build_mobile.py` copies the PC's `backend/lab/*.py` into `py/backend/lab/`, and `js/lab.js` runs them in the browser with Pyodide. Small stand-ins in `py/` (`fastapi.py`, `httpx.py`, `backend/brokers/fyers/client.py`, `backend/desk/routes.py`, `backend/markets/crypto*.py`) route their data calls to the phone's FYERS, Delta and Binance clients through `py/_bridge.py`, and `py/mobile_api.py` answers `/api/lab/*`. Re-running the build after a change on the PC keeps both versions identical. On the phone the recorder runs only while the Signal Lab page is open, and long backtests briefly freeze the screen while they calculate.

Educational tool. Not investment advice. MarketMiner Mobile never places orders.
