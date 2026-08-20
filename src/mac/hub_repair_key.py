"""Hub-owned repair key: hub-to-worker SSH that does not depend on the operator.

Once a fleet is bootstrapped, every remaining path into a worker belongs to the
human who provisioned it.  If that operator's key is rotated, their laptop is
offline, or a different operator inherits the fleet, nothing can reach a wedged
worker even though the hub is healthy and talks to it constantly over HTTP.
The hub therefore owns a keypair of its own and each worker authorizes it
during bootstrap, *in addition to* the provisioner's key -- never instead of it.

The four decisions that make that safe, and where they live:

``where the private key lives``
    ``~/.mac/keys/mac-hub-repair-id`` on the hub node.  ``~/.mac`` is node state
    that a deploy preserves (a deploy replaces ``~/.mac/src`` and
    ``~/.mac/venv`` and backs the old ones up), and it is never read into a
    release archive, so the key survives redeploys without ever becoming a
    deploy artifact.  Contrast ``~/.ssh/mac_tunnel_id``, the *reverse tunnel*
    key: that one is deliberately left alone here, because a tunnel key needs
    port forwarding and a repair key must not have it.

``rotation``
    Not on redeploy.  A key that rotates every deploy is a key that is stale on
    every worker that missed that deploy, which is exactly the population you
    need to reach.  Generation is create-if-absent; rotation is an explicit act.
    What makes rotation safe is :func:`merge_authorized_keys`, which replaces
    the previous hub entry instead of appending next to it, so a rotated key
    does not leave its predecessor authorized forever.

``what the key may do``
    Not a shell.  The authorized_keys entry is ``restrict`` plus a forced
    ``command=``, so the key can only run the generated shim
    (:func:`repair_shim_script`), which accepts the closed verb set in
    :data:`VERBS` and nothing else.  The shim is plain POSIX ``sh`` with no
    Python and no venv dependency on purpose: the repair path must survive the
    thing it exists to repair.

``ProxyJump``
    Unchanged and reused.  A deploy installs the fleet registry at
    ``~/.mac/fleets.yaml`` on every node, so the hub resolves a worker route
    through :mod:`mac.fleet_ssh` exactly like an operator does, inheriting the
    fleet's ``ssh_jump`` bastion for in-cluster pods.  The hub needs its own
    key, not its own transport.

The module is the single source of truth for all of it: the deploy installs
what this module renders rather than hand-rolling authorized_keys text in
shell, so the on-node grammar and the grammar this module validates cannot
drift apart.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

SCHEMA = "mac.hub_repair_key.v1"

#: Comment stamped into the hub's public key and used as the authorized_keys
#: marker.  Entry identity lives in this comment so a rotated key replaces its
#: predecessor rather than accumulating beside it.
KEY_COMMENT = "mac-hub-repair"

#: Default on-node locations.  Both sit under ``~/.mac`` (node state a deploy
#: preserves), not under the deployed source tree (which a deploy replaces).
DEFAULT_KEY_RELPATH = "keys/mac-hub-repair-id"
DEFAULT_SHIM_RELPATH = "bin/mac-hub-repair"

#: ``EX_CONFIG``.  Distinguishable from a repair command's own exit status, so
#: "the hub was refused" never reads as "the repair ran and failed".
EXIT_DENIED = 78

DEFAULT_TAIL_LINES = 200
MAX_TAIL_LINES = 500

#: Key algorithms accepted in an authorized_keys line.  Narrow on purpose: this
#: is the parser that decides which existing entries are ours, so it must
#: recognize real entries, and it is also the validator for the key the hub
#: presents.
KEY_ALGORITHMS = frozenset(
    {
        "ssh-ed25519",
        "sk-ssh-ed25519@openssh.com",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ecdsa-sha2-nistp256@openssh.com",
        "ssh-rsa",
        "rsa-sha2-256",
        "rsa-sha2-512",
    }
)

#: The alphabet a repair request may use.  ``/`` is absent deliberately: no
#: repair argument is a path, so excluding the separator removes directory
#: traversal from the grammar rather than filtering for it afterwards.
_REQUEST_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:@-]+$")
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,3}$")
_SERVICE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_UNIT_NAME_RE = re.compile(r"^[A-Za-z0-9._:@-]+$")

SUPERVISORS = ("systemd", "launchd", "supervisord")


class HubRepairKeyError(ValueError):
    """Raised for an unusable key, an unparseable file, or a denied request."""


@dataclass(frozen=True)
class VerbSpec:
    """One allowlisted repair verb and its arity."""

    name: str
    min_args: int
    max_args: int
    summary: str


#: The closed set of things the hub's key may ask a worker to do.  Every verb
#: is diagnosis or a restart of something the deploy already manages; nothing
#: here writes node configuration, and nothing takes a free-form command.
VERBS: Dict[str, VerbSpec] = {
    spec.name: spec
    for spec in (
        VerbSpec("status", 0, 0, "node identity, deployed revision, service states"),
        VerbSpec("services", 0, 0, "supervisor state of each mac-managed service"),
        VerbSpec("restart", 1, 1, "restart one allowlisted service"),
        VerbSpec("logs", 0, 0, "list the log files available to tail"),
        VerbSpec("tail", 1, 2, "tail one log file, bounded to %d lines" % MAX_TAIL_LINES),
        VerbSpec("deploy-info", 0, 0, "deployed source revision and deploy generation"),
    )
}


@dataclass(frozen=True)
class RepairRequest:
    """A validated repair request: a known verb and checked arguments."""

    verb: str
    args: Tuple[str, ...]

    @property
    def command(self) -> str:
        """Return the canonical wire form sent as ``SSH_ORIGINAL_COMMAND``."""

        return " ".join((self.verb,) + self.args)

    def to_dict(self) -> Dict[str, Any]:
        return {"schema": SCHEMA, "verb": self.verb, "args": list(self.args)}


@dataclass(frozen=True)
class PublicKey:
    """A parsed SSH public key, with the hub marker applied to the comment."""

    algorithm: str
    blob: str
    comment: str

    def text(self) -> str:
        return "%s %s %s" % (self.algorithm, self.blob, self.comment)


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

def mac_home(home: Optional[str] = None) -> Path:
    """Return the node's MAC state directory (``$MAC_HOME`` or ``~/.mac``)."""

    if home:
        return Path(home).expanduser()
    configured = os.environ.get("MAC_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".mac"


def hub_repair_key_path(home: Optional[str] = None) -> Path:
    """Return the hub's private repair key path."""

    override = os.environ.get("MAC_HUB_REPAIR_KEY")
    if override and home is None:
        return Path(override).expanduser()
    return mac_home(home) / DEFAULT_KEY_RELPATH


def hub_repair_shim_path(home: Optional[str] = None) -> Path:
    """Return the worker-side forced-command shim path."""

    return mac_home(home) / DEFAULT_SHIM_RELPATH


# ---------------------------------------------------------------------------
# Public key + authorized_keys grammar
# ---------------------------------------------------------------------------

def parse_public_key(text: str) -> PublicKey:
    """Parse one SSH public key and stamp it with the hub marker comment.

    The comment is normalized rather than preserved: it is the identity
    :func:`merge_authorized_keys` matches on, so it must be ours regardless of
    what ``ssh-keygen -C`` happened to write.
    """

    candidate = (text or "").strip()
    if not candidate:
        raise HubRepairKeyError("public key is empty")
    if "\n" in candidate or "\r" in candidate:
        raise HubRepairKeyError("public key must be a single line")
    fields = candidate.split()
    if len(fields) < 2:
        raise HubRepairKeyError("public key must be '<algorithm> <base64> [comment]'")
    algorithm, blob = fields[0], fields[1]
    if algorithm not in KEY_ALGORITHMS:
        raise HubRepairKeyError("unsupported key algorithm %r" % algorithm)
    if not _BASE64_RE.match(blob):
        raise HubRepairKeyError("public key body is not base64")
    return PublicKey(algorithm=algorithm, blob=blob, comment=KEY_COMMENT)


def authorized_keys_options(
    forced_command: str,
    *,
    from_patterns: Sequence[str] = (),
) -> str:
    """Return the option string for the hub's entry.

    ``restrict`` is the whole point: it withdraws pty allocation, agent, port,
    X11 forwarding and user rc, so the entry cannot be widened later by
    forgetting to add a new ``no-*`` option when OpenSSH grows a new capability.
    The forced ``command=`` then narrows the one remaining capability -- running
    a program -- to the generated shim.
    """

    command = (forced_command or "").strip()
    if not command:
        raise HubRepairKeyError("a forced command is required; an unrestricted hub key is not installable")
    if '"' in command or "\\" in command or "\n" in command:
        raise HubRepairKeyError("forced command must not contain quotes, backslashes, or newlines")
    options = ['restrict', 'command="%s"' % command]
    patterns = [str(item).strip() for item in from_patterns if str(item).strip()]
    if patterns:
        for pattern in patterns:
            if any(char in pattern for char in '", \\\n'):
                raise HubRepairKeyError("from pattern %r contains an unusable character" % pattern)
        options.append('from="%s"' % ",".join(patterns))
    return ",".join(options)


def authorized_keys_line(
    public_key: str,
    *,
    forced_command: str,
    from_patterns: Sequence[str] = (),
) -> str:
    """Return the complete, restricted authorized_keys line for the hub key."""

    parsed = parse_public_key(public_key)
    options = authorized_keys_options(forced_command, from_patterns=from_patterns)
    return "%s %s" % (options, parsed.text())


def _end_of_options(line: str) -> Optional[int]:
    """Return the index just past the option field, honoring quoted values.

    An authorized_keys option list is one whitespace-free field *except* inside
    double quotes, which is where forced commands live.  Splitting on
    whitespace without tracking quotes is the classic way to mangle exactly the
    entries this module writes.
    """

    in_quote = False
    index = 0
    while index < len(line):
        char = line[index]
        if char == "\\" and in_quote:
            index += 2
            continue
        if char == '"':
            in_quote = not in_quote
        elif not in_quote and char.isspace():
            return index
        index += 1
    return None


def _parse_authorized_keys_line(line: str) -> Optional[Tuple[str, str, str, str]]:
    """Return ``(options, algorithm, blob, comment)`` or ``None`` if not a key."""

    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    head = stripped.split(None, 1)[0]
    if head in KEY_ALGORITHMS:
        options, rest = "", stripped
    else:
        cut = _end_of_options(stripped)
        if cut is None:
            return None
        options, rest = stripped[:cut], stripped[cut:].lstrip()
    fields = rest.split()
    if len(fields) < 2 or fields[0] not in KEY_ALGORITHMS:
        return None
    return options, fields[0], fields[1], " ".join(fields[2:])


def merge_authorized_keys(existing: str, line: str) -> Tuple[str, bool]:
    """Merge the hub entry into *existing*, replacing any earlier hub entry.

    Returns ``(text, changed)``.  Two entries are considered the hub's: one
    whose comment is :data:`KEY_COMMENT` (a previous, possibly rotated hub key)
    and one carrying the same key material (the same key installed under a
    different comment).  Everything else -- the provisioner's key above all --
    is copied through byte for byte and keeps its position.
    """

    parsed = _parse_authorized_keys_line(line)
    if parsed is None:
        raise HubRepairKeyError("refusing to install an unparseable authorized_keys line")
    _, _, new_blob, new_comment = parsed
    if new_comment != KEY_COMMENT:
        raise HubRepairKeyError(
            "hub entry must carry the %r comment marker" % KEY_COMMENT
        )

    kept: List[str] = []
    for raw in (existing or "").splitlines():
        entry = _parse_authorized_keys_line(raw)
        if entry is not None:
            _, _, blob, comment = entry
            if comment == KEY_COMMENT or blob == new_blob:
                continue
        kept.append(raw.rstrip("\n"))
    while kept and not kept[-1].strip():
        kept.pop()
    kept.append(line.strip())
    text = "\n".join(kept) + "\n"
    return text, text != (existing or "")


def install_authorized_key(path: Path, line: str) -> bool:
    """Write the merged authorized_keys file at *path*; return whether it changed.

    The write is atomic and mode 0600 from creation: a partially written or
    briefly world-readable authorized_keys file is a live authorization bug,
    not a cleanup task.
    """

    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, stat.S_IRWXU)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    merged, changed = merge_authorized_keys(existing, line)
    if not changed:
        return False
    temp = path.with_name(path.name + ".mac-hub-repair.tmp")
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(merged)
    os.replace(str(temp), str(path))
    os.chmod(path, 0o600)
    return True


