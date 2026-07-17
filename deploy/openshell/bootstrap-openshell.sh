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
#   CODEGRAPH_VERSION   default v1.1.6        — CodeGraph version in runtime image
#   MAC_HOME            default $HOME/.mac
#   MAC_SRC             default $MAC_HOME/src/mac    — mac source tree (image build context)
#   OSH_DOCKER_BIN      default docker       — Docker Engine/Moby CLI path
#   OSH_IMAGE_TAG       default localhost/mac-hermes:net — sandbox image tag
#   OSH_RUNTIME_IMAGE_REF exact public ghcr.io/...@sha256 digest to pull instead
#                       of rebuilding the runtime independently on each node
#   OSH_GPU             auto|yes|no          — auto: detect nvidia-smi
#   OSH_HUB_URL         default from mac.env MAC_HUB_URL — the hub the sandbox egresses to
# Flags: --enable  --fail-closed  --skip-image
set -euo pipefail

OPENSHELL_VERSION="${OPENSHELL_VERSION:-0.0.72}"
# Multi-platform linux/amd64+linux/arm64 supervisor index reviewed with the
# OpenShell 0.0.72 fleet baseline. Never let a mutable `latest` image change the
# certifier's isolation runtime between otherwise identical executions.
OSH_SUPERVISOR_IMAGE="ghcr.io/nvidia/openshell/supervisor@sha256:80ed9cda5bf672fefdb9dcd4604b40a8b09c0891b6eb9d03e10227c7e3dfb49d"
case "$OPENSHELL_VERSION" in
  0.0.72)
    OSH_CLI_DARWIN_ARM64_SHA256="117b5354cc42d80bc4d5e070ea5ac4e341208ff6d3c29b516d8a9c80e2310f8d"
    OSH_CLI_LINUX_AMD64_SHA256="37836c3b50383e03249c5e16512c1806e591fba8451408a84fb2f628ddb318c4"
    OSH_CLI_LINUX_ARM64_SHA256="a5ff01a3240d73c72ec1700eda6cc6c752a86cf50c5dd1b5bdc459f544d03045"
    OSH_GATEWAY_LINUX_AMD64_SHA256="03225fb9388b682af1a5f1614b26b75f828da6031e3ffc1fd920b6fbe5f70877"
    OSH_GATEWAY_LINUX_ARM64_SHA256="a97dcb3acb04fb2d1170c1a2170228990c2337e25bb8c18817e5a6e952204108"
    ;;
  *)
    echo "unsupported unreviewed OPENSHELL_VERSION=$OPENSHELL_VERSION; add exact release-asset digests before upgrading" >&2
    exit 2
    ;;
esac
GH_VERSION="${GH_VERSION:-2.95.0}"
CODEGRAPH_VERSION="${CODEGRAPH_VERSION:-v1.1.6}"
MAC_HOME="${MAC_HOME:-$HOME/.mac}"
MAC_SRC="${MAC_SRC:-$MAC_HOME/src/mac}"
OSH_DOCKER_BIN="${OSH_DOCKER_BIN:-docker}"
OSH_IMAGE_TAG="${OSH_IMAGE_TAG:-localhost/mac-hermes:net}"
OSH_RUNTIME_IMAGE_REF="${OSH_RUNTIME_IMAGE_REF:-}"
OSH_GPU="${OSH_GPU:-auto}"
OPENSHELL_LOCAL_GATEWAY_ENDPOINT="http://127.0.0.1:17670"
ENVF="$MAC_HOME/mac.env"
OSH_DIR="$MAC_HOME/openshell"
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
publish_openshell_cli(){
  local cli="$1"
  mkdir -p "$MAC_HOME/bin"
  ln -sf "$cli" "$MAC_HOME/bin/openshell"
}

