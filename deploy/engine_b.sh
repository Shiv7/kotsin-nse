#!/usr/bin/env bash
# Engine B — phase35 as a second paper engine beside the live one (operator, 2026-10-03: "2 fully
# functional engines with all same except the 2 engines' core differences ... both running
# concurrently"). Engine A = kotsin-nse (phase34, :8500, com.kotsin.nse). Engine B = this folder
# (:8501, com.kotsin.nse.p35), its own data, database, wallets, logs and archive, the same 5paisa
# account (backend/.env is a symlink to A's — no second copy of the credentials).
#
#   engine_b.sh seed     snapshot A's state into B (database via sqlite .backup, daily/iv/scrip
#                        master caches, archive minus the tick tape, alerts) — refuses while B runs
#   engine_b.sh plist    write ~/Library/LaunchAgents/com.kotsin.nse.p35.plist (does NOT load it)
#   engine_b.sh start    launchctl bootstrap (the operator's yes first — it logs in to 5paisa)
#   engine_b.sh stop     launchctl bootout
#   engine_b.sh status   pid, ports, both engines' feed connection and reconnect counts
set -euo pipefail

A=/Users/devinakothari/Downloads/kotsincode/kotsin-nse/backend
B=/Users/devinakothari/Downloads/kotsincode/kotsin-nse-p35/backend
LABEL=com.kotsin.nse.p35
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT=8501
#: A logs in once 5paisa's TOTPLogin opens (~00:20 IST); B waits until this
LOGIN_NOT_BEFORE=00:45

b_running() { [ -f "$B/data/engine.pid" ] && kill -0 "$(cat "$B/data/engine.pid")" 2>/dev/null; }

case "${1:-}" in
  seed)
    if b_running; then echo "engine B is running (pid $(cat "$B/data/engine.pid")) — stop it first"; exit 1; fi
    mkdir -p "$B/data/archive" "$B/data/logs"
    # the database: an online, consistent copy while A runs (WAL included)
    rm -f "$B/data/kotsin_nse.db" "$B/data/kotsin_nse.db-wal" "$B/data/kotsin_nse.db-shm"
    sqlite3 "$A/data/kotsin_nse.db" ".backup '$B/data/kotsin_nse.db'"
    for f in charges.toml holidays.txt hotstocks-sectors.tsv decided.json; do
      [ -f "$A/data/$f" ] && cp -p "$A/data/$f" "$B/data/$f"
    done
    for d in daily iv scripmaster history alerts; do
      rm -rf "${B:?}/data/$d"; [ -d "$A/data/$d" ] && cp -Rp "$A/data/$d" "$B/data/$d"
    done
    # the archive B's research and OI reference read; the tick tape stays A's alone
    for k in bars oi micro option_quotes quotes quotes_held; do
      rm -rf "${B:?}/data/archive/$k"; [ -d "$A/data/archive/$k" ] && cp -Rp "$A/data/archive/$k" "$B/data/archive/$k"
    done
    [ -L "$B/.env" ] || ln -s "$A/.env" "$B/.env"
    echo "seeded B from A at $(date '+%Y-%m-%d %H:%M:%S'): $(du -sh "$B/data" | cut -f1)"
    sqlite3 "file:$B/data/kotsin_nse.db?immutable=1" "select 'positions open: ' || count(*) from positions where status='OPEN'"
    ;;
  plist)
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string><string>-i</string><string>-s</string>
    <string>$A/.venv/bin/python</string><string>-m</string><string>kotsin_nse.main</string><string>serve</string>
  </array>
  <key>WorkingDirectory</key><string>$B</string>
  <key>EnvironmentVariables</key>
  <dict>
    <!-- B's code ahead of A's editable install in the shared venv -->
    <key>PYTHONPATH</key><string>$B</string>
    <key>KN_API_PORT</key><string>$PORT</string>
    <key>KN_DATA_DIR</key><string>$B/data</string>
    <key>KN_DB_URL</key><string>sqlite+aiosqlite:///$B/data/kotsin_nse.db</string>
    <key>KN_FP_LOGIN_NOT_BEFORE_IST</key><string>$LOGIN_NOT_BEFORE</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$B/data/logs/launchd.out</string>
  <key>StandardErrorPath</key><string>$B/data/logs/launchd.out</string>
</dict>
</plist>
EOF
    plutil -lint "$PLIST"
    ;;
  start)
    [ -f "$PLIST" ] || { echo "no plist — run: $0 plist"; exit 1; }
    [ -f "$B/data/kotsin_nse.db" ] || { echo "B has no database — run: $0 seed"; exit 1; }
    [ -f "$B/../frontend/dist/index.html" ] || { echo "B has no built frontend/dist"; exit 1; }
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "started $LABEL on :$PORT"
    ;;
  stop)
    launchctl bootout "gui/$(id -u)/$LABEL" && echo "stopped $LABEL"
    ;;
  status)
    for e in "A 8500" "B $PORT"; do
      set -- $e
      curl -s -m 5 "http://127.0.0.1:$2/api/health" | python3 -c "
import json, sys
h = json.load(sys.stdin); f = h.get('feed') or {}
print('$1 :$2', 'mode', h.get('mode'), '| feed connected', f.get('connected'), 'reconnects', f.get('reconnects'),
      'silence_s', round(f.get('silence_s') or 0), '| rest calls', (h.get('rest') or {}).get('calls'), 'failures', (h.get('rest') or {}).get('failures'),
      '| open', h.get('positions_open'), '| booting', h.get('booting'), '| status', h.get('status'))" 2>/dev/null || echo "$1 :$2 not answering"
    done
    ;;
  *) sed -n 2,16p "$0"; exit 1 ;;
esac
