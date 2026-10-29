#!/bin/zsh
# Restart the NSE engine under caffeinate (the laptop must not idle-sleep through a session).
# Waits for a fresh 30 s TOTP window before the 5paisa login, then prints the health and the
# strategy snapshot. Settings come from backend/.env — nothing is hardcoded here.
#
#   scripts/restart-engine.sh            # stop the running engine (if any) and start it again
#
# Ask the owner before running this during market hours: a restart drops the feed for ~30 s.
set -u
B=${0:A:h}/../backend
B=${B:A}
LOG=$B/data-live/logs/session-$(date +%F).log
cd "$B" || exit 1
PID=$(cat data-live/engine.pid 2>/dev/null)
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
  kill -TERM "$PID"
  for i in $(seq 1 30); do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
fi
pkill -f 'caffeinate -dis .*kotsin-nse serve' 2>/dev/null
S=$(( $(date +%s) % 30 )); if [ $S -gt 20 ]; then sleep $((31 - S)); fi   # a fresh 30 s TOTP window
nohup /usr/bin/caffeinate -dis "$B/.venv/bin/kotsin-nse" serve >> "$LOG" 2>&1 &
sleep 2; echo "started pid $(cat data-live/engine.pid 2>/dev/null) log $LOG"
for i in $(seq 1 90); do
  sleep 2
  if curl -s --max-time 3 http://localhost:8500/api/health >/dev/null 2>&1; then break; fi
done
curl -s --max-time 5 http://localhost:8500/api/health | python3 -c "import sys,json; d=json.load(sys.stdin); print('health:', d.get('mode'), 'feed', d.get('feed',{}).get('connected'), 'universe', d.get('universe',{}).get('underlyings'), 'positional', d.get('positional'))"
curl -s --max-time 5 "http://localhost:8500/api/strategy?day=$(date +%F)" | python3 -c "import sys,json; d=json.load(sys.stdin); print('strategy:', {k: d.get(k) for k in ('day','rule','pdc','walls_round','max_move_adr','levels_hl','entry_cutoff','notional_inr')})"
pmset -g batt | tail -n 1
