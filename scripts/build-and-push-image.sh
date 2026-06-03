#!/usr/bin/env bash
# scripts/build-and-push-image.sh
#
# Build the `mac` container image with `docker buildx` (linux/amd64 by
# default so it runs cleanly on K8s nodes even when built from an Apple
# Silicon dev machine), optionally push it to a registry of your
# choice, and optionally rewrite the K8s deployment manifests to pin
# the resulting digest.
#
# Defaults can be overridden via flags or env vars; flags win.
#
# Usage:
#   scripts/build-and-push-image.sh \
#     --registry ghcr.io/your-org \
#     --image-name mac \
#     --tag v0.1.0 \
#     --push \
#     --update-manifests
#
# Env overrides (all optional):
#   MAC_IMAGE_REGISTRY      default: ghcr.io/anthropics
#   MAC_IMAGE_NAME          default: mac
#   MAC_IMAGE_TAG           default: git short SHA + (-dirty if working tree)
#   MAC_IMAGE_PLATFORM      default: linux/amd64
#   MAC_IMAGE_DOCKERFILE    default: Dockerfile
#   MAC_IMAGE_BUILD_CONTEXT default: .  (repo root)
#   MAC_IMAGE_BUILDER       default: mac-builder
#
# Notes:
#   * macOS Apple Silicon needs buildx + a builder that can target
#     amd64. The script bootstraps a docker-container builder named
#     `mac-builder` if one is not already set up.
#   * --push triggers `docker buildx build --push`. Without --push the
#     image is loaded into the local engine via --load (which only
#     works for a single platform; that's fine, we pin one platform).
#   * --update-manifests rewrites the three deploy/k8s/*/deployment.yaml
#     image: fields in place to the pushed `repo@sha256:digest`. The
#     digest is captured from the build metadata file, so this flag
#     implies --push.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ----------------------------------------------------------------------
# Defaults + flag parsing
# ----------------------------------------------------------------------

REGISTRY="${MAC_IMAGE_REGISTRY:-ghcr.io/anthropics}"
IMAGE_NAME="${MAC_IMAGE_NAME:-mac}"
TAG="${MAC_IMAGE_TAG:-}"
PLATFORM="${MAC_IMAGE_PLATFORM:-linux/amd64}"
DOCKERFILE="${MAC_IMAGE_DOCKERFILE:-Dockerfile}"
BUILD_CONTEXT="${MAC_IMAGE_BUILD_CONTEXT:-${REPO_ROOT}}"
BUILDER_NAME="${MAC_IMAGE_BUILDER:-mac-builder}"
PUSH=0
UPDATE_MANIFESTS=0
NO_CACHE=0

usage() {
    sed -n '2,40p' "${BASH_SOURCE[0]}"
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --registry)         REGISTRY="$2"; shift 2 ;;
        --registry=*)       REGISTRY="${1#*=}"; shift ;;
        --image-name)       IMAGE_NAME="$2"; shift 2 ;;
        --image-name=*)     IMAGE_NAME="${1#*=}"; shift ;;
        --tag)              TAG="$2"; shift 2 ;;
        --tag=*)            TAG="${1#*=}"; shift ;;
        --platform)         PLATFORM="$2"; shift 2 ;;
        --platform=*)       PLATFORM="${1#*=}"; shift ;;
        --dockerfile)       DOCKERFILE="$2"; shift 2 ;;
        --dockerfile=*)     DOCKERFILE="${1#*=}"; shift ;;
        --context)          BUILD_CONTEXT="$2"; shift 2 ;;
        --context=*)        BUILD_CONTEXT="${1#*=}"; shift ;;
        --builder)          BUILDER_NAME="$2"; shift 2 ;;
        --builder=*)        BUILDER_NAME="${1#*=}"; shift ;;
        --push)             PUSH=1; shift ;;
        --update-manifests) UPDATE_MANIFESTS=1; PUSH=1; shift ;;
        --no-cache)         NO_CACHE=1; shift ;;
        -h|--help)          usage 0 ;;
        *) echo "unknown flag: $1" >&2; usage 2 ;;
    esac
