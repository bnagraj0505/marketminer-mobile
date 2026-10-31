# MarketMiner Mobile

MarketMiner for iPhone, iPad and Android. It runs entirely in the browser (GitHub Pages), so it does **not** need the Windows PC.

Your FYERS app ID, secret key and login stay in the browser on each device. The app talks straight to FYERS, Binance, Delta Exchange and the news wires. Nothing passes through any server of ours.

## What's in it

| Page | What it shows | Data |
|---|---|---|
| **Dashboard** (`index.html`) | NIFTY/BANK/VIX and sector indices, MCX commodities, options-chain heatmap with OI change in each bar, option greeks and gamma-blast picks, OI support/resistance, OI change tracker, CPR and intraday range, confluence / liquidity sweeps / time decay, India and global news | FYERS, news via rss2json |
| **Crypto** (`crypto.html`) | Market view and strategy, Fear & Greed, perps and funding, TradFi perps, Delta options heatmap, OI change tracker, confluence / sweeps / time decay | Binance, Delta Exchange India, alternative.me |
| **Day Desk** (`desk.html`) | The 7-step paper-trading desk: scan, entry timer, exit plan, Time Machine, sizing, risk check, paper orders. Covers NSE, index F&O, MCX, Binance spot and futures, and Delta options. Paper trades and limits are kept on each device | FYERS, Binance, Delta |
| **Connections** (`fyers.html`) | FYERS app details, login, daily PIN renewal, quotes and option-chain test | FYERS |

These stay on the PC version, because they need NSE's website, Moneycontrol or the PC server: pre-open, FII/DII, large deals, OI spurts, volume shockers, events, seasonal and broker picks, the screener and holdings tabs, Shark Exchange, and AI analysis.

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

Educational tool. Not investment advice. MarketMiner Mobile never places orders.
