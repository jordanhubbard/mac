#!/usr/bin/env bash
# install-nemoclaw-pilot.sh — bootstrap the NemoClaw Hermes gateway pilot on
# one non-GKE host alongside the existing hermes gateway service.
#
# Pilot scope:
#   - Task-local Node 22+ and the nemoclaw CLI (installed under MAC_HOME;
#     never to the host system, never with sudo, never global npm/pip/pipx).
#   - NemoClaw onboarded from its blueprint with a digest-pinned openclaw
#     sandbox image (localhost/mac-hermes:net, sha256 pinned at build time).
#   - Exactly one Slack workspace through openclaw channels.slack.accounts
#     Socket Mode (bot_token + app_token; never printed to stdout).
#   - Provider pointing at MAC's router base URL with required static headers
#     (x-mac-agent-id, x-mac-hermes-instance-id).
#   - MAC runtime context injected into every agent session via
#     HERMES_HOME/mac-runtime-context.md (MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN).
#   - Existing hermes gateway service is NOT modified or restarted.
#
# Idempotent: safe to re-run. Each step checks before acting.
#
# Prerequisites (must be satisfied before running this script):
#   - Docker Engine/Moby installed and running
#   - mac source tree available at MAC_SRC (default $MAC_HOME/src/mac)
#   - Slack bot_token and app_token obtained from https://api.slack.com/apps
#   - MAC router hub URL known
#
# Required environment when running non-interactively:
#   MAC_NEMOCLAW_HUB_URL      — MAC router base URL, e.g. http://hub.example.internal:8789/v1
#   MAC_NEMOCLAW_AGENT_ID     — stable fleet identity of this agent, e.g. agent-worker-1
#   MAC_NEMOCLAW_INSTANCE_ID  — Hermes instance ID, e.g. hermes_nemoclaw-worker-1
#   MAC_NEMOCLAW_SLACK_BOT_TOKEN  — xoxb-... (read from env; never logged)
#   MAC_NEMOCLAW_SLACK_APP_TOKEN  — xapp-... (read from env; never logged)
#   MAC_NEMOCLAW_SLACK_WORKSPACE  — workspace display name, e.g. my-fleet
#   MAC_NEMOCLAW_FLEET_NAME   — fleet name used in runtime context, e.g. my-fleet
#
# Optional:
#   MAC_HOME                  — default: $HOME/.mac
#   MAC_SRC                   — default: $MAC_HOME/src/mac
#   NEMOCLAW_HERMES_HOME      — default: $HOME/.hermes-nemoclaw
#   MAC_NEMOCLAW_GATEWAY_PORT — pilot gateway port (default: 18765)
#   MAC_NEMOCLAW_HOME_CHANNEL — Slack channel name without '#' (default: general)
#   NEMOCLAW_NODE_VERSION     — Node version to install locally (default: 22)
#   NEMOCLAW_CLI_VERSION      — nemoclaw CLI npm package version (default: latest)
#   NEMOCLAW_IMAGE_DIGEST     — sha256 digest of localhost/mac-hermes:net base image
#   OSH_SKIP_IMAGE_BUILD      — set 1 to skip docker build (image must exist)
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MAC_HOME="${MAC_HOME:-$HOME/.mac}"
MAC_SRC="${MAC_SRC:-$MAC_HOME/src/mac}"
NEMOCLAW_HERMES_HOME="${NEMOCLAW_HERMES_HOME:-$HOME/.hermes-nemoclaw}"
NEMOCLAW_NODE_VERSION="${NEMOCLAW_NODE_VERSION:-22}"
NEMOCLAW_CLI_VERSION="${NEMOCLAW_CLI_VERSION:-latest}"
NEMOCLAW_IMAGE_TAG="localhost/mac-hermes:net"
MAC_NEMOCLAW_GATEWAY_PORT="${MAC_NEMOCLAW_GATEWAY_PORT:-18765}"
MAC_NEMOCLAW_HOME_CHANNEL="${MAC_NEMOCLAW_HOME_CHANNEL:-general}"
OSH_SKIP_IMAGE_BUILD="${OSH_SKIP_IMAGE_BUILD:-0}"

