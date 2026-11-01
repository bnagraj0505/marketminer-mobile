#!/usr/bin/env bash
# Pull the latest MarketMiner bot and restart it. Your .env and paper ledger (.lab/) are kept.
set -e; cd "$(dirname "$0")"; git -C .. pull --ff-only
if command -v systemctl >/dev/null && systemctl list-unit-files | grep -q mmbot; then sudo systemctl restart mmbot
else pkill -f mmbot.py || true; fi      # Termux: run.sh restarts it in 10 s
echo "Updated."
