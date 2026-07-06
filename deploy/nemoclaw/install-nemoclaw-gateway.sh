#!/usr/bin/env bash
# install-nemoclaw-gateway.sh — YOLO migration: replace the hermes chat-gateway
# role with NemoClaw (NemoClaw/OpenClaw) on one fleet host.
#
# YOLO scope (2026-07-04 rescope):
#   - Fleet is pre-release, sole user, no compat requirements.
#   - Replaces com.<fleet>.hermes-gateway / <fleet>-hermes-gateway.service with
#     the NemoClaw unit (deploy/systemd/mac-nemoclaw-gateway.service).
#   - The hermes gateway service is STOPPED and DISABLED after migration.
#     It is NOT uninstalled so rollback is: re-enable + start hermes, stop + disable nemoclaw.
#   - The vendored Hermes remains the task-executor fallback runner (ADR 0001).
#     That role is NOT migrating.
#   - Multi-slack patch and gateway-side hermes patches become dead code;
#     a follow-up task will prune them from deploy tooling.
#
# Host sequence:
#   Run on each host in this order: bullwinkle, natasha, pods, rocky (hub LAST).
#   If migration fails on a host, leave its hermes gateway running and record why.
#
# Credential migration (HOST-LOCAL ONLY — never committed):
#   1. Slack: ~/.hermes/slack_accounts.json -> NEMOCLAW_HERMES_HOME/slack_accounts.json
#      format: [{name, bot_token, app_token}] (openclaw channels.slack.accounts)
#      Tokens are read from env/SecretRef; never written to committed artifacts.
#   2. Router: MAC_API_TOKEN provides the api key; x-mac-agent-id / x-mac-hermes-instance-id
#      are injected via the config.yaml static_headers (written by this script from env).
#   3. Runbook section "Credential Audit" records which env vars map to which credentials.
#
# Idempotent: safe to re-run. Each step checks before acting.
#
# Prerequisites:
#   - OpenShell 0.0.72 already installed (`openshell --version` must output 0.0.72).
#     Install via deploy/openshell/bootstrap-openshell.sh if not present.
#   - Docker Engine/Moby installed and running.
#   - Node 22+ available (or will be installed task-locally below).
#   - mac source tree at MAC_SRC (default $MAC_HOME/src/mac).
#   - Slack app tokens obtained for this host's workspace.
#   - Hub reachable: curl $MAC_NEMOCLAW_HUB_URL/healthz returns 200.
#
# Required environment (set in /etc/mac/mac.env or export before running):
#   MAC_NEMOCLAW_HUB_URL       — MAC router base URL, e.g. http://hub.example.internal:8789/v1
#   MAC_NEMOCLAW_AGENT_ID      — stable fleet identity, e.g. agent_bullwinkle
#   MAC_NEMOCLAW_INSTANCE_ID   — Hermes instance ID, e.g. hermes_bullwinkle_nemoclaw
#   MAC_NEMOCLAW_SLACK_BOT_TOKEN  — xoxb-... (read from env; never logged)
#   MAC_NEMOCLAW_SLACK_APP_TOKEN  — xapp-... (read from env; never logged)
#   MAC_NEMOCLAW_SLACK_WORKSPACE  — workspace display name, e.g. my-fleet
#   MAC_NEMOCLAW_FLEET_NAME    — fleet name, e.g. my-fleet
#
# Optional:
#   MAC_HOME                   — default: $HOME/.mac
#   MAC_SRC                    — default: $MAC_HOME/src/mac
#   NEMOCLAW_HERMES_HOME       — default: $HOME/.hermes  (replaces old hermes home in-place)
#   MAC_NEMOCLAW_GATEWAY_PORT  — gateway port (default: 8765, replaces hermes gateway port)
#   MAC_NEMOCLAW_HOME_CHANNEL  — Slack channel name without '#' (default: general)
#   NEMOCLAW_NODE_VERSION      — Node version (default: 22)
#   NEMOCLAW_CLI_VERSION       — nemoclaw CLI npm package version (default: latest)
#   OSH_SKIP_IMAGE_BUILD       — set 1 to skip docker build (image must exist)
#   NEMOCLAW_SKIP_HERMES_DISABLE — set 1 to skip disabling the hermes service (dry-run)
#   NEMOCLAW_DRY_RUN           — set 1 to print steps without executing systemctl changes
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MAC_HOME="${MAC_HOME:-$HOME/.mac}"
MAC_SRC="${MAC_SRC:-$MAC_HOME/src/mac}"
# YOLO: NemoClaw takes over the existing hermes home by default so the MAC
# runtime context, creds, and channel config are already in place.
# Override NEMOCLAW_HERMES_HOME to keep them separate on a host that still
# needs the hermes executor.
NEMOCLAW_HERMES_HOME="${NEMOCLAW_HERMES_HOME:-$HOME/.hermes}"
NEMOCLAW_NODE_VERSION="${NEMOCLAW_NODE_VERSION:-22}"
NEMOCLAW_CLI_VERSION="${NEMOCLAW_CLI_VERSION:-latest}"
NEMOCLAW_IMAGE_TAG="localhost/mac-hermes:net"
# YOLO: gateway takes over the hermes port (8765 by default).
MAC_NEMOCLAW_GATEWAY_PORT="${MAC_NEMOCLAW_GATEWAY_PORT:-8765}"
MAC_NEMOCLAW_HOME_CHANNEL="${MAC_NEMOCLAW_HOME_CHANNEL:-general}"
OSH_SKIP_IMAGE_BUILD="${OSH_SKIP_IMAGE_BUILD:-0}"
NEMOCLAW_SKIP_HERMES_DISABLE="${NEMOCLAW_SKIP_HERMES_DISABLE:-0}"
NEMOCLAW_DRY_RUN="${NEMOCLAW_DRY_RUN:-0}"