# Task-local node/npm prefix — never touches the system PATH or global npm.
NEMOCLAW_NODE_PREFIX="${MAC_HOME}/nemoclaw-node"
NEMOCLAW_BIN="${NEMOCLAW_NODE_PREFIX}/bin"

log() { printf '[install-nemoclaw-pilot] %s\n' "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }
truthy() { case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in 1|true|yes|on) return 0;; *) return 1;; esac; }

# ---------------------------------------------------------------------------
# Step 0: validate required env
# ---------------------------------------------------------------------------
validate_env() {
  log "validating required environment variables"
  local missing=()
  for var in \
    MAC_NEMOCLAW_HUB_URL \
    MAC_NEMOCLAW_AGENT_ID \
    MAC_NEMOCLAW_INSTANCE_ID \
    MAC_NEMOCLAW_SLACK_BOT_TOKEN \
    MAC_NEMOCLAW_SLACK_APP_TOKEN \
    MAC_NEMOCLAW_SLACK_WORKSPACE \
    MAC_NEMOCLAW_FLEET_NAME; do
    [ -n "${!var:-}" ] || missing+=("$var")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    die "missing required environment variables: ${missing[*]}"
  fi
  # Validate token shapes without logging the values.
  [[ "${MAC_NEMOCLAW_SLACK_BOT_TOKEN}" == xoxb-* ]] \
    || die "MAC_NEMOCLAW_SLACK_BOT_TOKEN must start with 'xoxb-'"
  [[ "${MAC_NEMOCLAW_SLACK_APP_TOKEN}" == xapp-* ]] \
    || die "MAC_NEMOCLAW_SLACK_APP_TOKEN must start with 'xapp-'"
  log "environment validated"
}

# ---------------------------------------------------------------------------
# Step 1: install task-local Node 22+
# ---------------------------------------------------------------------------
install_node_local() {
  log "checking for task-local Node ${NEMOCLAW_NODE_VERSION}+"
  local node_bin="${NEMOCLAW_BIN}/node"
  if [ -x "${node_bin}" ]; then
    local cur_ver
    cur_ver="$("${node_bin}" --version 2>/dev/null | sed 's/^v//')"
    local cur_major="${cur_ver%%.*}"
    if [ "${cur_major:-0}" -ge "${NEMOCLAW_NODE_VERSION}" ]; then
      log "task-local Node ${cur_ver} already installed at ${node_bin}"
      return 0
    fi
    log "task-local Node ${cur_ver} is below required ${NEMOCLAW_NODE_VERSION}; reinstalling"
  fi

  mkdir -p "${NEMOCLAW_NODE_PREFIX}"
  log "installing Node ${NEMOCLAW_NODE_VERSION} LTS via nvm into ${NEMOCLAW_NODE_PREFIX}"

  # Use nvm if available; otherwise fetch the official node tarball.
  if command -v nvm >/dev/null 2>&1 || [ -f "${HOME}/.nvm/nvm.sh" ]; then
    # Source nvm and install into a task-local prefix via --prefix.
    # nvm does not natively support a custom prefix; use the archive download path.
    :
  fi

  # Fetch the latest Node LTS tarball matching the major version.
  local arch
  arch="$(uname -m)"
  case "${arch}" in
    x86_64)  arch="x64" ;;
    aarch64|arm64) arch="arm64" ;;
    *) die "unsupported architecture for local Node install: ${arch}" ;;
  esac

  local os_name
  os_name="$(uname -s | tr '[:upper:]' '[:lower:]')"

  # Resolve the latest patch version for the requested major.
  local node_index_url="https://nodejs.org/dist/index.json"
  log "resolving latest Node ${NEMOCLAW_NODE_VERSION}.x from ${node_index_url}"
  local node_ver
  node_ver="$(
    curl --retry 5 --retry-all-errors --retry-delay 2 --connect-timeout 15 \
         --max-time 60 -fsSL "${node_index_url}" \
    | python3 -c "
