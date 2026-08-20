#!/usr/bin/env bash
# headscale-key-vault.sh — move the hub's headscale pre-auth key between the
# secrets vault and the nodes that need it.
#
#     deploy/headscale-key-vault.sh publish    # hub: vault <- generated key
#     deploy/headscale-key-vault.sh fetch      # any node: vault -> stdout
#
# WHY. install-headscale.sh generates a reusable pre-auth key and writes it to
# the hub's local env file. Every worker that joined the mesh so far got that
# value because the deploy pipeline forwarded the env var over SSH during that
# worker's own bootstrap — the key was never a SecretRecord, so nothing off the
# deploy path could ask for it. An operator laptop, a provisioner host, or a
# control node added months later had no way to fetch it, which is why the
# first demo fleet ended up pasting a personal Tailscale auth key into
# ~/.mac/.env instead. `publish` closes that; `fetch` is the other end of it.
#
# Publication is a separate step from generation because the two have different
# preconditions: generation needs headscale, publication needs the mac hub to
# answer, and on a fresh fleet the network layer is installed BEFORE the
# control plane. install-headscale.sh therefore calls `publish` best-effort and
# stamps the outcome into the env file; the deploy pipeline or an operator can
# re-run it once the hub is up. `publish` is idempotent: an existing secret of
# the same name is ROTATED in place, keeping its id, scopes and audit trail.
#
# Inputs (all optional; sensible defaults):
#   FLEET_NAME                      fleet name, used in the default secret name
#   ENV_FILE                        hub env file to read the key from / stamp
#   HEADSCALE_PREAUTHKEY            the key itself; read from ENV_FILE if unset
#   HEADSCALE_PREAUTHKEY_SECRET     secret name (default headscale-preauthkey-<fleet>)
#   HEADSCALE_PREAUTHKEY_SCOPES     JSON scopes for the SecretRecord
#   MAC_BIN                         path to the mac CLI
#   HEADSCALE_VAULT_REQUIRED=1      make a failed publish fatal
#
# The key value is never echoed by `publish`, never passed as an argv word
# (argv is world-readable in /proc), and never written to a log: it reaches the
# CLI on stdin. `fetch` writes it to stdout and NOTHING else, so a caller can
# capture it; every message goes to stderr.
set -euo pipefail

FLEET_NAME="${FLEET_NAME:-mac}"
MAC_HOME="${MAC_HOME:-$HOME/.mac}"
ENV_FILE="${ENV_FILE:-$MAC_HOME/mac.env}"
SECRET_NAME="${HEADSCALE_PREAUTHKEY_SECRET:-headscale-preauthkey-${FLEET_NAME}}"
# Scoped by capability, not by agent id: the set of nodes that may join the
# mesh is open-ended (workers, an operator laptop, a provisioner added later),
# and enumerating agent ids here would have to be edited on every join.
DEFAULT_SECRET_SCOPES='{"capabilities": ["deploy", "mesh-join"]}'
SECRET_SCOPES="${HEADSCALE_PREAUTHKEY_SCOPES:-$DEFAULT_SECRET_SCOPES}"
CREATED_BY="${HEADSCALE_PREAUTHKEY_CREATED_BY:-install-headscale.sh}"
VAULT_REQUIRED="${HEADSCALE_VAULT_REQUIRED:-0}"

log() { echo "[headscale-key] $*" >&2; }

resolve_mac_bin() {
  if [ -n "${MAC_BIN:-}" ]; then
    printf '%s\n' "$MAC_BIN"
    return 0
  fi
  if [ -x "$MAC_HOME/venv/bin/mac" ]; then
    printf '%s\n' "$MAC_HOME/venv/bin/mac"
    return 0
  fi
  if command -v mac >/dev/null 2>&1; then
    command -v mac
    return 0
  fi
  return 1
}

read_env_key() {
  # Value of KEY in ENV_FILE, without sourcing the file: it holds credentials
  # for other subsystems and must not be executed.
  local key="$1"
  [ -f "$ENV_FILE" ] || return 0
  sed -n "s|^${key}=||p" "$ENV_FILE" | tail -n 1
}

