#!/usr/bin/env bash
# bootstrap-openshell.sh — stand up OpenShell sandbox enforcement on a mac node.
#
# Run ON the target node (it touches the local user's ~/.mac, ~/.local, Docker
# Engine/Moby, and — with sudo — the CDI spec + a firewall). Idempotent: safe to
# re-run. MAC standardizes on the OpenShell Docker driver only. Do not use
# Docker Desktop, Podman, or podman-docker for production fleet nodes; use the
# OSS Docker Engine/Moby daemon package supplied by the Linux distribution or a
# nested Docker daemon in containerized environments that support DinD.
#
# It does NOT flip enforcement by default. After it succeeds, validate a real
# task, then enable:  --enable (MAC_OPENSHELL_SANDBOX=1) and, once validated,
# --fail-closed (MAC_ALLOW_UNSANDBOXED_YOLO=0).
#
# Knobs (env):
#   OPENSHELL_VERSION   default 0.0.72        — CLI + gateway version (must match)
#   GH_VERSION          default 2.95.0        — GitHub CLI version in runtime image
#   MAC_HOME            default $HOME/.mac
#   MAC_SRC             default $MAC_HOME/src/mac    — mac source tree (image build context)
#   OSH_DOCKER_BIN      default docker       — Docker Engine/Moby CLI path
#   OSH_IMAGE_TAG       default localhost/mac-hermes:net — sandbox image tag
#   OSH_RUNTIME_IMAGE_REF exact public ghcr.io/...@sha256 digest to pull instead
#                       of rebuilding the runtime independently on each node
#   OSH_RUNTIME_INPUT_SHA256 exact sha256:... frozen-input identity proved by CI
#                       for OSH_RUNTIME_IMAGE_REF; independent of controller HEAD
#   OSH_GPU             auto|yes|no          — auto: detect nvidia-smi
#   OSH_HUB_URL         default from mac.env MAC_HUB_URL — the hub the sandbox egresses to
# Flags: --enable  --fail-closed  --skip-image
set -euo pipefail

OPENSHELL_ASSET_REGISTRY="$(cd "$(dirname "$0")" && pwd)/reviewed-cli-assets.sh"
[ -r "$OPENSHELL_ASSET_REGISTRY" ] || {
  echo "reviewed OpenShell CLI asset registry is missing" >&2
  exit 2
}
# shellcheck source=deploy/openshell/reviewed-cli-assets.sh
. "$OPENSHELL_ASSET_REGISTRY"
OPENSHELL_VERSION="${OPENSHELL_VERSION:-$OPENSHELL_REVIEWED_CLI_VERSION}"
# Multi-platform linux/amd64+linux/arm64 supervisor index reviewed with the
# OpenShell 0.0.72 fleet baseline. Never let a mutable `latest` image change the
# certifier's isolation runtime between otherwise identical executions.
OSH_SUPERVISOR_IMAGE="ghcr.io/nvidia/openshell/supervisor@sha256:80ed9cda5bf672fefdb9dcd4604b40a8b09c0891b6eb9d03e10227c7e3dfb49d"
case "$OPENSHELL_VERSION" in
  0.0.72)
    IFS='|' read -r _osh_asset OSH_CLI_LINUX_AMD64_SHA256 _osh_cli_sha \
      <<<"$(reviewed_openshell_cli_asset linux x86_64)"
    IFS='|' read -r _osh_asset OSH_CLI_LINUX_ARM64_SHA256 _osh_cli_sha \
      <<<"$(reviewed_openshell_cli_asset linux aarch64)"
    OSH_GATEWAY_LINUX_AMD64_SHA256="03225fb9388b682af1a5f1614b26b75f828da6031e3ffc1fd920b6fbe5f70877"
    OSH_GATEWAY_LINUX_ARM64_SHA256="a97dcb3acb04fb2d1170c1a2170228990c2337e25bb8c18817e5a6e952204108"
    ;;
  *)
    echo "unsupported unreviewed OPENSHELL_VERSION=$OPENSHELL_VERSION; add exact release-asset digests before upgrading" >&2
    exit 2
    ;;
esac
GH_VERSION="${GH_VERSION:-2.95.0}"
MAC_HOME="${MAC_HOME:-$HOME/.mac}"
MAC_SRC="${MAC_SRC:-$MAC_HOME/src/mac}"
OSH_DOCKER_BIN="${OSH_DOCKER_BIN:-docker}"
OSH_IMAGE_TAG="${OSH_IMAGE_TAG:-localhost/mac-hermes:net}"
OSH_RUNTIME_IMAGE_REF="${OSH_RUNTIME_IMAGE_REF:-}"
OSH_RUNTIME_INPUT_SHA256="${OSH_RUNTIME_INPUT_SHA256:-}"
OSH_GPU="${OSH_GPU:-auto}"
OPENSHELL_LOCAL_GATEWAY_ENDPOINT="http://127.0.0.1:17670"
ENVF="$MAC_HOME/mac.env"
OSH_DIR="$MAC_HOME/openshell"
OSH_GATEWAY_SUPERVISOR_CONFIG="/etc/supervisor/conf.d/openshell-gateway.conf"
DEPLOYED_SOURCE_REVISION_FILE="${MAC_DEPLOYED_SOURCE_REVISION_FILE:-$MAC_HOME/deployed-source-revision}"
BIN="$HOME/.local/bin"
ARCH="$(uname -m)"   # x86_64 | aarch64
DO_ENABLE=0; DO_FAILCLOSED=0; SKIP_IMAGE=0
for a in "$@"; do case "$a" in
  --enable) DO_ENABLE=1;; --fail-closed) DO_FAILCLOSED=1; DO_ENABLE=1;; --skip-image) SKIP_IMAGE=1;;
  *) echo "unknown arg: $a" >&2; exit 2;; esac; done
log(){ printf '[bootstrap-openshell] %s\n' "$*"; }
truthy(){ case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in 1|true|yes|on) return 0;; *) return 1;; esac; }
download(){ curl --retry 5 --retry-all-errors --retry-delay 2 --connect-timeout 15 --max-time 120 -fsSL "$@"; }
verify_sha256(){
  local path="$1" expected="$2" observed=""
  if command -v sha256sum >/dev/null 2>&1; then
    observed="$(sha256sum "$path" | awk '{print $1}')"
  elif command -v shasum >/dev/null 2>&1; then
    observed="$(shasum -a 256 "$path" | awk '{print $1}')"
  else
    echo "ERROR: sha256sum or shasum is required" >&2
    return 1
  fi
  [ "$observed" = "$expected" ] || {
    echo "ERROR: SHA-256 mismatch for $(basename "$path")" >&2
    return 1
  }
}
install_reviewed_openshell_archive(){
  local archive="$1"
  local helper="$MAC_SRC/deploy/openshell/reviewed-cli.py" spec
  local -a helper_args=()
  [ -r "$helper" ] || {
    echo "ERROR: reviewed OpenShell CLI installer is missing" >&2
    return 1
  }
  while IFS= read -r spec; do
    helper_args+=(--asset-spec "$spec")
  done < <(reviewed_openshell_cli_specs)
  python3 "$helper" install-archive \
    --mac-home "$MAC_HOME" \
    --expected-os "$(uname -s | tr '[:upper:]' '[:lower:]')" \
    --version "$OPENSHELL_REVIEWED_CLI_VERSION" \
    --base-url "$OPENSHELL_REVIEWED_CLI_BASE_URL" \
    "${helper_args[@]}" \
    --archive "$archive" >/dev/null
}

# Bootstrap validation must never inherit a stale selected gateway.  Bind every
# runtime operation to the gateway created by this invocation, independently of
# the CLI's user-level registration metadata.
openshell_local_gateway(){
  local cli="$1"
  shift
  OPENSHELL_GATEWAY_ENDPOINT="$OPENSHELL_LOCAL_GATEWAY_ENDPOINT" "$cli" "$@"
}

wait_for_local_gateway(){
  local cli="$1" attempt
  for ((attempt = 1; attempt <= 120; attempt++)); do
    if openshell_local_gateway "$cli" status >/dev/null 2>&1; then
      return 0
    fi
    ((attempt == 120)) || sleep 1
  done
  return 1
}

register_and_select_local_gateway(){
  local cli="$1" registrations
  # `gateway add` is not idempotent. Remove the reviewed name when it already
  # exists, then recreate and select it without allowing ambient gateway env to
  # redirect these metadata operations.
  if env -u OPENSHELL_GATEWAY_ENDPOINT -u OPENSHELL_GATEWAY \
      "$cli" gateway info --name openshell >/dev/null 2>&1; then
    env -u OPENSHELL_GATEWAY_ENDPOINT -u OPENSHELL_GATEWAY \
      "$cli" gateway remove openshell >/dev/null
  fi
  env -u OPENSHELL_GATEWAY_ENDPOINT -u OPENSHELL_GATEWAY \
    "$cli" gateway add --name openshell "$OPENSHELL_LOCAL_GATEWAY_ENDPOINT" >/dev/null
  env -u OPENSHELL_GATEWAY_ENDPOINT -u OPENSHELL_GATEWAY \
    "$cli" gateway select openshell >/dev/null
  registrations="$(env -u OPENSHELL_GATEWAY_ENDPOINT -u OPENSHELL_GATEWAY \
    "$cli" gateway list --output json)"
  if ! printf '%s\n' "$registrations" | "$MAC_HOME/venv/bin/python" -c '
import json
import sys

endpoint = sys.argv[1]
registrations = json.load(sys.stdin)
matches = [item for item in registrations if item.get("name") == "openshell"]
if len(matches) != 1 or matches[0].get("endpoint") != endpoint or not matches[0].get("active"):
    raise SystemExit("local OpenShell gateway registration is not exact and active")
' "$OPENSHELL_LOCAL_GATEWAY_ENDPOINT"; then
    echo "ERROR: failed to verify the selected local OpenShell gateway" >&2
    return 1
  fi
}

clear_repo_update_dispatch_blocker(){
  local configured="${MAC_REPO_UPDATE_DISPATCH_BLOCKER_FILE:-}"
  # The worker normally receives the override from mac.env, while this bootstrap
  # is launched in a fresh SSH shell. Resolve that same setting without importing
  # the rest of mac.env into bootstrap's own control variables.
  if [ -z "$configured" ] && [ -f "$ENVF" ]; then
    if ! configured="$(
      set +u
      # shellcheck disable=SC1090 -- this is the managed worker environment file.
      if ! . "$ENVF" >/dev/null 2>&1; then
        exit 1
      fi
      printf '%s' "${MAC_REPO_UPDATE_DISPATCH_BLOCKER_FILE:-}"
    )"; then
      echo "ERROR: could not resolve MAC_REPO_UPDATE_DISPATCH_BLOCKER_FILE from $ENVF" >&2
      return 1
    fi
  fi
  "$MAC_HOME/venv/bin/python" - "$MAC_HOME" "$configured" <<'PY'
import os
import sys
from pathlib import Path

mac_home = Path(sys.argv[1]).expanduser()
configured = sys.argv[2].strip()
raw_paths = [str(mac_home / "repo-update-dispatch-blocked.json")]
if configured:
    raw_paths.append(configured)

