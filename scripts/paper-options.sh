#!/bin/zsh
# Advance the option paper books and the volatility regime from the latest F&O bhavcopy (NSE publishes it in the
# evening; kotsin_nse.optsim.nightly fetches whatever recent session is missing, steps every pre-registered book in
# data-live/paper/, and rewrites data-live/regime/latest-NIFTY.json). Run after 18:30 on a session day or any morning;
# running it twice is harmless. Nothing here touches the engine or places an order.
#
#   scripts/paper-options.sh
set -u
B=${0:A:h}/../backend
B=${B:A}
cd "$B" || exit 1
mkdir -p data-live/logs
LOG=data-live/logs/paper-$(date +%F).log
nice -n 15 "$B/.venv/bin/python" -m kotsin_nse.optsim.nightly 2>&1 | tee -a "$LOG" | tail -24