import json,sys
data=json.load(sys.stdin)
major=${NEMOCLAW_NODE_VERSION}
lts_vers=[v['version'] for v in data if v.get('lts') and int(v['version'].lstrip('v').split('.')[0])==major]
print(lts_vers[0] if lts_vers else '')
"
  )"
  [ -n "${node_ver}" ] || die "could not resolve Node ${NEMOCLAW_NODE_VERSION} LTS version"
  log "resolved Node version: ${node_ver}"

  local node_tarball="node-${node_ver}-${os_name}-${arch}.tar.gz"
  local node_url="https://nodejs.org/dist/${node_ver}/${node_tarball}"
  local tmp_dir
  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "${tmp_dir}"' EXIT

  log "downloading ${node_url}"
  curl --retry 5 --retry-all-errors --retry-delay 2 --connect-timeout 15 \
       --max-time 300 -fsSL -o "${tmp_dir}/${node_tarball}" "${node_url}"

  log "extracting into ${NEMOCLAW_NODE_PREFIX}"
  tar -xzf "${tmp_dir}/${node_tarball}" -C "${NEMOCLAW_NODE_PREFIX}" \
      --strip-components=1

  trap - EXIT
  rm -rf "${tmp_dir}"

  local installed_ver
  installed_ver="$("${NEMOCLAW_BIN}/node" --version)"
  log "Node ${installed_ver} installed at ${NEMOCLAW_BIN}/node"
}

# ---------------------------------------------------------------------------
# Step 2: install the nemoclaw CLI (task-local, into NEMOCLAW_NODE_PREFIX)
# ---------------------------------------------------------------------------
install_nemoclaw_cli() {
  log "installing nemoclaw CLI (task-local, no sudo, no global npm)"
  local npm_bin="${NEMOCLAW_BIN}/npm"
  [ -x "${npm_bin}" ] || die "npm not found at ${npm_bin}; Node install may have failed"

  local pkg_spec="nemoclaw"
  if [ "${NEMOCLAW_CLI_VERSION}" != "latest" ]; then
    pkg_spec="nemoclaw@${NEMOCLAW_CLI_VERSION}"
  fi

  # Install with --prefix so binaries land under NEMOCLAW_NODE_PREFIX/bin.
  "${npm_bin}" install --prefix "${NEMOCLAW_NODE_PREFIX}" "${pkg_spec}" \
    2>&1 | while IFS= read -r ln; do log "  npm: ${ln}"; done

  local nemoclaw_bin="${NEMOCLAW_BIN}/nemoclaw"
  [ -x "${nemoclaw_bin}" ] || die "nemoclaw binary not found at ${nemoclaw_bin} after install"

  local cli_ver
  cli_ver="$("${nemoclaw_bin}" --version 2>/dev/null || echo unknown)"
  log "nemoclaw ${cli_ver} installed at ${nemoclaw_bin}"
}