done

# ----------------------------------------------------------------------
# Tag resolution: explicit > MAC_IMAGE_TAG > git-derived
# ----------------------------------------------------------------------

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

# ----------------------------------------------------------------------
# Tool checks
#
# Prefer real `docker` on PATH; fall back to `podman` (which exposes
# `podman buildx` as a buildah-backed shim). On macOS the user often
# only has podman installed and aliases `docker` to it in their shell
# — that alias isn't visible to non-interactive subshells, so we
# detect podman explicitly here.
# ----------------------------------------------------------------------

if command -v docker >/dev/null 2>&1; then
    DOCKER=docker
elif command -v podman >/dev/null 2>&1; then
    DOCKER=podman
    echo "==> docker not on PATH; using podman ($(podman --version))"
else
    echo "docker or podman is required on PATH" >&2
    exit 1
fi

if ! "${DOCKER}" buildx version >/dev/null 2>&1; then
    echo "${DOCKER} buildx is required (Docker Desktop ships it; podman provides a buildah-backed shim; on Linux: 'apt install docker-buildx-plugin')" >&2
    exit 1
fi

# ----------------------------------------------------------------------
# Builder bootstrap. On macOS Apple Silicon, the default 'desktop-linux'
# builder can usually do cross-arch via QEMU, but a docker-container
# builder is more reliable for amd64 from arm64. Create one if missing.
#
# podman's buildx shim doesn't support `buildx create --driver
# docker-container` — it has no concept of named builders. Skip the
# bootstrap when running under podman.
# ----------------------------------------------------------------------

if [ "${DOCKER}" = "docker" ]; then
    if ! "${DOCKER}" buildx inspect "${BUILDER_NAME}" >/dev/null 2>&1; then
        echo "==> bootstrapping buildx builder '${BUILDER_NAME}' (docker-container driver)"
        "${DOCKER}" buildx create \
            --name "${BUILDER_NAME}" \
            --driver docker-container \
            --bootstrap >/dev/null
    fi
    echo "==> using buildx builder: ${BUILDER_NAME}"
else
    echo "==> podman: skipping named-builder bootstrap (uses buildah backend directly)"
fi

# ----------------------------------------------------------------------
# Build
# ----------------------------------------------------------------------

METADATA_FILE="$(mktemp -t mac-build-meta.XXXXXX.json)"
trap 'rm -f "${METADATA_FILE}"' EXIT

BUILD_ARGS=(
    --platform "${PLATFORM}"
    --file "${DOCKERFILE}"
    --tag "${IMAGE_REF}"
)

# `--builder` and `--metadata-file` are docker-buildx-only flags;
# podman's buildx shim (buildah-backed) doesn't accept them.
# Skip them when running under podman — we can still resolve the
# pushed digest via `podman image inspect` after the build.
if [ "${DOCKER}" = "docker" ]; then
    BUILD_ARGS=(
        --builder "${BUILDER_NAME}"
        --metadata-file "${METADATA_FILE}"
        "${BUILD_ARGS[@]}"
    )
fi

if [[ "${NO_CACHE}" == "1" ]]; then
    BUILD_ARGS+=(--no-cache)
fi

# docker buildx supports `--push` and `--load` directly on the
# build invocation; podman's buildx shim does not — it builds into
# local storage and requires a separate `podman push`. Multi-platform
# builds still require --push under docker.
if [ "${DOCKER}" = "docker" ]; then
    if [[ "${PUSH}" == "1" ]]; then
        BUILD_ARGS+=(--push)
    else
        if [[ "${PLATFORM}" == *","* ]]; then
            echo "multi-platform build (${PLATFORM}) requires --push; either drop platforms to one, or pass --push" >&2
            exit 1
        fi
        BUILD_ARGS+=(--load)
    fi
