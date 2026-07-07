#!/usr/bin/env bash
# Prepare, verify, or roll back MAC's stock OpenClaw chat gateway.
#
# Secrets stay in an owner-only host file which OpenShell uploads into the
# sandbox.  Values never appear in the OpenShell command argv, committed config,
# logs, or evidence.  Service installation is handled by deploy-mac-fleet.sh so
# systemd, launchd, and supervisord share the same transactional cutover.
set -euo pipefail

OPENCLAW_VERSION="2026.6.11"
OPENCLAW_IMAGE_REVISION="4"
OPENCLAW_IMAGE="localhost/mac-openclaw:${OPENCLAW_VERSION}-mac.${OPENCLAW_IMAGE_REVISION}"

MAC_HOME="${MAC_HOME:-$HOME/.mac}"
MAC_SRC="${MAC_SRC:-$MAC_HOME/src/mac}"
OPENCLAW_HOST_DIR="${MAC_OPENCLAW_HOST_DIR:-$MAC_HOME/openclaw}"
MANAGED_DIR="$OPENCLAW_HOST_DIR/managed"
WORKSPACE_DIR="$OPENCLAW_HOST_DIR/workspace"
BACKUP_DIR="$OPENCLAW_HOST_DIR/backups"
POLICY_PATH="$OPENCLAW_HOST_DIR/openclaw-policy.yaml"
WRAPPER_PATH="$MAC_HOME/bin/openclaw-gateway"
MESSAGE_WRAPPER_PATH="$MAC_HOME/bin/openclaw-message"
CONTAINERFILE="${MAC_OPENCLAW_CONTAINERFILE:-$MAC_SRC/deploy/openclaw/OpenClaw.Containerfile}"
POLICY_TEMPLATE="${MAC_OPENCLAW_POLICY_TEMPLATE:-$MAC_SRC/deploy/openclaw/openclaw-policy.yaml}"
GATEWAY_PORT="${MAC_OPENCLAW_GATEWAY_PORT:-18789}"
DRY_RUN="${MAC_OPENCLAW_DRY_RUN:-0}"
SKIP_IMAGE="${MAC_OPENCLAW_SKIP_IMAGE:-0}"
LIVE_CANARY="${MAC_OPENCLAW_LIVE_CANARY:-0}"

