#!/usr/bin/env bash
# Prepare, verify, or withdraw MAC's Hermes chat gateway.
#
# Hermes is not vendored in this repo (see docs/hermes-vendor-fate.md for why
# that was tried once and abandoned) and does not support a normal `pip
# install` -- its own setup.py refuses to build a wheel or sdist ("Hermes is
# distributed via the shell installer, Docker image, or Nix"). This script
# prepares a reviewed external checkout and drives its CLI (`hermes config set`,
# `hermes gateway install`, `hermes claw migrate`) rather than reimplementing
# any of that logic in-tree. It is the host-level sibling of
# deploy/openclaw/install-openclaw-gateway.sh -- same prepare/verify/finalize/
# withdraw shape, same MAC_HOME conventions -- but Hermes runs as a bare host
# process (no OpenShell sandbox), so there is no container lifecycle here.
set -euo pipefail

FLEET_NAME="${MAC_HERMES_FLEET_NAME:-${MAC_FLEET_NAME:-mac}}"
MAC_HOME="${MAC_HOME:-$HOME/.mac}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
export HERMES_HOME
DRY_RUN="${MAC_HERMES_DRY_RUN:-0}"
RUNTIME_CONTEXT_MARKDOWN="${MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN:-$HERMES_HOME/mac-runtime-context.md}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
  [ -x "$HOME/.local/bin/hermes" ] && { printf '%s\n' "$HOME/.local/bin/hermes"; return 0; }
  command -v hermes 2>/dev/null && return 0
  return 1
}

hermes_runtime_dir() {
  local hermes
  hermes="$(hermes_bin)" || return 1
  "$MAC_HOME/venv/bin/python" -m mac.hermes_release resolve --launcher "$hermes"
}

release_command() {
  "$MAC_HOME/venv/bin/python" -m mac.hermes_release "$@" \
    --launcher "$HOME/.local/bin/hermes" \
    --home "$HERMES_HOME" --markdown "$RUNTIME_CONTEXT_MARKDOWN" \
    --manifest "$SCRIPT_DIR/python314-source.json" \
    --manifest "$SCRIPT_DIR/runtime-context-source.json"
}

hermes_python_bin() {
  local runtime_dir candidate
  runtime_dir="$(hermes_runtime_dir)" || return 1
  candidate="$runtime_dir/.venv/bin/python"
  [ -x "$candidate" ] || return 1
  "$candidate" -c 'import platform,sys; raise SystemExit(platform.python_version() != sys.argv[1])' 3.14.7 \
    || return 1
  # Keep the venv launcher path: resolving its symlink can select the base
  # interpreter and lose the active runtime's installed dependencies.
  printf '%s\n' "$candidate"
}

verify_runtime_context_bridge() {
  local runtime_dir hermes_python
  runtime_dir="$(hermes_runtime_dir)" \
    || die "cannot resolve active Hermes runtime from $(hermes_bin)"
  hermes_python="$(hermes_python_bin)" \
    || die "active Hermes runtime does not have its managed Python 3.14.7 interpreter"
  [ -s "$RUNTIME_CONTEXT_MARKDOWN" ] \
    || die "MAC runtime context is missing or empty: $RUNTIME_CONTEXT_MARKDOWN"
  MAC_HERMES_AGENT_DIR="$runtime_dir" \
  MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN="$RUNTIME_CONTEXT_MARKDOWN" \
  PYTHONPATH="$runtime_dir${PYTHONPATH:+:$PYTHONPATH}" \
  "$hermes_python" - <<'PY'
import os
from pathlib import Path

from agent import prompt_builder

runtime_dir = Path(os.environ["MAC_HERMES_AGENT_DIR"]).resolve()
if runtime_dir not in Path(prompt_builder.__file__).resolve().parents:
    raise SystemExit("prompt builder did not load from active Hermes runtime")

runtime = Path(os.environ["MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN"])
runtime_text = runtime.read_text(encoding="utf-8").strip()
prompt = prompt_builder.build_context_files_prompt(cwd=os.environ["HERMES_HOME"])
if not runtime_text or runtime_text not in prompt:
    raise SystemExit("active Hermes prompt omitted MAC runtime context")
soul = Path(os.environ["HERMES_HOME"]) / "SOUL.md"
if soul.is_file() and soul.read_text(encoding="utf-8").strip() not in prompt:
    raise SystemExit("active Hermes prompt omitted existing SOUL.md")
print("Hermes active runtime prompt includes MAC context and existing persona context")
PY
}