seen = set()
for raw in raw_paths:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        # Fleet services run with MAC_HOME as their working directory.
        path = mac_home / path
    key = os.path.abspath(os.fspath(path))
    if key in seen:
        continue
    seen.add(key)
    try:
        Path(key).unlink()
    except FileNotFoundError:
        pass
PY
}

resolve_deployed_source_revision(){
  local git_revision="" marker_revision="" revision=""
  git_revision="$(git -C "$MAC_SRC" rev-parse HEAD 2>/dev/null || true)"
  if [ -f "$DEPLOYED_SOURCE_REVISION_FILE" ]; then
    IFS= read -r marker_revision < "$DEPLOYED_SOURCE_REVISION_FILE" || true
    marker_revision="$(printf '%s' "$marker_revision" | tr -d '[:space:]')"
  fi
  if [ -n "$git_revision" ] && ! [[ "$git_revision" =~ ^[0-9a-f]{40}$ ]]; then
    echo "invalid Git revision for deployed source" >&2
    return 1
  fi
  if [ -n "$marker_revision" ] && ! [[ "$marker_revision" =~ ^[0-9a-f]{40}$ ]]; then
    echo "invalid durable deployed-source revision marker" >&2
    return 1
  fi
  if [ -n "$git_revision" ] && [ -n "$marker_revision" ] \
      && [ "$git_revision" != "$marker_revision" ]; then
    echo "deployed Git checkout does not match durable source revision marker" >&2
    return 1
  fi
  revision="${git_revision:-$marker_revision}"
  if [ -z "$revision" ]; then
    echo "cannot verify runtime image: deployed source revision is unavailable" >&2
    return 1
  fi
  printf '%s\n' "$revision"
}
export PATH="$BIN:$PATH" XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

build_runtime_image() {
  local image_source_sha image_source_sha_file runtime_digest runtime_config
  local image_revision image_input_sha runtime_ref_file runtime_ref_tmp builder
  local runtime_input_file runtime_input_tmp runtime_build_file runtime_build_tmp
  image_source_sha="$(resolve_deployed_source_revision)" || return 1
  image_source_sha_file="$OSH_DIR/image-source-sha"
  runtime_ref_file="$OSH_DIR/runtime-image-ref"
  runtime_input_file="$OSH_DIR/runtime-input-sha256"
  runtime_build_file="$OSH_DIR/runtime-image-build-revision"
  if [ -n "$OSH_RUNTIME_IMAGE_REF" ]; then
    [[ "$OSH_RUNTIME_IMAGE_REF" =~ ^ghcr\.io/jordanhubbard/mac-openshell-runtime@sha256:[0-9a-f]{64}$ ]] || {
      echo "invalid immutable OSH_RUNTIME_IMAGE_REF" >&2
      exit 2
    }
    runtime_digest="${OSH_RUNTIME_IMAGE_REF##*@sha256:}"
    [ "${#runtime_digest}" -eq 64 ] || {
      echo "invalid immutable OSH_RUNTIME_IMAGE_REF" >&2
      exit 2
    }
    [[ "$OSH_RUNTIME_INPUT_SHA256" =~ ^sha256:[0-9a-f]{64}$ ]] || {
      echo "digest-managed runtime requires exact OSH_RUNTIME_INPUT_SHA256" >&2
      exit 2
    }
    runtime_config="$(mktemp -d)"
    printf '{}' > "$runtime_config/config.json"
    log "pulling reviewed runtime $OSH_RUNTIME_IMAGE_REF"
    if ! DOCKER_CONFIG="$runtime_config" "$OSH_DOCKER_BIN" pull "$OSH_RUNTIME_IMAGE_REF"; then
      rm -rf "$runtime_config"
      return 1
    fi
    rm -rf "$runtime_config"
    image_revision="$("$OSH_DOCKER_BIN" image inspect \
      --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
      "$OSH_RUNTIME_IMAGE_REF" 2>/dev/null || true)"
    image_input_sha="$("$OSH_DOCKER_BIN" image inspect \
      --format '{{ index .Config.Labels "io.mac.frozen-inputs.sha256" }}' \
      "$OSH_RUNTIME_IMAGE_REF" 2>/dev/null || true)"
    if ! [[ "$image_revision" =~ ^[0-9a-f]{40}$ ]]; then
      echo "runtime image build revision label is absent or malformed" >&2
      return 1
    fi
    if [ "$image_input_sha" != "$OSH_RUNTIME_INPUT_SHA256" ]; then
      echo "runtime image frozen-input identity does not match reviewed publication" >&2
      return 1
    fi
    "$OSH_DOCKER_BIN" tag "$OSH_RUNTIME_IMAGE_REF" "$OSH_IMAGE_TAG"
    mkdir -p "$(dirname "$runtime_ref_file")"
    runtime_input_tmp="${runtime_input_file}.tmp.$$"
    printf '%s\n' "$image_input_sha" > "$runtime_input_tmp"
    chmod 600 "$runtime_input_tmp"
    mv -f "$runtime_input_tmp" "$runtime_input_file"
    runtime_build_tmp="${runtime_build_file}.tmp.$$"
    printf '%s\n' "$image_revision" > "$runtime_build_tmp"
    chmod 600 "$runtime_build_tmp"
    mv -f "$runtime_build_tmp" "$runtime_build_file"
    # This legacy marker described a locally built image whose build commit was
    # necessarily the deployed checkout. A reused publication deliberately has
    # distinct controller and image-build revisions, so retaining it would lie.
    rm -f "$image_source_sha_file"
    runtime_ref_tmp="${runtime_ref_file}.tmp.$$"
    printf '%s\n' "$OSH_RUNTIME_IMAGE_REF" > "$runtime_ref_tmp"
    chmod 600 "$runtime_ref_tmp"
    mv -f "$runtime_ref_tmp" "$runtime_ref_file"
    log "installed identical reviewed runtime as $OSH_IMAGE_TAG"
    return 0
  fi
  builder="$(cd "$(dirname "$0")" && pwd)/build-runtime-image.sh"
  log "building $OSH_IMAGE_TAG with Docker Engine/Moby from $MAC_SRC (development fallback)"
  GH_VERSION="$GH_VERSION" \
    MAC_SRC="$MAC_SRC" OSH_DOCKER_BIN="$OSH_DOCKER_BIN" \
    OSH_IMAGE_TAG="$OSH_IMAGE_TAG" MAC_IMAGE_SOURCE_SHA="$image_source_sha" \
    MAC_IMAGE_SOURCE_SHA_FILE="$image_source_sha_file" /bin/bash "$builder"
  # A successful explicit development build supersedes a prior digest-managed
  # install. Never leave a stale marker that would make the worker believe the
  # mutable local tag is still protected by the published digest.
  rm -f "$runtime_ref_file" "$runtime_input_file" "$runtime_build_file"
}

verify_supervisor_image() {
  local runtime_config version_output
  runtime_config="$(mktemp -d)"
  printf '{}' > "$runtime_config/config.json"
  log "pulling and verifying OpenShell supervisor $OSH_SUPERVISOR_IMAGE"
  if ! DOCKER_CONFIG="$runtime_config" "$OSH_DOCKER_BIN" pull "$OSH_SUPERVISOR_IMAGE" >/dev/null; then
    rm -rf "$runtime_config"
    echo "ERROR: failed to pull the reviewed OpenShell supervisor" >&2
    return 1
  fi
  rm -rf "$runtime_config"
  version_output="$("$OSH_DOCKER_BIN" run --rm "$OSH_SUPERVISOR_IMAGE" --version 2>&1 || true)"
  if [ "$version_output" != "openshell-sandbox $OPENSHELL_VERSION" ]; then
    echo "ERROR: OpenShell supervisor version mismatch: expected $OPENSHELL_VERSION, got '$version_output'" >&2
    return 1
  fi
  log "OpenShell supervisor: $version_output"
}

rollback_openclaw_promotion() {
  local host_root="$1" archive="$2" installed_workspace="$3" installed_state="$4"
  local archived_workspace="$5" archived_state="$6" failed=0
  if [ "$installed_workspace" = 1 ] && ! rm -rf "$host_root/workspace"; then
    failed=1
  fi
  if [ "$installed_state" = 1 ] && ! rm -rf "$host_root/state"; then
    failed=1
  fi
  if [ "$archived_workspace" = 1 ] \
      && ! mv -f "$archive/workspace" "$host_root/workspace"; then
    failed=1
  fi
  if [ "$archived_state" = 1 ] \
      && ! mv -f "$archive/state" "$host_root/state"; then
    failed=1
  fi
  [ "$failed" = 0 ]
}

promote_recovered_openclaw_state() {
  local recovered="$1" sandbox_name="$2" source_kind="$3"
  local host_root="$MAC_HOME/openclaw" archive staging stamp
  local archived_workspace=0 archived_state=0 installed_workspace=0 installed_state=0
  [ -d "$recovered/workspace" ] || {
    echo "ERROR: recovered OpenClaw workspace is absent for $sandbox_name" >&2
    return 1
  }
  [ -d "$recovered/state" ] || {
    echo "ERROR: recovered OpenClaw state is absent for $sandbox_name" >&2
    return 1
  }
  stamp="pre-openshell-upgrade-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  archive="$host_root/archive/$stamp"
  staging="$host_root/.upgrade-staging-$stamp"
  if ! mkdir -p "$host_root" "$host_root/archive" \
      || ! mkdir "$archive" \
      || ! mkdir "$staging" \
      || ! chmod 0700 "$host_root" "$host_root/archive" "$archive" "$staging" \
      || ! cp -rf "$recovered/workspace" "$staging/workspace" \
      || ! cp -rf "$recovered/state" "$staging/state" \
      || ! chmod -R go-rwx "$staging/workspace" "$staging/state" \
      || ! printf 'sandbox=%s\nsource=%s\nrecovered_at=%s\n' \
        "$sandbox_name" "$source_kind" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        > "$archive/recovery.txt" \
      || ! chmod 0600 "$archive/recovery.txt"; then
    rm -rf "$staging"
    echo "ERROR: could not stage recovered OpenClaw state for $sandbox_name" >&2
    return 1
  fi
  if [ -e "$host_root/workspace" ]; then
    if ! mv -f "$host_root/workspace" "$archive/workspace"; then
      rm -rf "$staging"
      echo "ERROR: could not archive the existing OpenClaw workspace" >&2
      return 1
    fi
    archived_workspace=1
  fi
  if [ -e "$host_root/state" ]; then
    if ! mv -f "$host_root/state" "$archive/state"; then
      rollback_openclaw_promotion "$host_root" "$archive" 0 0 \
        "$archived_workspace" 0 || true
      rm -rf "$staging"
      echo "ERROR: could not archive the existing OpenClaw state" >&2
      return 1
    fi
    archived_state=1
  fi
  if ! mv -f "$staging/workspace" "$host_root/workspace"; then
    rollback_openclaw_promotion "$host_root" "$archive" 0 0 \
      "$archived_workspace" "$archived_state" || true
    rm -rf "$staging"
    echo "ERROR: could not install the recovered OpenClaw workspace" >&2
    return 1
  fi
  installed_workspace=1
  if ! mv -f "$staging/state" "$host_root/state"; then
    rollback_openclaw_promotion "$host_root" "$archive" "$installed_workspace" 0 \
      "$archived_workspace" "$archived_state" || true
    rm -rf "$staging"
    echo "ERROR: could not install the recovered OpenClaw state" >&2
    return 1
  fi
  installed_state=1
  rm -rf "$staging"
  if ! chmod -R go-rwx "$host_root/workspace" "$host_root/state" \
      || ! touch "$OSH_DIR/upgrade-recovery.log" \
      || ! chmod 0600 "$OSH_DIR/upgrade-recovery.log" \
      || ! printf '%s\tsandbox=%s\tsource=%s\tarchive=%s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$sandbox_name" "$source_kind" "$archive" \
        >> "$OSH_DIR/upgrade-recovery.log"; then
    if ! rollback_openclaw_promotion "$host_root" "$archive" \
        "$installed_workspace" "$installed_state" \
        "$archived_workspace" "$archived_state"; then
      echo "ERROR: OpenClaw promotion rollback also failed; operator recovery is required" >&2
    fi
    echo "ERROR: could not finalize recovered OpenClaw state for $sandbox_name" >&2
    return 1
  fi
  log "checkpointed $sandbox_name before OpenShell upgrade ($source_kind; prior host state archived at $archive)"
}

