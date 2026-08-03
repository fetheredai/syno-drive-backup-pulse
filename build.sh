#!/usr/bin/env bash
# Build and publish the multi-arch image from your Mac.
#
# Synology "plus" models are Intel/AMD (linux/amd64); the value models are
# ARM (linux/arm64). Building both means one tag installs on any of them.
#
#   ./build.sh                    # build both arches, load amd64 locally
#   ./build.sh push               # build both and push to the registry
#
# Requires Docker Desktop (buildx is included) and, for push, a registry
# login:  echo $GHCR_TOKEN | docker login ghcr.io -u <user> --password-stdin

set -euo pipefail

cd "$(dirname "$0")"

# Default the image path to ghcr.io/<owner>/<repo> derived from the git remote,
# so this matches whatever GitHub Actions publishes. Override with IMAGE=...
default_image() {
  local url owner_repo
  url="$(git config --get remote.origin.url 2>/dev/null || true)"
  [ -z "$url" ] && return 1
  owner_repo="$(printf '%s' "$url" \
    | sed -E 's#^git@[^:]+:##; s#^https?://[^/]+/##; s#\.git$##')"
  [ -z "$owner_repo" ] && return 1
  printf 'ghcr.io/%s' "$(printf '%s' "$owner_repo" | tr '[:upper:]' '[:lower:]')"
}

IMAGE="${IMAGE:-$(default_image || true)}"
if [ -z "$IMAGE" ]; then
  echo "Could not derive an image path (no git remote). Set IMAGE=, e.g." >&2
  echo "  IMAGE=ghcr.io/you/syno-drive-backup-pulse ./build.sh push" >&2
  exit 1
fi
TAG="${TAG:-latest}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"

if ! docker buildx inspect pulse-builder >/dev/null 2>&1; then
  echo "==> creating buildx builder 'pulse-builder'"
  docker buildx create --name pulse-builder --use
else
  docker buildx use pulse-builder
fi

if [ "${1:-}" = "push" ]; then
  echo "==> building $PLATFORMS and pushing $IMAGE:$TAG"
  docker buildx build --platform "$PLATFORMS" -t "$IMAGE:$TAG" --push .
  echo
  echo "Pushed. On each NAS, Container Manager > Project > pull $IMAGE:$TAG"
else
  echo "==> building $PLATFORMS (not pushing)"
  docker buildx build --platform "$PLATFORMS" -t "$IMAGE:$TAG" .
  echo "==> loading a local single-arch image for testing"
  docker buildx build --load -t "$IMAGE:$TAG" .
  echo
  echo "Local test:"
  echo "  docker run --rm -p 8477:8477 \\"
  echo "    -e SYNO_HOST=<nas-ip> -e SYNO_PORT=5001 -e SYNO_HTTPS=true \\"
  echo "    -e SYNO_USER=svc-drivemonitor -e SYNO_PASS=... \\"
  echo "    $IMAGE:$TAG"
fi
