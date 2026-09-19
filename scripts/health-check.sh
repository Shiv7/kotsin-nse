#!/usr/bin/env bash
# Read the engine's own health. Exits non-zero when it is degraded, so cron can alert on it.
#
# Deliberately does NOT restart anything. A probe that restarts on one failure is how a slow box
# becomes a database restart loop.
set -euo pipefail
PORT="${KN_API_PORT:-8500}"
URL="http://127.0.0.1:${PORT}/api/health"

if ! body=$(curl -sf --max-time 10 "$URL"); then
  echo "UNREACHABLE $URL"
  exit 2
fi

python3 - "$body" <<'PY'
import json, sys
h = json.loads(sys.argv[1])
feed = h.get("feed", {})
print(f"status={h['status']} mode={h['mode']} halted={h['halted']} "
      f"open={h['positions_open']} feed={'up' if feed.get('connected') else 'down'} "
      f"ticks={feed.get('ticks', 0)} silence={feed.get('silence_s')}")
for note in h.get("boot_notes", []):
    print(f"  note: {note}")
for c in h.get("checks", []):
    if not c["ok"]:
        print(f"  failing: {c['name']} x{c['consecutive_failures']} {c['detail']}")
sys.exit(0 if h["status"] == "ok" else 1)
PY
