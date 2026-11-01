#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 24.04 box (AWS ap-south-1) for the kotsin-nse engine. Run ON the server as
# the default user (ubuntu). Idempotent: safe to re-run.
#
#   curl -fsSL ... | bash        # or: scp this file over and `bash bootstrap.sh`
#
# What it does: IST clock, a 2 GB swap (the box has 2 GB of RAM; pandas flushes spike), uv (which brings its own
# Python), the repo directory, the systemd unit. It does NOT copy credentials: backend/.env is scp'd by the
# deploy script from the laptop and never travels through git or this file.
set -euo pipefail
APP_DIR="${APP_DIR:-$HOME/kotsin-nse}"

echo "== timezone → Asia/Kolkata"
sudo timedatectl set-timezone Asia/Kolkata
sudo apt-get update -qq
sudo apt-get install -y -qq git curl rsync chrony >/dev/null
sudo systemctl enable --now chrony >/dev/null 2>&1 || true

if ! swapon --show | grep -q swapfile; then
  echo "== 2 GB swap"
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
  echo "== uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

mkdir -p "$APP_DIR/backend/data-live/logs"
echo "== repo dir $APP_DIR (the deploy script rsyncs the code here)"

if [ -f "$APP_DIR/deploy/kotsin-nse.service" ]; then
  echo "== systemd unit"
  sed "s#/home/ubuntu#$HOME#g" "$APP_DIR/deploy/kotsin-nse.service" | sudo tee /etc/systemd/system/kotsin-nse.service >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable kotsin-nse >/dev/null
fi

echo "== public IP (register THIS on 5paisa's Xstream dashboard; it must be the Elastic IP):"
curl -s --max-time 5 https://checkip.amazonaws.com || true
echo "done. Next: deploy.sh from the laptop (code + .env + the UI build), then: sudo systemctl start kotsin-nse"