checkpoint_openclaw_with_cli() {
  local cli="$1" sandbox_name="$2" recovered
  if ! recovered="$(mktemp -d "${TMPDIR:-/tmp}/mac-openclaw-upgrade.XXXXXX")" \
      || [ -z "$recovered" ]; then
    echo "ERROR: could not allocate an OpenClaw API checkpoint directory" >&2
    return 1
  fi
  if ! openshell_local_gateway "$cli" sandbox download \
      "$sandbox_name" /sandbox/workspace "$recovered/workspace" </dev/null \
      || ! openshell_local_gateway "$cli" sandbox download \
      "$sandbox_name" /sandbox/state "$recovered/state" </dev/null; then
    rm -rf "$recovered"
    echo "ERROR: could not checkpoint $sandbox_name through the existing OpenShell API" >&2
    return 1
  fi
  if ! promote_recovered_openclaw_state "$recovered" "$sandbox_name" "openshell-api"; then
    rm -rf "$recovered"
    return 1
  fi
  rm -rf "$recovered"
}

checkpoint_openclaw_with_docker() {
  local container_id="$1" sandbox_name="$2" recovered
  if ! recovered="$(mktemp -d "${TMPDIR:-/tmp}/mac-openclaw-docker-upgrade.XXXXXX")" \
      || [ -z "$recovered" ]; then
    echo "ERROR: could not allocate an OpenClaw Docker checkpoint directory" >&2
    return 1
  fi
  if ! "$OSH_DOCKER_BIN" cp \
      "$container_id:/sandbox/workspace" "$recovered/workspace" \
      || ! "$OSH_DOCKER_BIN" cp \
      "$container_id:/sandbox/state" "$recovered/state"; then
    rm -rf "$recovered"
    echo "ERROR: could not copy owner state from skewed OpenClaw container $container_id" >&2
    return 1
  fi
  if ! promote_recovered_openclaw_state "$recovered" "$sandbox_name" "docker-schema-recovery"; then
    rm -rf "$recovered"
    return 1
  fi
  rm -rf "$recovered"
}

write_managed_openshell_container_ids() {
  local scope="$1" output="$2"
  if [ "$scope" = all ]; then
    "$OSH_DOCKER_BIN" ps -a \
      --filter label=openshell.ai/managed-by=openshell \
      --format '{{.ID}}' > "$output"
  else
    "$OSH_DOCKER_BIN" ps \
      --filter label=openshell.ai/managed-by=openshell \
      --format '{{.ID}}' > "$output"
  fi
  if [ "$?" -ne 0 ]; then
    echo "ERROR: could not enumerate managed OpenShell containers ($scope)" >&2
    return 1
  fi
}

validate_managed_container_quiescence() {
  local container_id sandbox_name inventory
  if ! inventory="$(mktemp "${TMPDIR:-/tmp}/mac-openshell-running.XXXXXX")" \
      || [ -z "$inventory" ]; then
    echo "ERROR: could not allocate managed-container inventory" >&2
    return 1
  fi
  if ! write_managed_openshell_container_ids running "$inventory"; then
    rm -f "$inventory"
    return 1
  fi
  while IFS= read -r container_id; do
    [ -n "$container_id" ] || continue
    if ! sandbox_name="$("$OSH_DOCKER_BIN" inspect --format \
        '{{ index .Config.Labels "openshell.ai/sandbox-name" }}' \
        "$container_id")"; then
      rm -f "$inventory"
      echo "ERROR: could not inspect managed OpenShell container $container_id" >&2
      return 1
    fi
    rm -f "$inventory"
    echo "ERROR: managed OpenShell sandbox $sandbox_name is still running; refusing schema recovery" >&2
    return 1
  done < "$inventory"
  rm -f "$inventory"
}

running_openclaw_sandbox_present() {
  local container_id sandbox_name inventory
  if ! inventory="$(mktemp "${TMPDIR:-/tmp}/mac-openshell-running.XXXXXX")" \
      || [ -z "$inventory" ]; then
    echo "ERROR: could not allocate managed-container inventory" >&2
    return 2
  fi
  if ! write_managed_openshell_container_ids running "$inventory"; then
    rm -f "$inventory"
    return 2
  fi
  while IFS= read -r container_id; do
    [ -n "$container_id" ] || continue
    if ! sandbox_name="$("$OSH_DOCKER_BIN" inspect --format \
        '{{ index .Config.Labels "openshell.ai/sandbox-name" }}' \
        "$container_id")"; then
      rm -f "$inventory"
      echo "ERROR: could not inspect managed OpenShell container $container_id" >&2
      return 2
    fi
    case "$sandbox_name" in
      mac-openclaw-*) rm -f "$inventory"; return 0 ;;
    esac
  done < "$inventory"
  rm -f "$inventory"
  return 1
}

mac_owned_gateway_wrapper() {
  # Older MAC deployments predate the explicit manager marker. Preserve their
  # upgrade path only when the wrapper still has the exact reviewed binary and
  # config identity; a same-name service or a lookalike command is not enough.
  local wrapper="$OSH_DIR/run-gateway.sh"
  [ -f "$wrapper" ] && [ ! -L "$wrapper" ] \
    && grep -Fqx \
      "exec \"$BIN/openshell-gateway\" --config \"$OSH_DIR/gateway.toml\"" \
      "$wrapper"
}

mac_owned_systemd_gateway() {
  local exec_start exec_path argv environment fragment
  if ! exec_start="$(systemctl --user show openshell-gateway.service \
      --property=ExecStart --value 2>/dev/null)"; then
    return 1
  fi
  exec_path="$(printf '%s\n' "$exec_start" \
    | sed -n 's/^.*path=\([^;]*\) ; argv\[\]=.*$/\1/p')"
  argv="$(printf '%s\n' "$exec_start" \
    | sed -n 's/^.*argv\[\]=\([^;]*\) ;.*$/\1/p')"
  if [ "$exec_path" = "$OSH_DIR/run-gateway.sh" ] \
      && [ "$argv" = "$OSH_DIR/run-gateway.sh" ] \
      && mac_owned_gateway_wrapper; then
    return 0
  fi
  if [ "$exec_path" = "$BIN/openshell-gateway" ] \
      && [ "$argv" = "$BIN/openshell-gateway --config $OSH_DIR/gateway.toml" ]; then
    return 0
  fi

  # The marker is written only into MAC's exact user-unit path. It permits a
  # future wrapper evolution without weakening ownership to the generic unit
  # name alone.
  environment="$(systemctl --user show openshell-gateway.service \
    --property=Environment --value 2>/dev/null)" || return 1
  case " $environment " in
    *" MAC_OPENSH_GATEWAY_OWNER=mac "*) ;;
    *) return 1 ;;
  esac
  fragment="$(systemctl --user show openshell-gateway.service \
    --property=FragmentPath --value 2>/dev/null)" || return 1
  [ "$fragment" = "$HOME/.config/systemd/user/openshell-gateway.service" ]
}

mac_owned_supervisord_gateway() {
  local config="${1:-$OSH_GATEWAY_SUPERVISOR_CONFIG}" section environment
  [ -f "$config" ] && [ ! -L "$config" ] || return 1
  section="$(awk '
    $0 == "[program:openshell-gateway]" { owned_section = 1; print; next }
    /^\[/ { if (owned_section) exit }
    owned_section { print }
  ' "$config")" || return 1
  [ -n "$section" ] || return 1
  if printf '%s\n' "$section" \
      | grep -Fqx "command=$OSH_DIR/run-gateway.sh" \
      && printf '%s\n' "$section" \
      | grep -Fqx "directory=$OSH_DIR" \
      && mac_owned_gateway_wrapper; then
    return 0
  fi
  environment="$(printf '%s\n' "$section" | sed -n '/^environment=/p')"
  case "$environment" in
    *'MAC_OPENSH_GATEWAY_OWNER="mac"'*) return 0 ;;
    *) return 1 ;;
  esac
}

require_owned_gateway_manager_definitions() {
  # The service names are a shared host namespace. Refuse the entire Linux
  # bootstrap before it mutates the host when either name is already backed by
  # a definition that cannot be tied to MAC's exact command/config or marker.
  if command -v systemctl >/dev/null 2>&1 \
      && systemctl --user cat openshell-gateway.service >/dev/null 2>&1 \
      && ! mac_owned_systemd_gateway; then
    echo "ERROR: unowned systemd service named openshell-gateway blocks bootstrap" >&2
    return 1
  fi
  if [ -f "$OSH_GATEWAY_SUPERVISOR_CONFIG" ] \
      && ! mac_owned_supervisord_gateway; then
    echo "ERROR: unowned supervisord service named openshell-gateway blocks bootstrap" >&2
    return 1
  fi
}