# ---------------------------------------------------------------------------
# Request grammar
# ---------------------------------------------------------------------------

def parse_repair_request(
    command: Optional[str],
    *,
    services: Optional[Iterable[str]] = None,
) -> RepairRequest:
    """Validate one repair request and return it, or raise :class:`HubRepairKeyError`.

    This mirrors, in Python, the check the generated shim performs on the node.
    It exists so the grammar can be tested directly and so hub-side callers
    refuse a bad request before spending an SSH connection on it.
    """

    request = (command or "").strip()
    if not request:
        raise HubRepairKeyError(
            "interactive shells are not permitted on the hub repair key; send one repair verb"
        )
    tokens = request.split()
    for token in tokens:
        if not _REQUEST_TOKEN_RE.match(token) or ".." in token:
            raise HubRepairKeyError("request token %r is outside the repair alphabet" % token)
    verb, args = tokens[0], tuple(tokens[1:])
    spec = VERBS.get(verb)
    if spec is None:
        raise HubRepairKeyError(
            "verb %r is not in the repair allowlist (%s)" % (verb, ", ".join(sorted(VERBS)))
        )
    if not spec.min_args <= len(args) <= spec.max_args:
        raise HubRepairKeyError(
            "%s takes %d..%d arguments, got %d" % (verb, spec.min_args, spec.max_args, len(args))
        )
    if verb == "restart" and services is not None and args[0] not in set(services):
        raise HubRepairKeyError(
            "service %r is not in the repair allowlist (%s)"
            % (args[0], ", ".join(sorted(services)))
        )
    if verb == "tail" and len(args) == 2:
        if not args[1].isdigit() or not 1 <= int(args[1]) <= MAX_TAIL_LINES:
            raise HubRepairKeyError(
                "tail line count must be an integer in 1..%d" % MAX_TAIL_LINES
            )
    return RepairRequest(verb=verb, args=args)


