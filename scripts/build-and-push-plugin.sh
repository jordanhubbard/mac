#!/usr/bin/env bash
# scripts/build-and-push-plugin.sh
#
# Build the mac Hermes plugin image (busybox + /plugin/ files) and push
# to the configured registry. Mirrors scripts/build-and-push-image.sh
# but targets docker/plugin.Dockerfile and the smaller payload.
#
# Usage:
#   scripts/build-and-push-plugin.sh \
#     --registry gitea.omv.a113.casa/vpogu \
#     --tag v0.1.0 \
#     --push
#
# Env overrides:
#   MAC_PLUGIN_REGISTRY      default: gitea.omv.a113.casa/vpogu
#   MAC_PLUGIN_IMAGE_NAME    default: mac-hermes-plugin
#   MAC_PLUGIN_TAG           default: git short SHA + (-dirty if dirty)
#   MAC_PLUGIN_PLATFORM      default: linux/amd64
#   MAC_PLUGIN_DOCKERFILE    default: docker/plugin.Dockerfile

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

REGISTRY="${MAC_PLUGIN_REGISTRY:-gitea.omv.a113.casa/vpogu}"
IMAGE_NAME="${MAC_PLUGIN_IMAGE_NAME:-mac-hermes-plugin}"
TAG="${MAC_PLUGIN_TAG:-}"
PLATFORM="${MAC_PLUGIN_PLATFORM:-linux/amd64}"
DOCKERFILE="${MAC_PLUGIN_DOCKERFILE:-docker/plugin.Dockerfile}"
BUILDER_NAME="${MAC_IMAGE_BUILDER:-mac-builder}"
PUSH=0
NO_CACHE=0
USE_PODMAN=0

usage() { sed -n '2,21p' "${BASH_SOURCE[0]}"; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --registry)   REGISTRY="$2"; shift 2 ;;
        --registry=*) REGISTRY="${1#*=}"; shift ;;
        --image-name) IMAGE_NAME="$2"; shift 2 ;;
        --tag)        TAG="$2"; shift 2 ;;
        --tag=*)      TAG="${1#*=}"; shift ;;
        --platform)   PLATFORM="$2"; shift 2 ;;
        --dockerfile) DOCKERFILE="$2"; shift 2 ;;
        --push)       PUSH=1; shift ;;
        --no-cache)   NO_CACHE=1; shift ;;
        --podman)     USE_PODMAN=1; shift ;;
        -h|--help)    usage 0 ;;
        *) echo "unknown flag: $1" >&2; usage 2 ;;
    esac
done

if [[ -z "${TAG}" ]]; then
    if git -C "${REPO_ROOT}" rev-parse --short=12 HEAD >/dev/null 2>&1; then
        SHA="$(git -C "${REPO_ROOT}" rev-parse --short=12 HEAD)"
        if [[ -n "$(git -C "${REPO_ROOT}" status --porcelain)" ]]; then
            TAG="${SHA}-dirty"
        else
            TAG="${SHA}"
        fi
    else
        TAG="dev"
    fi
fi

IMAGE="${REGISTRY}/${IMAGE_NAME}"
IMAGE_REF="${IMAGE}:${TAG}"

# Auto-detect podman when the docker CLI is just an alias.
if [[ "${USE_PODMAN}" == "0" ]] && ! command -v docker >/dev/null 2>&1 \
        && command -v podman >/dev/null 2>&1; then
    USE_PODMAN=1
fi

if [[ "${USE_PODMAN}" == "1" ]]; then
    if ! command -v podman >/dev/null 2>&1; then
        echo "podman not on PATH" >&2; exit 1
    fi
    echo "==> building with podman: ${IMAGE_REF}"
    BUILD_ARGS=(--platform "${PLATFORM}" --file "${DOCKERFILE}" --tag "${IMAGE_REF}")
    [[ "${NO_CACHE}" == "1" ]] && BUILD_ARGS+=(--no-cache)
    podman build "${BUILD_ARGS[@]}" "${REPO_ROOT}"
    if [[ "${PUSH}" == "1" ]]; then
        echo "==> pushing ${IMAGE_REF}"
        podman push "${IMAGE_REF}"
    fi
else
    if ! docker buildx version >/dev/null 2>&1; then
        echo "docker buildx not available; rerun with --podman if you have podman instead" >&2
        exit 1
    fi
    if ! docker buildx inspect "${BUILDER_NAME}" >/dev/null 2>&1; then
        echo "==> bootstrapping buildx builder '${BUILDER_NAME}'"
        docker buildx create --name "${BUILDER_NAME}" --driver docker-container --bootstrap >/dev/null
    fi
    echo "==> building with buildx: ${IMAGE_REF}"
    BUILD_ARGS=(
        --builder "${BUILDER_NAME}"
        --platform "${PLATFORM}"
        --file "${DOCKERFILE}"
        --tag "${IMAGE_REF}"
    )
    [[ "${NO_CACHE}" == "1" ]] && BUILD_ARGS+=(--no-cache)
    if [[ "${PUSH}" == "1" ]]; then
        BUILD_ARGS+=(--push)
    else
        BUILD_ARGS+=(--load)
    fi
    docker buildx build "${BUILD_ARGS[@]}" "${REPO_ROOT}"
fi

echo
echo "Done. Image: ${IMAGE_REF}"
echo "Set MAC_PLUGIN_IMAGE_TAG=${TAG} in home-ops hermes-agent values to roll it."
