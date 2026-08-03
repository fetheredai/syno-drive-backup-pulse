#!/usr/bin/env bash
# Deploy Backup Pulse on a Synology that has the legacy Docker package
# (DSM 7.0 / 7.1) rather than Container Manager.
#
# Those DSM versions have no Project tab, so there is no GUI for compose, and
# ghcr.io cannot usefully be added under the Docker package's Registry tab.
# This script does the whole thing with plain `docker` over SSH instead — no
# compose needed at all.
#
# Usage, on the NAS over SSH:
#   1. Put this script and your .env in a folder, e.g.
#      /volume1/docker/backup-pulse/
#   2. cd /volume1/docker/backup-pulse
#   3. sudo ./deploy.sh
#
# Re-run it any time to update: it pulls the current image and recreates the
# container. Your .env is the only state.

set -euo pipefail

cd "$(dirname "$0")"

NAME="${CONTAINER_NAME:-backup-pulse}"
ENV_FILE="${ENV_FILE:-.env}"

if [ ! -f "$ENV_FILE" ]; then
  echo "No $ENV_FILE here. Copy .env.example to .env and fill it in." >&2
  exit 1
fi

# Parse .env by hand rather than sourcing it. Docker .env files leave values
# unquoted, so a line like
#     SYNO_NAS_NAME=Acme Co - DS923+
# makes `. .env` try to execute `Co`. This handles unquoted values with
# spaces, optional surrounding quotes, comments, blank lines and CRLF endings.
load_env() {
  local line key val
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in ''|'#'*) continue ;; esac
    case "$line" in *=*) ;; *) continue ;; esac
    key="${line%%=*}"
    val="${line#*=}"
    key="${key//[[:space:]]/}"
    [ -z "$key" ] && continue
    if [ "${val#\"}" != "$val" ] && [ "${val%\"}" != "$val" ]; then
      val="${val#\"}"; val="${val%\"}"
    elif [ "${val#\'}" != "$val" ] && [ "${val%\'}" != "$val" ]; then
      val="${val#\'}"; val="${val%\'}"
    fi
    export "$key=$val"
  done < "$1"
}
load_env "$ENV_FILE"

# Synology's docker socket is root-only; re-exec under sudo if we need to.
DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
    DOCKER="sudo docker"
  elif command -v sudo >/dev/null 2>&1; then
    echo "Docker needs elevated rights on DSM. Re-run as: sudo ./deploy.sh" >&2
    exit 1
  else
    echo "Cannot talk to the Docker daemon. Is the Docker package running?" >&2
    exit 1
  fi
fi

: "${PULSE_IMAGE:?PULSE_IMAGE is not set in $ENV_FILE}"
: "${SYNO_USER:?SYNO_USER is not set in $ENV_FILE}"
: "${SYNO_PASS:?SYNO_PASS is not set in $ENV_FILE}"

WEB_PORT="${SYNO_WEB_PORT:-8477}"

echo "==> pulling $PULSE_IMAGE"
$DOCKER pull "$PULSE_IMAGE"

if $DOCKER ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "==> removing the existing $NAME container"
  $DOCKER rm -f "$NAME" >/dev/null
fi

echo "==> starting $NAME"
# --network host so the collector can reach DSM on localhost:5000. That
# traffic never leaves the NAS, which is why plain HTTP is fine here.
$DOCKER run -d \
  --name "$NAME" \
  --restart unless-stopped \
  --network host \
  -e SYNO_NAS_NAME="${SYNO_NAS_NAME:-$(hostname)}" \
  -e SYNO_HOST="${SYNO_HOST:-localhost}" \
  -e SYNO_PORT="${SYNO_PORT:-5000}" \
  -e SYNO_HTTPS="${SYNO_HTTPS:-false}" \
  -e SYNO_VERIFY_SSL="${SYNO_VERIFY_SSL:-false}" \
  -e SYNO_USER="$SYNO_USER" \
  -e SYNO_PASS="$SYNO_PASS" \
  -e SYNO_DAYS="${SYNO_DAYS:-90}" \
  -e SYNO_INTERVAL_HOURS="${SYNO_INTERVAL_HOURS:-4}" \
  -e SYNO_FILE_COUNTS="${SYNO_FILE_COUNTS:-false}" \
  -e SYNO_WEB_PORT="$WEB_PORT" \
  "$PULSE_IMAGE" >/dev/null

echo
echo "Started. Give it a few seconds, then:"
echo "  dashboard  http://$(hostname -i 2>/dev/null | awk '{print $1}' || echo '<nas-ip>'):${WEB_PORT}/"
echo "  health     curl -s http://localhost:${WEB_PORT}/healthz"
echo "  logs       $DOCKER logs -f $NAME"
echo
echo "Validate the Drive API field mappings before trusting the numbers:"
echo "  $DOCKER exec $NAME python3 collector.py --discover"