def parse_service_map(values: Iterable[str]) -> Dict[str, str]:
    """Parse ``name=unit`` pairs into the shim's service allowlist."""

    services: Dict[str, str] = {}
    for value in values:
        name, sep, unit = str(value).partition("=")
        name, unit = name.strip(), unit.strip()
        if not sep or not name or not unit:
            raise HubRepairKeyError("service mapping %r must be name=unit" % value)
        if not _SERVICE_NAME_RE.match(name):
            raise HubRepairKeyError("service name %r must be lowercase alphanumeric" % name)
        if not _UNIT_NAME_RE.match(unit):
            raise HubRepairKeyError("service unit %r is outside the repair alphabet" % unit)
        services[name] = unit
    return services


# ---------------------------------------------------------------------------
# Shim
# ---------------------------------------------------------------------------

def repair_shim_script(
    *,
    supervisor: str,
    services: Mapping[str, str],
    agent: str = "",
) -> str:
    """Render the worker-side forced command as self-contained POSIX ``sh``.

    No Python, no venv, no repository checkout: the shim must still run on a
    node whose venv is half-installed or whose deployed source was rolled back,
    because that is when someone needs it.  Everything variable -- the
    supervisor kind and the service allowlist -- is baked in at install time by
    the deploy that already knows those values.
    """

    if supervisor not in SUPERVISORS:
        raise HubRepairKeyError(
            "supervisor must be one of: %s" % ", ".join(SUPERVISORS)
        )
    if not services:
        raise HubRepairKeyError("at least one allowlisted service is required")
    for name, unit in services.items():
        if not _SERVICE_NAME_RE.match(name) or not _UNIT_NAME_RE.match(unit):
            raise HubRepairKeyError("service mapping %r=%r is not installable" % (name, unit))
    if agent and not _UNIT_NAME_RE.match(agent):
        raise HubRepairKeyError("agent name %r is outside the repair alphabet" % agent)

    ordered = sorted(services.items())
    case_arms = "\n".join(
        "    %s) printf '%%s\\n' '%s' ;;" % (name, unit) for name, unit in ordered
    )
    service_names = " ".join(name for name, _ in ordered)
    verb_help = "\n".join(
        "#   %-12s %s" % (spec.name, spec.summary) for spec in VERBS.values()
    )

    return """#!/bin/sh
# Generated by mac.hub_repair_key ({schema}). Do not edit on the node.
#
# Forced command for the hub's self-registered repair key. The hub's
# authorized_keys entry is `restrict,command="<this file>"`, so this script --
# not a shell -- is everything that key can do. It accepts one verb from a
# closed allowlist and refuses everything else, including an empty request
# (which is what an attempt at an interactive shell looks like from here).
#
{verb_help}
#
# Deliberately dependency-free: no Python, no venv, no deployed source tree.
# The repair path must outlive the thing it repairs.
set -u

SCHEMA='{schema}'
SUPERVISOR='{supervisor}'
AGENT_NAME='{agent}'
SERVICE_NAMES='{service_names}'
MAC_HOME="${{MAC_HOME:-$HOME/.mac}}"
LOG_DIR="$MAC_HOME/logs"
AUDIT_LOG="$LOG_DIR/hub-repair.log"
MAX_TAIL_LINES={max_tail}
DEFAULT_TAIL_LINES={default_tail}
EXIT_DENIED={exit_denied}

request="${{SSH_ORIGINAL_COMMAND-}}"

audit() {{
  [ -d "$LOG_DIR" ] || mkdir -p "$LOG_DIR" 2>/dev/null || return 0
  printf '%s %s peer=%s request=%s\\n' \\
    "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)" \\
    "$1" "${{SSH_CONNECTION:-unknown}}" "$2" >>"$AUDIT_LOG" 2>/dev/null || true
}}

deny() {{
  audit denied "$request"
  printf 'mac-hub-repair: denied: %s\\n' "$*" >&2
  exit "$EXIT_DENIED"
}}

# Run privileged helpers the way the deploy does: directly as root, otherwise
# through non-interactive sudo. A node without either simply cannot be repaired
# over this path, and says so instead of appearing to succeed.
priv() {{
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo -n "$@"
  else
    printf 'mac-hub-repair: no root and no non-interactive sudo\\n' >&2
    return 127
  fi
}}

service_unit() {{
  case "$1" in
{case_arms}
    *) return 1 ;;
  esac
}}

service_state() {{
  unit="$1"
  case "$SUPERVISOR" in
    supervisord) priv supervisorctl status "$unit" 2>&1 || true ;;
    systemd)
      if systemctl --user cat "$unit" >/dev/null 2>&1; then
        systemctl --user is-active "$unit" 2>&1 || true
      else
        priv systemctl is-active "$unit" 2>&1 || true
      fi
      ;;
    launchd) launchctl list "$unit" >/dev/null 2>&1 && echo loaded || echo not-loaded ;;
    *) echo "unsupported-supervisor" ;;
  esac
}}

do_restart() {{
  unit="$(service_unit "$1")" || deny "service '$1' is not in the repair allowlist ($SERVICE_NAMES)"
  audit allowed "$request"
  case "$SUPERVISOR" in
    supervisord) priv supervisorctl restart "$unit" ;;
    systemd)
      if systemctl --user cat "$unit" >/dev/null 2>&1; then
        systemctl --user restart "$unit"
      else
        priv systemctl restart "$unit"
      fi
      ;;
    launchd) launchctl kickstart -k "gui/$(id -u)/$unit" ;;
    *) deny "unsupported supervisor '$SUPERVISOR'" ;;
  esac
}}

do_services() {{
  for name in $SERVICE_NAMES; do
    unit="$(service_unit "$name")" || continue
    printf 'service %s unit=%s state=%s\\n' "$name" "$unit" "$(service_state "$unit" | tr '\\n' ' ')"
  done
}}

do_deploy_info() {{
  if [ -f "$MAC_HOME/deployed-source-revision" ]; then
    printf 'deployed_source_revision=%s\\n' "$(cat "$MAC_HOME/deployed-source-revision")"
  else
    printf 'deployed_source_revision=unknown\\n'
  fi
  if [ -f "$MAC_HOME/deploy-start-barrier" ]; then
    printf 'deploy_start_barrier=%s\\n' "$(cat "$MAC_HOME/deploy-start-barrier")"
  fi
}}

do_status() {{
  printf 'schema=%s\\n' "$SCHEMA"
  printf 'agent=%s\\n' "$AGENT_NAME"
  printf 'host=%s\\n' "$(uname -n 2>/dev/null || echo unknown)"
  printf 'supervisor=%s\\n' "$SUPERVISOR"
  do_deploy_info
  do_services
}}

do_logs() {{
  [ -d "$LOG_DIR" ] || {{ printf 'no log directory at %s\\n' "$LOG_DIR"; return 0; }}
  ls -1 "$LOG_DIR" 2>/dev/null || true
}}

do_tail() {{
  name="$1"
  lines="${{2:-$DEFAULT_TAIL_LINES}}"
  case "$lines" in
    ''|*[!0-9]*) deny "tail line count must be a positive integer" ;;
  esac
  [ "$lines" -ge 1 ] || deny "tail line count must be at least 1"
  [ "$lines" -le "$MAX_TAIL_LINES" ] || deny "tail line count exceeds $MAX_TAIL_LINES"
  [ -f "$LOG_DIR/$name" ] || deny "no such log file: $name"
  audit allowed "$request"
  tail -n "$lines" "$LOG_DIR/$name"
}}

[ -n "$request" ] || deny "interactive shells are not permitted; send one repair verb"

# Split with globbing off, then vet every token before dispatching on any of
# them. The alphabet has no '/', no quotes, and no shell metacharacters, so
# nothing below can be talked into traversing a path or starting a second
# command. Word-splitting an expansion never re-parses operators, so a ';' in
# the request is only ever a rejected character.
set -f
# shellcheck disable=SC2086
set -- $request
[ $# -ge 1 ] || deny "interactive shells are not permitted; send one repair verb"
for token in "$@"; do
  case "$token" in
    *[!A-Za-z0-9._:@-]*) deny "request token '$token' is outside the repair alphabet" ;;
    *..*) deny "request contains a path traversal" ;;
  esac
done
verb="$1"
shift

case "$verb" in
  status) [ $# -eq 0 ] || deny "status takes no arguments"; audit allowed "$request"; do_status ;;
  services) [ $# -eq 0 ] || deny "services takes no arguments"; audit allowed "$request"; do_services ;;
  deploy-info) [ $# -eq 0 ] || deny "deploy-info takes no arguments"; audit allowed "$request"; do_deploy_info ;;
  logs) [ $# -eq 0 ] || deny "logs takes no arguments"; audit allowed "$request"; do_logs ;;
  restart) [ $# -eq 1 ] || deny "restart takes exactly one service"; do_restart "$1" ;;
  tail)
    [ $# -ge 1 ] && [ $# -le 2 ] || deny "tail takes a log name and an optional line count"
    do_tail "$@"
    ;;
  *) deny "verb '$verb' is not in the repair allowlist" ;;
esac
""".format(
        schema=SCHEMA,
        supervisor=supervisor,
        agent=agent,
        service_names=service_names,
        case_arms=case_arms,
        verb_help=verb_help,
        max_tail=MAX_TAIL_LINES,
        default_tail=DEFAULT_TAIL_LINES,
        exit_denied=EXIT_DENIED,
    )