log() { printf '[install-openclaw-gateway] %s\n' "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }
truthy() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

find_openshell() {
  if [ -n "${MAC_OPENSHELL_BIN:-}" ] && [ -x "$MAC_OPENSHELL_BIN" ]; then
    printf '%s\n' "$MAC_OPENSHELL_BIN"
    return
  fi
  if command -v openshell >/dev/null 2>&1; then
    command -v openshell
    return
  fi
  for candidate in "$HOME/.local/bin/openshell" "$HOME/.cargo/bin/openshell" /usr/local/bin/openshell; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return
    fi
  done
  return 1
}

source_host_env() {
  # Generated runtime.env is trusted local state and preserves the gateway auth
  # token across idempotent deploys.  Fleet/Hermes env files then refresh the
  # router and channel credentials from their canonical host-local sources.
  local persisted_gateway_token=""
  if [ -f "$MANAGED_DIR/runtime.env" ]; then
    persisted_gateway_token="$(
      set +u
      . "$MANAGED_DIR/runtime.env"
      printf '%s' "${OPENCLAW_GATEWAY_TOKEN:-}"
    )"
  fi
  set +u
  set -a
  [ -f "$MAC_HOME/mac.env" ] && . "$MAC_HOME/mac.env"
  [ -f "$HOME/.hermes/.env" ] && . "$HOME/.hermes/.env"
  [ -f "$OPENCLAW_HOST_DIR/credentials.env" ] && . "$OPENCLAW_HOST_DIR/credentials.env"
  set +a
  set -u

  MAC_OPENCLAW_AGENT_ID="${MAC_OPENCLAW_AGENT_ID:-${MAC_AGENT_ID:-}}"
  MAC_OPENCLAW_INSTANCE_ID="${MAC_OPENCLAW_INSTANCE_ID:-${MAC_HERMES_INSTANCE_ID:-${MAC_WORKER_HERMES_INSTANCE_ID:-}}}"
  MAC_OPENCLAW_ROUTER_URL="${MAC_OPENCLAW_ROUTER_URL:-${MAC_HERMES_GATEWAY_BASE_URL:-${OPENAI_BASE_URL:-${CUSTOM_BASE_URL:-}}}}"
  MAC_OPENCLAW_ROUTER_API_KEY="${MAC_OPENCLAW_ROUTER_API_KEY:-${MAC_HERMES_GATEWAY_API_KEY:-${MAC_API_TOKEN:-}}}"
  MAC_OPENCLAW_MODEL="${MAC_OPENCLAW_MODEL:-${MAC_HERMES_GATEWAY_MODEL:-${HERMES_INFERENCE_MODEL:-}}}"
  MAC_OPENCLAW_FLEET_NAME="${MAC_OPENCLAW_FLEET_NAME:-${MAC_FLEET_NAME:-mac}}"
  MAC_OPENCLAW_HOME_CHANNEL="${MAC_OPENCLAW_HOME_CHANNEL:-${MAC_HERMES_SLACK_HOME_CHANNEL_NAME:-${SLACK_HOME_CHANNEL_NAME:-}}}"
  MAC_OPENCLAW_SLACK_BOT_TOKEN="${MAC_OPENCLAW_SLACK_BOT_TOKEN:-${SLACK_BOT_TOKEN:-}}"
  MAC_OPENCLAW_SLACK_APP_TOKEN="${MAC_OPENCLAW_SLACK_APP_TOKEN:-${SLACK_APP_TOKEN:-}}"
  MAC_OPENCLAW_TELEGRAM_BOT_TOKEN="${MAC_OPENCLAW_TELEGRAM_BOT_TOKEN:-${TELEGRAM_BOT_TOKEN:-}}"
  MAC_OPENCLAW_TELEGRAM_CANARY_TARGET="${MAC_OPENCLAW_TELEGRAM_CANARY_TARGET:-${TELEGRAM_CANARY_TARGET:-}}"
  MAC_OPENCLAW_PUBLIC_IDENTITY="${MAC_OPENCLAW_PUBLIC_IDENTITY:-}"
  MAC_OPENCLAW_REPRESENTED_BY="${MAC_OPENCLAW_REPRESENTED_BY:-}"
  MAC_OPENCLAW_REPRESENTATION_MODE="${MAC_OPENCLAW_REPRESENTATION_MODE:-delegated}"
  MAC_OPENCLAW_SLACK_ACCOUNT_ID="${MAC_OPENCLAW_SLACK_ACCOUNT_ID:-default}"
  MAC_OPENCLAW_TELEGRAM_ACCOUNT_ID="${MAC_OPENCLAW_TELEGRAM_ACCOUNT_ID:-default}"
  # Merely having old Hermes credentials on a worker must not turn that worker
  # into a public bot.  Channel activation is owned by a logical public
  # identity assignment; unassigned OpenClaw runtimes are deliberately headless.
  if [ -z "$MAC_OPENCLAW_PUBLIC_IDENTITY" ]; then
    MAC_OPENCLAW_SLACK_BOT_TOKEN=""
    MAC_OPENCLAW_SLACK_APP_TOKEN=""
    MAC_OPENCLAW_TELEGRAM_BOT_TOKEN=""
  fi
  local channels=()
  if [ -n "$MAC_OPENCLAW_SLACK_BOT_TOKEN" ] || [ -n "$MAC_OPENCLAW_SLACK_APP_TOKEN" ]; then
    channels+=(slack)
  fi
  if [ -n "$MAC_OPENCLAW_TELEGRAM_BOT_TOKEN" ]; then
    channels+=(telegram)
  fi
  MAC_OPENCLAW_CHANNELS="$(IFS=,; printf '%s' "${channels[*]}")"
  OPENCLAW_GATEWAY_TOKEN="${OPENCLAW_GATEWAY_TOKEN:-$persisted_gateway_token}"
  if [ -z "$OPENCLAW_GATEWAY_TOKEN" ]; then
    command -v openssl >/dev/null 2>&1 || die "openssl is required to create the local gateway token"
    OPENCLAW_GATEWAY_TOKEN="$(openssl rand -hex 32)"
  fi
  local suffix
  suffix="$(printf '%s' "$MAC_OPENCLAW_AGENT_ID" | sed -E 's/^agent_//; s/[^A-Za-z0-9]+/-/g; s/^-+//; s/-+$//' | tr '[:upper:]' '[:lower:]')"
  SANDBOX_NAME="${MAC_OPENCLAW_SANDBOX_NAME:-mac-openclaw-${suffix:-gateway}}"
  export MAC_OPENCLAW_AGENT_ID MAC_OPENCLAW_INSTANCE_ID MAC_OPENCLAW_ROUTER_URL
  export MAC_OPENCLAW_ROUTER_API_KEY MAC_OPENCLAW_MODEL MAC_OPENCLAW_FLEET_NAME
  export MAC_OPENCLAW_HOME_CHANNEL MAC_OPENCLAW_SLACK_BOT_TOKEN
  export MAC_OPENCLAW_SLACK_APP_TOKEN MAC_OPENCLAW_TELEGRAM_BOT_TOKEN
  export MAC_OPENCLAW_TELEGRAM_CANARY_TARGET OPENCLAW_GATEWAY_TOKEN SANDBOX_NAME
  export MAC_OPENCLAW_PUBLIC_IDENTITY MAC_OPENCLAW_CHANNELS
  export MAC_OPENCLAW_REPRESENTED_BY MAC_OPENCLAW_REPRESENTATION_MODE
  export MAC_OPENCLAW_SLACK_ACCOUNT_ID MAC_OPENCLAW_TELEGRAM_ACCOUNT_ID
  export MAC_OPENCLAW_GATEWAY_PORT="$GATEWAY_PORT"
}