stop_existing_gateway_for_schema_recovery() {
  # The API is already unreadable. Stop only a reviewed MAC gateway, then prove
  # that its manager, exact process/container, and bound endpoint are all down
  # before direct Docker recovery may remove a sandbox supervisor.
  local status status_rc=0 deadline gateway_ids gateway_id label
  local systemd_present=0 supervisord_present=0
  if [ "$(uname -s)" = Darwin ]; then
    if ! gateway_ids="$(mktemp "${TMPDIR:-/tmp}/mac-openshell-gateway.XXXXXX")" \
        || [ -z "$gateway_ids" ]; then
      echo "ERROR: could not allocate gateway-container inventory" >&2
      return 1
    fi
    if ! "$OSH_DOCKER_BIN" ps -a --filter 'name=^/openshell-gw$' \
        --format '{{.ID}}' > "$gateway_ids"; then
      rm -f "$gateway_ids"
      echo "ERROR: could not enumerate the reviewed OpenShell gateway container" >&2
      return 1
    fi
    if [ -s "$gateway_ids" ]; then
      [ "$(wc -l < "$gateway_ids" | tr -d ' ')" = 1 ] || {
        rm -f "$gateway_ids"
        echo "ERROR: multiple exact openshell-gw containers require operator recovery" >&2
        return 1
      }
      gateway_id="$(sed -n '1p' "$gateway_ids")"
      if ! label="$("$OSH_DOCKER_BIN" inspect --format \
          '{{ index .Config.Labels "mac.owner" }}:{{ index .Config.Labels "mac.kind" }}' \
          "$gateway_id")"; then
        rm -f "$gateway_ids"
        echo "ERROR: could not inspect the reviewed OpenShell gateway container" >&2
        return 1
      fi
      [ "$label" = "mac:openshell-gateway" ] || {
        rm -f "$gateway_ids"
        echo "ERROR: refusing to stop an unowned Docker container named openshell-gw" >&2
        return 1
      }
      if ! "$OSH_DOCKER_BIN" stop "$gateway_id" >/dev/null; then
        rm -f "$gateway_ids"
        echo "ERROR: could not stop the reviewed OpenShell gateway container" >&2
        return 1
      fi
    fi
    if ! "$OSH_DOCKER_BIN" ps --filter 'name=^/openshell-gw$' \
        --format '{{.ID}}' > "$gateway_ids" || [ -s "$gateway_ids" ]; then
      rm -f "$gateway_ids"
      echo "ERROR: reviewed OpenShell gateway container is still running" >&2
      return 1
    fi
    rm -f "$gateway_ids"
  else
    require_owned_gateway_manager_definitions || return 1
    if command -v systemctl >/dev/null 2>&1 \
        && systemctl --user cat openshell-gateway.service >/dev/null 2>&1; then
      systemd_present=1
    fi
    if [ -f "$OSH_GATEWAY_SUPERVISOR_CONFIG" ]; then
      supervisord_present=1
    fi
    if [ "$systemd_present" = 1 ]; then
      if ! systemctl --user stop openshell-gateway.service >/dev/null 2>&1 \
          || systemctl --user is-active --quiet openshell-gateway.service; then
        echo "ERROR: systemd did not stop the reviewed OpenShell gateway" >&2
        return 1
      fi
    fi
    if [ "$supervisord_present" = 1 ]; then
      status="$(sudo supervisorctl status openshell-gateway 2>&1)" \
        && status_rc=0 || status_rc=$?
      case "$status" in
        *RUNNING*)
          if ! sudo supervisorctl stop openshell-gateway >/dev/null 2>&1; then
            echo "ERROR: supervisord did not stop the reviewed OpenShell gateway" >&2
            return 1
          fi
          ;;
        *STOPPED*|*EXITED*|*FATAL*|*"no such process"*) ;;
        *)
          echo "ERROR: could not prove the supervisord OpenShell gateway state (status=$status_rc)" >&2
          return 1
          ;;
      esac
      status="$(sudo supervisorctl status openshell-gateway 2>&1)" \
        && status_rc=0 || status_rc=$?
      case "$status" in
        *RUNNING*)
          echo "ERROR: supervisord OpenShell gateway remains running" >&2
          return 1
          ;;
        *STOPPED*|*EXITED*|*FATAL*|*"no such process"*) ;;
        *)
          echo "ERROR: could not verify the stopped supervisord gateway (status=$status_rc)" >&2
          return 1
          ;;
      esac
    fi
    command -v pgrep >/dev/null 2>&1 || {
      echo "ERROR: pgrep is required to prove the old OpenShell gateway is stopped" >&2
      return 1
    }
    if pgrep -f -- "$BIN/openshell-gateway --config $OSH_DIR/gateway.toml" >/dev/null 2>&1 \
        && ! sudo pkill -TERM -f -- \
          "$BIN/openshell-gateway --config $OSH_DIR/gateway.toml" >/dev/null 2>&1; then
      echo "ERROR: could not terminate the exact old OpenShell gateway process" >&2
      return 1
    fi
    deadline=$((SECONDS + 30))
    while pgrep -f -- \
        "$BIN/openshell-gateway --config $OSH_DIR/gateway.toml" >/dev/null 2>&1 \
        && [ "$SECONDS" -lt "$deadline" ]; do
      sleep 1
    done
    if pgrep -f -- \
        "$BIN/openshell-gateway --config $OSH_DIR/gateway.toml" >/dev/null 2>&1; then
      echo "ERROR: exact old OpenShell gateway process remains running" >&2
      return 1
    fi
  fi
  deadline=$((SECONDS + 30))
  while "$MAC_HOME/venv/bin/python" - <<'PY' >/dev/null 2>&1
import socket
s = socket.socket()
s.settimeout(0.25)
raise SystemExit(0 if s.connect_ex(("127.0.0.1", 17670)) == 0 else 1)
PY
  do
    [ "$SECONDS" -lt "$deadline" ] || {
      echo "ERROR: old OpenShell gateway endpoint remains reachable on 127.0.0.1:17670" >&2
      return 1
    }
    sleep 1
  done
}

wait_for_empty_openshell_api_inventory() {
  local cli="$1" output="$2"
  local timeout_seconds="${3:-30}"
  if ! [[ "$timeout_seconds" =~ ^[0-9]+$ ]] \
      || [ "$timeout_seconds" -lt 1 ] \
      || [ "$timeout_seconds" -gt 30 ]; then
    echo "ERROR: OpenShell retirement timeout must be between 1 and 30 seconds" >&2
    return 2
  fi
  # Python's monotonic clock and process-group teardown provide the same hard
  # deadline on macOS and Linux without relying on a GNU `timeout` binary.
  # Each API call gets at most five seconds and the whole convergence loop gets
  # at most timeout_seconds, including the delay between valid non-empty reads.
  OPENSHELL_GATEWAY_ENDPOINT="$OPENSHELL_LOCAL_GATEWAY_ENDPOINT" \
    "$MAC_HOME/venv/bin/python" - "$cli" "$output" "$timeout_seconds" <<'PY'
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

cli = sys.argv[1]
output = Path(sys.argv[2])
timeout_seconds = int(sys.argv[3])
deadline = time.monotonic() + timeout_seconds


def stop_process_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=0.25)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=0.25)
    except subprocess.TimeoutExpired:
        pass


def fail_malformed(message):
    print(f"malformed post-retirement OpenShell inventory: {message}", file=sys.stderr)
    raise SystemExit(65)


while True:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        print(
            "OpenShell API inventory did not become empty before the "
            f"{timeout_seconds}-second retirement deadline",
            file=sys.stderr,
        )
        raise SystemExit(124)
    try:
        with output.open("wb") as stream:
            process = subprocess.Popen(
                [cli, "sandbox", "list", "--limit", "1000", "--output", "json"],
                stdin=subprocess.DEVNULL,
                stdout=stream,
                start_new_session=True,
            )
            try:
                process.wait(timeout=min(5.0, remaining))
            except subprocess.TimeoutExpired:
                stop_process_group(process)
                print(
                    "OpenShell sandbox inventory call exceeded its bounded wait",
                    file=sys.stderr,
                )
                raise SystemExit(124)
    except OSError as exc:
        print(f"could not execute the OpenShell inventory call: {exc}", file=sys.stderr)
        raise SystemExit(1)
    if process.returncode != 0:
        print(
            f"OpenShell sandbox inventory call failed with status {process.returncode}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    try:
        value = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail_malformed(str(exc))
    if not isinstance(value, list):
        fail_malformed("expected a list")
    for item in value:
        if not isinstance(item, dict):
            fail_malformed("expected objects")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            fail_malformed("missing sandbox name")
    if not value:
        raise SystemExit(0)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        continue
    time.sleep(min(0.25, remaining))
PY
}

retire_managed_sandboxes_via_api() {
  local cli="$1" inventory="$2" plan remaining containers sandbox_name action openclaw_count
  local retirement_poll_status=0
  local retirement_timeout_seconds="${3:-30}"
  if ! plan="$(mktemp "${TMPDIR:-/tmp}/mac-openshell-upgrade-plan.XXXXXX")" \
      || [ -z "$plan" ]; then
    echo "ERROR: could not allocate the API retirement plan" >&2
    return 1
  fi
  if ! "$MAC_HOME/venv/bin/python" - "$inventory" \
      "${MAC_OPENSH_EXPECTED_OPENCLAW_SANDBOX:-}" > "$plan" <<'PY'
import json
import os
import re
import sys
from pathlib import Path

value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected_openclaw = sys.argv[2]
if not isinstance(value, list):
    raise SystemExit("OpenShell sandbox inventory is not a list")
plan = []
for item in value:
    if not isinstance(item, dict):
        raise SystemExit("OpenShell sandbox inventory contains a non-object")
    name = item.get("name")
    labels = item.get("labels") or {}
    if not isinstance(name, str) or not re.fullmatch(r"mac-[A-Za-z0-9._-]+", name):
        raise SystemExit("non-MAC OpenShell sandbox blocks the gateway upgrade")
    if not isinstance(labels, dict):
        raise SystemExit("OpenShell sandbox labels are malformed")
    if str(item.get("phase") or "").strip().lower() != "ready":
        raise SystemExit("non-ready OpenShell sandbox blocks the gateway upgrade")
    if labels.get("mac.role") == "openclaw-gateway":
        if not expected_openclaw or name != expected_openclaw:
            raise SystemExit("role-labeled OpenClaw sandbox does not match the expected identity")
        action = "openclaw"
    elif name.startswith("mac-openclaw-"):
        raise SystemExit("prefix-matching sandbox lacks exact OpenClaw ownership proof")
    elif labels.get("mac.owner") == "mac":
        kind = str(labels.get("mac.kind") or "").strip()
        disposable_patterns = {
            "task": r"mac-task-[A-Za-z0-9._-]+",
            "hubverify": r"mac-hubverify-[A-Za-z0-9._-]+",
            "codingcap": r"mac-codingcap-[A-Za-z0-9._-]+",
            "runtime-smoke": r"mac-runtime-smoke-[A-Za-z0-9._-]+",
            "security-probe": r"mac-security-probe-[A-Za-z0-9._-]+",
        }
        pattern = disposable_patterns.get(kind)
        if pattern is None or re.fullmatch(pattern, name) is None:
            raise SystemExit("managed sandbox kind or identity is not disposable")
        if str(labels.get("mac.keep") or "").strip().lower() != "false":
            raise SystemExit("retained managed sandbox blocks the gateway upgrade")
        raw_pid = str(labels.get("mac.pid") or "").strip()
        try:
            pid = int(raw_pid)
            if pid <= 0:
                raise ValueError
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        except ValueError:
            raise SystemExit("invalid managed sandbox creator PID blocks the gateway upgrade")
        except PermissionError:
            raise SystemExit("live managed sandbox creator blocks the gateway upgrade")
        else:
            raise SystemExit("live managed sandbox creator blocks the gateway upgrade")
        action = "disposable"
    else:
        raise SystemExit("unowned or retained OpenShell sandbox blocks the gateway upgrade")
    plan.append((0 if action == "openclaw" else 1, action, name))
if sum(1 for _, action, _ in plan if action == "openclaw") > 1:
    raise SystemExit("multiple OpenClaw sandboxes require operator reconciliation")
for _, action, name in sorted(plan):
    print("%s\t%s" % (action, name))
PY
  then
    rm -f "$plan"
    return 1
  fi

  while IFS=$'\t' read -r action sandbox_name; do
    [ -n "$sandbox_name" ] || continue
    if [ "$action" = openclaw ]; then
      checkpoint_openclaw_with_cli "$cli" "$sandbox_name" || {
        rm -f "$plan"
        return 1
      }
    fi
    if ! openshell_local_gateway "$cli" sandbox delete "$sandbox_name" >/dev/null; then
      rm -f "$plan"
      echo "ERROR: could not retire managed sandbox $sandbox_name before gateway upgrade" >&2
      return 1
    fi
    log "requested pre-upgrade retirement of managed sandbox $sandbox_name"
  done < "$plan"
  rm -f "$plan"

  if ! remaining="$(mktemp "${TMPDIR:-/tmp}/mac-openshell-upgrade-remaining.XXXXXX")" \
      || [ -z "$remaining" ]; then
    echo "ERROR: could not allocate the post-retirement API inventory" >&2
    return 1
  fi
  wait_for_empty_openshell_api_inventory \
    "$cli" "$remaining" "$retirement_timeout_seconds" \
    || retirement_poll_status=$?
  if [ "$retirement_poll_status" -ne 0 ]; then
    rm -f "$remaining"
    case "$retirement_poll_status" in
      65) echo "ERROR: malformed post-retirement OpenShell API inventory" >&2 ;;
      124) echo "ERROR: timed out waiting for OpenShell API inventory retirement" >&2 ;;
      *) echo "ERROR: could not read the post-retirement OpenShell API inventory" >&2 ;;
    esac
    return 1
  fi
  rm -f "$remaining"
  log "OpenShell API confirmed the pre-upgrade sandbox inventory is empty"
  if ! containers="$(mktemp "${TMPDIR:-/tmp}/mac-openshell-remaining.XXXXXX")" \
      || [ -z "$containers" ]; then
    echo "ERROR: could not allocate the post-retirement container inventory" >&2
    return 1
  fi
  if ! write_managed_openshell_container_ids all "$containers"; then
    rm -f "$containers"
    return 1
  fi
  if [ -s "$containers" ]; then
    rm -f "$containers"
    echo "ERROR: OpenShell API retired its inventory but managed containers remain" >&2
    return 1
  fi
  rm -f "$containers"
}