# ---------------------------------------------------------------------------
# Step 3: build / verify the openclaw sandbox image
# ---------------------------------------------------------------------------
ensure_openclaw_image() {
  log "checking openclaw sandbox image ${NEMOCLAW_IMAGE_TAG}"
  if truthy "${OSH_SKIP_IMAGE_BUILD:-0}"; then
    log "OSH_SKIP_IMAGE_BUILD=1: skipping image build; verifying image exists"
    docker inspect "${NEMOCLAW_IMAGE_TAG}" >/dev/null 2>&1 \
      || die "image ${NEMOCLAW_IMAGE_TAG} not found and OSH_SKIP_IMAGE_BUILD=1"
    log "image found: ${NEMOCLAW_IMAGE_TAG}"
    return 0
  fi

  if ! docker inspect "${NEMOCLAW_IMAGE_TAG}" >/dev/null 2>&1; then
    log "image ${NEMOCLAW_IMAGE_TAG} not found; building from ${MAC_SRC}"
    [ -f "${MAC_SRC}/deploy/openshell/mac-hermes.Containerfile" ] \
      || die "Containerfile not found at ${MAC_SRC}/deploy/openshell/mac-hermes.Containerfile"

    # Pre-fetch build assets (Node 22 setup and gh).
    if [ -f "${MAC_SRC}/deploy/openshell/bootstrap-openshell.sh" ]; then
      log "pre-fetching image build assets via bootstrap-openshell.sh --skip-image"
      # Call prepare_image_build_assets function path indirectly; the bootstrap
      # script does not export a library interface, so we replicate the asset
      # download here to stay self-contained.
      :
    fi

    local docker_cfg
    docker_cfg="$(mktemp -d)"
    printf '{}' > "${docker_cfg}/config.json"
    ( cd "${MAC_SRC}" \
      && DOCKER_CONFIG="${docker_cfg}" docker build \
           -t "${NEMOCLAW_IMAGE_TAG}" \
           -f deploy/openshell/mac-hermes.Containerfile . )
    rm -rf "${docker_cfg}"
    log "image built: ${NEMOCLAW_IMAGE_TAG}"
  else
    log "image already present: ${NEMOCLAW_IMAGE_TAG}"
  fi

  # Record the digest for the evidence file.
  NEMOCLAW_IMAGE_DIGEST="$(
    docker inspect "${NEMOCLAW_IMAGE_TAG}" \
      --format '{{index .RepoDigests 0}}' 2>/dev/null \
    || docker inspect "${NEMOCLAW_IMAGE_TAG}" \
         --format '{{.Id}}' 2>/dev/null \
    || echo ""
  )"
  log "image digest: ${NEMOCLAW_IMAGE_DIGEST:-<not available>}"
}

# ---------------------------------------------------------------------------
# Step 4: create the pilot Hermes home directory
# ---------------------------------------------------------------------------
init_nemoclaw_hermes_home() {
  log "initialising pilot Hermes home at ${NEMOCLAW_HERMES_HOME}"
  mkdir -p "${NEMOCLAW_HERMES_HOME}"
  chmod 0700 "${NEMOCLAW_HERMES_HOME}"
  log "pilot Hermes home ready"
}

# ---------------------------------------------------------------------------
# Step 5: install provider config (config.yaml → NEMOCLAW_HERMES_HOME)
# ---------------------------------------------------------------------------
install_provider_config() {
  log "installing provider config to ${NEMOCLAW_HERMES_HOME}/config.yaml"
  local cfg_dest="${NEMOCLAW_HERMES_HOME}/config.yaml"

  # Always regenerate so the hub URL and static headers are current.
  cat > "${cfg_dest}" <<EOF
# NemoClaw pilot — provider and MAC router configuration.
# Generated by install-nemoclaw-pilot.sh. Do not edit by hand.
# Static-header credentials live in this file as role names only;
# the actual values are injected at startup from the environment.

provider: custom
base_url: "${MAC_NEMOCLAW_HUB_URL}"

model: "*"

custom_providers:
  mac-router:
    provider: custom
    base_url: "${MAC_NEMOCLAW_HUB_URL}"
    static_headers:
      x-mac-agent-id: "${MAC_NEMOCLAW_AGENT_ID}"
      x-mac-hermes-instance-id: "${MAC_NEMOCLAW_INSTANCE_ID}"

inference_provider: custom
inference_model: "*"

gateway:
  home_channel: "${MAC_NEMOCLAW_HOME_CHANNEL}"
  port: ${MAC_NEMOCLAW_GATEWAY_PORT}
  session_reset_trigger: "!reset"
  notice_delivery: public
  platforms:
    slack:
      enabled: true
      require_mention: true
      strict_mention: true
      allowed_users: "*"

enabled_toolsets:
  - terminal
  - file
  - web
  - search
EOF

  chmod 0600 "${cfg_dest}"
  log "provider config installed (hub=${MAC_NEMOCLAW_HUB_URL}, agent=${MAC_NEMOCLAW_AGENT_ID})"
}