qualify_staged_runtime() {
  local stage="${1:-}" active="" mac_python="$MAC_HOME/venv/bin/python"
  [ -n "$stage" ] && [ -d "$stage" ] && git -C "$stage" rev-parse --git-dir >/dev/null 2>&1 \
    || die "qualify-stage requires a separate staged Hermes git checkout"
  active="$(hermes_runtime_dir 2>/dev/null || true)"
  [ -z "$active" ] || [ "$(cd "$stage" && pwd -P)" != "$(cd "$active" && pwd -P)" ] \
    || die "refusing to patch the active serving Hermes runtime"
  [ -x "$mac_python" ] || die "MAC deployment Python is unavailable: $mac_python"
  "$mac_python" -m mac.hermes_patch "$stage" \
    "$SCRIPT_DIR/python314-source.json" \
    "$SCRIPT_DIR/runtime-context-source.json" \
    || die "staged Hermes reviewed patch qualification failed"
  [ -x "$stage/.venv/bin/python" ] \
    || die "staged Hermes runtime has no managed interpreter"
  MAC_HERMES_AGENT_DIR="$stage" \
  MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN="$RUNTIME_CONTEXT_MARKDOWN" \
  PYTHONPATH="$stage${PYTHONPATH:+:$PYTHONPATH}" \
  "$stage/.venv/bin/python" - <<'PY'
import os
import tempfile
from pathlib import Path
from agent import prompt_builder

runtime = Path(os.environ["MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN"]).read_text(
    encoding="utf-8"
).strip()
with tempfile.TemporaryDirectory(prefix="mac-hermes-prompt-qualification-") as root:
    root_path = Path(root)
    workspace = root_path / "workspace"
    home = root_path / "home"
    workspace.mkdir()
    home.mkdir()
    project = "MAC qualification project instructions"
    soul = "MAC qualification persona context"
    (workspace / "AGENTS.md").write_text(project, encoding="utf-8")
    (home / "SOUL.md").write_text(soul, encoding="utf-8")
    prompt = prompt_builder.build_context_files_prompt(
        cwd=str(workspace), home_override=home
    )
    missing = [item for item in (project, runtime, soul) if not item or item not in prompt]
    if missing:
        raise SystemExit(
            "staged Hermes prompt omitted project, MAC runtime, or persona context"
        )
print("staged Hermes prompt preserved project, MAC runtime, and persona context")
PY
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

ensure_user_allowlist() {
  # Hermes defaults every platform to dm_policy/group_policy=pairing and
  # rejects any sender not on an explicit allowlist -- confirmed live: a
  # cutover that never set this left all three fleet nodes silently
  # rejecting every Slack message, including @mentions in the home channel,
  # with no startup failure (only a log line: "No env user allowlists
  # configured"). ~/.hermes/.env (not config.yaml) is what Hermes reads this
  # from; idempotent append, since prepare may run more than once.
  local env_file="$HERMES_HOME/.env"
  mkdir -p "$HERMES_HOME"
  touch "$env_file"
  grep -q '^SLACK_ALLOWED_USERS=' "$env_file" 2>/dev/null \
    || printf 'SLACK_ALLOWED_USERS=*\n' >> "$env_file"
}

# Set Hermes's own home channel (where it delivers cron-job results and
# cross-platform messages) from fleet config, not the interactive `/hermes
# sethome` Slack command -- confirmed live: every fleet node greeted its
# first message with "No home channel is set for Slack... Type /hermes
# sethome", because nothing had ever set one. `SLACK_HOME_CHANNEL` (+
# `_NAME`) in ~/.hermes/.env is what Hermes's own config loader
# (gateway/config_env.py::_slack_home) reads at every startup -- the same
# load path already used for SLACK_ALLOWED_USERS, and distinct from
# slack.free_response_channels/slack.require_mention (configure_gateway,
# below), which govern response behavior, not delivery routing.
#
# Resolving the channel name to an ID needs channel_directory.json, which
# Hermes only writes after its first successful Slack connection -- same
# ordering constraint resolve_home_channel_id already has for
# free_response_channels. A node that has never connected simply gets this
# on its next prepare, after the gateway installed by this same run has
# connected once.
ensure_home_channel_env() {
  [ -n "$SLACK_HOME_CHANNEL_NAME" ] || return 0
  local env_file="$HERMES_HOME/.env" channel_id
  mkdir -p "$HERMES_HOME"
  touch "$env_file"
  if channel_id="$(resolve_home_channel_id "$SLACK_HOME_CHANNEL_NAME")"; then
    log "setting Hermes home channel to $SLACK_HOME_CHANNEL_NAME ($channel_id)"
    grep -v '^SLACK_HOME_CHANNEL=\|^SLACK_HOME_CHANNEL_NAME=' "$env_file" > "$env_file.tmp" 2>/dev/null \
      || : > "$env_file.tmp"
    {
      cat "$env_file.tmp"
      printf 'SLACK_HOME_CHANNEL=%s\n' "$channel_id"
      printf 'SLACK_HOME_CHANNEL_NAME=%s\n' "$SLACK_HOME_CHANNEL_NAME"
    } > "$env_file"
    rm -f "$env_file.tmp"
  else
    log "WARNING: could not resolve #$SLACK_HOME_CHANNEL_NAME to a channel id yet" \
        "(gateway may not have connected to Slack before); re-run prepare after it has"
  fi
}

configure_gateway() {
  local hermes
  hermes="$(hermes_bin)" || die "hermes CLI not found"
  [ "$DRY_RUN" = 1 ] && { log "dry-run: skipping hermes config set calls"; return 0; }

  configure_terminal_cwd

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

configure_terminal_cwd() {
  local hermes backend cwd
  hermes="$(hermes_bin)" || die "hermes CLI not found"
  backend="$("$hermes" config get terminal.backend)" || die "cannot read terminal.backend"
  # Remote backends resolve paths in their own filesystem. Only the native
  # gateway's local terminal is subject to host-directory validation.
  [ "$backend" = local ] || return 0
  cwd="$("$hermes" config get terminal.cwd)" || die "cannot read terminal.cwd"
  case "$cwd" in
    ''|.|auto|cwd|/sandbox/workspace|/sandbox/workspace/)
      # Hermes resolves placeholder cwd through legacy MESSAGING_CWD. Migrated
      # profiles can still name OpenClaw's /sandbox/workspace in .env, even
      # though MAC's native gateway no longer has that filesystem. Pin the
      # canonical config so inherited legacy environment cannot win again.
      cwd="$HOME"
      ;;
  esac
  cwd="$(validate_terminal_cwd "$cwd")" || die "local terminal.cwd is not an accessible host directory; set it with hermes config set terminal.cwd"
  "$hermes" config set terminal.cwd "$cwd" --force
}

validate_terminal_cwd() {
  python3 - "$1" <<'PY'
import os
import sys

path = os.path.expanduser(sys.argv[1])
if not os.path.isabs(path):
    raise SystemExit(1)
try:
    os.chdir(path)
except OSError:
    raise SystemExit(1)
print(os.getcwd())
PY
}

install_service() {
  local hermes previous_pid
  hermes="$(hermes_bin)" || die "hermes CLI not found"
  [ "$DRY_RUN" = 1 ] && { log "dry-run: skipping hermes gateway install"; return 0; }
  log "installing Hermes gateway as a supervised background service"
  # Drain the old process through Hermes before replacing its definition.
  # launchd's bootout is asynchronous; upstream's force-install path can
  # bootstrap immediately after bootout and fail while the old job exits.
  # Capture the old runtime writer before stop can remove its state file.
  previous_pid="$(python3 - "$HERMES_HOME/gateway_state.json" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
if not path.exists():
    print(0)
else:
    pid = json.loads(path.read_text(encoding="utf-8")).get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        raise SystemExit("Cannot identify the previous Hermes runtime process")
    print(pid)
PY
)" || die "Cannot identify the previous Hermes gateway; refusing service replacement"
  "$hermes" gateway stop || die "Hermes gateway did not stop; refusing service replacement"
  # Upstream can report success after its own exit wait times out. Never
  # interpret that return code as permission to race the surviving process.
  python3 - "$previous_pid" <<'PY' || die "Hermes gateway is still exiting; refusing service replacement"
import os
import sys
import time

pid = int(sys.argv[1])
deadline = time.monotonic() + 10
while pid:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        break
    if time.monotonic() >= deadline:
        raise SystemExit(1)
    time.sleep(0.2)
PY
  # No --system: both systemd and launchd targets here are user-level
  # services (linger is what keeps a systemd --user unit alive across
  # logout on Linux nodes without root). --force makes this idempotent
  # against a prior manual install.
  "$hermes" gateway install --force --start-now --start-on-login \
    || die "hermes gateway install failed"
}