# Task-local node/npm prefix — never touches the system PATH or global npm.
NEMOCLAW_NODE_PREFIX="${MAC_HOME}/nemoclaw-node"
NEMOCLAW_BIN="${NEMOCLAW_NODE_PREFIX}/bin"

# Derive fleet name from agent ID for service naming (e.g. agent_bullwinkle -> mac).
FLEET_NAME="${MAC_NEMOCLAW_FLEET_NAME:-mac}"

log()  { printf '[install-nemoclaw-gateway] %s\n' "$*"; }
die()  { log "ERROR: $*" >&2; exit 1; }
info() { log "INFO: $*"; }
truthy() { case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in 1|true|yes|on) return 0;; *) return 1;; esac; }

maybe_run() {
  # In dry-run mode, print the command instead of running it.
  if truthy "${NEMOCLAW_DRY_RUN:-0}"; then
    log "DRY-RUN: $*"
  else
    "$@"
  fi
}

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
  [[ "${MAC_NEMOCLAW_SLACK_BOT_TOKEN}" == xoxb-* ]] \
    || die "MAC_NEMOCLAW_SLACK_BOT_TOKEN must start with 'xoxb-'"
  [[ "${MAC_NEMOCLAW_SLACK_APP_TOKEN}" == xapp-* ]] \
    || die "MAC_NEMOCLAW_SLACK_APP_TOKEN must start with 'xapp-'"
  log "environment validated"
}

# ---------------------------------------------------------------------------
# Step 1: verify OpenShell 0.0.72 is installed (NemoClaw hard pin)
# ---------------------------------------------------------------------------
verify_openshell() {
  log "verifying OpenShell 0.0.72 (NemoClaw hard pin)"
  local required_ver="0.0.72"
  if ! command -v openshell >/dev/null 2>&1; then
    die "openshell CLI not found. Install via deploy/openshell/bootstrap-openshell.sh OPENSHELL_VERSION=${required_ver}"
  fi
  local installed_ver
  installed_ver="$(openshell --version 2>/dev/null | awk '{print $NF}')"
  if [ "${installed_ver}" != "${required_ver}" ]; then
    die "OpenShell ${installed_ver} installed but NemoClaw requires ${required_ver}. Run: OPENSHELL_VERSION=${required_ver} deploy/openshell/bootstrap-openshell.sh"
  fi
  log "OpenShell ${installed_ver} confirmed"
}