retire_managed_sandboxes_via_docker() {
  local plan inventory container_id status sandbox_name action openclaw_count=0
  local expected_openclaw="${MAC_OPENSH_EXPECTED_OPENCLAW_SANDBOX:-}"
  if ! plan="$(mktemp "${TMPDIR:-/tmp}/mac-openshell-docker-plan.XXXXXX")" \
      || [ -z "$plan" ] \
      || ! inventory="$(mktemp "${TMPDIR:-/tmp}/mac-openshell-containers.XXXXXX")" \
      || [ -z "$inventory" ]; then
    rm -f "${plan:-}" "${inventory:-}"
    echo "ERROR: could not allocate the Docker recovery inventory" >&2
    return 1
  fi
  if ! write_managed_openshell_container_ids all "$inventory"; then
    rm -f "$plan" "$inventory"
    return 1
  fi
  while IFS= read -r container_id; do
    [ -n "$container_id" ] || continue
    if ! status="$("$OSH_DOCKER_BIN" inspect --format '{{.State.Status}}' "$container_id")" \
        || ! sandbox_name="$("$OSH_DOCKER_BIN" inspect --format \
          '{{ index .Config.Labels "openshell.ai/sandbox-name" }}' "$container_id")"; then
      rm -f "$plan" "$inventory"
      echo "ERROR: could not inspect managed OpenShell container $container_id" >&2
      return 1
    fi
    case "$status" in
      exited|created|dead) ;;
      *)
        rm -f "$plan" "$inventory"
        echo "ERROR: refusing direct recovery of non-quiescent OpenShell container $container_id" >&2
        return 1
        ;;
    esac
    # OpenShell does not propagate user labels (mac.owner/mac.keep/mac.kind) to
    # Docker.  Schema-skew recovery is therefore deliberately narrower than
    # the API path: only the historical disposable families already reviewed
    # by mac.openshell_sandbox_gc are eligible, and only after every container
    # is stopped. Any future family fails closed until explicitly reviewed.
    if [[ "$sandbox_name" =~ ^mac-(task|hubverify|codingcap|runtime-smoke|security-probe)-[A-Za-z0-9._-]+$ ]]; then
      action=disposable
    elif [ -n "$expected_openclaw" ] && [ "$sandbox_name" = "$expected_openclaw" ]; then
      action=openclaw
      openclaw_count=$((openclaw_count + 1))
    else
      rm -f "$plan" "$inventory"
      echo "ERROR: skewed OpenShell container $sandbox_name is outside the exact recovery allowlist" >&2
      return 1
    fi
    printf '%s\t%s\t%s\n' "$action" "$container_id" "$sandbox_name" >> "$plan"
  done < "$inventory"
  rm -f "$inventory"
  if [ "$openclaw_count" -gt 1 ]; then
    rm -f "$plan"
    echo "ERROR: multiple skewed OpenClaw containers require operator reconciliation" >&2
    return 1
  fi

  while IFS=$'\t' read -r action container_id sandbox_name; do
    [ -n "$container_id" ] || continue
    if [ "$action" = openclaw ]; then
      checkpoint_openclaw_with_docker "$container_id" "$sandbox_name" || {
        rm -f "$plan"
        return 1
      }
    fi
    if ! "$OSH_DOCKER_BIN" rm "$container_id" >/dev/null; then
      rm -f "$plan"
      echo "ERROR: could not remove exact skewed OpenShell container $container_id" >&2
      return 1
    fi
    log "retired schema-skewed managed sandbox $sandbox_name"
  done < "$plan"
  rm -f "$plan"

  if ! inventory="$(mktemp "${TMPDIR:-/tmp}/mac-openshell-remaining.XXXXXX")" \
      || [ -z "$inventory" ]; then
    echo "ERROR: could not allocate the final Docker recovery inventory" >&2
    return 1
  fi
  if ! write_managed_openshell_container_ids all "$inventory"; then
    rm -f "$inventory"
    return 1
  fi
  if [ -s "$inventory" ]; then
    rm -f "$inventory"
    echo "ERROR: managed OpenShell containers remain after schema recovery" >&2
    return 1
  fi
  rm -f "$inventory"
}

retire_managed_sandboxes_before_upgrade() {
  local cli="$BIN/openshell" inventory
  if ! inventory="$(mktemp "${TMPDIR:-/tmp}/mac-openshell-upgrade-inventory.XXXXXX")" \
      || [ -z "$inventory" ]; then
    echo "ERROR: could not allocate the pre-upgrade sandbox inventory" >&2
    return 1
  fi
  if [ -x "$cli" ] \
      && openshell_local_gateway "$cli" sandbox list --limit 1000 --output json > "$inventory" 2>/dev/null; then
    if running_openclaw_sandbox_present; then
      rm -f "$inventory"
      echo "ERROR: OpenClaw service is still running; stop it before OpenShell upgrade" >&2
      return 1
    else
      local openclaw_probe_rc=$?
      if [ "$openclaw_probe_rc" -ne 1 ]; then
        rm -f "$inventory"
        echo "ERROR: could not prove the running OpenClaw container inventory" >&2
        return 1
      fi
    fi
    if ! retire_managed_sandboxes_via_api "$cli" "$inventory"; then
      rm -f "$inventory"
      echo "ERROR: existing OpenShell API could not retire its managed sandboxes" >&2
      return 1
    fi
  else
    log "existing OpenShell API is unreadable; using exact-label recovery for stopped managed containers"
    # Prove quiescence before touching the gateway. Otherwise stopping it could
    # disguise a genuinely running task as exited/137 and make an unsafe direct
    # deletion appear eligible (especially during an operator drain skip).
    if ! validate_managed_container_quiescence \
        || ! stop_existing_gateway_for_schema_recovery; then
      rm -f "$inventory"
      return 1
    fi
    if ! validate_managed_container_quiescence; then
      rm -f "$inventory"
      return 1
    fi
    if ! retire_managed_sandboxes_via_docker; then
      rm -f "$inventory"
      return 1
    fi
  fi
  rm -f "$inventory"
  log "pre-upgrade OpenShell sandbox inventory is empty"
}

run_live_confinement_probe() {
  local cli="$1" name="$2" output="$3"
  local probe="$MAC_SRC/deploy/openshell/live-confinement-probe.sh"
  [ -f "$probe" ] || { echo "ERROR: missing OpenShell confinement probe: $probe" >&2; return 1; }
  rm -f "$output"
  openshell_local_gateway "$cli" sandbox delete "$name" >/dev/null 2>&1 || true
  if openshell_local_gateway "$cli" sandbox create \
      --no-auto-providers \
      --policy "$MAC_HOME/openshell-policy.yaml" \
      --name "$name" \
      --label mac.owner=mac \
      --label mac.kind=security-probe \
      --label "mac.pid=$$" \
      --label mac.keep=false \
      --from "$OSH_IMAGE_TAG" \
      --env HOME=/tmp \
      --upload "$probe:/sandbox" \
      -- /bin/bash /sandbox/live-confinement-probe.sh \
      >"$output" 2>&1; then
    openshell_local_gateway "$cli" sandbox delete "$name" >/dev/null 2>&1 || true
    grep -q '^CONFINEMENT_PROBE_OK$' "$output" \
      || { echo "ERROR: OpenShell confinement probe omitted success sentinel" >&2; return 1; }
    log "live confinement probe: filesystem/network/privilege/syscall boundaries enforced"
    return 0
  else
    rc=$?
    openshell_local_gateway "$cli" sandbox delete "$name" >/dev/null 2>&1 || true
    echo "ERROR: OpenShell live confinement probe failed; see $output" >&2
    tail -80 "$output" >&2 || true
    return "$rc"
  fi
}

# --- macOS: host install, no container runtime ------------------------------
# The managed OpenShell runtime is Linux-only (ADR 0015). macOS fleet nodes run
# the agent as a plain host application: there is no gateway container, no
# Docker Desktop requirement, and no runtime image to build or pull. This exit
# is deliberately successful -- a macOS node with no OpenShell is correctly
# provisioned, not broken -- and it is the same state fleet-node-install.sh
# reaches on its "optional OpenShell runtime disabled" path.
if [ "$(uname -s)" = "Darwin" ]; then
  log "macOS host install: the managed OpenShell runtime is Linux-only (ADR 0015); nothing to bootstrap"
  log "isolation posture on this node is macos_host: a standard macOS application, with no container, VM, seccomp filter or egress proxy"
  exit 0
fi

# Collision detection deliberately precedes directory creation, package
# installation, image pulls, sandbox retirement, and every manager rewrite.
require_owned_gateway_manager_definitions || exit $?
mkdir -p "$OSH_DIR" "$BIN"