verify_gateway() {
  release_command verify || die "Hermes selected release or service identity failed verification"
  local hermes status
  hermes="$(hermes_bin)" || die "hermes CLI not found"
  local backend cwd
  backend="$("$hermes" config get terminal.backend)" || die "cannot read terminal.backend"
  if [ "$backend" = local ]; then
    cwd="$("$hermes" config get terminal.cwd)" || die "cannot read terminal.cwd"
    validate_terminal_cwd "$cwd" >/dev/null \
      || die "local terminal.cwd is not an accessible absolute host directory; run prepare before verifying gateway health"
  fi
  grep -q '^SLACK_ALLOWED_USERS=' "$HERMES_HOME/.env" 2>/dev/null \
    || die "SLACK_ALLOWED_USERS is not set in $HERMES_HOME/.env -- Hermes defaults" \
           "every platform to dm_policy/group_policy=pairing and silently rejects" \
           "every sender (including @mentions) with no startup failure; run" \
           "ensure_user_allowlist (prepare) first"
  grep -q '^MAC_CHAT_GATEWAY_IMPL=hermes$' "$MAC_HOME/mac.env" 2>/dev/null \
    || die "MAC_CHAT_GATEWAY_IMPL is not 'hermes' in $MAC_HOME/mac.env -- mac-agent's" \
           "own startup self-test derives its OpenClaw-required branch from this" \
           "variable and will crash-loop forever demanding an OpenClaw advertisement" \
           "that no longer exists; run ensure_chat_gateway_impl_env (prepare) first"
  verify_runtime_context_bridge
  # --deep appends historical log lines, including normal shutdowns from
  # earlier processes. Only inspect the current service status here.
  status="$("$hermes" gateway status 2>&1)" || die "hermes gateway status failed:
$status"
  python3 - "$HERMES_HOME" 3<<<"$status" <<'PY_VERIFY'
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

with os.fdopen(3) as stream:
    status = re.sub(r"\x1b\[[0-9;]*m", "", stream.read())
# Match upstream's current-state summary, never a substring in journal/log
# history or the "registered but not supervising" fallback explanation.
launchd = re.search(r"(?m)^[✓ ]*Gateway is supervised by launchd \(PID ([0-9]+)\)\s*$", status)
systemd = re.search(r"(?m)^[✓ ]*User gateway service is running\s*$", status)
main_pid = re.search(r"(?m)^\s*Main PID: ([0-9]+)\b", status) if systemd else launchd
if main_pid is None:
    raise SystemExit("Hermes gateway is not healthy and supervised")
supervisor_pid = int(main_pid.group(1))
if supervisor_pid <= 1:
    raise SystemExit("Hermes gateway supervisor has no valid process")
path = Path(sys.argv[1]) / "gateway_state.json"
deadline = time.monotonic() + 20
while True:
    try:
        metadata = path.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o022 or metadata.st_size > 1024 * 1024):
            raise ValueError("runtime status is not an owner-controlled bounded file")
        state = json.loads(path.read_text(encoding="utf-8"))
        pid = state.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
            raise ValueError("runtime status has no valid process")
        # Hermes can launch a child behind its supervised entrypoint. Bind the
        # runtime writer to that current process tree so an old profile's JSON
        # cannot satisfy readiness for a different live service.
        process = subprocess.run(
            ["ps", "-p", str(pid), "-o", "ppid=", "-o", "uid="],
            capture_output=True, text=True, timeout=3,
        )
        if process.returncode == 0:
            parent, uid = map(int, process.stdout.split())
            current_writer = uid == os.getuid() and (
                pid == supervisor_pid or parent == supervisor_pid
            )
            if current_writer:
                slack = state.get("platforms", {}).get("slack", {})
                if state.get("gateway_state") in {"startup_failed", "draining"} or slack.get("state") == "fatal":
                    raise ValueError("gateway runtime is draining or failed")
                if (state.get("gateway_state") == "running"
                        and slack.get("state") == "connected"
                        and slack.get("writer_pid") == pid):
                    print("Hermes gateway is supervised; its live runtime reports Slack connected")
                    break
        # Upstream retains the previous writer's status during replacement,
        # then can stamp the new PID before replacing its inherited "stopped"
        # state. Neither snapshot proves that the current gateway has failed.
        # Wait for current-writer readiness, never accept an old Slack result.
    except FileNotFoundError:
        # The selected profile may not have its first runtime snapshot yet.
        pass
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
        # Do not print runtime payloads or platform error messages: they may
        # contain credentials or message content.
        raise SystemExit("Hermes messaging readiness failed: " + type(exc).__name__) from None
    if time.monotonic() >= deadline:
        raise SystemExit("Hermes messaging readiness failed: timed out waiting for current gateway and Slack")
    time.sleep(1)
