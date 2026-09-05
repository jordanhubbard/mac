#!/usr/bin/env bash
# Prepare, verify, or withdraw MAC's Hermes chat gateway.
#
# Hermes is not vendored in this repo (see docs/hermes-vendor-fate.md for why
# that was tried once and abandoned) and does not support a normal `pip
# install` -- its own setup.py refuses to build a wheel or sdist ("Hermes is
# distributed via the shell installer, Docker image, or Nix"). This script
# drives upstream's own shell installer and its own CLI (`hermes config set`,
# `hermes gateway install`, `hermes claw migrate`) rather than reimplementing
# any of that logic in-tree. It is the host-level sibling of
# deploy/openclaw/install-openclaw-gateway.sh -- same prepare/verify/finalize/
# withdraw shape, same MAC_HOME conventions -- but Hermes runs as a bare host
# process (no OpenShell sandbox), so there is no container lifecycle here.
set -euo pipefail

HERMES_INSTALL_URL="${MAC_HERMES_INSTALL_URL:-https://hermes-agent.nousresearch.com/install.sh}"
FLEET_NAME="${MAC_HERMES_FLEET_NAME:-${MAC_FLEET_NAME:-mac}}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
# Migration source: the chat gateway Hermes is taking channel ownership from.
# "openclaw" is the only interface `mac admin human-interface port` and
# `hermes claw migrate` currently know how to read from.
FROM_INTERFACE="${MAC_HERMES_FROM_INTERFACE:-openclaw}"
OPENCLAW_HOME="${MAC_HERMES_OPENCLAW_SOURCE:-$HOME/.openclaw}"
DRY_RUN="${MAC_HERMES_DRY_RUN:-0}"

# Fleet-config-supplied gateway policy (docs/fleet-registry-schema.md's
# `hermes:` block: slack_home_channel_name, gateway_model, gateway_provider,
# gateway_base_url). Any of these may be empty -- an empty value means "leave
# whatever Hermes already has configured alone".
SLACK_HOME_CHANNEL_NAME="${MAC_HERMES_SLACK_HOME_CHANNEL_NAME:-}"
GATEWAY_MODEL="${MAC_HERMES_GATEWAY_MODEL:-}"
GATEWAY_PROVIDER="${MAC_HERMES_GATEWAY_PROVIDER:-}"
GATEWAY_BASE_URL="${MAC_HERMES_GATEWAY_BASE_URL:-}"

