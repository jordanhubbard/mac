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
#   OSH_GPU             auto|yes|no          — auto: detect nvidia-smi
#   OSH_HUB_URL         default from mac.env MAC_HUB_URL — the hub the sandbox egresses to
#   OSH_FIREWALL_NIC    default: the default-route NIC
# Flags: --enable  --fail-closed  --skip-image
set -euo pipefail

OPENSHELL_VERSION="${OPENSHELL_VERSION:-0.0.72}"
GH_VERSION="${GH_VERSION:-2.95.0}"
CODEGRAPH_VERSION="${CODEGRAPH_VERSION:-v1.1.6}"
MAC_HOME="${MAC_HOME:-$HOME/.mac}"
MAC_SRC="${MAC_SRC:-$MAC_HOME/src/mac}"
OSH_DOCKER_BIN="${OSH_DOCKER_BIN:-docker}"
OSH_IMAGE_TAG="${OSH_IMAGE_TAG:-localhost/mac-hermes:net}"
OSH_GPU="${OSH_GPU:-auto}"
ENVF="$MAC_HOME/mac.env"
OSH_DIR="$MAC_HOME/openshell"
BIN="$HOME/.local/bin"
ARCH="$(uname -m)"   # x86_64 | aarch64
DO_ENABLE=0; DO_FAILCLOSED=0; SKIP_IMAGE=0
for a in "$@"; do case "$a" in
  --enable) DO_ENABLE=1;; --fail-closed) DO_FAILCLOSED=1; DO_ENABLE=1;; --skip-image) SKIP_IMAGE=1;;
  *) echo "unknown arg: $a" >&2; exit 2;; esac; done
log(){ printf '[bootstrap-openshell] %s\n' "$*"; }
truthy(){ case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in 1|true|yes|on) return 0;; *) return 1;; esac; }
download(){ curl --retry 5 --retry-all-errors --retry-delay 2 --connect-timeout 15 --max-time 120 -fsSL "$@"; }
export PATH="$BIN:$PATH" XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
mkdir -p "$OSH_DIR" "$BIN"

build_runtime_image() {
  builder="$(cd "$(dirname "$0")" && pwd)/build-runtime-image.sh"
  image_source_sha="$(git -C "$MAC_SRC" rev-parse HEAD 2>/dev/null || true)"
  log "building $OSH_IMAGE_TAG with Docker Engine/Moby from $MAC_SRC"
  GH_VERSION="$GH_VERSION" CODEGRAPH_VERSION="$CODEGRAPH_VERSION" \
    MAC_SRC="$MAC_SRC" OSH_DOCKER_BIN="$OSH_DOCKER_BIN" \
    OSH_IMAGE_TAG="$OSH_IMAGE_TAG" MAC_IMAGE_SOURCE_SHA="$image_source_sha" \
    MAC_IMAGE_SOURCE_SHA_FILE="$OSH_DIR/image-source-sha" /bin/bash "$builder"
}