validate_env() {
  local missing=() name
  for name in \
    MAC_OPENCLAW_AGENT_ID \
    MAC_OPENCLAW_INSTANCE_ID \
    MAC_OPENCLAW_ROUTER_URL \
    MAC_OPENCLAW_ROUTER_API_KEY \
    MAC_OPENCLAW_MODEL; do
    [ -n "${!name:-}" ] || missing+=("$name")
  done
  [ "${#missing[@]}" -eq 0 ] || die "missing required host-local inputs: ${missing[*]}"
  case "$MAC_OPENCLAW_ROUTER_URL" in
    http://*|https://*) ;;
    *) die "MAC_OPENCLAW_ROUTER_URL must be an http(s) URL" ;;
  esac
  if [ -n "$MAC_OPENCLAW_SLACK_BOT_TOKEN" ] || [ -n "$MAC_OPENCLAW_SLACK_APP_TOKEN" ]; then
    [ -n "$MAC_OPENCLAW_SLACK_BOT_TOKEN" ] && [ -n "$MAC_OPENCLAW_SLACK_APP_TOKEN" ] \
      || die "Slack requires both bot and app tokens"
    [[ "$MAC_OPENCLAW_SLACK_BOT_TOKEN" == xoxb-* ]] || die "Slack bot token has the wrong type"
    [[ "$MAC_OPENCLAW_SLACK_APP_TOKEN" == xapp-* ]] || die "Slack app token has the wrong type"
  fi
  if [ -n "$MAC_OPENCLAW_TELEGRAM_BOT_TOKEN" ]; then
    [[ "$MAC_OPENCLAW_TELEGRAM_BOT_TOKEN" =~ ^[0-9]+:.+ ]] || die "Telegram bot token has the wrong type"
  fi
  if [ -n "$MAC_OPENCLAW_PUBLIC_IDENTITY" ] && [ -z "$MAC_OPENCLAW_CHANNELS" ]; then
    die "public identity $MAC_OPENCLAW_PUBLIC_IDENTITY has no configured channel credentials"
  fi
  case "$MAC_OPENCLAW_REPRESENTATION_MODE" in
    direct|delegated) ;;
    *) die "MAC_OPENCLAW_REPRESENTATION_MODE must be direct or delegated" ;;
  esac
  if truthy "$LIVE_CANARY"; then
    case ",$MAC_OPENCLAW_CHANNELS," in
      *,slack,*) [ -n "$MAC_OPENCLAW_HOME_CHANNEL" ] || die "Slack live canary requires a home channel" ;;
    esac
    case ",$MAC_OPENCLAW_CHANNELS," in
      *,telegram,*) [ -n "$MAC_OPENCLAW_TELEGRAM_CANARY_TARGET" ] \
        || die "Telegram live canary requires MAC_OPENCLAW_TELEGRAM_CANARY_TARGET" ;;
    esac
  fi
  [[ "$GATEWAY_PORT" =~ ^[0-9]+$ ]] || die "MAC_OPENCLAW_GATEWAY_PORT must be numeric"
  [ "$GATEWAY_PORT" -ge 1 ] && [ "$GATEWAY_PORT" -le 65535 ] || die "gateway port is out of range"
}

prepare_directories() {
  umask 077
  mkdir -p "$MANAGED_DIR" "$WORKSPACE_DIR" "$BACKUP_DIR" "$MAC_HOME/bin"
  chmod 0700 "$OPENCLAW_HOST_DIR" "$MANAGED_DIR" "$WORKSPACE_DIR" "$BACKUP_DIR"
}