# --- Docker Engine/Moby + GPU detection -------------------------------------
[ "$OSH_GPU" = auto ] && { command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1 && OSH_GPU=yes || OSH_GPU=no; }
if [ "${OSH_DRIVER:-docker}" != docker ]; then
  echo "OSH_DRIVER is no longer supported; MAC/OpenShell uses Docker Engine/Moby only" >&2
  exit 2
fi

install_docker_engine() {
  if command -v apt-get >/dev/null; then
    log "installing Docker Engine/Moby from distro packages (docker.io)"
    sudo apt-get update
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io acl
    return 0
  fi
  echo "Docker Engine/Moby is required, but '$OSH_DOCKER_BIN' is missing and this host has no supported package installer" >&2
  echo "Install an OSS Docker Engine/Moby daemon, then rerun this bootstrap." >&2
  exit 1
}

replace_podman_docker_shim() {
  if command -v apt-get >/dev/null; then
    log "replacing podman-docker compatibility shim with Docker Engine/Moby"
    if dpkg -s podman-docker >/dev/null 2>&1; then
      sudo DEBIAN_FRONTEND=noninteractive apt-get remove -y podman-docker
    fi
    install_docker_engine
    return 0
  fi
  return 1
}

ensure_docker_engine() {
  if ! command -v "$OSH_DOCKER_BIN" >/dev/null 2>&1; then
    install_docker_engine
  fi
  docker_version="$("$OSH_DOCKER_BIN" --version 2>/dev/null || true)"
  case "$docker_version" in
    *[Pp]odman*)
      if ! replace_podman_docker_shim; then
        echo "'$OSH_DOCKER_BIN' resolves to a Podman compatibility shim, not Docker Engine/Moby: $docker_version" >&2
        echo "Remove podman-docker or set OSH_DOCKER_BIN to a real Docker Engine/Moby CLI." >&2
        exit 1
      fi
      ;;
  esac
  docker_version="$("$OSH_DOCKER_BIN" --version 2>/dev/null || true)"
  case "$docker_version" in
    *[Pp]odman*)
      echo "'$OSH_DOCKER_BIN' is still a Podman compatibility shim after remediation: $docker_version" >&2
      exit 1
      ;;
  esac
  if command -v systemctl >/dev/null; then
    sudo systemctl enable --now docker >/dev/null 2>&1 || true
  fi
  if ! "$OSH_DOCKER_BIN" info >/dev/null 2>&1; then
    if getent group docker >/dev/null 2>&1; then
      sudo usermod -aG docker "$USER" >/dev/null 2>&1 || true
    fi
    if command -v setfacl >/dev/null && [ -S /var/run/docker.sock ]; then
      sudo setfacl -m "u:$(id -u):rw" /var/run/docker.sock >/dev/null 2>&1 || true
    fi
  fi
  if ! "$OSH_DOCKER_BIN" info >/dev/null 2>&1; then
    echo "Docker Engine/Moby is installed but this user cannot reach the daemon." >&2
    echo "Ensure docker.service is running and '$USER' can read/write /var/run/docker.sock, then rerun." >&2
    exit 1
  fi
}

ensure_docker_buildx() {
  local package=""
  if "$OSH_DOCKER_BIN" buildx version >/dev/null 2>&1; then
    return 0
  fi
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "Docker Buildx is required for repository contract tests, but this host has no supported package installer." >&2
    exit 1
  fi

  log "installing the Docker Buildx CLI plugin required by repository contract tests"
  sudo apt-get update
  for candidate in docker-buildx docker-buildx-plugin; do
    if apt-cache show "$candidate" >/dev/null 2>&1; then
      package="$candidate"
      break
    fi
  done
  if [ -z "$package" ]; then
    echo "Docker is installed but neither docker-buildx nor docker-buildx-plugin is available from configured apt repositories." >&2
    exit 1
  fi
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "$package"
  if ! "$OSH_DOCKER_BIN" buildx version >/dev/null 2>&1; then
    echo "Installed '$package', but '$OSH_DOCKER_BIN buildx version' still fails." >&2
    exit 1
  fi
}

ensure_docker_engine
ensure_docker_buildx
log "arch=$ARCH gpu=$OSH_GPU driver=docker-engine version=$OPENSHELL_VERSION docker=$("$OSH_DOCKER_BIN" --version 2>&1 | head -1) buildx=$("$OSH_DOCKER_BIN" buildx version 2>&1 | head -1)"

ensure_openshell_docker_bridge() {
  local network_name="openshell-docker" network_id="" network_driver="" bridge_iface=""
  if ! "$OSH_DOCKER_BIN" network inspect "$network_name" >/dev/null 2>&1; then
    "$OSH_DOCKER_BIN" network create --driver bridge \
      --label mac.owner=mac --label mac.kind=openshell-gateway \
      "$network_name" >/dev/null
  fi
  network_driver="$("$OSH_DOCKER_BIN" network inspect \
    --format '{{.Driver}}' "$network_name")"
  [ "$network_driver" = bridge ] || {
    echo "OpenShell Docker network must use the bridge driver" >&2
    return 1
  }
  network_id="$("$OSH_DOCKER_BIN" network inspect \
    --format '{{.Id}}' "$network_name")"
  [[ "$network_id" =~ ^[0-9a-f]{64}$ ]] || {
    echo "OpenShell Docker network has an invalid immutable ID" >&2
    return 1
  }
  bridge_iface="br-${network_id:0:12}"
  ip link show "$bridge_iface" >/dev/null 2>&1 || {
    echo "OpenShell Docker bridge interface is unavailable: $bridge_iface" >&2
    return 1
  }
  printf '%s\n' "$bridge_iface"
}

stop_gateway_fail_closed() {
  if command -v systemctl >/dev/null 2>&1 \
      && systemctl --user cat openshell-gateway.service >/dev/null 2>&1; then
    if mac_owned_systemd_gateway; then
      systemctl --user stop openshell-gateway.service >/dev/null 2>&1 || true
    else
      echo "WARNING: left unowned systemd service openshell-gateway untouched" >&2
    fi
  fi
  if [ -f "$OSH_GATEWAY_SUPERVISOR_CONFIG" ]; then
    if mac_owned_supervisord_gateway; then
      sudo supervisorctl stop openshell-gateway >/dev/null 2>&1 || true
    else
      echo "WARNING: left unowned supervisord service openshell-gateway untouched" >&2
    fi
  fi
  sudo pkill -f -- \
    "$BIN/openshell-gateway --config $OSH_DIR/gateway.toml" >/dev/null 2>&1 || true
}

# A previous deployment may still be running with an older, narrower firewall.
# Stop it before downloads or image builds so bootstrap latency never extends an
# unauthenticated mesh exposure window. The gateway is restarted only after the
# strict current rule and its persistence manager have both passed.
retire_managed_sandboxes_before_upgrade || exit $?
stop_gateway_fail_closed
verify_supervisor_image || exit $?

mirror_image_for_openshell_runtime() {
  # OpenShell 0.0.62 accepts a Docker-driver config but the deployed gateway
  # still logs `openshell_driver_podman` and reads its runtime image from the
  # user's Podman store. Docker Engine/Moby remains the authoritative build
  # path; this compatibility mirror prevents a stale runtime-visible image from
  # silently defeating the deployment. The smoke test below proves which image
  # OpenShell actually executes.
  if command -v podman >/dev/null 2>&1; then
    log "mirroring $OSH_IMAGE_TAG into OpenShell's runtime-visible image store"
    "$OSH_DOCKER_BIN" image save "$OSH_IMAGE_TAG" | podman load >/dev/null
  fi
}

# A GKE worker pod inherits a CDI spec listing driver files that its container
# runtime cannot bind-mount, and ANY such entry stops every GPU container dead:
#
#   error during container init: error mounting "<path>" to rootfs ...
#   no such file or directory
#
# Measured on all five GKE workers 2026-08-05: a real RTX PRO 6000 MIG device
# on every node, and not one could start a GPU container. Plain
# `docker run --gpus all` failed identically, so this is not an OpenShell bug.
# Five GPU nodes ran as CPU-only nodes and nothing said so, because the GPU
# smoke below only warns.
#
# The paths are visible to this shell yet unmountable by the daemon -- dockerd
# does not share our mount namespace -- so "does the path exist" is the wrong
# test, and `nvidia-ctk cdi generate` is the wrong fix: regenerating on worker1
# put the bad entries straight back, because nvidia-ctk resolves them in ITS
# view. The only authority on what the runtime can mount is the runtime, so ask
# it: start a GPU container, and when it names a mount it cannot make, drop
# that entry and try again.
#
# Removing an entry the runtime has just refused cannot lose capability -- the
# container was not going to start at all -- and every removal is logged. On
# worker1 this converged in two removals, after which nvidia-smi inside the
# container reported the full MIG device.
#
# Pod filesystems are recreated from an image, so this has to run on every
# bring-up rather than once by hand.
prune_unmountable_cdi_entries() {
  local spec=/etc/cdi/nvidia.yaml
  [ -f "$spec" ] || return 0
  command -v "$OSH_DOCKER_BIN" >/dev/null 2>&1 || return 0

  local sudo_cmd=""
  if [ ! -w "$spec" ]; then
    if sudo -n true 2>/dev/null; then
      sudo_cmd="sudo"
    else
      log "WARNING: $spec is not writable and sudo is unavailable; cannot repair GPU mounts"
      return 0
    fi
  fi

  local probe_log="$OSH_DIR/cdi-mount-repair.log"
  local attempt removed=0 bad=""
  rm -f "$probe_log"
  for attempt in $(seq 1 12); do
    if "$OSH_DOCKER_BIN" run --rm --gpus all "$OSH_IMAGE_TAG" true \
        >>"$probe_log" 2>&1; then
      [ "$removed" -eq 0 ] \
        || log "GPU mount repair: dropped $removed unmountable CDI entr(ies); GPU containers start"
      return 0
    fi
    bad="$(grep -oE 'error mounting "[^"]+"' "$probe_log" | tail -1 \
      | sed 's/error mounting //; s/"//g')"
    if [ -z "$bad" ]; then
      # Not a mount problem: leave the spec alone and let the smoke report it.
      log "GPU probe failed for a non-mount reason; leaving $spec untouched (see $probe_log)"
      return 0
    fi
    # Auxiliary files (a persistenced socket, GSP firmware) are safe to drop --
    # that is what was actually stale on the GKE workers. The CUDA/NVML driver
    # libraries are not: removing those would leave a container that STARTS with
    # no working GPU, converting a loud failure into a silent one. If the
    # runtime cannot mount those, the node is genuinely broken and must say so.
    case "$bad" in
      *libcuda*|*libnvidia*|*libcudart*|*libnvml*)
        log "WARNING: the container runtime cannot mount driver library $bad; refusing to remove it, because a GPU container without it would start and then not work. This node needs operator attention (see $probe_log)"
        return 0
        ;;
    esac
    [ "$removed" -eq 0 ] && $sudo_cmd cp -n "$spec" "$spec.mac-bak" 2>/dev/null
    log "GPU mount repair: the container runtime cannot mount $bad; removing it from $spec"
    $sudo_cmd python3 - "$spec" "$bad" <<'CDIPRUNE'
import pathlib
import sys

spec = pathlib.Path(sys.argv[1])
needle = "hostPath: %s" % sys.argv[2]
lines = spec.read_text().splitlines(keepends=True)
out, index = [], 0
while index < len(lines):
    if needle in lines[index]:
        # Drop this list item: its hostPath line plus continuation lines, up to
        # the next sibling item or any dedent.
        indent = len(lines[index]) - len(lines[index].lstrip())
        index += 1
        while index < len(lines):
            stripped = lines[index].lstrip()
            current = len(lines[index]) - len(stripped)
            if current < indent or (stripped.startswith("- ") and current <= indent):
                break
            index += 1
        continue
    out.append(lines[index])
    index += 1
spec.write_text("".join(out))
CDIPRUNE
    removed=$((removed + 1))
    : > "$probe_log"
  done
  log "WARNING: GPU mounts still unrepaired after $removed removals; the smoke below will report it"
}