def install_repair_shim(path: Path, script: str) -> bool:
    """Write the shim at *path* mode 0700; return whether it changed."""

    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, stat.S_IRWXU)
    if path.is_file() and path.read_text(encoding="utf-8") == script:
        os.chmod(path, 0o700)
        return False
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(script)
    os.replace(str(temp), str(path))
    os.chmod(path, 0o700)
    return True


# ---------------------------------------------------------------------------
# Hub side
# ---------------------------------------------------------------------------

def repair_ssh_argv(
    config: Mapping[str, Any],
    agent: str,
    request: RepairRequest,
    *,
    fleet: Optional[str] = None,
    identity_file: Optional[str] = None,
    connect_timeout: int = 10,
) -> List[str]:
    """Build the hub's ``ssh`` argv for one repair request against *agent*.

    The route comes from the fleet registry the deploy installs on every node,
    so the hub inherits the fleet's ``ssh_jump`` bastion and host-key policy
    unchanged; only the identity is swapped to the hub's own repair key.  The
    remote command is still sent even though the entry forces its own: the
    worker reads it from ``SSH_ORIGINAL_COMMAND``.
    """

    from mac.fleet_ssh import FleetSshError, resolve_fleet_ssh, ssh_argv

    try:
        spec = resolve_fleet_ssh(config, fleet, agent)
    except FleetSshError as exc:
        raise HubRepairKeyError(str(exc)) from exc
    key = Path(identity_file).expanduser() if identity_file else hub_repair_key_path()
    if not key.is_file():
        raise HubRepairKeyError(
            "hub repair key %s is absent; deploy the hub to generate it" % key
        )
    routed = replace(spec, identity_file=str(key), identity_ref=None)
    return ssh_argv(routed, request.command, connect_timeout=connect_timeout)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _read_public_key(args: argparse.Namespace) -> str:
    if getattr(args, "public_key", None):
        return str(args.public_key)
    path = Path(str(args.public_key_file)).expanduser()
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HubRepairKeyError("cannot read public key %s: %s" % (path, exc)) from exc


