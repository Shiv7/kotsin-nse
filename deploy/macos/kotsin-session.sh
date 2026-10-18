#!/bin/zsh
# Start the kotsin-nse engine for the trading day (launchd: com.shiv7.kotsin-nse.session).
# Weekdays only; the engine itself knows NSE holidays and says so in its boot notes.
# caffeinate keeps the Mac awake while it runs (-s needs AC power; on battery it can still sleep).
set -u
ROOT="$HOME/Downloads/kotsin/kotsin-nse/backend"
DATA="$ROOT/data-live"
mkdir -p "$DATA/logs"
LOG="$DATA/logs/session-$(date +%F).log"
if [ "$(date +%u)" -ge 6 ]; then echo "$(date) weekend - not starting" >> "$LOG"; exit 0; fi
if [ -f "$DATA/engine.pid" ] && kill -0 "$(cat "$DATA/engine.pid")" 2>/dev/null; then
  echo "$(date) already running (pid $(cat "$DATA/engine.pid"))" >> "$LOG"; exit 0
fi
cd "$ROOT" || exit 1
echo "$(date) starting kotsin-nse (branch $(git -C "$ROOT/.." branch --show-current))" >> "$LOG"
exec /usr/bin/caffeinate -dis "$HOME/.local/bin/uv" run kotsin-nse serve >> "$LOG" 2>&1