set_env_key() {
  local file="$1" key="$2" value="$3"
  mkdir -p "$(dirname "$file")"
  if [ ! -f "$file" ]; then
    : > "$file"
    chmod 600 "$file"
  fi
  if grep -q "^${key}=" "$file" 2>/dev/null; then
    # -i.bak + rm is the spelling that works on both GNU and BSD sed; the hub
    # can be either.
    sed -i.bak "s|^${key}=.*|${key}=${value}|" "$file" && rm -f "$file.bak"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

secret_exists() {
  # `secret list` returns metadata only (no values), so this is a safe
  # existence probe and it doubles as a hub-reachability check. Exit 0 present,
  # 1 absent, 2 hub unreachable / unparseable.
  local mac_bin="$1" name="$2" listed=""
  listed="$("$mac_bin" admin secret list 2>/dev/null)" || return 2
  printf '%s' "$listed" | python3 -c '
import json, sys
try:
    records = json.load(sys.stdin)
except ValueError:
    sys.exit(2)
if not isinstance(records, list):
    sys.exit(2)
sys.exit(0 if any(r.get("name") == sys.argv[1] for r in records) else 1)
' "$name"
}

cmd_publish() {
  stamp() {
    # Record the outcome next to the key so a later deploy phase (or an
    # operator reading the env file) can tell "published" from "hub was down".
    set_env_key "$ENV_FILE" HEADSCALE_PREAUTHKEY_SECRET "$SECRET_NAME"
    set_env_key "$ENV_FILE" HEADSCALE_PREAUTHKEY_VAULT "$1"
  }
  give_up() {
    # Publication failed. Not fatal by default: the key is still in the hub env
    # file and the deploy pipeline still forwards it, so the fleet comes up. It
    # just comes up with the gap this script exists to close, and says so
    # rather than reporting success.
    log "ERROR: $1"
    stamp deferred
    if [ "$VAULT_REQUIRED" = "1" ]; then
      exit 1
    fi
    log "key is NOT in the vault; re-run 'headscale-key-vault.sh publish' once the hub is up"
    exit 0
  }

  local preauthkey="${HEADSCALE_PREAUTHKEY:-}"
  if [ -z "$preauthkey" ]; then
    preauthkey="$(read_env_key HEADSCALE_PREAUTHKEY)"
  fi
  [ -n "$preauthkey" ] || give_up "no HEADSCALE_PREAUTHKEY in the environment or ${ENV_FILE}"

  local mac_bin=""
  mac_bin="$(resolve_mac_bin)" || give_up "could not find the mac CLI (set MAC_BIN)"

  local present=0
  if secret_exists "$mac_bin" "$SECRET_NAME"; then
    present=0
  else
    present=$?
  fi
  case "$present" in
    0)
      log "rotating existing secret ${SECRET_NAME}"
      printf '%s' "$preauthkey" \
        | "$mac_bin" admin secret rotate "$SECRET_NAME" --from-stdin --actor "$CREATED_BY" >/dev/null \
        || give_up "could not rotate ${SECRET_NAME}"
      ;;
    1)
      log "creating secret ${SECRET_NAME}"
      printf '%s' "$preauthkey" \
        | "$mac_bin" admin secret set "$SECRET_NAME" --from-stdin \
            --scopes "$SECRET_SCOPES" --created-by "$CREATED_BY" >/dev/null \
        || give_up "could not create ${SECRET_NAME}"
      ;;
    *)
      give_up "the mac hub did not answer 'admin secret list'"
      ;;
  esac

  stamp published
  log "published to the vault as ${SECRET_NAME}"
  log "fetch it with: mac admin secret get ${SECRET_NAME} --raw"
}

cmd_fetch() {
  local mac_bin=""
  if ! mac_bin="$(resolve_mac_bin)"; then
    log "could not find the mac CLI (set MAC_BIN); cannot fetch ${SECRET_NAME}"
    return 1
  fi
  local value=""
  if ! value="$("$mac_bin" admin secret get "$SECRET_NAME" --raw --purpose mesh-join 2>/dev/null)"; then
    log "vault has no usable ${SECRET_NAME} (hub unreachable, token lacks the 'secret' scope, or the key was never published)"
    return 1
  fi
  value="${value%$'\n'}"
  [ -n "$value" ] || { log "vault returned an empty ${SECRET_NAME}"; return 1; }
  printf '%s\n' "$value"
}

case "${1:-}" in
  publish) cmd_publish ;;
  fetch) cmd_fetch ;;
  *)
    echo "usage: $(basename "$0") {publish|fetch}" >&2
    exit 2
    ;;
esac