log() { printf '[install-hermes-gateway] %s\n' "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

hermes_bin() {
  command -v hermes 2>/dev/null && return 0
  [ -x "$HOME/.local/bin/hermes" ] && { printf '%s\n' "$HOME/.local/bin/hermes"; return 0; }
  return 1
}

install_hermes() {
  if hermes_bin >/dev/null 2>&1; then
    log "hermes CLI already installed ($(hermes_bin))"
    return 0
  fi
  log "installing Hermes via upstream's shell installer ($HERMES_INSTALL_URL)"
  [ "$DRY_RUN" = 1 ] && { log "dry-run: skipping installer"; return 0; }
  curl -fsSL "$HERMES_INSTALL_URL" | bash \
    || die "Hermes shell installer failed"
  hermes_bin >/dev/null 2>&1 || die "hermes CLI not found on PATH or in ~/.local/bin after install"
}

port_credentials() {
  command -v mac >/dev/null 2>&1 || { log "no 'mac' CLI on PATH; skipping credential port"; return 0; }
  log "porting identity/memory/messaging credentials from $FROM_INTERFACE to hermes"
  [ "$DRY_RUN" = 1 ] && { mac admin human-interface port --from "$FROM_INTERFACE" --to hermes || true; return 0; }
  mac admin human-interface port --from "$FROM_INTERFACE" --to hermes --apply \
    || log "WARNING: credential port reported a problem; continuing (Hermes may already have credentials)"
}

migrate_claw_state() {
  [ -d "$OPENCLAW_HOME" ] || { log "no OpenClaw home at $OPENCLAW_HOME; skipping claw migrate"; return 0; }
  local hermes
  hermes="$(hermes_bin)" || die "hermes CLI not found"
  log "migrating OpenClaw identity/memory/skills into Hermes ($OPENCLAW_HOME -> $HERMES_HOME)"
  [ "$DRY_RUN" = 1 ] && { "$hermes" claw migrate --source "$OPENCLAW_HOME" --dry-run || true; return 0; }
  "$hermes" claw migrate --source "$OPENCLAW_HOME" --preset full --overwrite --yes \
    || log "WARNING: claw migrate reported a problem; continuing with whatever Hermes already has"
}

# Resolve a Slack channel *name* (e.g. "rockyandfriends") to the channel ID
# Hermes's own config wants for slack.free_response_channels. Hermes caches a
# channel directory once the gateway has connected at least once; before that
# there is nothing to resolve against, and this deliberately does not fail --
# a later run (after the gateway has connected) will pick it up.
resolve_home_channel_id() {
  local name="$1" hermes
  hermes="$(hermes_bin)" || return 1
  local directory
  directory="$(find "$HERMES_HOME" -iname '*channel_directory*.json' 2>/dev/null | head -1)"
  [ -n "$directory" ] || return 1
  python3 - "$directory" "$name" <<'PY'
import json
import sys

path, wanted = sys.argv[1], sys.argv[2].lstrip("#").lower()
try:
    data = json.load(open(path, encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
channels = data if isinstance(data, list) else data.get("channels", [])
for entry in channels:
    if not isinstance(entry, dict):
        continue
    name = str(entry.get("name") or "").lstrip("#").lower()
    if name == wanted:
        channel_id = entry.get("id") or entry.get("channel_id")
        if channel_id:
            print(channel_id)
            raise SystemExit(0)
raise SystemExit(1)
PY
}

configure_gateway() {
  local hermes
  hermes="$(hermes_bin)" || die "hermes CLI not found"
  [ "$DRY_RUN" = 1 ] && { log "dry-run: skipping hermes config set calls"; return 0; }

  # Dotted paths ("model.default", not "model") are load-bearing here: Hermes
  # stores the active model as a nested object (model.default/.provider/
  # .base_url/.api_key). `config set model ...` replaces that whole object
  # with a bare scalar, silently discarding base_url/api_key/provider --
  # confirmed live: it broke a working custom-router setup on the first
  # idempotent re-run of `prepare` against an already-configured node.
  [ -z "$GATEWAY_MODEL" ]    || "$hermes" config set model.default "$GATEWAY_MODEL" --force
  [ -z "$GATEWAY_PROVIDER" ] || "$hermes" config set model.provider "$GATEWAY_PROVIDER" --force
  [ -z "$GATEWAY_BASE_URL" ] || "$hermes" config set model.base_url "$GATEWAY_BASE_URL" --force

  # Home channel: listen to and respond to everything, unprompted. Every
  # other channel the agent is invited into stays mention-only -- that is
  # slack.require_mention's default (true) and is left untouched here.
  "$hermes" config set slack.require_mention true --force
  if [ -n "$SLACK_HOME_CHANNEL_NAME" ]; then
    local channel_id
    if channel_id="$(resolve_home_channel_id "$SLACK_HOME_CHANNEL_NAME")"; then
      log "setting free_response_channels for home channel $SLACK_HOME_CHANNEL_NAME ($channel_id)"
      "$hermes" config set slack.free_response_channels "$channel_id" --force
    else
      log "WARNING: could not resolve #$SLACK_HOME_CHANNEL_NAME to a channel id yet" \
          "(gateway may not have connected to Slack before); re-run prepare after it has"
    fi
  fi
}

install_service() {
  local hermes
  hermes="$(hermes_bin)" || die "hermes CLI not found"
  [ "$DRY_RUN" = 1 ] && { log "dry-run: skipping hermes gateway install"; return 0; }
  log "installing Hermes gateway as a supervised background service"
  # No --system: both systemd and launchd targets here are user-level
  # services (linger is what keeps a systemd --user unit alive across
  # logout on Linux nodes without root). --force makes this idempotent
  # against a prior manual install.
  "$hermes" gateway install --force --start-now --start-on-login \
    || die "hermes gateway install failed"
}

verify_gateway() {
  local hermes status
  hermes="$(hermes_bin)" || die "hermes CLI not found"
  status="$("$hermes" gateway status --deep 2>&1)" || die "hermes gateway status failed:
$status"
  printf '%s\n' "$status"
  # Order matters: "not supervised"/"unsupervised" both contain the substring
  # "supervised", so the negative checks must run first or a detached-process
  # gateway would pass the naive positive match below.
  case "$status" in
    *[Nn]ot\ supervised*|*[Uu]nsupervised*|*detached\ process*)
      die "Hermes gateway is not supervised (would not survive a crash/reboot):
$status" ;;
  esac
  case "$status" in
    *[Uu]nhealthy*|*[Nn]ot\ running*|*[Ss]topped*)
      die "Hermes gateway is not healthy:
$status" ;;
  esac
  case "$status" in
    *[Ss]upervised*) ;;
    *) die "Hermes gateway does not report itself as supervised:
$status" ;;
  esac
}

prepare() {
  install_hermes
  port_credentials
  migrate_claw_state
  configure_gateway
  install_service
}

verify() { verify_gateway; }

finalize() {
  verify_gateway
  log "Hermes gateway prepared and verified for fleet '$FLEET_NAME'"
}

withdraw() {
  local hermes
  hermes="$(hermes_bin)" || { log "hermes CLI not installed; nothing to withdraw"; return 0; }
  log "stopping Hermes gateway (config and credentials are left in place)"
  "$hermes" gateway stop || log "WARNING: hermes gateway stop reported a problem"
}

case "${1:-prepare}" in
  prepare)  prepare ;;
  verify)   verify ;;
  finalize) finalize ;;
  withdraw) withdraw ;;
  *) die "usage: $0 [prepare|verify|finalize|withdraw]" ;;
esac