gpu_runtime_available=0
validate_openshell_runtime_image() {
  [ "$DO_ENABLE" = 1 ] || return 0
  smoke_name="mac-runtime-smoke-$$"
  smoke_log="$OSH_DIR/runtime-image-smoke.log"
  rm -f "$smoke_log"
  if openshell_local_gateway "$BIN/openshell" sandbox create \
      --no-auto-providers \
      --policy "$MAC_HOME/openshell-policy.yaml" \
      --name "$smoke_name" \
      --label mac.owner=mac \
      --label mac.kind=runtime-smoke \
      --label "mac.pid=$$" \
      --label mac.keep=false \
      --from "$OSH_IMAGE_TAG" \
      --env HOME=/tmp \
      -- /bin/bash -c 'set -euo pipefail; /usr/local/bin/mac-verify-bash-contract; command -v gh; gh --version | head -1; command -v codex; codex --version; command -v claude; claude --version | grep -F 2.1.220; command -v cursor-agent; cursor-agent --version | grep -F 2026.07.23-e383d2b; /usr/local/lib/docker/cli-plugins/docker-buildx version | grep -F v0.30.1; /opt/mac-venv/bin/python -c "import mac.agent_command"' \
      > "$smoke_log" 2>&1; then
    openshell_local_gateway "$BIN/openshell" sandbox delete "$smoke_name" >/dev/null 2>&1 || true
    log "runtime image smoke: Bash >=5.2 plus gh/codex/claude/cursor-agent/buildx visible through OpenShell"
  else
    rc=$?
    openshell_local_gateway "$BIN/openshell" sandbox delete "$smoke_name" >/dev/null 2>&1 || true
    echo "ERROR: OpenShell runtime image smoke failed; see $smoke_log" >&2
    tail -80 "$smoke_log" >&2 || true
    exit "$rc"
  fi
  if [ "$OSH_GPU" = yes ]; then
    prune_unmountable_cdi_entries
    gpu_smoke_name="mac-gpu-smoke-$$"
    gpu_smoke_log="$OSH_DIR/runtime-gpu-smoke.log"
    rm -f "$gpu_smoke_log"
    if openshell_local_gateway "$BIN/openshell" sandbox create \
        --no-auto-providers \
        --policy "$MAC_HOME/openshell-policy.yaml" \
        --name "$gpu_smoke_name" \
        --label mac.owner=mac \
        --label mac.kind=gpu-smoke \
        --label "mac.pid=$$" \
        --label mac.keep=false \
        --from "$OSH_IMAGE_TAG" \
        --gpu \
        -- /bin/true > "$gpu_smoke_log" 2>&1; then
      gpu_runtime_available=1
      log "OpenShell GPU smoke passed; GPU tasks may request accelerator access"
    else
      log "WARNING: host GPU detected but nested OpenShell GPU smoke failed; CPU tasks remain enabled"
      tail -40 "$gpu_smoke_log" >&2 || true
    fi
    openshell_local_gateway "$BIN/openshell" sandbox delete "$gpu_smoke_name" >/dev/null 2>&1 || true
  fi
  run_live_confinement_probe "$BIN/openshell" "mac-security-probe-$$" \
    "$OSH_DIR/live-confinement-probe.log"
}

# --- 1. openshell CLI (reviewed static release asset) -----------------------
install_openshell_cli_static() {
  case "$ARCH" in
    x86_64)  ca="openshell-x86_64-unknown-linux-musl.tar.gz"; ca_sha="$OSH_CLI_LINUX_AMD64_SHA256";;
    aarch64) ca="openshell-aarch64-unknown-linux-musl.tar.gz"; ca_sha="$OSH_CLI_LINUX_ARM64_SHA256";;
    *) echo "unsupported arch $ARCH" >&2; exit 1;;
  esac
  url="https://github.com/NVIDIA/OpenShell/releases/download/v$OPENSHELL_VERSION/$ca"
  log "fetching static openshell CLI: $url"
  tmp="$(mktemp -d)"
  download -o "$tmp/openshell.tgz" "$url"
  verify_sha256 "$tmp/openshell.tgz" "$ca_sha"
  chmod 0600 "$tmp/openshell.tgz"
  install_reviewed_openshell_archive "$tmp/openshell.tgz"
  rm -f "$BIN/openshell"
  install -m755 "$MAC_HOME/bin/openshell" "$BIN/openshell"
  rm -rf "$tmp"
}

log "installing reviewed openshell CLI $OPENSHELL_VERSION"
install_openshell_cli_static
log "openshell CLI: $(openshell --version 2>&1 | head -1)"

# --- 2. openshell-gateway daemon (prebuilt per-arch release asset) ----------
install_openshell_gateway() {
  case "$ARCH" in
    x86_64)  ga="openshell-gateway-x86_64-unknown-linux-gnu.tar.gz"; ga_sha="$OSH_GATEWAY_LINUX_AMD64_SHA256";;
    aarch64) ga="openshell-gateway-aarch64-unknown-linux-gnu.tar.gz"; ga_sha="$OSH_GATEWAY_LINUX_ARM64_SHA256";;
    *) echo "unsupported arch $ARCH" >&2; exit 1;;
  esac
  url="https://github.com/NVIDIA/OpenShell/releases/download/v$OPENSHELL_VERSION/$ga"
  log "fetching gateway: $url"
  tmp="$(mktemp -d)"; download -o "$tmp/gw.tgz" "$url"
  verify_sha256 "$tmp/gw.tgz" "$ga_sha"
  tar -xzf "$tmp/gw.tgz" -C "$tmp"
  install -m755 "$(find "$tmp" -name openshell-gateway -type f | head -1)" "$BIN/openshell-gateway"
  rm -rf "$tmp"
}

log "installing reviewed openshell-gateway $OPENSHELL_VERSION"
install_openshell_gateway
log "gateway bin: $(openshell-gateway --version 2>&1 | head -1)"

# --- 3. GPU: refresh the CDI spec to the current driver ---------------------
if [ "$OSH_GPU" = yes ]; then
  log "regenerating CDI spec for driver $(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)"
  sudo mkdir -p /etc/cdi && sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml >/dev/null 2>&1 || true
fi

# --- 4. mac-hermes image (native build; multi-arch Containerfile) -----------
if [ "$SKIP_IMAGE" = 0 ]; then
  build_runtime_image
  mirror_image_for_openshell_runtime
fi

# --- 5. JWT signing keys ----------------------------------------------------
[ -f "$OSH_DIR/pki/jwt/signing.pem" ] || openshell-gateway generate-certs --output-dir "$OSH_DIR/pki" >/dev/null 2>&1
log "jwt keys: $(ls "$OSH_DIR/pki/jwt" 2>/dev/null | tr '\n' ' ')"

# Own the one user-defined bridge used by OpenShell before exposing the
# unauthenticated local gateway. The firewall below embeds this exact interface
# and therefore does not grant gateway access to unrelated local containers.
OPENSH_BRIDGE_IFACE="$(ensure_openshell_docker_bridge)" || exit $?
log "OpenShell Docker bridge: $OPENSH_BRIDGE_IFACE"

# --- 6. gateway.toml (Docker driver) ----------------------------------------
cat > "$OSH_DIR/gateway.toml" <<EOF
[openshell]
version = 1
[openshell.gateway]
bind_address = "0.0.0.0:17670"
log_level = "info"
compute_drivers = ["docker"]
disable_tls = true
[openshell.gateway.auth]
allow_unauthenticated_users = true
[openshell.gateway.gateway_jwt]
signing_key_path = "$OSH_DIR/pki/jwt/signing.pem"
public_key_path = "$OSH_DIR/pki/jwt/public.pem"
kid_path = "$OSH_DIR/pki/jwt/kid"
[openshell.drivers.docker]
default_image = "$OSH_IMAGE_TAG"
supervisor_image = "$OSH_SUPERVISOR_IMAGE"
network_name = "openshell-docker"
grpc_endpoint = "http://host.openshell.internal:17670"
image_pull_policy = "IfNotPresent"
EOF
cat > "$OSH_DIR/run-gateway.sh" <<EOF
#!/usr/bin/env sh
# mac.owner=mac; mac.kind=openshell-gateway
# This gateway is explicitly configured for Docker-in-Docker. Kubernetes injects
# these variables into every pod; leaving them set makes OpenShell assume the
# Kubernetes driver must also be configured and abort before listening.
unset KUBERNETES_SERVICE_HOST KUBERNETES_SERVICE_PORT KUBERNETES_PORT
exec "$BIN/openshell-gateway" --config "$OSH_DIR/gateway.toml"
EOF
chmod 700 "$OSH_DIR/run-gateway.sh"

# --- 7. firewall :17670 (loopback + exact OpenShell bridge; persistent) ------
# The gateway intentionally listens on 0.0.0.0 because Docker supervisors must
# reach it through a bridge. Blocking only the default NIC left mesh interfaces
# (for example tailscale0) exposed. Route every gateway packet through a
# dedicated chain: loopback and only the exact openshell-docker bridge return
# to the caller; every other interface is dropped. SSH local forwarding still
# reaches 127.0.0.1.
#
# Install and prove this policy *before* starting or restarting the unauthenticated
# gateway. A persistence-manager failure also aborts the bootstrap instead of
# leaving a reachable daemon behind.
sudo tee /usr/local/sbin/mac-openshell-firewall.sh >/dev/null <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
chain=MAC_OPENSH_GW
bridge_iface=__OPENSH_BRIDGE_IFACE__
ip link show "$bridge_iface" >/dev/null
configured=0
for ipt in iptables ip6tables; do
  command -v "$ipt" >/dev/null || continue
  configured=$((configured + 1))
  "$ipt" -N "$chain" 2>/dev/null || true
  "$ipt" -F "$chain"
  "$ipt" -A "$chain" -i lo -j RETURN
  "$ipt" -A "$chain" -i "$bridge_iface" -j RETURN
  "$ipt" -A "$chain" -j DROP
  "$ipt" -C INPUT -p tcp --dport 17670 -j "$chain" 2>/dev/null \
    || "$ipt" -I INPUT 1 -p tcp --dport 17670 -j "$chain"
  "$ipt" -C "$chain" -i lo -j RETURN
  "$ipt" -C "$chain" -i "$bridge_iface" -j RETURN
  "$ipt" -C "$chain" -j DROP
  "$ipt" -C INPUT -p tcp --dport 17670 -j "$chain"
