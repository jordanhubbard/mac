#!/usr/bin/env bash
# headscale-key-vault.sh — publish/fetch the headscale pre-auth key via the
# mac secrets vault.
#
# WHY THIS EXISTS. install-headscale.sh generates a reusable pre-auth key on the
# hub and writes it to the hub's local env file. Every consumer of that key --
# each worker, and any control node an operator later wants on the mesh -- had
# to receive it by having the deploy pipeline forward the env value over ssh.
# That works for nodes the pipeline touches and for nobody else, and it means
# the key that admits a machine to the fleet network is the one credential the
# fleet's own vault never held.
#
# So: after generation, the key is written to SecretsService under a
# fleet-scoped name, and any authorized caller fetches it with
# `mac admin secret get <name> --raw`.
#
# SOURCE IT, don't exec it:
#
#     . "$(dirname "$0")/headscale-key-vault.sh"
#     printf '%s' "$key" | headscale_vault_publish "$(headscale_vault_secret_name)"
#
# The key never appears in argv (it would be world-readable in `ps`) and never
# on stdout. Publication reads it from stdin; the CLI is invoked with
# --from-stdin for the same reason.

# Fleet scope for the secret name. Two fleets sharing a vault must not share a
# mesh key, so the fleet name is part of the name rather than a convention.
FLEET_NAME="${FLEET_NAME:-mac}"

# auto     — publish when a hub is reachable; warn and continue when it is not.
# required — a publication failure fails the caller.
# off      — skip publication entirely.
#
# `auto` is the default because install-headscale.sh runs during hub bring-up,
# where the control plane it would publish to may not be listening yet. A fleet
# that sources worker keys from the vault should set `required` so a silent
# no-op cannot masquerade as a working install.
HEADSCALE_VAULT_PUBLISH="${HEADSCALE_VAULT_PUBLISH:-auto}"

headscale_vault_secret_name() {
  printf '%s\n' "${HEADSCALE_PREAUTHKEY_SECRET_NAME:-headscale-preauthkey-${FLEET_NAME}}"
}

# Scopes are what SecretsService checks when a *registered agent* asks through
# the handle flow: any agent carrying the `mesh` capability, plus any agent
# explicitly listed via HEADSCALE_VAULT_SCOPE_AGENTS. Operator and provisioner
# fetches do not go through this gate at all -- they authorize on the token's
# `secret` scope (see `mac admin secret get`), because a node that is not yet on the
# mesh cannot be a registered agent on a trusted machine.
headscale_vault_scopes() {
  local agents="${HEADSCALE_VAULT_SCOPE_AGENTS:-}"
  local agents_json="[]"
  if [ -n "$agents" ]; then
    agents_json="$(printf '%s' "$agents" | python3 -c '
import json, sys
raw = sys.stdin.read()
items = [item.strip() for item in raw.replace(",", " ").split() if item.strip()]
print(json.dumps(items))
')"
  fi
  printf '{"capabilities": ["mesh"], "agents": %s}\n' "$agents_json"
}

# Resolve the mac CLI. On a deployed node it lives in the fleet venv; in a
# development checkout it is whatever is on PATH.
headscale_vault_cli() {
  if [ -n "${MAC_DEPLOY_VAULT_CLI:-}" ]; then
    printf '%s\n' "$MAC_DEPLOY_VAULT_CLI"
    return 0
  fi
  local venv_cli="${MAC_HOME:-$HOME/.mac}/venv/bin/mac"
  if [ -x "$venv_cli" ]; then
    printf '%s\n' "$venv_cli"
    return 0
  fi
  command -v mac 2>/dev/null || return 1
}

# headscale_vault_publish <secret-name> [actor]
#
# Reads the key from stdin. Rotates first and creates on failure, rather than
# the other way round: rotation is the common case on redeploy, it preserves
# the secret's id and scopes, and it is recorded in the audit trail as a
# rotation rather than as a second secret with the same purpose.
#
# Prints the disposition (created|rotated) on stdout. Never prints the key.
headscale_vault_publish() {
  local name="$1" actor="${2:-install-headscale}" cli key
  key="$(cat)"
  if [ -z "$key" ]; then
    echo "[headscale-vault] ERROR: refusing to publish an empty key" >&2
    return 1
  fi
  cli="$(headscale_vault_cli)" || {
    echo "[headscale-vault] ERROR: mac CLI not found (set MAC_DEPLOY_VAULT_CLI)" >&2
    return 1
  }
  if printf '%s' "$key" \
    | "$cli" admin secret rotate "$name" --from-stdin --actor "$actor" >/dev/null 2>&1; then
    printf 'rotated\n'
    return 0
  fi
  if printf '%s' "$key" \
    | "$cli" admin secret set "$name" --from-stdin \
        --scopes "$(headscale_vault_scopes)" --created-by "$actor" >/dev/null 2>&1; then
    printf 'created\n'
    return 0
  fi
  echo "[headscale-vault] ERROR: could not publish ${name} to the secrets vault" >&2
  return 1
}

# headscale_vault_fetch <secret-name>
#
# Prints the plaintext key on stdout and nothing else, so a caller can do
#   HEADSCALE_PREAUTHKEY="$(headscale_vault_fetch "$name")"
# This is the provisioner path: it needs a token with the `secret` scope and
# does NOT need the caller to be a registered fleet agent.
headscale_vault_fetch() {
  local name="$1" cli
  cli="$(headscale_vault_cli)" || {
    echo "[headscale-vault] ERROR: mac CLI not found (set MAC_DEPLOY_VAULT_CLI)" >&2
    return 1
  }
  "$cli" admin secret get "$name" --raw --purpose "headscale-enrollment" 2>/dev/null
}

# headscale_vault_publish_guarded <secret-name> [actor]
#
# The policy wrapper install-headscale.sh calls: applies HEADSCALE_VAULT_PUBLISH
# and turns a failure into a warning or an error accordingly. Reads the key from
# stdin like headscale_vault_publish.
headscale_vault_publish_guarded() {
  local name="$1" actor="${2:-install-headscale}" disposition
  case "$HEADSCALE_VAULT_PUBLISH" in
    off|0|false|no)
      echo "[headscale-vault] publication disabled (HEADSCALE_VAULT_PUBLISH=off)"
      cat >/dev/null
      return 0
      ;;
    auto|required|1|true|yes) ;;
    *)
      echo "[headscale-vault] ERROR: unsupported HEADSCALE_VAULT_PUBLISH: ${HEADSCALE_VAULT_PUBLISH}" >&2
      cat >/dev/null
      return 1
      ;;
  esac
  if disposition="$(headscale_vault_publish "$name" "$actor")"; then
    echo "[headscale-vault] ${disposition} secret ${name}"
    return 0
  fi
  if [ "$HEADSCALE_VAULT_PUBLISH" = "required" ]; then
    echo "[headscale-vault] ERROR: HEADSCALE_VAULT_PUBLISH=required and publication failed" >&2
    return 1
  fi
  echo "[headscale-vault] WARNING: key not published to the vault; workers and control" >&2
  echo "[headscale-vault] nodes will have to receive it over the deploy pipeline instead." >&2
  return 0
}