run_live_confinement_probe() {
  local cli="$1" name="$2" output="$3"
  local probe="$MAC_SRC/deploy/openshell/live-confinement-probe.sh"
  [ -f "$probe" ] || { echo "ERROR: missing OpenShell confinement probe: $probe" >&2; return 1; }
  rm -f "$output"
  "$cli" sandbox delete "$name" >/dev/null 2>&1 || true
  if "$cli" sandbox create \
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
    "$cli" sandbox delete "$name" >/dev/null 2>&1 || true
    grep -q '^CONFINEMENT_PROBE_OK$' "$output" \
      || { echo "ERROR: OpenShell confinement probe omitted success sentinel" >&2; return 1; }
    log "live confinement probe: filesystem/network/privilege/syscall boundaries enforced"
    return 0
  else
    rc=$?
    "$cli" sandbox delete "$name" >/dev/null 2>&1 || true
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
  case "$ARCH" in arm64|aarch64) gwarch=aarch64;; x86_64|amd64) gwarch=x86_64;; *) echo "unsupported arch $ARCH" >&2; exit 1;; esac
  # 1. openshell CLI (uv tool)
  command -v uv >/dev/null 2>&1 || { echo "uv required to install the openshell CLI on macOS (brew install uv)" >&2; exit 1; }
  if [ "$(openshell --version 2>/dev/null | awk '{print $NF}')" != "$OPENSHELL_VERSION" ]; then
    log "installing openshell CLI $OPENSHELL_VERSION"; uv tool install --force "openshell==$OPENSHELL_VERSION" >/dev/null
  fi
  OSH_CLI="$(command -v openshell || echo "$BIN/openshell")"
  log "openshell CLI: $("$OSH_CLI" --version 2>&1 | head -1)"
  # 2. sandbox image (arch-native build against Docker Desktop)
  if [ "$SKIP_IMAGE" = 0 ]; then
    build_runtime_image
  fi
  # 3. gateway Linux binary (runs inside a container). Check its actual version
  # through the Linux image: an existing older binary must not silently survive
  # a CLI upgrade on the macOS host.
  current_gateway_version=""
  if [ -x "$OSH_DIR/openshell-gateway" ]; then
    current_gateway_version="$("$OSH_DOCKER_BIN" run --rm -v "$OSH_DIR:/osh" "$OSH_IMAGE_TAG" \
      /osh/openshell-gateway --version 2>/dev/null | awk 'NR==1 {print $NF}' || true)"
  fi
  if [ "$current_gateway_version" != "$OPENSHELL_VERSION" ]; then
    url="https://github.com/NVIDIA/OpenShell/releases/download/v$OPENSHELL_VERSION/openshell-gateway-$gwarch-unknown-linux-gnu.tar.gz"
    log "installing gateway $OPENSHELL_VERSION (current: ${current_gateway_version:-missing})"
    log "fetching gateway: $url"; tmp="$(mktemp -d)"; download -o "$tmp/gw.tgz" "$url"; tar -xzf "$tmp/gw.tgz" -C "$tmp"
    install -m755 "$(find "$tmp" -name openshell-gateway -type f | head -1)" "$OSH_DIR/openshell-gateway"; rm -rf "$tmp"
  fi
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
supervisor_image = "ghcr.io/nvidia/openshell/supervisor:latest"
network_name = "openshell-docker"
grpc_endpoint = "http://host.openshell.internal:17670"
image_pull_policy = "IfNotPresent"
EOF
  # 6. gateway container — identical-path HOME so supervisor-binary bind mounts resolve on the Docker host
  GH="$OSH_DIR/ghome"; mkdir -p "$GH"
  "$OSH_DOCKER_BIN" rm -f openshell-gw >/dev/null 2>&1 || true
  "$OSH_DOCKER_BIN" run -d --name openshell-gw --restart unless-stopped \
    -v /var/run/docker.sock:/var/run/docker.sock -v "$OSH_DIR:/osh" -v "$GH:$GH" -e HOME="$GH" \
    -p 127.0.0.1:17670:17670 "$OSH_IMAGE_TAG" /osh/openshell-gateway --config /osh/gateway.toml >/dev/null
  sleep 5
  "$OSH_DOCKER_BIN" ps --filter name=openshell-gw --format '{{.Status}}' | grep -q Up \
    || { echo "gateway container failed to start:" >&2; "$OSH_DOCKER_BIN" logs openshell-gw 2>&1 | tail -20 >&2; exit 1; }
  "$OSH_CLI" gateway add http://127.0.0.1:17670 >/dev/null 2>&1 || true
  "$OSH_CLI" gateway select openshell >/dev/null 2>&1 || true
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
  [ -f "$HOME/.hermes/config.yaml" ] && { sed -E "s#(https?://)[^/@:]+(:${HUB_PORT}/)#\1${POLICY_HUB}\2#g" "$HOME/.hermes/config.yaml" > "$OSH_DIR/sandbox-hermes-config.yaml"; chmod 600 "$OSH_DIR/sandbox-hermes-config.yaml"; }
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
    echo "MAC_HERMES_PYTHON=/opt/mac-venv/bin/python"
    echo "MAC_OPENSHELL_POLICY=$MAC_HOME/openshell-policy.yaml"
    echo "MAC_OPENSHELL_BIN=$OSH_CLI"
    echo "MAC_OPENSHELL_CREATE_ARGS=\"--from $OSH_IMAGE_TAG --upload $OSH_DIR/sandbox-hermes-config.yaml:/tmp/.hermes/config.yaml\""
    echo "MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT=1"
    [ "$DO_FAILCLOSED" = 1 ] && echo "MAC_ALLOW_UNSANDBOXED_YOLO=0"
  } >> "$ENVF"
  ( set -a; . "$ENVF" >/dev/null 2>&1; ) || { echo "ERROR: mac.env failed to source after edit" >&2; exit 1; }
  # 9. smoke (only when enabling enforcement)
  if [ "$DO_ENABLE" = 1 ]; then
    sm="mac-runtime-smoke-$$"
    if "$OSH_CLI" sandbox create --no-auto-providers --policy "$MAC_HOME/openshell-policy.yaml" --name "$sm" \
        --label mac.owner=mac --label mac.kind=runtime-smoke --label "mac.pid=$$" --label mac.keep=false \
        --from "$OSH_IMAGE_TAG" --env HOME=/tmp \
        -- /bin/bash -c 'set -euo pipefail; /usr/local/bin/mac-verify-bash-contract; command -v gh; command -v codegraph; command -v python3; /opt/mac-venv/bin/python -c "import mac.agent_command"' >"$OSH_DIR/runtime-image-smoke.log" 2>&1; then
      "$OSH_CLI" sandbox delete "$sm" >/dev/null 2>&1 || true
      log "runtime image smoke: Bash >=5.2 plus gh/codegraph/python visible through OpenShell on Docker Desktop"
    else
      "$OSH_CLI" sandbox delete "$sm" >/dev/null 2>&1 || true
      echo "ERROR: OpenShell smoke failed; see $OSH_DIR/runtime-image-smoke.log" >&2; tail -40 "$OSH_DIR/runtime-image-smoke.log" >&2; exit 1
    fi
    run_live_confinement_probe "$OSH_CLI" "mac-security-probe-$$" \
      "$OSH_DIR/live-confinement-probe.log" || exit $?
  fi
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
  if "$BIN/openshell" sandbox create \
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
    "$BIN/openshell" sandbox delete "$smoke_name" >/dev/null 2>&1 || true
    log "runtime image smoke: Bash >=5.2 plus gh/codex/codegraph visible through OpenShell"
  else
    rc=$?
    "$BIN/openshell" sandbox delete "$smoke_name" >/dev/null 2>&1 || true
    echo "ERROR: OpenShell runtime image smoke failed; see $smoke_log" >&2
    tail -80 "$smoke_log" >&2 || true
    exit "$rc"
  fi
  run_live_confinement_probe "$BIN/openshell" "mac-security-probe-$$" \
    "$OSH_DIR/live-confinement-probe.log"
}

