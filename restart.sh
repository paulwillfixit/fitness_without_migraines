#!/bin/bash
# restart_app.sh - restarts the docker-compose app service

set -e

cd "$(dirname "$0")/infra"

echo "Stopping app..."
docker compose stop app

echo "Rebuilding app..."
docker compose build app

echo "Starting app..."
docker compose up -d app

echo "Tailing logs..."
docker compose logs -f app