write_config() {
  python3 - "$MANAGED_DIR/openclaw.json" <<'PY'
import json
import os
import sys

def secret_ref(name: str) -> dict:
    return {"source": "env", "provider": "default", "id": name}

model = os.environ["MAC_OPENCLAW_MODEL"]
provider_model = "mac-router/%s" % model
channels = {}
configured = {
    item.strip()
    for item in os.environ.get("MAC_OPENCLAW_CHANNELS", "").split(",")
    if item.strip()
}
if "slack" in configured:
    channels["slack"] = {
        "enabled": True,
        "mode": "socket",
        "accounts": {
            os.environ.get("MAC_OPENCLAW_SLACK_ACCOUNT_ID", "default"): {
                "botToken": secret_ref("SLACK_BOT_TOKEN"),
                "appToken": secret_ref("SLACK_APP_TOKEN"),
                "groupPolicy": "open",
            }
        },
    }
if "telegram" in configured:
    channels["telegram"] = {
        "enabled": True,
        "accounts": {
            os.environ.get("MAC_OPENCLAW_TELEGRAM_ACCOUNT_ID", "default"): {
                "botToken": secret_ref("TELEGRAM_BOT_TOKEN"),
                "dmPolicy": "pairing",
                "groupPolicy": "allowlist",
            }
        },
    }

config = {
    "gateway": {
        "mode": "local",
        "port": int(os.environ.get("MAC_OPENCLAW_GATEWAY_PORT", "18789")),
        "bind": "lan",
        "auth": {
            "mode": "token",
            "token": secret_ref("OPENCLAW_GATEWAY_TOKEN"),
        },
        "controlUi": {"enabled": False},
    },
    "channels": channels,
    "plugins": {
        "enabled": True,
        "entries": {
            "slack": {"enabled": "slack" in configured},
            "telegram": {"enabled": "telegram" in configured},
        },
    },
    "models": {
        "mode": "merge",
        "providers": {
            "mac-router": {
                "baseUrl": os.environ["MAC_OPENCLAW_ROUTER_URL"],
                "apiKey": "${MAC_OPENCLAW_ROUTER_API_KEY}",
                "api": "openai-completions",
                "headers": {
                    "x-mac-agent-id": os.environ["MAC_OPENCLAW_AGENT_ID"],
                    "x-mac-hermes-instance-id": os.environ["MAC_OPENCLAW_INSTANCE_ID"],
                },
                "models": [{"id": model, "name": model}],
            }
        },
    },
    "agents": {
        "defaults": {
            "model": {"primary": provider_model},
            "workspace": "/home/sandbox/workspace",
        },
        "list": [{
            "id": "main",
            "default": True,
            "name": os.environ.get("MAC_OPENCLAW_PUBLIC_IDENTITY") or os.environ["MAC_OPENCLAW_AGENT_ID"],
            "workspace": "/home/sandbox/workspace",
        }],
    },
}
if configured:
    config["plugins"]["allow"] = sorted(configured)
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(config, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
  chmod 0600 "$MANAGED_DIR/openclaw.json"
}

write_runtime_env() {
  python3 - "$MANAGED_DIR/runtime.env" <<'PY'
import os
import shlex
import sys

values = {
    "HOME": "/home/sandbox",
    "MAC_OPENCLAW_ROUTER_API_KEY": os.environ["MAC_OPENCLAW_ROUTER_API_KEY"],
    "NODE_ENV": "production",
    "OPENCLAW_CONFIG_PATH": "/home/sandbox/.config/mac-openclaw/openclaw.json",
    "OPENCLAW_GATEWAY_TOKEN": os.environ["OPENCLAW_GATEWAY_TOKEN"],
    "OPENCLAW_STATE_DIR": "/home/sandbox/.openclaw-data",
}
if os.environ.get("MAC_OPENCLAW_SLACK_APP_TOKEN"):
    values["SLACK_APP_TOKEN"] = os.environ["MAC_OPENCLAW_SLACK_APP_TOKEN"]
    values["SLACK_BOT_TOKEN"] = os.environ["MAC_OPENCLAW_SLACK_BOT_TOKEN"]
if os.environ.get("MAC_OPENCLAW_TELEGRAM_BOT_TOKEN"):
    values["TELEGRAM_BOT_TOKEN"] = os.environ["MAC_OPENCLAW_TELEGRAM_BOT_TOKEN"]
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    handle.write("# Generated host-local OpenClaw runtime environment.\n")
    for key in sorted(values):
        handle.write("%s=%s\n" % (key, shlex.quote(values[key])))
PY
  chmod 0600 "$MANAGED_DIR/runtime.env"
}

write_managed_entrypoint() {
  cat > "$MANAGED_DIR/entrypoint.sh" <<'EOF'
#!/bin/sh
set -eu
set -a
. /home/sandbox/.config/mac-openclaw/runtime.env
set +a
exec /usr/local/bin/openclaw gateway run
EOF
  chmod 0700 "$MANAGED_DIR/entrypoint.sh"
}

write_workspace_context() {
  cat > "$WORKSPACE_DIR/AGENTS.md" <<EOF
# MAC OpenClaw Gateway Context

- Fleet: ${MAC_OPENCLAW_FLEET_NAME}
- Agent: ${MAC_OPENCLAW_AGENT_ID}
- Runtime role: stock-openclaw-internal-agent
- Public identity: ${MAC_OPENCLAW_PUBLIC_IDENTITY:-none (represented through another gateway)}
- Representation mode: ${MAC_OPENCLAW_REPRESENTATION_MODE}
- Human channels: ${MAC_OPENCLAW_CHANNELS:-none}
- MAC router: ${MAC_OPENCLAW_ROUTER_URL}
- Model route: mac-router/${MAC_OPENCLAW_MODEL}
- Task execution is a separate MAC worker role and is not performed by this gateway.
EOF
  chmod 0600 "$WORKSPACE_DIR/AGENTS.md"
}

render_policy() {
  [ -f "$POLICY_TEMPLATE" ] || die "policy template not found: $POLICY_TEMPLATE"
  python3 - "$POLICY_TEMPLATE" "$POLICY_PATH" <<'PY'
import os
import sys
from urllib.parse import urlsplit

source, dest = sys.argv[1:]
parsed = urlsplit(os.environ["MAC_OPENCLAW_ROUTER_URL"])
if not parsed.hostname:
    raise SystemExit("router URL has no hostname")
port = parsed.port or (443 if parsed.scheme == "https" else 80)
text = open(source, encoding="utf-8").read()
text = text.replace("__MAC_ROUTER_HOST__", parsed.hostname)
text = text.replace("__MAC_ROUTER_PORT__", str(port))
if "__MAC_" in text:
    raise SystemExit("unresolved MAC OpenClaw policy placeholder")
with open(dest, "w", encoding="utf-8") as handle:
    handle.write(text)
PY
  chmod 0600 "$POLICY_PATH"
}

write_host_wrapper() {
  local openshell_bin="$1"
  cat > "$WRAPPER_PATH" <<EOF
#!/usr/bin/env bash
set -euo pipefail
OPEN_SHELL=$(printf '%q' "$openshell_bin")
SANDBOX=$(printf '%q' "$SANDBOX_NAME")
IMAGE=$(printf '%q' "$OPENCLAW_IMAGE")
POLICY=$(printf '%q' "$POLICY_PATH")
MANAGED=$(printf '%q' "$MANAGED_DIR")
WORKSPACE=$(printf '%q' "$WORKSPACE_DIR")
PORT=$(printf '%q' "$GATEWAY_PORT")

upload_managed() {
  "\$OPEN_SHELL" sandbox upload "\$SANDBOX" "\$MANAGED/openclaw.json" /home/sandbox/.config/mac-openclaw/openclaw.json >/dev/null
  "\$OPEN_SHELL" sandbox upload "\$SANDBOX" "\$MANAGED/runtime.env" /home/sandbox/.config/mac-openclaw/runtime.env >/dev/null
  "\$OPEN_SHELL" sandbox upload "\$SANDBOX" "\$MANAGED/entrypoint.sh" /home/sandbox/.config/mac-openclaw/entrypoint.sh >/dev/null
  "\$OPEN_SHELL" sandbox upload "\$SANDBOX" "\$WORKSPACE/AGENTS.md" /home/sandbox/workspace/AGENTS.md >/dev/null
}

if "\$OPEN_SHELL" sandbox get "\$SANDBOX" >/dev/null 2>&1; then
  upload_managed
  exec "\$OPEN_SHELL" sandbox exec --name "\$SANDBOX" --no-tty -- /bin/sh /home/sandbox/.config/mac-openclaw/entrypoint.sh
fi

exec "\$OPEN_SHELL" sandbox create \
  --no-auto-providers \
  --from "\$IMAGE" \
  --policy "\$POLICY" \
  --name "\$SANDBOX" \
  --forward "127.0.0.1:\$PORT" \
  --label mac.role=openclaw-gateway \
  --upload "\$MANAGED/openclaw.json:/home/sandbox/.config/mac-openclaw/openclaw.json" \
  --upload "\$MANAGED/runtime.env:/home/sandbox/.config/mac-openclaw/runtime.env" \
  --upload "\$MANAGED/entrypoint.sh:/home/sandbox/.config/mac-openclaw/entrypoint.sh" \
  --upload "\$WORKSPACE/AGENTS.md:/home/sandbox/workspace/AGENTS.md" \
  -- /bin/sh /home/sandbox/.config/mac-openclaw/entrypoint.sh
EOF
  chmod 0700 "$WRAPPER_PATH"
  cat > "$MESSAGE_WRAPPER_PATH" <<EOF
#!/usr/bin/env bash
set -euo pipefail
OPEN_SHELL=$(printf '%q' "$openshell_bin")
SANDBOX=$(printf '%q' "$SANDBOX_NAME")
exec "\$OPEN_SHELL" sandbox exec --name "\$SANDBOX" --no-tty -- \
  /bin/sh -lc 'set -a; . /home/sandbox/.config/mac-openclaw/runtime.env; set +a; exec /usr/local/bin/openclaw message "\$@"' mac-openclaw-message "\$@"
EOF
  chmod 0700 "$MESSAGE_WRAPPER_PATH"
}

build_image() {
  if truthy "$SKIP_IMAGE"; then
    docker image inspect "$OPENCLAW_IMAGE" >/dev/null 2>&1 \
      || die "MAC_OPENCLAW_SKIP_IMAGE=1 but $OPENCLAW_IMAGE is absent"
    return
  fi
  if docker image inspect "$OPENCLAW_IMAGE" >/dev/null 2>&1; then
    log "pinned stock OpenClaw image already present"
    return
  fi
  [ -f "$CONTAINERFILE" ] || die "Containerfile not found: $CONTAINERFILE"
  if truthy "$DRY_RUN"; then
    log "DRY-RUN: docker build --pull -t $OPENCLAW_IMAGE -f $CONTAINERFILE"
    return
  fi
  docker build --pull -t "$OPENCLAW_IMAGE" -f "$CONTAINERFILE" "$(dirname "$CONTAINERFILE")"
}

backup_and_delete_stale_sandbox() {
  local openshell_bin="$1"
  "$openshell_bin" sandbox get "$SANDBOX_NAME" >/dev/null 2>&1 || return 0
  local version
  version="$($openshell_bin sandbox exec --name "$SANDBOX_NAME" --no-tty -- /usr/local/bin/openclaw --version 2>/dev/null || true)"
  local image_revision
  image_revision="$($openshell_bin sandbox exec --name "$SANDBOX_NAME" --no-tty -- cat /etc/mac-openclaw-image-revision 2>/dev/null || true)"
  if [[ "$version" == *"$OPENCLAW_VERSION"* ]] \
    && [ "$image_revision" = "$OPENCLAW_IMAGE_REVISION" ]; then
    return 0
  fi
  local stamp="$BACKUP_DIR/$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$stamp"
  chmod 0700 "$stamp"
  "$openshell_bin" sandbox download "$SANDBOX_NAME" /home/sandbox/.openclaw-data "$stamp" >/dev/null 2>&1 || true
  "$openshell_bin" sandbox download "$SANDBOX_NAME" /home/sandbox/workspace "$stamp" >/dev/null 2>&1 || true
  "$openshell_bin" sandbox delete "$SANDBOX_NAME" >/dev/null
  log "replaced stale sandbox (version=$version image_revision=${image_revision:-missing}) after owner-only state backup at $stamp"
}

prepare() {
  source_host_env
  validate_env
  prepare_directories
  write_config
  write_runtime_env
  write_managed_entrypoint
  write_workspace_context
  render_policy
  local openshell_bin
  openshell_bin="$(find_openshell)" || die "OpenShell CLI not found"
  build_image
  if ! truthy "$DRY_RUN"; then
    backup_and_delete_stale_sandbox "$openshell_bin"
  fi
  write_host_wrapper "$openshell_bin"
  printf '%s\n' "$OPENCLAW_IMAGE" > "$OPENCLAW_HOST_DIR/image-ref"
  chmod 0600 "$OPENCLAW_HOST_DIR/image-ref"
  log "prepared stock OpenClaw $OPENCLAW_VERSION for sandbox $SANDBOX_NAME"
}

sandbox_command() {
  local openshell_bin="$1"
  shift
  "$openshell_bin" sandbox exec --name "$SANDBOX_NAME" --no-tty -- \
    /bin/sh -lc 'set -a; . /home/sandbox/.config/mac-openclaw/runtime.env; set +a; exec "$@"' mac-openclaw "$@"
}

verify() {
  source_host_env
  validate_env
  local openshell_bin
  openshell_bin="$(find_openshell)" || die "OpenShell CLI not found"
  "$openshell_bin" sandbox get "$SANDBOX_NAME" >/dev/null 2>&1 || die "sandbox $SANDBOX_NAME is absent"
  curl -fsS --max-time 10 "http://127.0.0.1:${GATEWAY_PORT}/healthz" >/dev/null \
    || die "OpenClaw liveness probe failed"
  curl -fsS --max-time 15 "http://127.0.0.1:${GATEWAY_PORT}/readyz" >/dev/null \
    || die "OpenClaw readiness probe failed"
  sandbox_command "$openshell_bin" /usr/local/bin/openclaw config validate --json >/dev/null
  sandbox_command "$openshell_bin" /usr/local/bin/openclaw health --verbose --json >/dev/null
  case ",$MAC_OPENCLAW_CHANNELS," in
    *,slack,*)
      sandbox_command "$openshell_bin" /usr/local/bin/openclaw message send \
        --channel slack --account "$MAC_OPENCLAW_SLACK_ACCOUNT_ID" \
        --target channel:C00000000 --message 'MAC plugin preflight' \
        --dry-run --json >/dev/null
      ;;
  esac
  case ",$MAC_OPENCLAW_CHANNELS," in
    *,telegram,*)
      sandbox_command "$openshell_bin" /usr/local/bin/openclaw message send \
        --channel telegram --account "$MAC_OPENCLAW_TELEGRAM_ACCOUNT_ID" \
        --target 0 --message 'MAC plugin preflight' --dry-run --json >/dev/null
      ;;
  esac
  local channel_status="$OPENCLAW_HOST_DIR/channel-status.json"
  sandbox_command "$openshell_bin" /usr/local/bin/openclaw channels status --probe --json > "$channel_status"
  chmod 0600 "$channel_status"
  python3 "$MAC_SRC/scripts/validate-openclaw-channel-status.py" \
    "$channel_status" --required "$MAC_OPENCLAW_CHANNELS"
  if truthy "$LIVE_CANARY"; then
    local output
    output="$(sandbox_command "$openshell_bin" /usr/local/bin/openclaw agent \
      --agent main --message 'Respond exactly MAC_OPENCLAW_CANARY_OK' \
      --session-id mac-openclaw-canary --json)"
    printf '%s' "$output" | grep -q 'MAC_OPENCLAW_CANARY_OK' \
      || die "authenticated model canary did not return the sentinel"
    local canary_message="MAC OpenClaw canary from ${MAC_OPENCLAW_AGENT_ID}"
    case ",$MAC_OPENCLAW_CHANNELS," in
      *,slack,*)
        sandbox_command "$openshell_bin" /usr/local/bin/openclaw message send \
          --channel slack --account "$MAC_OPENCLAW_SLACK_ACCOUNT_ID" \
          --target "$MAC_OPENCLAW_HOME_CHANNEL" \
          --message "$canary_message" --json >/dev/null
        ;;
    esac
    case ",$MAC_OPENCLAW_CHANNELS," in
      *,telegram,*)
        sandbox_command "$openshell_bin" /usr/local/bin/openclaw message send \
          --channel telegram --account "$MAC_OPENCLAW_TELEGRAM_ACCOUNT_ID" \
          --target "$MAC_OPENCLAW_TELEGRAM_CANARY_TARGET" \
          --message "$canary_message" --json >/dev/null
        ;;
    esac
  fi
  python3 - "$OPENCLAW_HOST_DIR/service-advertisement.json" <<'PY'