# ---------------------------------------------------------------------------
# Step 2: install task-local Node 22+
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

  # Fall back to system node if it satisfies the version requirement.
  if command -v node >/dev/null 2>&1; then
    local sys_ver
    sys_ver="$(node --version 2>/dev/null | sed 's/^v//')"
    local sys_major="${sys_ver%%.*}"
    if [ "${sys_major:-0}" -ge "${NEMOCLAW_NODE_VERSION}" ]; then
      log "system Node ${sys_ver} satisfies requirement; symlinking into task prefix"
      mkdir -p "${NEMOCLAW_BIN}"
      ln -sf "$(command -v node)" "${NEMOCLAW_BIN}/node"
      ln -sf "$(command -v npm)"  "${NEMOCLAW_BIN}/npm" 2>/dev/null || true
      return 0
    fi
  fi

  mkdir -p "${NEMOCLAW_NODE_PREFIX}"
  log "installing Node ${NEMOCLAW_NODE_VERSION} LTS via tarball into ${NEMOCLAW_NODE_PREFIX}"

  local arch
  arch="$(uname -m)"
  case "${arch}" in
    x86_64)          arch="x64" ;;
    aarch64|arm64)   arch="arm64" ;;
    *) die "unsupported architecture for local Node install: ${arch}" ;;
  esac

  local os_name
  os_name="$(uname -s | tr '[:upper:]' '[:lower:]')"

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
# Step 3: install the nemoclaw CLI (task-local)
# ---------------------------------------------------------------------------
install_nemoclaw_cli() {
  log "installing nemoclaw CLI (task-local, no sudo, no global npm)"
  local npm_bin="${NEMOCLAW_BIN}/npm"
  [ -x "${npm_bin}" ] || die "npm not found at ${npm_bin}; Node install may have failed"

  local pkg_spec="nemoclaw"
  if [ "${NEMOCLAW_CLI_VERSION}" != "latest" ]; then
    pkg_spec="nemoclaw@${NEMOCLAW_CLI_VERSION}"
  fi

  "${npm_bin}" install --prefix "${NEMOCLAW_NODE_PREFIX}" "${pkg_spec}" \
    2>&1 | while IFS= read -r ln; do log "  npm: ${ln}"; done

  local nemoclaw_bin="${NEMOCLAW_BIN}/nemoclaw"
  [ -x "${nemoclaw_bin}" ] || die "nemoclaw binary not found at ${nemoclaw_bin} after install"

  local cli_ver
  cli_ver="$("${nemoclaw_bin}" --version 2>/dev/null || echo unknown)"
  log "nemoclaw ${cli_ver} installed at ${nemoclaw_bin}"
}