# --- 1. openshell CLI (uv tool, else pip venv) ------------------------------
tool_version() {
  "$1" --version 2>/dev/null | awk 'NR==1 {print $NF}'
}

install_openshell_cli_static() {
  case "$ARCH" in
    x86_64)  ca="openshell-x86_64-unknown-linux-musl.tar.gz";;
    aarch64) ca="openshell-aarch64-unknown-linux-musl.tar.gz";;
    *) echo "unsupported arch $ARCH" >&2; exit 1;;
  esac
  url="https://github.com/NVIDIA/OpenShell/releases/download/v$OPENSHELL_VERSION/$ca"
  log "fetching static openshell CLI: $url"
  tmp="$(mktemp -d)"
  download -o "$tmp/openshell.tgz" "$url"
  tar -xzf "$tmp/openshell.tgz" -C "$tmp"
  rm -f "$BIN/openshell"
  install -m755 "$(find "$tmp" -name openshell -type f | head -1)" "$BIN/openshell"
  rm -rf "$tmp"
}

current_openshell_version=""
if command -v openshell >/dev/null 2>&1; then
  current_openshell_version="$(tool_version openshell || true)"
fi
if [ "$current_openshell_version" != "$OPENSHELL_VERSION" ]; then
  log "installing openshell CLI $OPENSHELL_VERSION (current: ${current_openshell_version:-missing})"
  if command -v uv >/dev/null; then
    if ! uv tool install --force "openshell==$OPENSHELL_VERSION"; then
      log "Python wheel is incompatible with this host; using the static musl CLI"
      install_openshell_cli_static
    fi
  else
    rm -rf "$HOME/.openshell-cli-venv"
    python3 -m venv "$HOME/.openshell-cli-venv"
    if "$HOME/.openshell-cli-venv/bin/pip" install -q "openshell==$OPENSHELL_VERSION"; then
      ln -sf "$HOME/.openshell-cli-venv/bin/openshell" "$BIN/openshell"
    else
      log "Python wheel is incompatible with this host; using the static musl CLI"
      rm -rf "$HOME/.openshell-cli-venv"
      install_openshell_cli_static
    fi
  fi
fi
log "openshell CLI: $(openshell --version 2>&1 | head -1)"

# --- 2. openshell-gateway daemon (prebuilt per-arch release asset) ----------
install_openshell_gateway() {
  case "$ARCH" in
    x86_64)  ga="openshell-gateway-x86_64-unknown-linux-gnu.tar.gz";;
    aarch64) ga="openshell-gateway-aarch64-unknown-linux-gnu.tar.gz";;
    *) echo "unsupported arch $ARCH" >&2; exit 1;;
  esac
  url="https://github.com/NVIDIA/OpenShell/releases/download/v$OPENSHELL_VERSION/$ga"
  log "fetching gateway: $url"
  tmp="$(mktemp -d)"; download -o "$tmp/gw.tgz" "$url"; tar -xzf "$tmp/gw.tgz" -C "$tmp"
  install -m755 "$(find "$tmp" -name openshell-gateway -type f | head -1)" "$BIN/openshell-gateway"
  rm -rf "$tmp"
}