# Bootstrap validation must never inherit a stale selected gateway.  Bind every
# runtime operation to the gateway created by this invocation, independently of
# the CLI's user-level registration metadata.
openshell_local_gateway(){
  local cli="$1"
  shift
  OPENSHELL_GATEWAY_ENDPOINT="$OPENSHELL_LOCAL_GATEWAY_ENDPOINT" "$cli" "$@"
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
mkdir -p "$OSH_DIR" "$BIN"

build_runtime_image() {
  local image_source_sha image_source_sha_file runtime_digest runtime_config
  local image_revision marker_tmp runtime_ref_file runtime_ref_tmp builder
  image_source_sha="$(resolve_deployed_source_revision)" || return 1
  image_source_sha_file="$OSH_DIR/image-source-sha"
  runtime_ref_file="$OSH_DIR/runtime-image-ref"
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
    if [ -n "$image_source_sha" ] && [ "$image_revision" != "$image_source_sha" ]; then
      echo "runtime image revision does not match deployed source commit" >&2
      return 1
    fi
    "$OSH_DOCKER_BIN" tag "$OSH_RUNTIME_IMAGE_REF" "$OSH_IMAGE_TAG"
    if [ -n "$image_source_sha" ]; then
      mkdir -p "$(dirname "$image_source_sha_file")"
      marker_tmp="${image_source_sha_file}.tmp.$$"
      printf '%s\n' "$image_source_sha" > "$marker_tmp"
      mv -f "$marker_tmp" "$image_source_sha_file"
    fi
    runtime_ref_tmp="${runtime_ref_file}.tmp.$$"
    printf '%s\n' "$OSH_RUNTIME_IMAGE_REF" > "$runtime_ref_tmp"
    chmod 600 "$runtime_ref_tmp"
    mv -f "$runtime_ref_tmp" "$runtime_ref_file"
    log "installed identical reviewed runtime as $OSH_IMAGE_TAG"
    return 0
  fi
  builder="$(cd "$(dirname "$0")" && pwd)/build-runtime-image.sh"
  log "building $OSH_IMAGE_TAG with Docker Engine/Moby from $MAC_SRC (development fallback)"
  GH_VERSION="$GH_VERSION" CODEGRAPH_VERSION="$CODEGRAPH_VERSION" \
    MAC_SRC="$MAC_SRC" OSH_DOCKER_BIN="$OSH_DOCKER_BIN" \
    OSH_IMAGE_TAG="$OSH_IMAGE_TAG" MAC_IMAGE_SOURCE_SHA="$image_source_sha" \
    MAC_IMAGE_SOURCE_SHA_FILE="$image_source_sha_file" /bin/bash "$builder"
  # A successful explicit development build supersedes a prior digest-managed
  # install. Never leave a stale marker that would make the worker believe the
  # mutable local tag is still protected by the published digest.
  rm -f "$runtime_ref_file"
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

# --- macOS / Docker Desktop path --------------------------------------------
# On macOS the OpenShell *gateway* (a Linux ELF) and the sandbox containers run
# inside Docker (Desktop)'s Linux VM. The gateway runs as a socket-mounted
# container; the host CLI (uv) drives it. The LinuxKit kernel does not surface
# Landlock to containers, so confinement is seccomp + namespaces + the egress
# proxy and we set MAC_OPENSHELL_ALLOW_NO_LANDLOCK=1 (see ADR 0008 amendment).
# The "identical-path HOME" mount makes the gateway's supervisor-binary bind
# mounts resolve on the Docker host. Self-contained + early-exit so the Linux
# flow below is untouched.
bootstrap_darwin() {
  # launchd and non-interactive SSH sessions do not inherit the interactive
  # shell's Homebrew or Docker Desktop paths.  Bootstrap must be runnable by
  # the fleet deployer, not only from a configured terminal.
  export PATH="/Applications/Docker.app/Contents/Resources/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
  command -v "$OSH_DOCKER_BIN" >/dev/null 2>&1 || { echo "docker CLI not found on PATH (Docker Desktop?)" >&2; exit 1; }
  "$OSH_DOCKER_BIN" info >/dev/null 2>&1 || { echo "docker daemon unreachable — is Docker Desktop running?" >&2; exit 1; }
  log "macOS: OpenShell via Docker ($("$OSH_DOCKER_BIN" --version 2>&1 | head -1)); gateway runs in a container"
  verify_supervisor_image || exit $?
  case "$ARCH" in
    arm64|aarch64) gwarch=aarch64; gw_sha="$OSH_GATEWAY_LINUX_ARM64_SHA256";;
    x86_64|amd64) gwarch=x86_64; gw_sha="$OSH_GATEWAY_LINUX_AMD64_SHA256";;
    *) echo "unsupported arch $ARCH" >&2; exit 1;;
  esac
  # 1. openshell CLI (reviewed native release asset, never an ambient binary
  # or mutable package). Reinstall on every bootstrap so a same-version local
  # replacement cannot survive on version text alone.
  case "$ARCH" in
    arm64|aarch64)
      cli_asset="openshell-aarch64-apple-darwin.tar.gz"
      cli_sha="$OSH_CLI_DARWIN_ARM64_SHA256"
      ;;
    *) echo "unsupported Darwin OpenShell architecture $ARCH" >&2; exit 1 ;;
  esac
  cli_url="https://github.com/NVIDIA/OpenShell/releases/download/v$OPENSHELL_VERSION/$cli_asset"
  log "installing openshell CLI $OPENSHELL_VERSION from reviewed release asset"
  tmp="$(mktemp -d)"
  download -o "$tmp/openshell.tgz" "$cli_url"
  verify_sha256 "$tmp/openshell.tgz" "$cli_sha"
  tar -xzf "$tmp/openshell.tgz" -C "$tmp"
  install -m755 "$(find "$tmp" -name openshell -type f | head -1)" "$BIN/openshell"
  rm -rf "$tmp"
  OSH_CLI="$BIN/openshell"
  publish_openshell_cli "$OSH_CLI"
  log "openshell CLI: $("$OSH_CLI" --version 2>&1 | head -1)"
  # 2. sandbox image (arch-native build against Docker Desktop)
  if [ "$SKIP_IMAGE" = 0 ]; then
    build_runtime_image
  fi
  # 3. gateway Linux binary (runs inside a container). Check its actual version
  # through the Linux image: an existing older binary must not silently survive
  # a CLI upgrade on the macOS host.
  url="https://github.com/NVIDIA/OpenShell/releases/download/v$OPENSHELL_VERSION/openshell-gateway-$gwarch-unknown-linux-gnu.tar.gz"
  log "installing reviewed gateway $OPENSHELL_VERSION"
  log "fetching gateway: $url"; tmp="$(mktemp -d)"; download -o "$tmp/gw.tgz" "$url"
  verify_sha256 "$tmp/gw.tgz" "$gw_sha"
  tar -xzf "$tmp/gw.tgz" -C "$tmp"
  install -m755 "$(find "$tmp" -name openshell-gateway -type f | head -1)" "$OSH_DIR/openshell-gateway"; rm -rf "$tmp"
  # 4. JWT certs (generated by the gateway, in a container)
  [ -f "$OSH_DIR/pki/jwt/signing.pem" ] || "$OSH_DOCKER_BIN" run --rm -v "$OSH_DIR:/osh" "$OSH_IMAGE_TAG" \
    /osh/openshell-gateway generate-certs --output-dir /osh/pki >/dev/null 2>&1
  log "jwt keys: $(ls "$OSH_DIR/pki/jwt" 2>/dev/null | tr '\n' ' ')"
  # 5. gateway.toml (Docker driver; container paths under /osh)
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
signing_key_path = "/osh/pki/jwt/signing.pem"
public_key_path = "/osh/pki/jwt/public.pem"
kid_path = "/osh/pki/jwt/kid"
[openshell.drivers.docker]
default_image = "$OSH_IMAGE_TAG"
supervisor_image = "$OSH_SUPERVISOR_IMAGE"
network_name = "openshell-docker"
grpc_endpoint = "http://host.openshell.internal:17670"
image_pull_policy = "IfNotPresent"
EOF
  # 6. gateway container — identical-path HOME so supervisor-binary bind mounts resolve on the Docker host
  GH="$OSH_DIR/ghome"; mkdir -p "$GH"
  "$OSH_DOCKER_BIN" rm -f openshell-gw >/dev/null 2>&1 || true
  "$OSH_DOCKER_BIN" run -d --name openshell-gw --restart unless-stopped \
    --label mac.owner=mac --label mac.kind=openshell-gateway \
    -v /var/run/docker.sock:/var/run/docker.sock -v "$OSH_DIR:/osh" -v "$GH:$GH" -e HOME="$GH" \
    -p 127.0.0.1:17670:17670 "$OSH_IMAGE_TAG" /osh/openshell-gateway --config /osh/gateway.toml >/dev/null
  sleep 5
  "$OSH_DOCKER_BIN" ps --filter name=openshell-gw --format '{{.Status}}' | grep -q Up \
    || { echo "gateway container failed to start:" >&2; "$OSH_DOCKER_BIN" logs openshell-gw 2>&1 | tail -20 >&2; exit 1; }
  register_and_select_local_gateway "$OSH_CLI"
  openshell_local_gateway "$OSH_CLI" status >/dev/null
  log "gateway: container 'openshell-gw' up; CLI selected @127.0.0.1:17670"
  # 7. egress policy + sandbox hermes config
  HUB_URL="${OSH_HUB_URL:-$(grep -oE '^MAC_HUB_URL=[^ ]+' "$ENVF" 2>/dev/null | head -1 | cut -d= -f2-)}"
  HUB_HOST="$(printf '%s' "$HUB_URL" | sed -E 's#^https?://##; s#[:/].*##')"
  case "$HUB_HOST" in 127.0.0.1|localhost|0.0.0.0|"") POLICY_HUB=host.openshell.internal;; *) POLICY_HUB="$HUB_HOST";; esac
  HUB_PORT="$(printf '%s' "$HUB_URL" | grep -oE ':[0-9]+' | tr -d : | head -1)"; HUB_PORT="${HUB_PORT:-8789}"
  log "policy egress hub: $POLICY_HUB:$HUB_PORT"
  "$MAC_HOME/venv/bin/mac" openshell render-policy \
    --template "$(cd "$(dirname "$0")" && pwd)/mac-hermes-policy.yaml" \
    --agent-user "$USER" --hub-host "$POLICY_HUB" --hub-port "$HUB_PORT" \
    --image-runtime /opt/mac-venv --into "$MAC_HOME/openshell-policy.yaml" >/dev/null
  chmod 600 "$MAC_HOME/openshell-policy.yaml"
  # Normalize the mac-hub gateway endpoint (hub port) to THIS node's current hub
  # authority ($POLICY_HUB): loopback -> host.openshell.internal, and a STALE hub
  # host (e.g. a pre-migration tailnet IP) -> the live hub instead of passing
  # through. Non-hub-port providers (other services, external APIs) are untouched.
  # 8. env recipe (BSD sed -i '')
  cp -a "$ENVF" "$ENVF.bak-openshell-$(date +%Y%m%dT%H%M%S 2>/dev/null || echo bootstrap)"
  sed -i '' '/^# OpenShell sandbox enforcement/d;/^MAC_OPENSHELL_SANDBOX=/d;/^MAC_OPENSHELL_ALLOW_NO_LANDLOCK=/d;/^MAC_OPENSHELL_GC=/d;/^MAC_OPENSHELL_STALE_AFTER_SECONDS=/d;/^MAC_HERMES_PYTHON=/d;/^MAC_OPENSHELL_POLICY=/d;/^MAC_OPENSHELL_BIN=/d;/^MAC_OPENSHELL_CREATE_ARGS=/d;/^MAC_ALLOW_UNSANDBOXED_YOLO=/d;/^MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT=/d' "$ENVF" 2>/dev/null || true
  {
    echo ""
    echo "# OpenShell sandbox enforcement (macOS/Docker Desktop; gateway in container; LinuxKit has no host Landlock)"
    echo "MAC_OPENSHELL_SANDBOX=$DO_ENABLE"
    echo "MAC_OPENSHELL_ALLOW_NO_LANDLOCK=1"
    echo "MAC_OPENSHELL_GC=1"
    echo "MAC_OPENSHELL_STALE_AFTER_SECONDS=86400"
    echo "MAC_OPENSHELL_POLICY=$MAC_HOME/openshell-policy.yaml"
    echo "MAC_OPENSHELL_BIN=$OSH_CLI"
    echo "MAC_OPENSHELL_CREATE_ARGS=\"--from $OSH_IMAGE_TAG\""
    echo "MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT=1"
    [ "$DO_FAILCLOSED" = 1 ] && echo "MAC_ALLOW_UNSANDBOXED_YOLO=0"
  } >> "$ENVF"
  ( set -a; . "$ENVF" >/dev/null 2>&1; ) || { echo "ERROR: mac.env failed to source after edit" >&2; exit 1; }
  # 9. smoke (only when enabling enforcement)
  if [ "$DO_ENABLE" = 1 ]; then
    sm="mac-runtime-smoke-$$"
    if openshell_local_gateway "$OSH_CLI" sandbox create --no-auto-providers --policy "$MAC_HOME/openshell-policy.yaml" --name "$sm" \
        --label mac.owner=mac --label mac.kind=runtime-smoke --label "mac.pid=$$" --label mac.keep=false \
        --from "$OSH_IMAGE_TAG" --env HOME=/tmp \
        -- /bin/bash -c 'set -euo pipefail; /usr/local/bin/mac-verify-bash-contract; command -v gh; command -v codegraph; command -v python3; /opt/mac-venv/bin/python -c "import mac.agent_command"' >"$OSH_DIR/runtime-image-smoke.log" 2>&1; then
      openshell_local_gateway "$OSH_CLI" sandbox delete "$sm" >/dev/null 2>&1 || true
      log "runtime image smoke: Bash >=5.2 plus gh/codegraph/python visible through OpenShell on Docker Desktop"
    else
      openshell_local_gateway "$OSH_CLI" sandbox delete "$sm" >/dev/null 2>&1 || true
      echo "ERROR: OpenShell smoke failed; see $OSH_DIR/runtime-image-smoke.log" >&2; tail -40 "$OSH_DIR/runtime-image-smoke.log" >&2; exit 1
    fi
    run_live_confinement_probe "$OSH_CLI" "mac-security-probe-$$" \
      "$OSH_DIR/live-confinement-probe.log" || exit $?
  fi
  # A complete source+runtime bootstrap is the only operation authorized to
  # clear a worker's persistent consistency hold.
  clear_repo_update_dispatch_blocker
  log "DONE (macOS). sandbox-enabled=$DO_ENABLE fail-closed=$DO_FAILCLOSED; gateway=docker container 'openshell-gw'"
  log "restart the agent to apply: launchctl kickstart -k gui/\$(id -u)/com.<fleet_name>.agent (then validate a real task)"
}
if [ "$(uname -s)" = "Darwin" ]; then bootstrap_darwin; exit 0; fi

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

