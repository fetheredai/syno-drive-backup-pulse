#!/usr/bin/env bash
# Deploy Backup Pulse on a Synology that has the legacy Docker package
# (DSM 7.0 / 7.1) rather than Container Manager.
#
# Those DSM versions have no Project tab, so there is no GUI for compose, and
# ghcr.io cannot usefully be added under the Docker package's Registry tab.
# This script does the whole thing with plain `docker` over SSH instead.
#
#   sudo ./deploy.sh              prompts for anything it needs
#   sudo ./deploy.sh --show       print the current container's health
#
# CREDENTIALS
#   By default the script prompts and passes the password to the container as
#   an environment variable — the same place every other Synology container
#   keeps its passwords. Nothing is written to disk by this script, but the
#   value is stored in Docker's own container config and is visible to root
#   via `docker inspect`.
#
#   SECRET_MODE=file instead writes the password to a root-only 0600 file and
#   mounts it read-only, so `docker inspect` shows only a path:
#       sudo SECRET_MODE=file ./deploy.sh
#
#   Either way, re-running this script is how you update the site.

set -euo pipefail

# Print something immediately. If this banner does not appear, the script did
# not run at all — an empty or truncated copy exits 0 silently, which is
# indistinguishable from "nothing happened".
echo "Backup Pulse deploy — $(date '+%Y-%m-%d %H:%M:%S')"

# DEBUG=1 ./deploy.sh  traces every command.
[ "${DEBUG:-0}" = "1" ] && set -x

cd "$(dirname "$0")"

NAME="${CONTAINER_NAME:-backup-pulse}"
ENV_FILE="${ENV_FILE:-.env}"
SECRET_MODE="${SECRET_MODE:-env}"
SECRET_FILE="${SECRET_FILE:-$PWD/.pulse-secret}"
DEFAULT_IMAGE="ghcr.io/fetheredai/syno-drive-backup-pulse:latest"

# --- optional .env --------------------------------------------------------
# Entirely optional. Useful for unattended re-deploys; skip it and the script
# asks instead. Docker .env files leave values unquoted, so a line like
#     SYNO_NAS_NAME=Acme Co - DS923+
# would make `source` try to execute `Co`. Parse it by hand.
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
    [ -z "${!key:-}" ] && export "$key=$val"
  done < "$1"
}
[ -f "$ENV_FILE" ] && load_env "$ENV_FILE"

# --- docker ---------------------------------------------------------------
# DSM puts the docker binary in /usr/local/bin, which sudo's secure_path does
# not always include, so a bare `docker` can be "not found" under sudo even
# though the package is running. Probe the known locations.
DOCKER=""
for cand in docker /usr/local/bin/docker /usr/bin/docker \
            /volume1/@appstore/Docker/usr/bin/docker \
            /volume1/@appstore/ContainerManager/usr/bin/docker; do
  if command -v "$cand" >/dev/null 2>&1 || [ -x "$cand" ]; then
    if "$cand" info >/dev/null 2>&1; then DOCKER="$cand"; break; fi
    if command -v sudo >/dev/null 2>&1 && sudo -n "$cand" info >/dev/null 2>&1; then
      DOCKER="sudo $cand"; break
    fi
    DOCKER_SEEN="$cand"
  fi
done

if [ -z "$DOCKER" ]; then
  if [ -n "${DOCKER_SEEN:-}" ]; then
    echo "Found the docker binary at $DOCKER_SEEN but could not talk to the" >&2
    echo "daemon. Re-run with sudo, and check the Docker package is started" >&2
    echo "in Package Center." >&2
  else
    echo "No docker binary found. Is the Docker (or Container Manager)" >&2
    echo "package installed and started in Package Center?" >&2
  fi
  exit 1
fi
echo "==> using: $DOCKER"

WEB_PORT="${SYNO_WEB_PORT:-8477}"

health() {
  $DOCKER exec "$NAME" python3 -c \
    "import urllib.request,sys;print(urllib.request.urlopen('http://127.0.0.1:${WEB_PORT}/healthz',timeout=4).read().decode())" \
    2>/dev/null || true
}

if [ "${1:-}" = "--show" ]; then
  health
  exit 0
fi

# --- gather what we need --------------------------------------------------
PULSE_IMAGE="${PULSE_IMAGE:-$DEFAULT_IMAGE}"

