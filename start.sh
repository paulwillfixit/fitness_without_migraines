#!/bin/bash
# start.sh - fast (no rebuild) bring-up with auto-detect restart
# Usage: ./start.sh

set -euo pipefail

cd "$(dirname "$0")/infra"

# Is the app service container present?
APP_CID="$(docker compose ps -q app || true)"

if [[ -n "${APP_CID}" ]]; then
  # Is it running?
  APP_STATE="$(docker inspect -f '{{.State.Status}}' "${APP_CID}" || echo 'unknown')"
  if [[ "${APP_STATE}" == "running" ]]; then
    echo "App is running (container ${APP_CID:0:12}). Restarting..."
    docker compose restart app
  else
    echo "App container exists but is '${APP_STATE}'. Starting..."
    docker compose up -d app
  fi
else
  echo "No app container found. Creating and starting..."
  docker compose up -d app
fi

echo "Tailing logs (Ctrl+C to exit)…"
docker compose logs -f app