ensure_docker_engine
log "arch=$ARCH gpu=$OSH_GPU driver=docker-engine version=$OPENSHELL_VERSION docker=$("$OSH_DOCKER_BIN" --version 2>&1 | head -1)"

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
  systemctl --user stop openshell-gateway >/dev/null 2>&1 || true
  sudo supervisorctl stop openshell-gateway >/dev/null 2>&1 || true
  sudo pkill -f "$BIN/openshell-gateway" >/dev/null 2>&1 || true
}

# A previous deployment may still be running with an older, narrower firewall.
# Stop it before downloads or image builds so bootstrap latency never extends an
# unauthenticated mesh exposure window. The gateway is restarted only after the
# strict current rule and its persistence manager have both passed.
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

validate_openshell_runtime_image() {
  [ "$DO_ENABLE" = 1 ] || return 0
  smoke_name="mac-runtime-smoke-$$"
  smoke_log="$OSH_DIR/runtime-image-smoke.log"
  rm -f "$smoke_log"
  gpu_flag=()
  [ "$OSH_GPU" = yes ] && gpu_flag=(--gpu)
  if openshell_local_gateway "$BIN/openshell" sandbox create \
      --no-auto-providers \
      --policy "$MAC_HOME/openshell-policy.yaml" \
      --name "$smoke_name" \
      --label mac.owner=mac \
      --label mac.kind=runtime-smoke \
      --label "mac.pid=$$" \
      --label mac.keep=false \
      --from "$OSH_IMAGE_TAG" \
      "${gpu_flag[@]}" \
      --env HOME=/tmp \
      -- /bin/bash -c 'set -euo pipefail; /usr/local/bin/mac-verify-bash-contract; command -v gh; gh --version | head -1; command -v codex; codex --version; command -v codegraph; codegraph --version; /opt/mac-venv/bin/python -c "import mac.agent_command"' \
      > "$smoke_log" 2>&1; then
    openshell_local_gateway "$BIN/openshell" sandbox delete "$smoke_name" >/dev/null 2>&1 || true
    log "runtime image smoke: Bash >=5.2 plus gh/codex/codegraph visible through OpenShell"
  else
    rc=$?
    openshell_local_gateway "$BIN/openshell" sandbox delete "$smoke_name" >/dev/null 2>&1 || true
    echo "ERROR: OpenShell runtime image smoke failed; see $smoke_log" >&2
    tail -80 "$smoke_log" >&2 || true
    exit "$rc"
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
  tar -xzf "$tmp/openshell.tgz" -C "$tmp"
  rm -f "$BIN/openshell"
  install -m755 "$(find "$tmp" -name openshell -type f | head -1)" "$BIN/openshell"
  rm -rf "$tmp"
}