import json
import os
import sys
import time

path = sys.argv[1]
agent_id = os.environ["MAC_OPENCLAW_AGENT_ID"]
suffix = agent_id.removeprefix("agent_")
channels = {
    channel: {
        "enabled": True,
        "transport": "socket" if channel == "slack" else "long_polling",
        "account_id": os.environ.get(
            "MAC_OPENCLAW_%s_ACCOUNT_ID" % channel.upper(), "default"
        ),
    }
    for channel in os.environ.get("MAC_OPENCLAW_CHANNELS", "").split(",")
    if channel
}
runtime = {
    "schema": "mac.openclaw_runtime.v1",
    "implementation": "openclaw",
    "version": "2026.6.11",
    "mode": "gateway" if channels else "internal",
    "confinement": {
        "provider": "openshell",
        "sandbox": "mac-openclaw-%s" % suffix,
    },
    "verified": True,
    "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}
record = {
    "openclaw_runtime": runtime,
    "representation": {
        "schema": "mac.agent_representation.v1",
        "mode": (
            os.environ.get("MAC_OPENCLAW_REPRESENTATION_MODE", "delegated")
            if (channels or os.environ.get("MAC_OPENCLAW_REPRESENTED_BY"))
            else "internal_only"
        ),
        "identity": (
            os.environ.get("MAC_OPENCLAW_PUBLIC_IDENTITY")
            if channels
            else os.environ.get("MAC_OPENCLAW_REPRESENTED_BY")
        ) or None,
        "human_facing": bool(channels),
    },
}
if channels:
    record["chat_gateway"] = {
        "schema": "mac.chat_gateway_service.v1",
        "implementation": "openclaw",
        "version": "2026.6.11",
        "service_role": "chat_gateway",
        "service_name": "%s-openclaw-gateway"
        % os.environ["MAC_OPENCLAW_FLEET_NAME"],
        "endpoint": "http://127.0.0.1:%s"
        % os.environ["MAC_OPENCLAW_GATEWAY_PORT"],
        "public_identity": os.environ.get("MAC_OPENCLAW_PUBLIC_IDENTITY") or None,
        "confinement": runtime["confinement"],
        "channels": channels,
        "verified": True,
        "verified_at": runtime["verified_at"],
    }
with open(path, "w", encoding="utf-8") as handle:
    json.dump(record, handle, indent=2, sort_keys=True)
    handle.write("\n")
os.chmod(path, 0o600)
PY
  if truthy "$LIVE_CANARY"; then
    log "verified stock OpenClaw runtime: liveness, readiness, config, RPC health, configured channel probes, model canary, channel sends"
  elif [ -n "$MAC_OPENCLAW_CHANNELS" ]; then
    log "verified stock OpenClaw gateway: liveness, readiness, config, RPC health, configured channel probes ($MAC_OPENCLAW_CHANNELS)"
  else
    log "verified stock OpenClaw headless runtime: liveness, readiness, config, RPC health"
  fi
}

