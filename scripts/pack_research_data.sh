#!/usr/bin/env bash
# Pack the research data (git-ignored) into one archive for Claude Code cloud sessions.
#
#   scripts/pack_research_data.sh [out-dir]      # default: ~/Downloads
#
# Takes from backend/data-live/: price history, NSE bhavcopies (+ derived caches), scrip masters,
# recorded sessions, past research runs, holiday / symbol-change / universe lists. Leaves out the
# ledger database, logs, the engine's pid and anything secret (.env lives outside data-live anyway).
# A plain tar: the Parquet files are already compressed, and every machine can untar it.
set -euo pipefail
cd "$(dirname "$0")/../backend"
out="${1:-$HOME/Downloads}"
name="kotsin-research-data.tar"
items=(history oi_daily oi_daily_legacy scripmaster archive obtests iv daily
       holidays.txt holidays-mcx.txt nse_symbolchange.csv universe-nifty50.txt universe-reliance.txt)
present=()
for i in "${items[@]}"; do [ -e "data-live/$i" ] && present+=("data-live/$i"); done
tar -cf "$out/$name" --exclude='*.db*' --exclude='*.pid' --exclude='logs' "${present[@]}"
size=$(du -h "$out/$name" | cut -f1)
sum=$(shasum -a 256 "$out/$name" | cut -d' ' -f1)
echo "wrote $out/$name ($size)"
echo "sha256 $sum"
echo "Upload it, then set in the cloud environment: KOTSIN_DATA_URL=<direct download link>  KOTSIN_DATA_SHA256=$sum"