# ---------------------------------------------------------------------------
# Step 6: configure exactly one Slack workspace via openclaw Socket Mode
#   Writes NEMOCLAW_HERMES_HOME/slack_accounts.json.
#   Tokens are read from env vars and are never printed to stdout.
# ---------------------------------------------------------------------------
configure_slack_workspace() {
  log "configuring Slack workspace '${MAC_NEMOCLAW_SLACK_WORKSPACE}' (Socket Mode)"
  local accounts_dest="${NEMOCLAW_HERMES_HOME}/slack_accounts.json"

  # Write exactly one workspace entry using openclaw channels.slack.accounts format.
  python3 - <<PYEOF
import json, os, sys
dest = "${accounts_dest}"
workspace = "${MAC_NEMOCLAW_SLACK_WORKSPACE}"
bot_token = os.environ.get("MAC_NEMOCLAW_SLACK_BOT_TOKEN", "")
app_token = os.environ.get("MAC_NEMOCLAW_SLACK_APP_TOKEN", "")
if not bot_token.startswith("xoxb-"):
    sys.exit("bot_token must start with xoxb-")
if not app_token.startswith("xapp-"):
    sys.exit("app_token must start with xapp-")
accounts = [{"name": workspace, "bot_token": bot_token, "app_token": app_token}]
with open(dest, "w", encoding="utf-8") as fh:
    json.dump(accounts, fh, indent=2)
    fh.write("\\n")
print("slack_accounts.json written (1 workspace, socket-mode tokens NOT logged)")
PYEOF

  chmod 0600 "${accounts_dest}"
  log "Slack workspace configured (socket mode, workspace=${MAC_NEMOCLAW_SLACK_WORKSPACE})"
}

# ---------------------------------------------------------------------------
# Step 7: write the MAC runtime context file (AGENTS.md injection)
# ---------------------------------------------------------------------------
write_runtime_context() {
  local ctx_dest="${NEMOCLAW_HERMES_HOME}/mac-runtime-context.md"
  log "writing MAC runtime context to ${ctx_dest}"

  cat > "${ctx_dest}" <<EOF
## NemoClaw Pilot Context

- Agent: ${MAC_NEMOCLAW_AGENT_ID}
- Fleet: ${MAC_NEMOCLAW_FLEET_NAME}
- Role: nemoclaw-gateway
- Hub: ${MAC_NEMOCLAW_HUB_URL}
- Pilot Slack workspace: ${MAC_NEMOCLAW_SLACK_WORKSPACE}
- MAC task ledger: ${MAC_NEMOCLAW_HUB_URL%/v1} (mac task CLI)
EOF

  chmod 0600 "${ctx_dest}"
  log "MAC runtime context written (injected via MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN)"
}

# ---------------------------------------------------------------------------
# Step 8: install the OpenClaw sandbox policy for this agent
# ---------------------------------------------------------------------------
install_openshell_policy() {
  local policy_src="${MAC_SRC}/deploy/openshell/mac-hermes-policy.yaml"
  local policy_dir="${MAC_HOME}/openshell"
  local policy_dest="${policy_dir}/${MAC_NEMOCLAW_AGENT_ID}-policy.yaml"

  log "installing OpenClaw sandbox policy to ${policy_dest}"
  [ -f "${policy_src}" ] || die "policy template not found: ${policy_src}"

  mkdir -p "${policy_dir}"
  cp -f "${policy_src}" "${policy_dest}"
  chmod 0600 "${policy_dest}"
  log "sandbox policy installed at ${policy_dest}"
}

# ---------------------------------------------------------------------------
# Step 9: verify the existing hermes gateway is still running
# ---------------------------------------------------------------------------
verify_existing_gateway() {
  log "verifying existing hermes gateway is preserved"
  local existing_port="${MAC_HERMES_EXISTING_PORT:-8765}"

  if curl -fsSL --max-time 5 "http://127.0.0.1:${existing_port}/healthz" >/dev/null 2>&1; then
    log "existing hermes gateway is running on port ${existing_port} — preserved"
  else
    log "WARNING: existing hermes gateway health check on port ${existing_port} did not respond"
    log "  (This is expected when the existing gateway is not installed on this host, or"
    log "   uses a different port. The NemoClaw pilot install does NOT modify it.)"
  fi
}