elif [[ "${PLATFORM}" == *","* ]]; then
    echo "multi-platform builds aren't supported under podman in this script" >&2
    exit 1
fi

echo "==> building ${IMAGE_REF}"
echo "    platform:    ${PLATFORM}"
echo "    dockerfile:  ${DOCKERFILE}"
echo "    context:     ${BUILD_CONTEXT}"
echo "    push:        $( [[ ${PUSH} == 1 ]] && echo yes || echo no )"
echo "    runtime:     ${DOCKER}"

"${DOCKER}" buildx build "${BUILD_ARGS[@]}" "${BUILD_CONTEXT}"

# podman builds into local storage; push separately when requested.
if [ "${DOCKER}" = "podman" ] && [[ "${PUSH}" == "1" ]]; then
    echo "==> pushing ${IMAGE_REF}"
    "${DOCKER}" push "${IMAGE_REF}"
fi

# ----------------------------------------------------------------------
# Resolve the immutable digest written by buildx into the metadata file.
# When --push runs, buildx populates "containerimage.digest" with the
# registry-side sha256:... — the value operators want to pin in K8s.
# ----------------------------------------------------------------------

DIGEST=""
if [[ "${PUSH}" == "1" ]] && [[ -s "${METADATA_FILE}" ]]; then
    if command -v jq >/dev/null 2>&1; then
        DIGEST="$(jq -r '."containerimage.digest" // empty' "${METADATA_FILE}")"
    else
        # Fallback regex if jq isn't installed.
        DIGEST="$(grep -oE '"containerimage.digest":[[:space:]]*"sha256:[a-f0-9]+"' "${METADATA_FILE}" \
                    | sed -E 's/.*"(sha256:[a-f0-9]+)".*/\1/' || true)"
    fi
fi

echo
echo "==> built ${IMAGE_REF}"
if [[ -n "${DIGEST}" ]]; then
    PINNED="${IMAGE}@${DIGEST}"
    echo "==> registry digest: ${PINNED}"
fi

# ----------------------------------------------------------------------
# Optionally rewrite the K8s deployment manifests to pin the digest.
# Targets the three Deployments that consume the mac image.
# ----------------------------------------------------------------------

if [[ "${UPDATE_MANIFESTS}" == "1" ]]; then
    if [[ -z "${DIGEST}" ]]; then
        echo "--update-manifests requested but the build did not return a digest; skipping" >&2
        exit 1
    fi
    PINNED="${IMAGE}@${DIGEST}"
    echo "==> rewriting image: lines in deploy/k8s/{mac-api,mac-runner,mac-controller}/deployment.yaml"
    # Portable in-place sed for both macOS and GNU. The pattern matches
    # the whole image: line (everything from `image:` through the rest
    # of the line) and replaces it with the new pinned reference.
    for f in \
        "${REPO_ROOT}/deploy/k8s/mac-api/deployment.yaml" \
        "${REPO_ROOT}/deploy/k8s/mac-runner/deployment.yaml" \
        "${REPO_ROOT}/deploy/k8s/mac-controller/deployment.yaml"; do
        if [[ ! -f "${f}" ]]; then
            echo "    skip (missing): ${f}"
            continue
        fi
        sed -i.bak -E "s|(^[[:space:]]*image:[[:space:]]*).*|\1${PINNED}|" "${f}"
        rm -f "${f}.bak"
        # Also rewrite any literal MAC_RUNNER_DEFAULT_IMAGE env value
        # pointing at the same repo, so task Jobs use the new digest too.
        sed -i.bak -E "/name: MAC_RUNNER_DEFAULT_IMAGE/{n;s|(value:[[:space:]]*).*|\1\"${PINNED}\"|;}" "${f}"
        rm -f "${f}.bak"
        echo "    updated: ${f#${REPO_ROOT}/}"
    done
    echo
    echo "==> review the diff and commit:"
    echo "    git diff -- deploy/k8s"
    echo "    git commit -m 'deploy: pin mac image to ${DIGEST}'"
fi

echo
echo "Done."