# ---------------------------------------------------------------------------
# Step 4: build / verify the openclaw sandbox image
# ---------------------------------------------------------------------------
ensure_openclaw_image() {
  log "checking openclaw sandbox image ${NEMOCLAW_IMAGE_TAG}"
  if truthy "${OSH_SKIP_IMAGE_BUILD:-0}"; then
    docker inspect "${NEMOCLAW_IMAGE_TAG}" >/dev/null 2>&1 \
      || die "image ${NEMOCLAW_IMAGE_TAG} not found and OSH_SKIP_IMAGE_BUILD=1"
    log "image found (build skipped): ${NEMOCLAW_IMAGE_TAG}"
    return 0
  fi

  if ! docker inspect "${NEMOCLAW_IMAGE_TAG}" >/dev/null 2>&1; then
    log "image ${NEMOCLAW_IMAGE_TAG} not found; building from ${MAC_SRC}"
    [ -f "${MAC_SRC}/deploy/openshell/mac-hermes.Containerfile" ] \
      || die "Containerfile not found at ${MAC_SRC}/deploy/openshell/mac-hermes.Containerfile"
    ( cd "${MAC_SRC}" && docker build -t "${NEMOCLAW_IMAGE_TAG}" \
        -f deploy/openshell/mac-hermes.Containerfile . )
    log "image built: ${NEMOCLAW_IMAGE_TAG}"
  else
    log "image already present: ${NEMOCLAW_IMAGE_TAG}"
  fi

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
# Step 5: prepare the nemoclaw Hermes home directory
# ---------------------------------------------------------------------------
init_nemoclaw_hermes_home() {
  log "preparing nemoclaw Hermes home at ${NEMOCLAW_HERMES_HOME}"
  mkdir -p "${NEMOCLAW_HERMES_HOME}"
  chmod 0700 "${NEMOCLAW_HERMES_HOME}"
  log "Hermes home ready"
}

# ---------------------------------------------------------------------------
# Step 6: migrate Slack credentials from hermes format to openclaw format
# ---------------------------------------------------------------------------
migrate_slack_credentials() {
  log "migrating Slack credentials to openclaw format"
  local accounts_dest="${NEMOCLAW_HERMES_HOME}/slack_accounts.json"
  local hermes_accounts="${HOME}/.hermes/slack_accounts.json"

  # If the destination already has real tokens (from a prior run or manual
  # setup), do not overwrite.  The operator may have already rotated.
  if [ -f "${accounts_dest}" ]; then
    local existing_size
    existing_size="$(wc -c < "${accounts_dest}" 2>/dev/null || echo 0)"
    if [ "${existing_size}" -gt 10 ]; then
      log "slack_accounts.json already present at ${accounts_dest} (${existing_size} bytes); skipping migration"
      return 0
    fi
  fi

  # Write from env vars (canonical: env/SecretRef only, never literal tokens).
  python3 - <<PYEOF
import json, os, sys
dest = "${accounts_dest}"
workspace = "${MAC_NEMOCLAW_SLACK_WORKSPACE}"
bot_token = os.environ.get("MAC_NEMOCLAW_SLACK_BOT_TOKEN", "")
app_token = os.environ.get("MAC_NEMOCLAW_SLACK_APP_TOKEN", "")
if not bot_token.startswith("xoxb-"):
    sys.exit("MAC_NEMOCLAW_SLACK_BOT_TOKEN must start with xoxb-")
if not app_token.startswith("xapp-"):
    sys.exit("MAC_NEMOCLAW_SLACK_APP_TOKEN must start with xapp-")
accounts = [{"name": workspace, "bot_token": bot_token, "app_token": app_token}]
with open(dest, "w", encoding="utf-8") as fh:
    json.dump(accounts, fh, indent=2)
    fh.write("\n")
print("slack_accounts.json written (1 workspace; tokens NOT logged)")
PYEOF

  chmod 0600 "${accounts_dest}"
  log "Slack credentials migrated (workspace=${MAC_NEMOCLAW_SLACK_WORKSPACE})"
}

# ---------------------------------------------------------------------------
# Step 7: install/update the provider config
# ---------------------------------------------------------------------------
install_provider_config() {
  log "writing nemoclaw provider config to ${NEMOCLAW_HERMES_HOME}/config.yaml"
  local cfg_dest="${NEMOCLAW_HERMES_HOME}/config.yaml"

  cat > "${cfg_dest}" <<EOF
# NemoClaw gateway — provider and MAC router configuration.
# Generated by install-nemoclaw-gateway.sh. Do not edit by hand.
# Credential values are injected at runtime from mac.env; never committed here.

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
  log "provider config written (hub=${MAC_NEMOCLAW_HUB_URL}, agent=${MAC_NEMOCLAW_AGENT_ID})"
}

# ---------------------------------------------------------------------------
# Step 8: write the MAC runtime context file
# ---------------------------------------------------------------------------
write_runtime_context() {
  local ctx_dest="${NEMOCLAW_HERMES_HOME}/mac-runtime-context.md"
  log "writing MAC runtime context to ${ctx_dest}"

  cat > "${ctx_dest}" <<EOF
## NemoClaw Gateway Runtime Context

- Agent: ${MAC_NEMOCLAW_AGENT_ID}
- Fleet: ${MAC_NEMOCLAW_FLEET_NAME}
- Role: nemoclaw-gateway (chat-gateway; task-executor is separate hermes process)
- Hub: ${MAC_NEMOCLAW_HUB_URL%/v1}
- Gateway workspace: ${MAC_NEMOCLAW_SLACK_WORKSPACE}
- MAC task ledger: ${MAC_NEMOCLAW_HUB_URL%/v1} (mac task CLI)
EOF

  chmod 0600 "${ctx_dest}"
  log "runtime context written"
}

# ---------------------------------------------------------------------------
# Step 9: install the OpenClaw sandbox policy for this agent
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
  log "sandbox policy installed"
}

