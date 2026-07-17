#!/usr/bin/env bash
# Build the trusted certifier locally. Registry publication belongs exclusively
# to the protected GitHub Actions workflow in .github/workflows/ci.yml.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${MAC_CERTIFIER_IMAGE:-ghcr.io/jordanhubbard/mac-certifier:local}"
PLATFORM="${MAC_CERTIFIER_PLATFORM:-}"
ALLOW_DIRTY=0

usage() {
    cat <<'EOF'
usage: scripts/build-certifier-image.sh [--image NAME:TAG] [--platform OS/ARCH] [--allow-dirty]

Builds and loads one local certifier image. This script never pushes.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --image) IMAGE="$2"; shift 2 ;;
        --image=*) IMAGE="${1#*=}"; shift ;;
        --platform) PLATFORM="$2"; shift 2 ;;
        --platform=*) PLATFORM="${1#*=}"; shift ;;
        --allow-dirty) ALLOW_DIRTY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 1; }
docker buildx version >/dev/null 2>&1 || { echo "docker buildx is required" >&2; exit 1; }

SOURCE_REVISION="$(git -C "$ROOT" rev-parse --verify HEAD)"
if [ "$ALLOW_DIRTY" -ne 1 ] && [ -n "$(git -C "$ROOT" status --porcelain)" ]; then
    echo "refusing to build an activation artifact from a dirty worktree" >&2
    echo "commit the reviewed inputs, or use --allow-dirty for a local-only test" >&2
    exit 1
fi
BUILD_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/mac-certifier-context.XXXXXX")"
cleanup() { rm -rf "$BUILD_ROOT"; }
trap cleanup EXIT INT TERM
BUILD_CONTEXT="$BUILD_ROOT/context"
CONTEXT_DIGEST="$($ROOT/scripts/certifier-context-manifest.py \
    --root "$ROOT" --materialize "$BUILD_CONTEXT" --print-digest)"

if [ -z "$PLATFORM" ]; then
    case "$(uname -m)" in
        x86_64|amd64) PLATFORM=linux/amd64 ;;
        arm64|aarch64) PLATFORM=linux/arm64 ;;
        *) echo "unsupported build architecture: $(uname -m)" >&2; exit 1 ;;
    esac
fi
case "$PLATFORM" in
    *,*) echo "local --load builds support one platform only" >&2; exit 2 ;;
esac

printf 'building %s from %s for %s\n' "$IMAGE" "$SOURCE_REVISION" "$PLATFORM"
docker buildx build \
    --file "$BUILD_CONTEXT/deploy/certifier/Containerfile" \
    --platform "$PLATFORM" \
    --build-arg "MAC_CERTIFIER_SOURCE_REVISION=$SOURCE_REVISION" \
    --build-arg "MAC_CERTIFIER_CONTEXT_DIGEST=$CONTEXT_DIGEST" \
    --label "org.opencontainers.image.created=$(git -C "$ROOT" show -s --format=%cI HEAD)" \
    --tag "$IMAGE" \
    --load \
    "$BUILD_CONTEXT"

docker run --rm "$IMAGE" \
    /opt/mac-certifier/bin/run-contract-tests --image-self-test
printf 'local certifier image verified: %s\n' "$IMAGE"