ask() {                       # ask VAR "prompt" "default"
  local var="$1" prompt="$2" default="${3:-}" reply
  [ -n "${!var:-}" ] && return 0
  if [ ! -t 0 ]; then
    echo "$var is not set and there is no terminal to ask on. Set it in the" >&2
    echo "environment or in $ENV_FILE, e.g.  SYNO_USER=svc-drivemonitor" >&2
    exit 1
  fi
  if [ -n "$default" ]; then
    read -r -p "$prompt [$default]: " reply
    printf -v "$var" '%s' "${reply:-$default}"
  else
    while [ -z "${reply:-}" ]; do read -r -p "$prompt: " reply; done
    printf -v "$var" '%s' "$reply"
  fi
  export "${var?}"
}

ask SYNO_NAS_NAME "Client / NAS name shown in the dashboard" "$(hostname)"
ask SYNO_USER     "DSM service account (admin group, no 2FA)" "svc-drivemonitor"

if [ -z "${SYNO_PASS:-}" ]; then
  if [ ! -t 0 ]; then
    echo "SYNO_PASS is not set and there is no terminal to prompt on." >&2
    exit 1
  fi
  # Confirm on entry: a mistyped password means repeated failed logins, and
  # DSM's auto-block will start rejecting the container's source IP.
  while :; do
    read -r -s -p "Password for $SYNO_USER: " SYNO_PASS; echo
    read -r -s -p "Confirm: " _confirm; echo
    [ "$SYNO_PASS" = "$_confirm" ] && [ -n "$SYNO_PASS" ] && break
    echo "  did not match (or was empty) — try again" >&2
  done
  unset _confirm
  export SYNO_PASS
fi

# --- how the secret reaches the container ---------------------------------
SECRET_ARGS=()
if [ "$SECRET_MODE" = "file" ]; then
  umask 077
  printf '%s' "$SYNO_PASS" > "$SECRET_FILE"
  chmod 600 "$SECRET_FILE"
  SECRET_ARGS=(-v "$SECRET_FILE:/run/secrets/syno_pass:ro"
               -e SYNO_PASS_FILE=/run/secrets/syno_pass)
  echo "==> password written to $SECRET_FILE (0600) and mounted read-only"
else
  SECRET_ARGS=(-e SYNO_PASS="$SYNO_PASS")
fi

# --- deploy ---------------------------------------------------------------
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
  -e SYNO_NAS_NAME="$SYNO_NAS_NAME" \
  -e SYNO_HOST="${SYNO_HOST:-localhost}" \
  -e SYNO_PORT="${SYNO_PORT:-5000}" \
  -e SYNO_HTTPS="${SYNO_HTTPS:-false}" \
  -e SYNO_VERIFY_SSL="${SYNO_VERIFY_SSL:-false}" \
  -e SYNO_USER="$SYNO_USER" \
  "${SECRET_ARGS[@]}" \
  -e SYNO_DAYS="${SYNO_DAYS:-90}" \
  -e SYNO_INTERVAL_HOURS="${SYNO_INTERVAL_HOURS:-4}" \
  -e SYNO_FILE_COUNTS="${SYNO_FILE_COUNTS:-false}" \
  -e SYNO_WEB_PORT="$WEB_PORT" \
  "$PULSE_IMAGE" >/dev/null

# --- verify the credentials actually worked -------------------------------
# Worth doing here rather than leaving them to discover it later: a bad
# password otherwise shows up only as an empty dashboard.
echo -n "==> waiting for the first collection"
status=""
for _ in $(seq 1 40); do
  sleep 3; echo -n "."
  status="$(health)"
  case "$status" in
    *'"last_run": null'*|"") continue ;;
    *) break ;;
  esac
done
echo

if printf '%s' "$status" | grep -q '"last_ok": true'; then
  echo "OK — first collection succeeded."
elif [ -z "$status" ]; then
  echo "Could not reach the container's health endpoint. Check:" >&2
  echo "  $DOCKER logs $NAME" >&2
else
  echo "The container is up but the first collection FAILED:" >&2
  printf '%s\n' "$status" | sed 's/^/  /' >&2
  echo >&2
  echo "Most likely: wrong password, the account is not in the administrators" >&2
  echo "group, or it has 2FA enabled. Full detail: $DOCKER logs $NAME" >&2
fi

echo
echo "  dashboard  http://<nas-ip>:${WEB_PORT}/"
echo "  health     $DOCKER exec $NAME python3 -c \"import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:${WEB_PORT}/healthz').read().decode())\""
echo "  logs       $DOCKER logs -f $NAME"
echo
echo "Validate the Drive API field mappings before trusting the numbers:"
echo "  $DOCKER exec $NAME python3 collector.py --discover"