log "installing reviewed openshell CLI $OPENSHELL_VERSION"
install_openshell_cli_static
publish_openshell_cli "$BIN/openshell"
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
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/openshell-gateway.service" <<EOF
[Unit]
Description=OpenShell gateway (Docker Engine/Moby driver)
After=network-online.target
Wants=network-online.target
[Service]
ExecStart=%h/.mac/openshell/run-gateway.sh
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
  sudo tee /etc/supervisor/conf.d/openshell-gateway.conf >/dev/null <<EOF
[program:openshell-gateway]
command=$OSH_DIR/run-gateway.sh
directory=$OSH_DIR
user=$USER
environment=HOME="$HOME",PATH="$BIN:/usr/local/bin:/usr/bin:/bin"
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
sleep 3
if ! register_and_select_local_gateway "$BIN/openshell" \
    || ! openshell_local_gateway "$BIN/openshell" status >/dev/null 2>&1; then
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
"$MAC_HOME/venv/bin/mac" openshell render-policy \
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
gpuarg=""; [ "$OSH_GPU" = yes ] && gpuarg=" --gpu"
codex_uploads=""
if truthy "${MAC_OPENSHELL_UPLOAD_CODEX_AUTH:-0}"; then
  if [ -s "$HOME/.codex/auth.json" ]; then
    codex_uploads="$codex_uploads --upload $HOME/.codex/auth.json:/tmp/.codex/auth.json"
  fi
  # Deliberately NOT uploading config.toml: its top-level model pin is
  # specific to the operator workstation's codex version and breaks a worker
  # on an older codex ("model X requires a newer version of Codex"). auth.json
  # is the portable credential; the fleet sets the model per task via --model.
  # (mac fleet creds-sync ships a model-pin-stripped config.toml when custom
  # provider config is genuinely needed.)