# ---------------------------------------------------------------------------
# Step 10: install the nemoclaw-gateway launch wrapper
# ---------------------------------------------------------------------------
install_gateway_wrapper() {
  local wrapper="${MAC_HOME}/bin/nemoclaw-gateway"
  mkdir -p "${MAC_HOME}/bin"
  cat > "${wrapper}" <<EOF
#!/usr/bin/env bash
# nemoclaw-gateway wrapper — written by install-nemoclaw-gateway.sh
set -euo pipefail
ulimit -n "\${MAC_SERVICE_NOFILE_LIMIT:-4096}" 2>/dev/null || true
set -a
set +u
[ -f "\$HOME/.mac/mac.env" ] && . "\$HOME/.mac/mac.env"
set -u
set +a
export PATH="${NEMOCLAW_BIN}:\$HOME/.mac/bin:\$HOME/.mac/venv/bin:\$PATH"
export HERMES_HOME="\${NEMOCLAW_HERMES_HOME:-\$HOME/.hermes}"
export NEMOCLAW_HERMES_HOME="\${NEMOCLAW_HERMES_HOME:-\$HOME/.hermes}"
export MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN="\${NEMOCLAW_HERMES_HOME}/mac-runtime-context.md"
export MAC_HERMES_RUNTIME_CONTEXT_REQUIRED="\${MAC_HERMES_RUNTIME_CONTEXT_REQUIRED:-1}"
export MAC_OPENSHELL_REQUIRED="\${MAC_OPENSHELL_REQUIRED:-1}"
export MAC_OPENSHELL_POLICY="\${MAC_HOME:-\$HOME/.mac}/openshell/${MAC_NEMOCLAW_AGENT_ID}-policy.yaml"
exec "\${NEMOCLAW_BIN}/nemoclaw" gateway run
EOF
  chmod 700 "${wrapper}"
  log "gateway wrapper written to ${wrapper}"
}

# ---------------------------------------------------------------------------
# Step 11: stop and disable the hermes gateway service
# ---------------------------------------------------------------------------
disable_hermes_gateway() {
  if truthy "${NEMOCLAW_SKIP_HERMES_DISABLE:-0}"; then
    log "NEMOCLAW_SKIP_HERMES_DISABLE=1: skipping hermes gateway disable (dry-run safe path)"
    return 0
  fi

  local hermes_svc="${FLEET_NAME}-hermes-gateway.service"
  log "stopping and disabling hermes gateway service: ${hermes_svc}"

  if systemctl is-active --quiet "${hermes_svc}" 2>/dev/null; then
    maybe_run sudo systemctl stop "${hermes_svc}"
    log "hermes gateway stopped"
  else
    log "hermes gateway was not running (or service not installed)"
  fi

  if systemctl is-enabled --quiet "${hermes_svc}" 2>/dev/null; then
    maybe_run sudo systemctl disable "${hermes_svc}"
    log "hermes gateway disabled"
  else
    log "hermes gateway was already disabled (or service not installed)"
  fi
}

# ---------------------------------------------------------------------------
# Step 12: install and start the nemoclaw gateway systemd service
# ---------------------------------------------------------------------------
install_nemoclaw_service() {
  local unit_src="${MAC_SRC}/deploy/systemd/mac-nemoclaw-gateway.service"
  local unit_name="${FLEET_NAME}-nemoclaw-gateway.service"
  local unit_dest="/etc/systemd/system/${unit_name}"

  log "installing systemd unit ${unit_dest}"
  [ -f "${unit_src}" ] || die "service template not found: ${unit_src}"

  if sudo test -f "${unit_dest}"; then
    local backup_path="${MAC_HOME}/backups/${unit_name}.$(date -u +%Y%m%dT%H%M%SZ)"
    maybe_run sudo cp -f "${unit_dest}" "${backup_path}"
    log "existing unit backed up to ${backup_path}"
  fi

  maybe_run sudo cp -f "${unit_src}" "${unit_dest}"
  maybe_run sudo systemctl daemon-reload
  maybe_run sudo systemctl enable "${unit_name}"
  maybe_run sudo systemctl restart "${unit_name}"

  sleep 3
  if ! truthy "${NEMOCLAW_DRY_RUN:-0}"; then
    sudo systemctl --no-pager -l status "${unit_name}" || true
  fi
  log "nemoclaw gateway service enabled and started"
}

