#!/usr/bin/env bash
# Deploy from the laptop to the server: build the UI here (no node on the 2 GB box), rsync the code and the
# built UI, optionally the .env and the holidays file, sync the venv on the server and restart the unit.
#
#   deploy/aws/deploy.sh ubuntu@<elastic-ip>            # code + UI
#   deploy/aws/deploy.sh ubuntu@<elastic-ip> --with-env # also backend/.env (credentials: over ssh only, never git)
#
# Never run during market hours without asking: the restart drops the feed for ~30 s.
set -euo pipefail
HOST="${1:?usage: deploy.sh user@host [--with-env]}"
WITH_ENV="${2:-}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
APP_DIR="${APP_DIR:-kotsin-nse}"

echo "== UI build (laptop)"
(cd "$ROOT/frontend" && npm ci --silent && npm run build --silent)

echo "== rsync code → $HOST:$APP_DIR"
rsync -az --delete \
  --exclude '.git' --exclude 'backend/.venv' --exclude 'backend/data-live*' --exclude 'backend/data' \
  --exclude 'frontend/node_modules' --exclude 'backend/.env' --exclude '__pycache__' --exclude '.pytest_cache' \
  --exclude '*.parquet' --exclude 'docs/research/scripts' \
  "$ROOT/" "$HOST:$APP_DIR/"
rsync -az --delete "$ROOT/frontend/dist/" "$HOST:$APP_DIR/frontend/dist/"
rsync -az "$ROOT/backend/data-live/holidays.txt" "$HOST:$APP_DIR/backend/data-live/holidays.txt" 2>/dev/null || true

if [ "$WITH_ENV" = "--with-env" ]; then
  echo "== .env → server (chmod 600)"
  scp -q "$ROOT/backend/.env" "$HOST:$APP_DIR/backend/.env"
  ssh "$HOST" "chmod 600 $APP_DIR/backend/.env"
fi

echo "== venv sync + restart"
ssh "$HOST" "export PATH=\$HOME/.local/bin:\$PATH; cd $APP_DIR/backend && uv sync --frozen --no-dev -q && sudo systemctl restart kotsin-nse && sleep 8 && curl -s --max-time 5 http://127.0.0.1:8500/api/health | head -c 300; echo"
echo "done. UI: ssh -L 8500:127.0.0.1:8500 $HOST  → http://127.0.0.1:8500"