else
  log "codex file auth upload: disabled (rotating OAuth state is not durable in throwaway sandboxes)"
fi
cp -a "$ENVF" "$ENVF.bak-openshell-$(date +%Y%m%dT%H%M%S 2>/dev/null || echo bootstrap)"
sed -i '/^# OpenShell sandbox enforcement/d;/^MAC_OPENSHELL_SANDBOX=/d;/^MAC_OPENSHELL_GC=/d;/^MAC_OPENSHELL_STALE_AFTER_SECONDS=/d;/^MAC_HERMES_PYTHON=/d;/^MAC_OPENSHELL_POLICY=/d;/^MAC_OPENSHELL_BIN=/d;/^MAC_OPENSHELL_CREATE_ARGS=/d;/^MAC_ALLOW_UNSANDBOXED_YOLO=/d;/^MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT=/d' "$ENVF"
{
  echo ""
  echo "# OpenShell sandbox enforcement (bootstrap-openshell.sh; Docker Engine/Moby driver, gpu=$OSH_GPU)"
  echo "MAC_OPENSHELL_SANDBOX=$DO_ENABLE"
  echo "MAC_OPENSHELL_GC=1"
  echo "MAC_OPENSHELL_STALE_AFTER_SECONDS=86400"
  echo "MAC_OPENSHELL_POLICY=$MAC_HOME/openshell-policy.yaml"
  echo "MAC_OPENSHELL_BIN=$BIN/openshell"
  echo "MAC_OPENSHELL_CREATE_ARGS=\"--from $OSH_IMAGE_TAG$gpuarg$codex_uploads\""
  echo "MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT=1"
  [ "$DO_FAILCLOSED" = 1 ] && echo "MAC_ALLOW_UNSANDBOXED_YOLO=0"
} >> "$ENVF"
# sanity: mac.env must still source cleanly (quoting)
( set -a; . "$ENVF" >/dev/null 2>&1; ) || { echo "ERROR: mac.env failed to source after edit" >&2; exit 1; }
validate_openshell_runtime_image
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
