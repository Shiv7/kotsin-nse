#!/usr/bin/env bash
# Pull the server's recorded session (archives, strategy day files, official OI, logs) to the laptop for the
# research, into backend/data-live-aws/ — never over the laptop's own data-live.
#
#   deploy/aws/sync-archives.sh ubuntu@<elastic-ip>
set -euo pipefail
HOST="${1:?usage: sync-archives.sh user@host}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="$ROOT/backend/data-live-aws"
mkdir -p "$DEST"
for d in archive strategy oi_daily logs; do
  rsync -az --partial "$HOST:kotsin-nse/backend/data-live/$d/" "$DEST/$d/"
done
du -sh "$DEST"/* 2>/dev/null