done
[ "$configured" -gt 0 ] || {
  echo "neither iptables nor ip6tables is available" >&2
  exit 1
}
EOF
sudo sed -i "s/__OPENSH_BRIDGE_IFACE__/$OPENSH_BRIDGE_IFACE/" \
  /usr/local/sbin/mac-openshell-firewall.sh
sudo chmod +x /usr/local/sbin/mac-openshell-firewall.sh

if ! sudo /usr/local/sbin/mac-openshell-firewall.sh; then
  stop_gateway_fail_closed
  echo "ERROR: refusing to run the unauthenticated OpenShell gateway without its firewall" >&2
  exit 1
fi
if command -v systemctl >/dev/null 2>&1 && sudo systemctl show-environment >/dev/null 2>&1; then
  sudo tee /etc/systemd/system/mac-openshell-firewall.service >/dev/null <<'EOF'
[Unit]
Description=Block external access to the OpenShell gateway (:17670)
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/mac-openshell-firewall.sh
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable mac-openshell-firewall.service >/dev/null 2>&1
  if ! sudo systemctl restart mac-openshell-firewall.service >/dev/null 2>&1 \
      || ! sudo systemctl is-active --quiet mac-openshell-firewall.service; then
    stop_gateway_fail_closed
    echo "ERROR: OpenShell firewall persistence service failed" >&2
    exit 1
  fi
elif command -v supervisorctl >/dev/null 2>&1; then
  sudo tee /etc/supervisor/conf.d/mac-openshell-firewall.conf >/dev/null <<'EOF'
[program:mac-openshell-firewall]
command=/usr/local/sbin/mac-openshell-firewall.sh
user=root
autostart=true
autorestart=false
startsecs=0
exitcodes=0
stdout_logfile=/var/log/mac-openshell-firewall.log
redirect_stderr=true
EOF
  if ! sudo supervisorctl reread >/dev/null \
      || ! sudo supervisorctl update >/dev/null \
      || ! sudo /usr/local/sbin/mac-openshell-firewall.sh; then
    stop_gateway_fail_closed
    echo "ERROR: OpenShell firewall persistence configuration failed" >&2
    exit 1
  fi
else
  stop_gateway_fail_closed
  echo "ERROR: OpenShell firewall requires systemd or supervisord persistence" >&2
  exit 1
fi
firewall_jumps="$(sudo iptables -S INPUT 2>/dev/null | grep -c MAC_OPENSH_GW || true)"
[ "$firewall_jumps" -gt 0 ] || {
  stop_gateway_fail_closed
  echo "ERROR: OpenShell firewall jump is absent after installation" >&2
  exit 1
}
log "firewall: loopback + $OPENSH_BRIDGE_IFACE only on :17670 ($firewall_jumps jumps)"

# --- 8. gateway service + register ------------------------------------------
gateway_manager=""
gateway_state="unknown"
# Recheck at the replacement boundary so a definition introduced during image
# preparation cannot be overwritten based on an earlier ownership decision.
require_owned_gateway_manager_definitions || exit $?
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/openshell-gateway.service" <<EOF
[Unit]
Description=OpenShell gateway (Docker Engine/Moby driver)
After=network-online.target
Wants=network-online.target
[Service]
ExecStart=$OSH_DIR/run-gateway.sh
Environment=MAC_OPENSH_GATEWAY_OWNER=mac
Restart=on-failure
RestartSec=5
[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable openshell-gateway >/dev/null 2>&1
  if ! systemctl --user restart openshell-gateway >/dev/null 2>&1 \
      || ! systemctl --user is-active --quiet openshell-gateway; then
    stop_gateway_fail_closed
    echo "ERROR: OpenShell gateway systemd service failed" >&2
    exit 1
  fi
  gateway_manager="systemd-user"
  gateway_state="$(systemctl --user is-active openshell-gateway)"
elif command -v supervisorctl >/dev/null 2>&1; then
  sudo tee "$OSH_GATEWAY_SUPERVISOR_CONFIG" >/dev/null <<EOF
[program:openshell-gateway]
command=$OSH_DIR/run-gateway.sh
directory=$OSH_DIR
user=$USER
environment=MAC_OPENSH_GATEWAY_OWNER="mac",HOME="$HOME",PATH="$BIN:/usr/local/bin:/usr/bin:/bin"
autostart=true
autorestart=true
startsecs=2
stopasgroup=true
killasgroup=true
stdout_logfile=$OSH_DIR/gateway-supervisor.log
redirect_stderr=true
EOF
  sudo supervisorctl reread >/dev/null
  sudo supervisorctl update >/dev/null
  if ! sudo supervisorctl restart openshell-gateway >/dev/null 2>&1 \
      && ! sudo supervisorctl start openshell-gateway >/dev/null 2>&1; then
    stop_gateway_fail_closed
    echo "ERROR: OpenShell gateway supervisor service failed" >&2
    exit 1
  fi
  gateway_manager="supervisord"
  gateway_state="$(sudo supervisorctl status openshell-gateway 2>/dev/null | awk '{print tolower($2)}')"
  [ "$gateway_state" = running ] || {
    stop_gateway_fail_closed
    echo "ERROR: OpenShell gateway is not running under supervisord" >&2
    exit 1
  }
else
  stop_gateway_fail_closed
  echo "ERROR: OpenShell gateway requires a working systemd user manager or supervisord" >&2
  exit 1
fi
if ! wait_for_local_gateway "$BIN/openshell" \
    || ! register_and_select_local_gateway "$BIN/openshell"; then
  stop_gateway_fail_closed
  echo "ERROR: OpenShell gateway did not pass its local status probe" >&2
  exit 1
fi
log "gateway: manager=$gateway_manager state=$gateway_state | listening: $(ss -tlnp 2>/dev/null | grep -c :17670 || true)"

# --- 9. policy (egress to this node's hub) ----------------------------------
HUB_URL="${OSH_HUB_URL:-$(grep -oE '^MAC_HUB_URL=[^ ]+' "$ENVF" 2>/dev/null | head -1 | cut -d= -f2-)}"
HUB_HOST="$(printf '%s' "$HUB_URL" | sed -E 's#^https?://##; s#[:/].*##')"
case "$HUB_HOST" in 127.0.0.1|localhost|0.0.0.0|"") POLICY_HUB=host.openshell.internal;; *) POLICY_HUB="$HUB_HOST";; esac
HUB_PORT="$(printf '%s' "$HUB_URL" | grep -oE ':[0-9]+' | tr -d : | head -1)"; HUB_PORT="${HUB_PORT:-8789}"
log "policy egress hub: $POLICY_HUB:$HUB_PORT"
"$MAC_HOME/venv/bin/mac" admin openshell render-policy \
  --template "$(cd "$(dirname "$0")" && pwd)/mac-hermes-policy.yaml" \
  --agent-user "$USER" --hub-host "$POLICY_HUB" --hub-port "$HUB_PORT" \
  --image-runtime /opt/mac-venv --into "$MAC_HOME/openshell-policy.yaml" >/dev/null
chmod 600 "$MAC_HOME/openshell-policy.yaml"

# --- 10. rewritten hermes config for the sandbox ----------------------------
# Normalize the mac-hub gateway endpoint (hub port) to THIS node's current hub
# authority ($POLICY_HUB): loopback -> host.openshell.internal, and a STALE hub
# host (e.g. a pre-migration tailnet IP) -> the live hub instead of passing
# through. Non-hub-port providers (other services, external APIs) are untouched.

# --- 11. env recipe in mac.env (quoted — mac.env is shell-sourced) ----------
validate_openshell_runtime_image
codex_uploads=""
if truthy "${MAC_OPENSHELL_UPLOAD_CODEX_AUTH:-0}"; then
  if [ -s "$HOME/.codex/auth.json" ]; then
    codex_uploads="$codex_uploads --upload $HOME/.codex/auth.json:/tmp/.codex/auth.json"
  fi
  # Deliberately NOT uploading config.toml: its top-level model pin is
  # specific to the operator workstation's codex version and breaks a worker
  # on an older codex ("model X requires a newer version of Codex"). auth.json
  # is the portable credential; the fleet sets the model per task via --model.
  # (mac admin fleet creds-sync ships a model-pin-stripped config.toml when custom
  # provider config is genuinely needed.)
else
  log "codex file auth upload: disabled (rotating OAuth state is not durable in throwaway sandboxes)"
fi
cp -a "$ENVF" "$ENVF.bak-openshell-$(date +%Y%m%dT%H%M%S 2>/dev/null || echo bootstrap)"
sed -i '/^# OpenShell sandbox enforcement/d;/^MAC_OPENSHELL_SANDBOX=/d;/^MAC_OPENSHELL_GC=/d;/^MAC_OPENSHELL_STALE_AFTER_SECONDS=/d;/^MAC_HERMES_PYTHON=/d;/^MAC_OPENSHELL_POLICY=/d;/^MAC_OPENSHELL_BIN=/d;/^MAC_OPENSHELL_CREATE_ARGS=/d;/^MAC_OPENSHELL_GPU_AVAILABLE=/d;/^MAC_ALLOW_UNSANDBOXED_YOLO=/d;/^MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT=/d' "$ENVF"
sandbox_image_ref="${OSH_RUNTIME_IMAGE_REF:-$OSH_IMAGE_TAG}"
{
  echo ""
  echo "# OpenShell sandbox enforcement (bootstrap-openshell.sh; Docker Engine/Moby driver, gpu=$OSH_GPU)"
  echo "MAC_OPENSHELL_SANDBOX=$DO_ENABLE"
  echo "MAC_OPENSHELL_GC=1"
  echo "MAC_OPENSHELL_STALE_AFTER_SECONDS=86400"
  echo "MAC_OPENSHELL_POLICY=$MAC_HOME/openshell-policy.yaml"
  echo "MAC_OPENSHELL_BIN=$BIN/openshell"
  echo "MAC_OPENSHELL_CREATE_ARGS=\"--from $sandbox_image_ref$codex_uploads\""
  echo "MAC_OPENSHELL_GPU_AVAILABLE=$gpu_runtime_available"
  echo "MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT=1"
  [ "$DO_FAILCLOSED" = 1 ] && echo "MAC_ALLOW_UNSANDBOXED_YOLO=0"
} >> "$ENVF"
# sanity: mac.env must still source cleanly (quoting)
( set -a; . "$ENVF" >/dev/null 2>&1; ) || { echo "ERROR: mac.env failed to source after edit" >&2; exit 1; }
clear_repo_update_dispatch_blocker

log "DONE. sandbox-enabled=$DO_ENABLE fail-closed=$DO_FAILCLOSED"
if [ "$DO_ENABLE" = 1 ]; then
  if [ "$gateway_manager" = "supervisord" ]; then
    log "restart the agent to apply: sudo supervisorctl restart mac-agent (then validate a real task)"
  else
    log "restart the agent to apply: sudo systemctl restart mac-agent.service (then validate a real task)"
  fi
else
  log "set up but NOT enforcing yet; re-run with --enable (then --fail-closed once a real task validates)"
fi
