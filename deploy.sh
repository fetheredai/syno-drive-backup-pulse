#!/usr/bin/env bash
# Deploy Backup Pulse on a Synology that has the legacy Docker package
# (DSM 7.0 / 7.1) rather than Container Manager.
#
# Those DSM versions have no Project tab, so there is no GUI for compose, and
# ghcr.io cannot usefully be added under the Docker package's Registry tab.
# This script does the whole thing with plain `docker` over SSH instead.
#
#   sudo ./deploy.sh                first run: asks, then remembers
#   sudo ./deploy.sh                later runs: no prompts, just updates
#   sudo ./deploy.sh --reconfigure  change the saved answers
#   sudo ./deploy.sh --show         print the current container's health
#
# CREDENTIALS
#   Asked once, then stored in a root-only 0600 file next to this script and
#   mounted read-only into the container, so `docker inspect` shows a path
#   rather than the password. Re-running needs no re-entry.
#
#   SECRET_MODE=env passes it as an environment variable instead — the same
#   place other Synology containers keep passwords, but visible to root via
#   `docker inspect`.
#
#   Non-secret answers live in .pulse-config beside this script.

set -euo pipefail

echo "Backup Pulse deploy — $(date '+%Y-%m-%d %H:%M:%S')"
[ "${DEBUG:-0}" = "1" ] && set -x

cd "$(dirname "$0")"

NAME="${CONTAINER_NAME:-backup-pulse}"
ENV_FILE="${ENV_FILE:-.env}"
CONFIG_FILE="${CONFIG_FILE:-.pulse-config}"
SECRET_MODE="${SECRET_MODE:-file}"
SECRET_FILE="${SECRET_FILE:-$PWD/.pulse-secret}"
DEFAULT_IMAGE="ghcr.io/fetheredai/syno-drive-backup-pulse:latest"

MODE="${1:-}"

# --- reading saved answers -------------------------------------------------
# Parsed by hand rather than sourced: these files leave values unquoted, so a
# line like  SYNO_NAS_NAME=Acme Co - DS923+  would make `source` run `Co`.
# Values already in the environment win, so one-off overrides work.
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
[ -f "$CONFIG_FILE" ] && load_env "$CONFIG_FILE"
[ -f "$ENV_FILE" ] && load_env "$ENV_FILE"

# --- docker ----------------------------------------------------------------
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
    "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:${WEB_PORT}/healthz',timeout=4).read().decode())" \
    2>/dev/null || true
}

if [ "$MODE" = "--show" ]; then
  health
  exit 0
fi

# --- gather what we need ---------------------------------------------------
PULSE_IMAGE="${PULSE_IMAGE:-$DEFAULT_IMAGE}"

ask() {                       # ask VAR "prompt" "default"
  local var="$1" prompt="$2" default="${3:-}" reply
  [ -n "${!var:-}" ] && return 0
  if [ ! -t 0 ]; then
    echo "$var is not set and there is no terminal to ask on. Set it in the" >&2
    echo "environment or in $CONFIG_FILE, e.g.  SYNO_USER=svc-drivemonitor" >&2
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

ask_optional() {              # ask_optional VAR "prompt"  (blank allowed)
  local var="$1" prompt="$2" reply
  if [ ! -t 0 ]; then export "${var}=${!var:-}"; return 0; fi
  read -r -p "$prompt: " reply
  printf -v "$var" '%s' "$reply"
  export "${var?}"
}

FIRST_RUN=0
if [ "${PULSE_CONFIGURED:-}" != "1" ] || [ "$MODE" = "--reconfigure" ]; then
  FIRST_RUN=1
  if [ "$MODE" = "--reconfigure" ]; then
    unset SYNO_NAS_NAME SYNO_USER SYNO_INCLUDE_GROUPS SYNO_LOGIN_GROUP SYNO_AUTH
  fi
  ask SYNO_NAS_NAME "Client / NAS name shown in the dashboard" "$(hostname)"
  ask SYNO_USER     "DSM service account (admin group, no 2FA)" "svc-drivemonitor"
  echo
  echo "Only show users in particular DSM groups? Comma-separated, blank for all."
  echo "  e.g. SynologyDriveUsers"
  ask_optional SYNO_INCLUDE_GROUPS "  Groups to include"
  echo
  echo "Require sign-in with a DSM account to view the dashboard? [Y/n]"
  read -r _auth_reply || true
  case "${_auth_reply:-y}" in [Nn]*) SYNO_AUTH=false ;; *) SYNO_AUTH=true ;; esac
  export SYNO_AUTH
  if [ "$SYNO_AUTH" = "true" ]; then
    echo "  Limit sign-in to members of a DSM group? Blank for any DSM account."
    ask_optional SYNO_LOGIN_GROUP "  Group allowed to sign in"
  fi
  echo
