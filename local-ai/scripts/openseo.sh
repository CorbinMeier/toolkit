#!/usr/bin/env bash
# OpenSEO (Docker Compose, standalone image) on http://localhost:3001
# Usage: openseo.sh up | down | logs | status | backup <dir> | restore <dir> | save-images | <any docker compose args>
set -euo pipefail
ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
REPO="$ROOT/repos/open-seo"
ENV="$REPO/deploy/standalone/.env"
export COMPOSE_FILE="$REPO/deploy/standalone/compose.yaml"
cd "$REPO"

if [[ ! -f $ENV ]]; then
  cp deploy/standalone/.env.example "$ENV"
  sed -i "s/^STORAGE_SECRET_KEY=.*/STORAGE_SECRET_KEY=$(openssl rand -hex 32)/" "$ENV"
  echo "Created $ENV with a random STORAGE_SECRET_KEY. Add DATAFORSEO_API_KEY there, then rerun."
fi

# Offline machine: load the saved images once instead of pulling.
if ! docker image inspect ghcr.io/every-app/open-seo-standalone:latest >/dev/null 2>&1; then
  docker load -i "$ROOT/images/open-seo-images.tar"
fi

case "${1:-up}" in
  up)     docker compose up -d --pull missing && docker compose logs -f init ;;
  down)   docker compose down ;;
  logs)   docker compose logs -f "${@:2}" ;;
  status) docker compose ps ;;
  backup)  node scripts/celld/backup.mjs backup "${2:?backup dir}" ;;
  restore) node scripts/celld/backup.mjs restore "${2:?backup dir}" ;;
  save-images)  # refresh images/open-seo-images.tar after an update
    docker save -o "$ROOT/images/open-seo-images.tar" ghcr.io/every-app/open-seo-standalone:latest \
      $(grep -oE '(rustfs/rustfs|nginx):[^ ]+@sha256:[0-9a-f]+' "$COMPOSE_FILE" | sed -E 's/:[^@]+@/@/') ;;
  *)      docker compose "$@" ;;
esac