PY_VERIFY
}

ensure_chat_gateway_impl_env() {
  # mac-agent's own startup self-test derives its OpenClaw-required-or-not
  # branch from MAC_CHAT_GATEWAY_IMPL in ~/.mac/mac.env (see
  # deploy/fleet-node-install.sh's embedded self-test:
  # `openclaw_required = MAC_CHAT_GATEWAY_IMPL == "openclaw"`) -- but only
  # fleet-node-install.sh's own full deploy path ever wrote that variable.
  # A cutover run through this standalone installer (as every node's Hermes
  # cutover was, this session) never touched it, so mac.env kept claiming
  # "openclaw" after the gateway was gone. Confirmed live: mac-agent then
  # crash-loops forever at startup, since the self-test hard-requires an
  # OpenClaw advertisement that no longer exists -- and the agent is
  # `agent_offline`/`agent_unhealthy` for as long as it never starts.
  local env_file="$MAC_HOME/mac.env"
  mkdir -p "$MAC_HOME"
  touch "$env_file"
  local runtime_dir hermes_python mac_python
  runtime_dir="$(hermes_runtime_dir)" || die "cannot resolve active Hermes runtime from $(hermes_bin)"
  hermes_python="$(hermes_python_bin)" \
    || die "active Hermes runtime does not have its managed Python 3.14.7 interpreter"
  mac_python="$MAC_HOME/venv/bin/python"
  [ -x "$mac_python" ] || die "MAC deployment Python is unavailable: $mac_python"
  "$mac_python" - "$env_file" "$runtime_dir" "$hermes_python" <<'PY'
import sys
from pathlib import Path

from mac.deploy_env import update_env_file

update_env_file(
    Path(sys.argv[1]),
    {
        "MAC_CHAT_GATEWAY_IMPL": "hermes",
        "MAC_HERMES_AGENT_DIR": sys.argv[2],
        "MAC_HERMES_PYTHON": sys.argv[3],
    },
)
PY
}

prepare() {
  [ "$DRY_RUN" = 1 ] && { log "dry-run: skipping Hermes preparation and activation"; return 0; }
  # Qualification completes before profile writes, service stop or selection.
  # The caller's existing deployment transaction owns rollback of the launcher
  # and service definition. Failed candidates remain available for diagnosis.
  local candidate
  candidate="$(release_command prepare --root "$MAC_HOME/hermes-runtimes" "$@")" \
    || die "Hermes candidate qualification failed; active runtime was not changed"
  release_command activate --runtime "$candidate" \
    || die "Hermes runtime activation failed"
  ensure_user_allowlist
  ensure_home_channel_env
  ensure_chat_gateway_impl_env
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
  prepare)  prepare "${@:2}" ;;
  verify)   verify ;;
  finalize) finalize ;;
  withdraw) withdraw ;;
  qualify-stage) qualify_staged_runtime "${2:-}" ;;
  *) die "usage: $0 [prepare|verify|finalize|withdraw|qualify-stage <checkout>]" ;;
esac