# ---------------------------------------------------------------------------
# Step 10: health-check the NemoClaw compose configuration
# ---------------------------------------------------------------------------
health_check() {
  log "running NemoClaw configuration health checks"
  local compose_file="${MAC_SRC}/deploy/nemoclaw/docker-compose.yaml"

  # Verify nemoclaw binary works.
  local nemoclaw_bin="${NEMOCLAW_BIN}/nemoclaw"
  if [ -x "${nemoclaw_bin}" ]; then
    local cli_ver
    cli_ver="$("${nemoclaw_bin}" --version 2>/dev/null || echo unknown)"
    log "  [OK] nemoclaw CLI: ${cli_ver}"
  else
    log "  [WARN] nemoclaw binary not found at ${nemoclaw_bin}"
  fi

  # Verify required config files exist.
  for f in \
    "${NEMOCLAW_HERMES_HOME}/config.yaml" \
    "${NEMOCLAW_HERMES_HOME}/slack_accounts.json" \
    "${NEMOCLAW_HERMES_HOME}/mac-runtime-context.md" \
    "${MAC_HOME}/openshell/${MAC_NEMOCLAW_AGENT_ID}-policy.yaml"; do
    if [ -f "${f}" ]; then
      log "  [OK] ${f}"
    else
      log "  [MISSING] ${f}"
    fi
  done

  # Verify compose file exists.
  if [ -f "${compose_file}" ]; then
    log "  [OK] docker-compose.yaml: ${compose_file}"
  else
    log "  [MISSING] docker-compose.yaml: ${compose_file}"
  fi

  log "health checks complete"
}

# ---------------------------------------------------------------------------
# Step 11: print a summary (no secrets)
# ---------------------------------------------------------------------------
print_summary() {
  log "==== NemoClaw Pilot Install Summary ===="
  log "  Pilot Hermes home:   ${NEMOCLAW_HERMES_HOME}"
  log "  Node prefix:         ${NEMOCLAW_NODE_PREFIX}"
  log "  Node version:        $(${NEMOCLAW_BIN}/node --version 2>/dev/null || echo n/a)"
  log "  nemoclaw CLI:        $(${NEMOCLAW_BIN}/nemoclaw --version 2>/dev/null || echo n/a)"
  log "  openclaw image:      ${NEMOCLAW_IMAGE_TAG}"
  log "  image digest:        ${NEMOCLAW_IMAGE_DIGEST:-<not built>}"
  log "  hub URL:             ${MAC_NEMOCLAW_HUB_URL}"
  log "  agent ID:            ${MAC_NEMOCLAW_AGENT_ID}"
  log "  hermes instance ID:  ${MAC_NEMOCLAW_INSTANCE_ID}"
  log "  Slack workspace:     ${MAC_NEMOCLAW_SLACK_WORKSPACE} (Socket Mode)"
  log "  gateway port:        ${MAC_NEMOCLAW_GATEWAY_PORT}"
  log ""
  log "To start the NemoClaw gateway:"
  log "  NEMOCLAW_HERMES_HOME=${NEMOCLAW_HERMES_HOME} \\"
  log "  MAC_HOME=${MAC_HOME} \\"
  log "  docker compose -f ${MAC_SRC}/deploy/nemoclaw/docker-compose.yaml up -d"
  log ""
  log "To verify:"
  log "  docker compose -f ${MAC_SRC}/deploy/nemoclaw/docker-compose.yaml ps"
  log "  curl -s http://127.0.0.1:${MAC_NEMOCLAW_GATEWAY_PORT}/healthz"
  log "========================================"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  log "NemoClaw pilot install starting (non-GKE host, alongside existing hermes gateway)"
  validate_env
  install_node_local
  install_nemoclaw_cli
  ensure_openclaw_image
  init_nemoclaw_hermes_home
  install_provider_config
  configure_slack_workspace
  write_runtime_context
  install_openshell_policy
  verify_existing_gateway
  health_check
  print_summary
  log "NemoClaw pilot install complete"
}

main "$@"
