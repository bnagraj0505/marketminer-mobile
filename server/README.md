# MarketMiner server bot (always on, paper trading only)

Runs MarketMiner's Signal Lab engine (the PC's own `backend/` code) on a small Linux box, without a browser,
and reports to Telegram. It **never places real orders**.

Works on:
- **Android with Termux** (a spare phone kept on the charger and Wi-Fi)
- **Ubuntu/Debian cloud VM** (Oracle Cloud Always Free, Google Cloud e2-micro, any VPS)

## Install (one line)

Android: install **Termux** from **F-Droid** (the Play Store version is outdated), open it, then:

```
curl -fsSL https://bnagraj0505.github.io/marketminer-mobile/server/install.sh | bash
```

Cloud VM (Ubuntu): SSH in and run the same line.

It asks for:
- the Telegram **setup code** (`token|chatID`, from the MarketMiner Alerts chat)
- optionally a **FYERS App ID + secret** for Indian markets (skip = crypto only)
- markets and timeframe (Enter = defaults)

## Telegram commands (only your chat is obeyed)

| Command | What it does |
|---|---|
| `/status` | running state, markets, FYERS login, last scan |
| `/trades` | open paper trades |
| `/pnl` | closed-trade results after costs |
| `/pause` / `/resume` | stop / restart new entries and alerts (open trades are still followed) |
| `/symbols`, `/add SYM`, `/remove SYM` | e.g. `/add SHARK:SOLUSDT`, `/add NSE:SBIN-EQ`, `/add MCX:CRUDEOIL-FUT` |
| `/tf 15m` | timeframe |
| `/pin 1234` | renew FYERS for the day. The message is deleted and the PIN is never stored |
| `/login` then `/code <code>` | full FYERS login, needed every 15 days |

## FYERS

Use a FYERS API app whose redirect URL is `https://bnagraj0505.github.io/marketminer-mobile/fyers.html`.
A **separate app just for the bot** is best, so the bot's login doesn't replace the phone app's login.
`/login` sends you the FYERS sign-in link; after signing in, the Connections page shows a one-time code; send it as `/code …`.

## Android tips

- Settings → Apps → Termux → Battery → **Unrestricted** (otherwise Android stops it).
- Install **Termux:Boot** (F-Droid) and open it once so the bot starts after a reboot.
- Keep the phone on the charger and Wi-Fi.

## Update / logs

- Update: `~/marketminer-mobile/server/update.sh` (keeps your `.env` and paper ledger)
- Logs: Android `tail -f ~/marketminer-mobile/server/bot.log` · VM `journalctl -u mmbot -f`
