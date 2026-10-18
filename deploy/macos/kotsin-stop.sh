#!/bin/zsh
# Stop the kotsin-nse engine after the NSE close (launchd: com.shiv7.kotsin-nse.stop).
# SIGTERM is graceful: the engine flushes the day's archive and closes the ledger.
set -u
DATA="$HOME/Downloads/kotsin/kotsin-nse/backend/data-live"
LOG="$DATA/logs/session-$(date +%F).log"
PIDF="$DATA/engine.pid"
if [ ! -f "$PIDF" ]; then echo "$(date) stop: no pid file" >> "$LOG"; exit 0; fi
PID="$(cat "$PIDF")"
if kill -0 "$PID" 2>/dev/null; then
  kill -TERM "$PID"
  for i in $(seq 1 60); do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
  echo "$(date) stop: engine $PID stopped" >> "$LOG"
else
  echo "$(date) stop: engine $PID was not running" >> "$LOG"
fi
