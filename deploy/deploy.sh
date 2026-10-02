#!/bin/bash
# Deploy a reviewed copy of kotsin-nse into the live folder, then restart the engine once.
# Lives in kotsin-nse/deploy/ (operator, 2026-10-01): the session scratchpad under /private/tmp lost
# files on 1 Oct, so the script and its rollback copies are kept with the project.
#   bash deploy.sh approved_build   # the build approved on 2026-09-25 night
#   bash deploy.sh phase3_work      # that build + the 2026-09-26 additions (cards, fade reason, labels, 09:45 volume fix, A/B page)
# Refuses with a position open. Backs up every file it replaces. If the live test suite fails after
# the copy, it restores the backup and exits WITHOUT restarting.
set -euo pipefail
SP=/private/tmp/claude-501/-Users-devinakothari-Downloads-kotsincode/5405d416-d2e8-4d9c-a2ea-3ee96d86d293/scratchpad
ARG="${1:?usage: deploy.sh <work copy: a name under the scratchpad, or an absolute path>}"
case "$ARG" in /*) SRC="$ARG" ;; *) SRC="$SP/$ARG" ;; esac
L=/Users/devinakothari/Downloads/kotsincode/kotsin-nse
RBDIR="$L/deploy/rollbacks"   # one copy per deploy; the newest KEEP_ROLLBACKS are kept
KEEP_ROLLBACKS=10
RB="$RBDIR/$(date +%Y%m%d-%H%M%S)"
[ -d "$SRC/backend/kotsin_nse" ] || { echo "no such build: $SRC"; exit 1; }

open=$(curl -s -m 5 http://127.0.0.1:8500/api/health | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin).get("positions_open", "?"))' 2>/dev/null || echo "?")
[ "$open" = "0" ] || { echo "positions_open=$open — not deploying"; exit 1; }

changed=()
while IFS= read -r f; do
  if [ ! -f "$L/$f" ] || ! cmp -s "$SRC/$f" "$L/$f"; then changed+=("$f"); fi
done < <(cd "$SRC" && find backend/kotsin_nse backend/tests frontend/src docs -type f \( -name '*.py' -o -name '*.ts' -o -name '*.tsx' -o -name '*.css' -o -name '*.md' \) -not -path '*/__pycache__/*' | sort)
echo "files to deploy: ${#changed[@]}"; printf '  %s\n' "${changed[@]}"

mkdir -p "$RB"
# keep the newest KEEP_ROLLBACKS (this one included); older copies are removed
ls -1d "$RBDIR"/*/ 2>/dev/null | sort -r | tail -n +$((KEEP_ROLLBACKS + 1)) | while IFS= read -r old; do rm -rf "$old"; done
for f in "${changed[@]}"; do
  if [ -f "$L/$f" ]; then mkdir -p "$RB/$(dirname "$f")"; cp -p "$L/$f" "$RB/$f"; else echo "$f" >> "$RB/NEW_FILES.txt"; fi
done
cp -Rp "$L/frontend/dist" "$RB/frontend_dist"

restore() {
  echo "restoring from $RB"
  for f in "${changed[@]}"; do
    if [ -f "$RB/$f" ]; then cp -p "$RB/$f" "$L/$f"; else rm -f "$L/$f"; fi
  done
  rm -rf "$L/frontend/dist"; cp -Rp "$RB/frontend_dist" "$L/frontend/dist"
}

for f in "${changed[@]}"; do mkdir -p "$L/$(dirname "$f")"; cp -p "$SRC/$f" "$L/$f"; done
rm -rf "$L/frontend/dist.new"; cp -Rp "$SRC/frontend/dist" "$L/frontend/dist.new"
rm -rf "$L/frontend/dist"; mv "$L/frontend/dist.new" "$L/frontend/dist"

cd "$L/backend"
if ! .venv/bin/python -m pytest -q -p no:cacheprovider > "$RB/pytest.txt" 2>&1; then
  tail -5 "$RB/pytest.txt"; restore; echo "tests failed — live folder restored, engine NOT restarted"; exit 1
fi
tail -1 "$RB/pytest.txt"
.venv/bin/ruff check kotsin_nse tests | tail -1

launchctl kickstart -k "gui/$(id -u)/com.kotsin.nse"
echo "engine restarted; rollback copy: $RB"
for i in $(seq 1 60); do
  sleep 5
  if curl -s -m 3 http://127.0.0.1:8500/api/health > /dev/null; then echo "web up after $((i * 5)) s"; break; fi
done
