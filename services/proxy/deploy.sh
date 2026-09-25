#!/bin/sh
# Deploy home-chat server changes to the VPS.
#
# Syncs server/ and docker-compose.yml to the VPS over SSH, then rebuilds and
# restarts the chat-proxy container. Never touches data/ (users.json,
# devices.db, ntfy_config.json - live state) or .env (VPS-only secrets).
#
# Before deploying:
#   git stash     # stash any local changes
#   git pull      # pull latest upstream changes
#   git stash pop # restore local changes
#   resolve any conflicts
#
# Usage:
#   ./deploy.sh              # sync + rebuild + restart
#   ./deploy.sh --dry-run    # show what would be synced, change nothing
#   ./deploy.sh --restart    # skip sync, just restart the container
#
# Override target with env vars if needed:
#   VPS_HOST=root@vps.example.net VPS_DIR=/opt/home-chat ./deploy.sh

set -eu

# No default host. This used to name one, so a run with the variable unset
# tried to deploy to whatever that name happened to resolve to -- which is
# the one mistake a deploy script must not make quietly.
VPS_HOST="${VPS_HOST:?set VPS_HOST, e.g. root@vps.example.net}"
VPS_DIR="${VPS_DIR:-/opt/home-chat}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

MODE="deploy"
for arg in "$@"; do
  case "$arg" in
    --dry-run) MODE="dry-run" ;;
    --restart) MODE="restart" ;;
    -h|--help)
      sed -n '2,15p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 1
      ;;
  esac
done

if [ "$MODE" = "restart" ]; then
  echo "==> Restarting chat-proxy on $VPS_HOST (no sync)"
  ssh "$VPS_HOST" "cd $VPS_DIR && docker compose up -d --build --force-recreate"
  echo "==> Health check"
  ssh "$VPS_HOST" "curl -sf http://127.0.0.1:8080/healthz" || echo "warning: healthz check failed"
  exit 0
fi

RSYNC_FLAGS="-az"
[ "$MODE" = "dry-run" ] && RSYNC_FLAGS="$RSYNC_FLAGS -n -v"

echo "==> Syncing server/ to $VPS_HOST:$VPS_DIR/server/"
rsync $RSYNC_FLAGS --delete \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  "$SCRIPT_DIR/server/" "$VPS_HOST:$VPS_DIR/server/"

echo "==> Syncing docker-compose.yml"
rsync $RSYNC_FLAGS "$SCRIPT_DIR/docker-compose.yml" "$VPS_HOST:$VPS_DIR/docker-compose.yml"

if [ "$MODE" = "dry-run" ]; then
  echo "==> Dry run only, not rebuilding/restarting."
  exit 0
fi

echo "==> Rebuilding and restarting chat-proxy"
ssh "$VPS_HOST" "cd $VPS_DIR && docker compose up -d --build --force-recreate"

echo "==> Health check"
ssh "$VPS_HOST" "curl -sf http://127.0.0.1:8080/healthz" || echo "warning: healthz check failed"

echo "Done."