rollback() {
  local fleet="${MAC_OPENCLAW_FLEET_NAME:-${MAC_FLEET_NAME:-mac}}"
  local supervisor="${MAC_OPENCLAW_SUPERVISOR:-auto}"
  rm -f "$OPENCLAW_HOST_DIR/service-advertisement.json"
  if [ "$supervisor" = auto ]; then
    if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
      supervisor=systemd
    elif [ "$(uname -s)" = Darwin ]; then
      supervisor=launchd
    else
      supervisor=supervisord
    fi
  fi
  case "$supervisor" in
    systemd)
      sudo systemctl disable --now "${fleet}-openclaw-gateway.service" || true
      sudo systemctl enable --now "${fleet}-hermes-gateway.service"
      ;;
    launchd)
      local uid
      uid="$(id -u)"
      launchctl bootout "gui/$uid/com.${fleet}.openclaw-gateway" >/dev/null 2>&1 || true
      launchctl disable "gui/$uid/com.${fleet}.openclaw-gateway" >/dev/null 2>&1 || true
      launchctl enable "gui/$uid/com.${fleet}.hermes-gateway"
      launchctl bootstrap "gui/$uid" "$HOME/Library/LaunchAgents/com.${fleet}.hermes-gateway.plist" >/dev/null 2>&1 || true
      launchctl kickstart -k "gui/$uid/com.${fleet}.hermes-gateway"
      ;;
    supervisord)
      sudo supervisorctl stop "${fleet}-openclaw-gateway" >/dev/null 2>&1 || true
      sudo supervisorctl start "${fleet}-hermes-gateway"
      ;;
    *) die "unsupported supervisor for rollback: $supervisor" ;;
  esac
  log "rollback complete: OpenClaw stopped and Hermes gateway restored"
}

case "${1:-prepare}" in
  prepare) prepare ;;
  verify) verify ;;
  rollback) rollback ;;
  *) die "usage: $0 [prepare|verify|rollback]" ;;
esac
