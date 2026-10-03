#!/bin/bash
# Deploy the feed hub to both engines (operator, 2026-10-04: "yes go ahead. build" / "go ahead finish all
# work"): phase 34 keeps the only 5paisa socket and serves it on 127.0.0.1:8510; phase 35 reads it.
#   bash deploy_feed_hub.sh
# Refuses with a position open in either engine, or with a work copy not at the reviewed commit.
# Rollback: phase 34 — deploy.sh's copy in kotsin-nse/deploy/rollbacks/ (printed below);
#           phase 35 — git -C kotsin-nse-p35 reset --hard 9c66bd3, drop "feed_hub" from its engine.json,
#           launchctl kickstart -k gui/$(id -u)/com.kotsin.nse.p35
set -euo pipefail
SP=/private/tmp/claude-501/-Users-devinakothari-Downloads-kotsincode/5405d416-d2e8-4d9c-a2ea-3ee96d86d293/scratchpad
L=/Users/devinakothari/Downloads/kotsincode/kotsin-nse
B=/Users/devinakothari/Downloads/kotsincode/kotsin-nse-p35
A_WORK=$SP/phase34n_work
B_WORK=$SP/phase35b_work
A_COMMIT=149b910
B_COMMIT=7f2a208
HUB=127.0.0.1:8510

say() { printf '\n== %s\n' "$*"; }
health() { curl -s -m 5 "http://127.0.0.1:$1/api/health"; }

say "preflight"
[ "$(git -C "$A_WORK" rev-parse --short HEAD)" = "$A_COMMIT" ] || { echo "phase34n_work is not at $A_COMMIT"; exit 1; }
[ "$(git -C "$B_WORK" rev-parse --short HEAD)" = "$B_COMMIT" ] || { echo "phase35b_work is not at $B_COMMIT"; exit 1; }
[ -z "$(git -C "$B" status --porcelain --untracked-files=no)" ] || { echo "kotsin-nse-p35 has uncommitted changes"; exit 1; }
for p in 8500 8501; do
  open=$(health $p | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin).get("positions_open", "?"))' 2>/dev/null || echo "?")
  [ "$open" = "0" ] || { echo ":$p positions_open=$open — not deploying"; exit 1; }
done
if lsof -nP -iTCP:8510 -sTCP:LISTEN >/dev/null 2>&1; then echo "port 8510 is taken"; exit 1; fi
echo "ok: work copies at $A_COMMIT / $B_COMMIT, no position open, port 8510 free"

say "engine.json: phase 34 serves $HUB, phase 35 reads it"
/usr/bin/python3 - "$L/backend/data/engine.json" "$B/backend/data/engine.json" "$HUB" <<'EOF'
import json, sys
a, b, hub = sys.argv[1:4]
for path, cfg in ((a, {"serve": hub}), (b, {"connect": hub})):
    d = json.load(open(path))
    d["feed_hub"] = cfg
    open(path, "w").write(json.dumps(d) + "\n")
    print(path, "->", json.dumps(d))
EOF

say "phase 34: deploy.sh (tests in the live folder, one restart)"
bash "$L/deploy/deploy.sh" "$A_WORK"
for _ in $(seq 1 40); do
  health 8500 | /usr/bin/python3 -c 'import json,sys; h=json.load(sys.stdin); sys.exit(0 if (h.get("feed") or {}).get("hub") else 1)' 2>/dev/null && break
  sleep 3
done
health 8500 | /usr/bin/python3 -c 'import json,sys; h=json.load(sys.stdin); print("phase 34 hub:", (h.get("feed") or {}).get("hub"))'

say "phase 34's folder: git to the deployed commit (the files on disk already are it)"
git -C "$L" fetch -q "$A_WORK" phase34-names
for f in $(git -C "$L" diff --name-only --diff-filter=A HEAD FETCH_HEAD); do
  git -C "$L" show "FETCH_HEAD:$f" | cmp -s - "$L/$f" || { echo "new file differs from the commit: $f — git left as is"; exit 1; }
done
[ -z "$(git -C "$L" diff FETCH_HEAD --diff-filter=M --name-only)" ] || { echo "a tracked file differs from the commit — git left as is"; exit 1; }
git -C "$L" merge-base --is-ancestor HEAD FETCH_HEAD && git -C "$L" reset -q FETCH_HEAD
git -C "$L" log --oneline -1

say "phase 35: code to $B_COMMIT, restart"
git -C "$B" fetch -q "$B_WORK" phase35-engine-b
git -C "$B" merge -q --ff-only FETCH_HEAD
git -C "$B" log --oneline -1
launchctl kickstart -k "gui/$(id -u)/com.kotsin.nse.p35"
for _ in $(seq 1 40); do health 8501 >/dev/null 2>&1 && break; sleep 3; done

say "status (phase 35 holds its broker login until 00:45 IST; its feed starts after its boot)"
sleep 10
bash "$B/deploy/engine_b.sh" status
for p in 8500 8501; do
  health $p | /usr/bin/python3 -c "import json,sys; h=json.load(sys.stdin); print(':$p feed.hub', (h.get('feed') or {}).get('hub'))"
done
echo "done"