def _cmd_emit_shim(args: argparse.Namespace) -> int:
    script = repair_shim_script(
        supervisor=args.supervisor,
        services=parse_service_map(args.service),
        agent=args.agent or "",
    )
    sys.stdout.write(script)
    return 0


def _cmd_authorized_keys_line(args: argparse.Namespace) -> int:
    line = authorized_keys_line(
        _read_public_key(args),
        forced_command=args.shim or str(hub_repair_shim_path()),
        from_patterns=args.allow_from,
    )
    sys.stdout.write(line + "\n")
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    """Install the shim and authorize the hub key, in that order.

    Order matters: authorizing a key whose forced command does not exist yet
    turns every hub connection into an obscure exec failure for as long as the
    window lasts.
    """

    shim_path = Path(args.shim or str(hub_repair_shim_path())).expanduser()
    script = repair_shim_script(
        supervisor=args.supervisor,
        services=parse_service_map(args.service),
        agent=args.agent or "",
    )
    shim_changed = install_repair_shim(shim_path, script)
    line = authorized_keys_line(
        _read_public_key(args),
        forced_command=str(shim_path),
        from_patterns=args.allow_from,
    )
    authorized = Path(args.authorized_keys).expanduser()
    key_changed = install_authorized_key(authorized, line)
    sys.stdout.write(
        json.dumps(
            {
                "schema": SCHEMA,
                "shim": str(shim_path),
                "shim_changed": shim_changed,
                "authorized_keys": str(authorized),
                "authorized_keys_changed": key_changed,
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0


def _cmd_check_command(args: argparse.Namespace) -> int:
    request = " ".join(args.request) if args.request else os.environ.get("SSH_ORIGINAL_COMMAND", "")
    parsed = parse_repair_request(request, services=args.service or None)
    sys.stdout.write(json.dumps(parsed.to_dict(), sort_keys=True) + "\n")
    return 0


def _cmd_ssh_argv(args: argparse.Namespace) -> int:
    from mac.fleet_ssh import FleetSshError, load_fleet_config

    parsed = parse_repair_request(" ".join(args.request))
    try:
        config = load_fleet_config(args.fleets_config)
    except FleetSshError as exc:
        raise HubRepairKeyError(str(exc)) from exc
    argv = repair_ssh_argv(
        config,
        args.agent,
        parsed,
        fleet=args.fleet,
        identity_file=args.identity_file,
    )
    sys.stdout.write(json.dumps(argv) + "\n")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mac.hub_repair_key",
        description="hub-owned, forced-command SSH access to a worker for repair",
    )
    sub = parser.add_subparsers(dest="command")
    sub.required = True

    def _add_shim_args(target: argparse.ArgumentParser) -> None:
        target.add_argument("--supervisor", required=True, choices=SUPERVISORS)
        target.add_argument(
            "--service",
            action="append",
            default=[],
            metavar="NAME=UNIT",
            help="allowlisted service (repeatable)",
        )
        target.add_argument("--agent", default="", help="agent name recorded in status output")

    def _add_key_args(target: argparse.ArgumentParser) -> None:
        source = target.add_mutually_exclusive_group(required=True)
        source.add_argument("--public-key", help="the hub's public key, verbatim")
        source.add_argument("--public-key-file", help="file holding the hub's public key")
        target.add_argument(
            "--allow-from",
            action="append",
            default=[],
            metavar="PATTERN",
            help="restrict the entry to these source patterns (repeatable)",
        )
        target.add_argument("--shim", help="forced-command path (default: ~/.mac/bin/mac-hub-repair)")

    emit = sub.add_parser("emit-shim", help="print the worker-side forced command")
    _add_shim_args(emit)
    emit.set_defaults(handler=_cmd_emit_shim)

    line = sub.add_parser("authorized-keys-line", help="print the restricted authorized_keys entry")
    _add_key_args(line)
    line.set_defaults(handler=_cmd_authorized_keys_line)

    install = sub.add_parser("install", help="install the shim and authorize the hub key")
    _add_shim_args(install)
    _add_key_args(install)
    install.add_argument(
        "--authorized-keys",
        default=str(Path.home() / ".ssh" / "authorized_keys"),
    )
    install.set_defaults(handler=_cmd_install)

    check = sub.add_parser("check-command", help="validate a repair request (defaults to $SSH_ORIGINAL_COMMAND)")
    check.add_argument("--service", action="append", default=[], help="allowlisted service name (repeatable)")
    check.add_argument("request", nargs="*")
    check.set_defaults(handler=_cmd_check_command)

    argv = sub.add_parser("ssh-argv", help="print the hub's ssh argv for one repair request")
    argv.add_argument("--agent", required=True)
    argv.add_argument("--fleet")
    argv.add_argument("--fleets-config")
    argv.add_argument("--identity-file")
    argv.add_argument("request", nargs="+")
    argv.set_defaults(handler=_cmd_ssh_argv)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Run the hub-repair-key command-line interface."""

    args = _build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except HubRepairKeyError as exc:
        print("mac hub repair key: %s" % exc, file=sys.stderr)
        return EXIT_DENIED if args.command == "check-command" else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