else
  echo "==> using saved settings from $CONFIG_FILE (--reconfigure to change)"
fi

# --- the password ----------------------------------------------------------
NEED_PASSWORD=1
if [ "$SECRET_MODE" = "file" ] && [ -s "$SECRET_FILE" ] && [ "$MODE" != "--reconfigure" ]; then
  NEED_PASSWORD=0
  echo "==> reusing the saved service-account password ($SECRET_FILE)"
fi
if [ -n "${SYNO_PASS:-}" ]; then
  NEED_PASSWORD=0
fi

if [ "$NEED_PASSWORD" = "1" ]; then
  if [ ! -t 0 ]; then
    echo "No stored password and no terminal to prompt on. Set SYNO_PASS." >&2
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

SECRET_ARGS=()
if [ "$SECRET_MODE" = "file" ]; then
  if [ -n "${SYNO_PASS:-}" ]; then
    umask 077
    printf '%s' "$SYNO_PASS" > "$SECRET_FILE"
    chmod 600 "$SECRET_FILE"
  fi
  SECRET_ARGS=(-v "$SECRET_FILE:/run/secrets/syno_pass:ro"
               -e SYNO_PASS_FILE=/run/secrets/syno_pass)
else
  SECRET_ARGS=(-e SYNO_PASS="$SYNO_PASS")
fi

# --- remember the non-secret answers ---------------------------------------
if [ "$FIRST_RUN" = "1" ]; then
  umask 077
  cat > "$CONFIG_FILE" <<EOF
# Written by deploy.sh. Non-secret settings; the password is in $SECRET_FILE.
# Re-run with --reconfigure to change these.
PULSE_CONFIGURED=1
PULSE_IMAGE=$PULSE_IMAGE
SYNO_NAS_NAME=$SYNO_NAS_NAME
SYNO_USER=$SYNO_USER
SYNO_INCLUDE_GROUPS=${SYNO_INCLUDE_GROUPS:-}
SYNO_AUTH=${SYNO_AUTH:-true}
SYNO_LOGIN_GROUP=${SYNO_LOGIN_GROUP:-}
SYNO_EXCLUDE_USERS=${SYNO_EXCLUDE_USERS:-}
SYNO_DAYS=${SYNO_DAYS:-90}
SYNO_INTERVAL_HOURS=${SYNO_INTERVAL_HOURS:-4}
SYNO_FILE_COUNTS=${SYNO_FILE_COUNTS:-false}
SYNO_WEB_PORT=$WEB_PORT
EOF
  chmod 600 "$CONFIG_FILE"
  echo "==> settings saved to $CONFIG_FILE"
fi

# --- deploy ----------------------------------------------------------------
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
  -e SYNO_INCLUDE_GROUPS="${SYNO_INCLUDE_GROUPS:-}" \
  -e SYNO_EXCLUDE_USERS="${SYNO_EXCLUDE_USERS:-}" \
  -e SYNO_AUTH="${SYNO_AUTH:-true}" \
  -e SYNO_LOGIN_GROUP="${SYNO_LOGIN_GROUP:-}" \
  -e SYNO_SESSION_HOURS="${SYNO_SESSION_HOURS:-12}" \
  -e SYNO_WEB_PORT="$WEB_PORT" \
  "$PULSE_IMAGE" >/dev/null

# --- verify the credentials actually worked --------------------------------
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
  echo "Re-enter the password with: sudo ./deploy.sh --reconfigure" >&2
fi

# The deploy usually runs under sudo, so $DOCKER is a bare `docker`, but these
# follow-ups get run unprivileged where the socket is root-only.
HINT="$DOCKER"
if [ "$(id -u)" = "0" ] && [ "$HINT" = "${HINT#sudo }" ]; then
  HINT="sudo $DOCKER"
fi
NAS_IP="$(ip route get 1 2>/dev/null | awk '{print $7; exit}')"
[ -z "$NAS_IP" ] && NAS_IP="$(hostname -i 2>/dev/null | awk '{print $1}')"
[ -z "$NAS_IP" ] && NAS_IP="<nas-ip>"

echo
echo "  dashboard  http://${NAS_IP}:${WEB_PORT}/"
if [ "${SYNO_AUTH:-true}" = "true" ]; then
  echo "             sign in with a DSM account\
${SYNO_LOGIN_GROUP:+ in group $SYNO_LOGIN_GROUP}"
fi
echo "  logs       $HINT logs -f $NAME"
echo "  update     sudo ./deploy.sh          (no prompts)"
echo "  change     sudo ./deploy.sh --reconfigure"