current_gateway_version=""
if [ -x "$BIN/openshell-gateway" ]; then
  current_gateway_version="$(tool_version "$BIN/openshell-gateway" || true)"
fi
if [ "$current_gateway_version" != "$OPENSHELL_VERSION" ]; then
  log "installing openshell-gateway $OPENSHELL_VERSION (current: ${current_gateway_version:-missing})"
  install_openshell_gateway
fi
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
supervisor_image = "ghcr.io/nvidia/openshell/supervisor:latest"
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

# --- 7. gateway service + register ------------------------------------------
gateway_manager=""
gateway_state="unknown"
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/openshell-gateway.service" <<EOF
[Unit]
Description=OpenShell gateway (Docker Engine/Moby driver)
[Service]
ExecStart=%h/.mac/openshell/run-gateway.sh
Restart=on-failure
RestartSec=5
[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now openshell-gateway >/dev/null 2>&1
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
  sudo supervisorctl restart openshell-gateway >/dev/null 2>&1 || sudo supervisorctl start openshell-gateway >/dev/null
  gateway_manager="supervisord"
  gateway_state="$(sudo supervisorctl status openshell-gateway 2>/dev/null | awk '{print tolower($2)}')"
else
  echo "ERROR: OpenShell gateway requires a working systemd user manager or supervisord" >&2
  exit 1
fi
sleep 3
openshell gateway add http://127.0.0.1:17670 >/dev/null 2>&1 || true
openshell gateway select openshell >/dev/null 2>&1 || true
log "gateway: manager=$gateway_manager state=$gateway_state | listening: $(ss -tlnp 2>/dev/null | grep -c :17670)"

# --- 8. firewall :17670 (block public/LAN NIC; persistent) ------------------
NIC="${OSH_FIREWALL_NIC:-$(ip route show default 2>/dev/null | grep -oP 'dev \K\S+' | head -1)}"
if [ -n "$NIC" ]; then
  sudo tee /usr/local/sbin/mac-openshell-firewall.sh >/dev/null <<EOF
#!/usr/bin/env bash
for ipt in iptables ip6tables; do command -v \$ipt >/dev/null || continue
  \$ipt -C INPUT -i $NIC -p tcp --dport 17670 -j DROP 2>/dev/null || \$ipt -I INPUT 1 -i $NIC -p tcp --dport 17670 -j DROP
done
EOF
  sudo chmod +x /usr/local/sbin/mac-openshell-firewall.sh
  if command -v systemctl >/dev/null 2>&1 && sudo systemctl show-environment >/dev/null 2>&1; then
    sudo tee /etc/systemd/system/mac-openshell-firewall.service >/dev/null <<'EOF'
[Unit]
Description=Block external access to the OpenShell gateway (:17670)
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/mac-openshell-firewall.sh
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable --now mac-openshell-firewall.service >/dev/null 2>&1
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
    sudo supervisorctl reread >/dev/null
    sudo supervisorctl update >/dev/null
    sudo supervisorctl start mac-openshell-firewall >/dev/null 2>&1 || true
  else
    sudo /usr/local/sbin/mac-openshell-firewall.sh
  fi
  log "firewall: $NIC :17670 ($(sudo iptables -S INPUT 2>/dev/null | grep -c 17670) rules)"
fi

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
sed -E "s#(https?://)[^/@:]+(:${HUB_PORT}/)#\1${POLICY_HUB}\2#g" "$HOME/.hermes/config.yaml" > "$OSH_DIR/sandbox-hermes-config.yaml"
chmod 600 "$OSH_DIR/sandbox-hermes-config.yaml"

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
  echo "MAC_HERMES_PYTHON=/opt/mac-venv/bin/python"
  echo "MAC_OPENSHELL_POLICY=$MAC_HOME/openshell-policy.yaml"
  echo "MAC_OPENSHELL_BIN=$BIN/openshell"
  echo "MAC_OPENSHELL_CREATE_ARGS=\"--from $OSH_IMAGE_TAG$gpuarg --upload $OSH_DIR/sandbox-hermes-config.yaml:/tmp/.hermes/config.yaml$codex_uploads\""
  echo "MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT=1"
  [ "$DO_FAILCLOSED" = 1 ] && echo "MAC_ALLOW_UNSANDBOXED_YOLO=0"
} >> "$ENVF"
# sanity: mac.env must still source cleanly (quoting)
( set -a; . "$ENVF" >/dev/null 2>&1; ) || { echo "ERROR: mac.env failed to source after edit" >&2; exit 1; }
validate_openshell_runtime_image

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
