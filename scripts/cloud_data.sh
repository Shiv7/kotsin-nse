#!/usr/bin/env bash
# SessionStart hook (.claude/settings.json): in a Claude Code CLOUD session only, download and unpack
# the research data into backend/data-live/ once. Does nothing on the laptop.
#
# Needs, in the cloud environment's variables (not secrets — public market data, an expiring link):
#   KOTSIN_DATA_URL     a direct download link to kotsin-research-data.tar (scripts/pack_research_data.sh)
#                       — Google Drive: https://drive.usercontent.google.com/download?id=<FILE_ID>&export=download&confirm=t
#   KOTSIN_DATA_SHA256  optional: the archive's sha256, checked before unpacking
# Progress: /tmp/kotsin_data.log. Done when backend/data-live/.ready exists.
set -uo pipefail
[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0
root="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
dest="$root/backend"
log=/tmp/kotsin_data.log
[ -f "$dest/data-live/.ready" ] && exit 0
if [ -z "${KOTSIN_DATA_URL:-}" ]; then
  echo "KOTSIN_DATA_URL is not set in the cloud environment: no research data" >>"$log"
  exit 0
fi
{
  echo "$(date -u +%FT%TZ) downloading"
  tmp=/tmp/kotsin-research-data.tar
  if ! curl -fL --retry 3 -o "$tmp" "$KOTSIN_DATA_URL"; then echo "download failed"; exit 0; fi
  if [ -n "${KOTSIN_DATA_SHA256:-}" ]; then
    got=$(sha256sum "$tmp" | cut -d' ' -f1)
    if [ "$got" != "$KOTSIN_DATA_SHA256" ]; then echo "sha256 mismatch: $got"; exit 0; fi
  fi
  tar -xf "$tmp" -C "$dest" && rm -f "$tmp" && touch "$dest/data-live/.ready"
  echo "$(date -u +%FT%TZ) ready: $(du -sh "$dest/data-live" | cut -f1)"
} >>"$log" 2>&1
exit 0