# ---------------------------------------------------------------------------
# Step 13: verify hub-verify and executor sandbox still work
# ---------------------------------------------------------------------------
verify_executor_sandbox() {
  log "verifying executor sandbox + hub-verify still work after migration"

  # Executor uses the main venv's hermes, not the nemoclaw gateway.
  # Check that the main venv python is functional.
  if [ -f "${MAC_HOME}/venv/bin/python" ]; then
    local py_ver
    py_ver="$("${MAC_HOME}/venv/bin/python" --version 2>&1)"
    log "  [OK] executor venv: ${py_ver}"
  else
    log "  [WARN] executor venv not found at ${MAC_HOME}/venv/bin/python"
  fi

  # Check that mac.hermes_gateway module is importable (executor fallback).
  if "${MAC_HOME}/venv/bin/python" -c "import mac.hermes_gateway" 2>/dev/null; then
    log "  [OK] mac.hermes_gateway importable (executor fallback intact)"
  else
    log "  [WARN] mac.hermes_gateway import failed; check hermes-gateway extra installation"
  fi

  # Check hub connectivity.
  local hub_base="${MAC_NEMOCLAW_HUB_URL%/v1}"
  if curl -fsSL --max-time 10 "${hub_base}/healthz" >/dev/null 2>&1; then
    log "  [OK] hub healthz: ${hub_base}/healthz"
  else
    log "  [WARN] hub healthz did not respond at ${hub_base}/healthz"
  fi
}

# ---------------------------------------------------------------------------
# Step 14: verify the nemoclaw gateway is running
# ---------------------------------------------------------------------------
verify_nemoclaw_gateway() {
  log "verifying nemoclaw gateway is up"
  local unit_name="${FLEET_NAME}-nemoclaw-gateway.service"

  if truthy "${NEMOCLAW_DRY_RUN:-0}"; then
    log "  DRY-RUN: skipping live service check"
    return 0
  fi

  if systemctl is-active --quiet "${unit_name}" 2>/dev/null; then
    log "  [OK] ${unit_name} is active"
  else
    log "  [WARN] ${unit_name} is not active; check journalctl -u ${unit_name}"
  fi

  # Try the gateway health endpoint.
  if curl -fsSL --max-time 5 "http://127.0.0.1:${MAC_NEMOCLAW_GATEWAY_PORT}/healthz" >/dev/null 2>&1; then
    log "  [OK] gateway healthz on port ${MAC_NEMOCLAW_GATEWAY_PORT}"
  else
    log "  [WARN] gateway healthz did not respond on port ${MAC_NEMOCLAW_GATEWAY_PORT} (may be normal if gateway uses Slack socket mode only)"
  fi
}

# ---------------------------------------------------------------------------
# Step 15: print a summary (no secrets)
# ---------------------------------------------------------------------------
print_summary() {
  log "==== NemoClaw Gateway Migration Summary ===="
  log "  Hermes home:         ${NEMOCLAW_HERMES_HOME}"
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
  log "  hermes gateway:      DISABLED (executor fallback preserved)"
  log ""
  log "Credential audit:"
  log "  Slack bot_token:     from env MAC_NEMOCLAW_SLACK_BOT_TOKEN"
  log "  Slack app_token:     from env MAC_NEMOCLAW_SLACK_APP_TOKEN"
  log "  Router API key:      from mac.env OPENAI_API_KEY / MAC_API_TOKEN"
  log "  Router x-headers:    x-mac-agent-id=${MAC_NEMOCLAW_AGENT_ID}, x-mac-hermes-instance-id=${MAC_NEMOCLAW_INSTANCE_ID}"
  log ""
  log "Rollback (if needed):"
  log "  sudo systemctl stop  ${FLEET_NAME}-nemoclaw-gateway.service"
  log "  sudo systemctl disable ${FLEET_NAME}-nemoclaw-gateway.service"
  log "  sudo systemctl enable ${FLEET_NAME}-hermes-gateway.service"
  log "  sudo systemctl start  ${FLEET_NAME}-hermes-gateway.service"
  log "============================================"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  log "NemoClaw YOLO migration starting (host=$(hostname -s 2>/dev/null || hostname), fleet=${FLEET_NAME:-<unknown>})"
  log "YOLO: hermes gateway will be stopped+disabled; executor fallback preserved"
  validate_env
  verify_openshell
  install_node_local
  install_nemoclaw_cli
  ensure_openclaw_image
  init_nemoclaw_hermes_home
  migrate_slack_credentials
  install_provider_config
  write_runtime_context
  install_openshell_policy
  install_gateway_wrapper
  disable_hermes_gateway
  install_nemoclaw_service
  verify_executor_sandbox
  verify_nemoclaw_gateway
  print_summary
  log "NemoClaw YOLO migration complete"
}

main "$@"
