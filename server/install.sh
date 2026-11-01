#!/usr/bin/env bash
# MarketMiner server bot installer: Android (Termux) or Ubuntu/Debian (cloud VM). PAPER TRADING ONLY.
#   curl -fsSL https://bnagraj0505.github.io/marketminer-mobile/server/install.sh | bash
set -euo pipefail
REPO=https://github.com/bnagraj0505/marketminer-mobile
DIR="$HOME/marketminer-mobile"
say(){ printf '\n\033[1;33m== %s\033[0m\n' "$*"; }
TERMUX=0; [ -n "${PREFIX:-}" ] && [ -d "${PREFIX}" ] && echo "$PREFIX" | grep -q com.termux && TERMUX=1

say "Installing packages"
if [ $TERMUX = 1 ]; then
  pkg update -y && pkg install -y python git termux-api || pkg install -y python git
else
  sudo apt-get update -y && sudo apt-get install -y python3 python3-venv git
fi

say "Getting MarketMiner"
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only; else git clone --depth 1 "$REPO" "$DIR"; fi
cd "$DIR/server"

if [ $TERMUX = 1 ]; then PY=python; pip install --upgrade httpx python-dotenv
else python3 -m venv .venv; .venv/bin/pip install --upgrade pip httpx python-dotenv; PY="$DIR/server/.venv/bin/python"; fi

if [ ! -f .env ]; then
  say "Settings (saved only on this device, in $DIR/server/.env)"
  exec 3</dev/tty
  read -r -u 3 -p "Telegram setup code (token|chatID) or bot token: " TOK
  CHAT="${TOK#*|}"; [ "$CHAT" = "$TOK" ] && read -r -u 3 -p "Telegram chat ID: " CHAT; TOK="${TOK%%|*}"
  read -r -u 3 -p "FYERS App ID for the bot (Enter to skip = crypto only): " FID
  FSEC=""; [ -n "$FID" ] && { read -r -s -u 3 -p "FYERS secret key (hidden): " FSEC; echo; }
  read -r -u 3 -p "Markets [SHARK:BTCUSDT,SHARK:ETHUSDT,NSE:NIFTY50-INDEX,NSE:NIFTYBANK-INDEX]: " SYMS
  read -r -u 3 -p "Timeframe [15m]: " TF
  umask 077
  cat > .env <<CFG
TELEGRAM_TOKEN=$TOK
TELEGRAM_CHAT=$CHAT
FYERS_APP_ID=$FID
FYERS_APP_SECRET=$FSEC
FYERS_REDIRECT_URI=https://bnagraj0505.github.io/marketminer-mobile/fyers.html
BOT_SYMBOLS=${SYMS:-SHARK:BTCUSDT,SHARK:ETHUSDT,NSE:NIFTY50-INDEX,NSE:NIFTYBANK-INDEX}
BOT_TF=${TF:-15m}
BOT_EVERY=60
BOT_WATCH_MIN=45
BOT_EXIT_OPPOSITE=1
CFG
fi

cat > run.sh <<RUN
#!/usr/bin/env bash
# keeps the bot running: restarts it 10 s after any crash
cd "$DIR/server"
while true; do "$PY" mmbot.py >> bot.log 2>&1; echo "restart \$(date)" >> bot.log; sleep 10; done
RUN
chmod +x run.sh

if [ $TERMUX = 1 ]; then
  say "Starting (Android)"
  mkdir -p ~/.termux/boot
  printf '#!/data/data/com.termux/files/usr/bin/sh\ntermux-wake-lock\nnohup %s >/dev/null 2>&1 &\n' "$DIR/server/run.sh" > ~/.termux/boot/start-mmbot.sh
  chmod +x ~/.termux/boot/start-mmbot.sh
  pkill -f "mmbot.py" 2>/dev/null || true; pkill -f "server/run.sh" 2>/dev/null || true
  termux-wake-lock 2>/dev/null || true
  nohup "$DIR/server/run.sh" >/dev/null 2>&1 &
  echo "Running. Log: $DIR/server/bot.log"
  echo "Auto-start after reboot needs the Termux:Boot app (F-Droid), opened once."
else
  say "Starting (systemd service 'mmbot')"
  sudo tee /etc/systemd/system/mmbot.service >/dev/null <<SVC
[Unit]
Description=MarketMiner paper bot
After=network-online.target
Wants=network-online.target
[Service]
User=$USER
WorkingDirectory=$DIR/server
ExecStart=$PY $DIR/server/mmbot.py
Restart=always
RestartSec=10
Environment=TZ=Asia/Kolkata
[Install]
WantedBy=multi-user.target
SVC
  sudo systemctl daemon-reload && sudo systemctl enable --now mmbot && sleep 3 && systemctl --no-pager status mmbot | head -5
  echo "Logs: journalctl -u mmbot -f"
fi
say "Done. Check Telegram for 'MarketMiner server bot started', then send /help."
