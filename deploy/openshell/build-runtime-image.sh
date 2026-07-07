#!/usr/bin/env bash
# Build MAC's OpenShell runtime image from a checked-out source tree.
#
# This is the single image-build primitive used by both fleet bootstrap and
# source self-refresh.  The Containerfile deliberately consumes pinned assets
# from the local build context so sandbox builds do not depend on arbitrary
# network fetches; consequently every caller must prepare those assets first.
set -euo pipefail

GH_VERSION="${GH_VERSION:-2.95.0}"
CODEGRAPH_VERSION="${CODEGRAPH_VERSION:-v1.1.6}"
MAC_SRC="${MAC_SRC:-$HOME/.mac/src/mac}"
OSH_DOCKER_BIN="${OSH_DOCKER_BIN:-docker}"
OSH_IMAGE_TAG="${OSH_IMAGE_TAG:-localhost/mac-hermes:net}"
MAC_IMAGE_SOURCE_SHA="${MAC_IMAGE_SOURCE_SHA:-}"
MAC_IMAGE_SOURCE_SHA_FILE="${MAC_IMAGE_SOURCE_SHA_FILE:-}"
ARCH="$(uname -m)"
IMAGE_ASSET_DIR="$MAC_SRC/.mac-openshell-build-assets"
BUILD_LOCK_DIR="$MAC_SRC/.mac-openshell-build.lock"
BUILD_LOCK_WAIT_SECONDS="${MAC_OPENSHELL_BUILD_LOCK_WAIT_SECONDS:-1900}"
BUILD_LOCK_POLL_SECONDS="${MAC_OPENSHELL_BUILD_LOCK_POLL_SECONDS:-2}"
CONTAINERFILE="$MAC_SRC/deploy/openshell/mac-hermes.Containerfile"

log(){ printf '[build-openshell-image] %s\n' "$*"; }
download(){ curl --retry 5 --retry-all-errors --retry-delay 2 --connect-timeout 15 --max-time 120 -fsSL "$@"; }
cleanup(){
  rm -rf "$IMAGE_ASSET_DIR"
  [ -z "${DOCKER_CONFIG_TMP:-}" ] || rm -rf "$DOCKER_CONFIG_TMP"
  if [ -f "$BUILD_LOCK_DIR/owner-pid" ] \
      && [ "$(cat "$BUILD_LOCK_DIR/owner-pid" 2>/dev/null || true)" = "$$" ]; then
    rm -rf "$BUILD_LOCK_DIR"
  fi
}

acquire_build_lock(){
  local started_at now owner_pid
  started_at="$(date +%s)"
  while ! mkdir "$BUILD_LOCK_DIR" 2>/dev/null; do
    owner_pid="$(cat "$BUILD_LOCK_DIR/owner-pid" 2>/dev/null || true)"
    if [ -n "$owner_pid" ] && ! kill -0 "$owner_pid" 2>/dev/null; then
      log "removing stale image-build lock owned by dead pid $owner_pid"
      rm -rf "$BUILD_LOCK_DIR"
      continue
    fi
    # A process can be pre-empted between mkdir and writing owner-pid. Give it
    # time to finish that atomic acquisition; only reap an ownerless directory
    # after it has demonstrably been stale for at least one minute.
    if [ -z "$owner_pid" ] \
        && [ -n "$(find "$BUILD_LOCK_DIR" -type d -mmin +1 -print -quit 2>/dev/null)" ]; then
      log "removing stale ownerless image-build lock"
      rm -rf "$BUILD_LOCK_DIR"
      continue
    fi
    now="$(date +%s)"
    if [ $((now - started_at)) -ge "$BUILD_LOCK_WAIT_SECONDS" ]; then
      echo "timed out waiting for OpenShell image-build lock: $BUILD_LOCK_DIR" >&2
      exit 1
    fi
    sleep "$BUILD_LOCK_POLL_SECONDS"
  done
  printf '%s\n' "$$" > "$BUILD_LOCK_DIR/owner-pid"
  trap cleanup EXIT
}

case "$ARCH" in
  x86_64|amd64) gh_arch=amd64; codegraph_arch=x64;;
  aarch64|arm64) gh_arch=arm64; codegraph_arch=arm64;;
  *) echo "unsupported image-build architecture $ARCH" >&2; exit 1;;
esac

[ -f "$CONTAINERFILE" ] || { echo "missing OpenShell Containerfile: $CONTAINERFILE" >&2; exit 1; }
command -v "$OSH_DOCKER_BIN" >/dev/null 2>&1 || { echo "docker CLI not found: $OSH_DOCKER_BIN" >&2; exit 1; }

acquire_build_lock
rm -rf "$IMAGE_ASSET_DIR"
mkdir -p "$IMAGE_ASSET_DIR"
log "prefetching pinned runtime-image assets on the host"
download -o "$IMAGE_ASSET_DIR/nodesource_setup.sh" https://deb.nodesource.com/setup_22.x
download -o "$IMAGE_ASSET_DIR/gh.tgz" \
  "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${gh_arch}.tar.gz"
if ! download -o "$IMAGE_ASSET_DIR/lein" \
  https://raw.githubusercontent.com/technomancy/leiningen/stable/bin/lein; then
  log "raw.githubusercontent.com unavailable; fetching Leiningen via jsDelivr"
  download -o "$IMAGE_ASSET_DIR/lein" \
    https://cdn.jsdelivr.net/gh/technomancy/leiningen@stable/bin/lein
fi
download -o "$IMAGE_ASSET_DIR/codegraph.tgz" \
  "https://github.com/colbymchenry/codegraph/releases/download/${CODEGRAPH_VERSION}/codegraph-linux-${codegraph_arch}.tar.gz"
chmod 0755 "$IMAGE_ASSET_DIR/nodesource_setup.sh" "$IMAGE_ASSET_DIR/lein"

# Bypass workstation credential helpers that are commonly absent from service
# PATHs.  All base images are public, so an empty Docker config is sufficient.
DOCKER_CONFIG_TMP="$(mktemp -d)"
printf '{}' > "$DOCKER_CONFIG_TMP/config.json"
log "building $OSH_IMAGE_TAG from $MAC_SRC"
DOCKER_CONFIG="$DOCKER_CONFIG_TMP" "$OSH_DOCKER_BIN" build \
  --build-arg "GH_VERSION=$GH_VERSION" \
  --build-arg "CODEGRAPH_VERSION=$CODEGRAPH_VERSION" \
  -t "$OSH_IMAGE_TAG" -f "$CONTAINERFILE" "$MAC_SRC"

if [ -n "$MAC_IMAGE_SOURCE_SHA" ] && [ -n "$MAC_IMAGE_SOURCE_SHA_FILE" ]; then
  mkdir -p "$(dirname "$MAC_IMAGE_SOURCE_SHA_FILE")"
  marker_tmp="${MAC_IMAGE_SOURCE_SHA_FILE}.tmp.$$"
  printf '%s\n' "$MAC_IMAGE_SOURCE_SHA" > "$marker_tmp"
  mv -f "$marker_tmp" "$MAC_IMAGE_SOURCE_SHA_FILE"
fi
