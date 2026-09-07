"""Ambient CLI session auto-join to the AgentBus (ADR 0032, auto-trigger addendum).

ADR 0032 designed the *delivery* mechanism -- a per-harness hook adapter that
non-blocking-drains an agent's AgentBus inbox and returns the messages as
additional context at a turn boundary -- but assumed a human operator runs
``mac admin plugin install`` once, by hand, to wire the hook config in. That
manual step is a gap: a coding CLI session that has ``mac`` on ``$PATH`` and
never runs the installer is deaf to the bus forever, silently, with no error
to notice.

This module removes that manual step. The first time ``mac`` is invoked from
*inside* a detected coding-CLI harness (Claude Code today; see
``_HARNESS_ENV_SIGNATURES``), it self-registers a durable, host+user-scoped
agent identity and installs the harness's hook config -- both idempotent, so
every subsequent invocation is a cheap cache hit, not a repeated write.

Two deliberate scope cuts against the ADR's full design, both because this is
new code that has to be trustworthy before it is widened:

* Only the **inject** job (drain inbox -> additionalContext) ships here. The
  **record** job (turn/tool events -> ``mac.cli_session.turn.v1``) is not
  implemented; see the follow-up task filed alongside this change.
* Only Claude Code's harness-detection signal is verified against a live
  session (``CLAUDECODE=1``, confirmed 2026-09-07 from inside one). Codex,
  Cursor, and OpenCode are real harnesses ADR 0032 names, but guessing their
  env-var signal wrong would silently install into the wrong config file, so
  they are left for a follow-up that can verify each one live.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from mac import mac_paths
from mac.models import MACError, NotFoundError

logger = logging.getLogger("mac.cli_session")

#: Env-var signatures that reveal ``mac`` is running INSIDE a known coding
#: harness's own process tree, as opposed to a plain terminal. Add an entry
#: only once verified from inside a live session of that harness.
_HARNESS_ENV_SIGNATURES: Dict[str, str] = {
    "claude": "CLAUDECODE",
}

#: How long a successful registration is trusted before the next invocation
#: re-verifies with the hub. Idempotent on the hub side either way; this only
#: bounds how often an ordinary command pays the network round trip.
DEFAULT_CACHE_TTL_SECONDS = 6 * 3600

_LEGACY_HOOK_COMMAND = "mac admin cli-session hook"


def detect_live_harness(environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Which known coding-CLI harness this process is running inside, if any."""
    env = environ if environ is not None else os.environ
    for harness, var in _HARNESS_ENV_SIGNATURES.items():
        if str(env.get(var) or "").strip():
            return harness
    return None


def session_identity(hostname: str, user: str) -> Dict[str, str]:
    """A stable (machine_id, agent_id, name) for this (host, os-user) pair.

    Deterministic and content-free by design: re-deriving it never requires
    reading back what was registered last time, so a cold cache is not a
    different identity, it is the same identity computed again.
    """
    host_digest = hashlib.sha256(hostname.encode("utf-8")).hexdigest()[:16]
    pair_digest = hashlib.sha256(("%s:%s" % (hostname, user)).encode("utf-8")).hexdigest()[:16]
    return {
        "machine_id": "machine_cli_%s" % host_digest,
        "agent_id": "agent_cli_%s" % pair_digest,
        "name": "cli-session-%s@%s" % (user, hostname),
    }


def _cache_path(agent_id: str) -> Path:
    return mac_paths.mac_home() / "cli-session" / ("%s.json" % agent_id)


def _cached_registration(agent_id: str, ttl_seconds: int) -> Optional[Dict[str, Any]]:
    path = _cache_path(agent_id)
    try:
        stat = path.stat()
    except OSError:
        return None
    if (time.time() - stat.st_mtime) > ttl_seconds:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(agent_id: str, record: Dict[str, Any]) -> None:
    path = _cache_path(agent_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    except OSError:
        # Losing the cache costs one extra network round trip next time, not
        # correctness -- registration itself is idempotent.
        pass


def _resolve_owner_human_id(plane: Any, user: str) -> Optional[str]:
    """Best-effort: bind the agent to a registered Human if one matches.

    Not required -- ``register_agent`` accepts no owner and falls back to
    shared visibility -- but a private, owned identity is the better default
    when the caller's own username happens to already be a registered
    principal (e.g. a fleet operator's laptop).
    """
    for candidate in (user, os.environ.get("MAC_HUMAN_USERNAME") or ""):
        candidate = str(candidate or "").strip()
        if not candidate:
            continue
        try:
            return plane.get_human_by_username(candidate).id
        except NotFoundError:
            continue
        except MACError:
            continue
    return None


def ensure_registered(
    plane: Any,
    *,
    harness: str,
    hostname: Optional[str] = None,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    """Idempotently register this (host, user) as a live AgentBus identity.

    Safe to call on every invocation: ``register_machine``/``register_agent``
    are both ``INSERT ... ON CONFLICT DO UPDATE`` upserts on the hub, so a
    repeat call refreshes ``last_seen_at`` rather than creating a duplicate
    or erroring. Callers that want to avoid the network round trip on every
    command should check :func:`ensure_registered_cached` instead.
    """
    hostname = hostname or socket.gethostname()
    user = user or os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    identity = session_identity(hostname, user)
    plane.register_machine(
        hostname=hostname,
        machine_id=identity["machine_id"],
        labels={"kind": "cli-session-host"},
    )
    owner_human_id = _resolve_owner_human_id(plane, user)
    plane.register_agent(
        machine_id=identity["machine_id"],
        name=identity["name"],
        agent_id=identity["agent_id"],
        capabilities=["cli_session", "harness:%s" % harness],
        owner_human_id=owner_human_id,
        actor="cli-session-autojoin",
    )
    return identity


def ensure_registered_cached(
    plane: Any,
    *,
    harness: str,
    hostname: Optional[str] = None,
    user: Optional[str] = None,
    ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
) -> Dict[str, Any]:
    """:func:`ensure_registered`, but skip the network round trip on a warm cache."""
    hostname = hostname or socket.gethostname()
    user = user or os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    identity = session_identity(hostname, user)
    cached = _cached_registration(identity["agent_id"], ttl_seconds)
    if cached is not None:
        return cached
    identity = ensure_registered(plane, harness=harness, hostname=hostname, user=user)
    _write_cache(identity["agent_id"], identity)
    return identity


def auto_join(
    plane: Any,
    *,
    environ: Optional[Mapping[str, str]] = None,
    ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
) -> Optional[Dict[str, Any]]:
    """The single entry point the CLI's main() calls on every invocation.

    Returns the identity dict on success, ``None`` on anything else (no
    harness detected, no hub reachable, registration failed). Never raises:
    a session that cannot join the bus must still run the command it was
    actually asked to run (ADR 0032 §5, "failure must not take down the
    session").
    """
    harness = detect_live_harness(environ)
    if harness is None:
        return None
    try:
        identity = ensure_registered_cached(plane, harness=harness, ttl_seconds=ttl_seconds)
        # Source-tree tests and one-off PYTHONPATH invocations must not publish
        # a hook that the persistent CLI cannot execute on the next turn.
        if _hook_command_available():
            install_hook_config(harness)
        return identity
    except Exception:  # noqa: BLE001 - ambient best-effort, never fatal
        logger.debug("cli-session auto-join failed", exc_info=True)
        return None


# -- hook config installation -----------------------------------------------


def _claude_settings_path(user_home: Optional[Path] = None) -> Path:
    home = user_home or Path.home()
    return home / ".claude" / "settings.json"


def _hook_command(event: str = "UserPromptSubmit") -> str:
    """Shell adapter written into Claude settings.

    The fallback is deliberately valid for every Claude hook event. A missing,
    older, or temporarily broken ``mac`` executable must make the session deaf
    for one turn, never reject the user's prompt.
    """
    return (
        "mac admin cli-session hook --event %s 2>/dev/null || "
        "printf '%s\\n' '{\"continue\":true}'" % (event, "%s")
    )


def _hook_command_available(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether the persistent ``mac`` on PATH implements the hook command.

    Drop PYTHONPATH before probing. Otherwise a test running unreleased source
    can make an older installed entry point appear to support the new parser,
    which is exactly how the global Claude configuration escaped prematurely.
    """
    env = dict(environ if environ is not None else os.environ)
    env.pop("PYTHONPATH", None)
    executable = shutil.which("mac", path=env.get("PATH"))
    if not executable:
        return False
    try:
        result = subprocess.run(
            [executable, "admin", "cli-session", "hook", "--help"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _merge_claude_hooks(path: Path) -> bool:
    """Idempotently add mac's inject hook to Claude Code's settings.json.

    Merges alongside whatever hooks already exist for the same events --
    never replaces the array, never drops an entry that is not ours. Returns
    True iff the file was written (a real change happened).
    """
    document: Dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            loaded = {}
        if isinstance(loaded, dict):
            document = loaded
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        document["hooks"] = hooks
    changed = False
    for event in ("SessionStart", "UserPromptSubmit"):
        command = _hook_command(event)
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            entries = []
            hooks[event] = entries
        already_present = False
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                continue
            for hook in entry["hooks"]:
                if not isinstance(hook, dict):
                    continue
                configured = hook.get("command")
                if configured == command:
                    already_present = True
                elif configured == _LEGACY_HOOK_COMMAND:
                    hook["command"] = command
                    already_present = True
                    changed = True
        if already_present:
            continue
        entries.append({"hooks": [{"type": "command", "command": command}]})
        changed = True
    if not changed:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return True


def install_hook_config(harness: str, *, user_home: Optional[Path] = None) -> bool:
    """Wire ``harness``'s own hook config to point at ``mac admin cli-session hook``.

    Only Claude Code is implemented. Other harnesses return False (no-op)
    until their adapter is verified live -- see the module docstring.
    """
    if harness == "claude":
        return _merge_claude_hooks(_claude_settings_path(user_home))
    return False


# -- the hook program itself -------------------------------------------------


def render_claude_hook_output(
    messages: List[Dict[str, Any]], *, event: str = "UserPromptSubmit"
) -> Dict[str, Any]:
    """Shape drained AgentBus messages as Claude Code's hook output contract.

    Empty inbox -> empty additionalContext, not an error and not an absent
    key: ADR 0032 §1 requires an empty inbox to still produce a valid empty
    hook payload, so a session with nothing waiting sees a normal turn.
    """
    if not messages:
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": ""}}
    lines = ["You have %d pending message(s) from the MAC fleet AgentBus:" % len(messages)]
    for entry in messages:
        sender = str(entry.get("sender_agent_id") or "unknown")
        payload = entry.get("payload")
        text = payload if isinstance(payload, str) else json.dumps(payload)
        lines.append("- from %s: %s" % (sender, text))
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": "\n".join(lines),
        }
    }


def run_hook(
    plane: Any,
    *,
    harness: str = "claude",
    event: str = "UserPromptSubmit",
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Non-blocking drain + render, for the harness's hook adapter to print.

    Never raises: a hook that cannot reach the hub must record and inject
    nothing for that turn without blocking the user's prompt (ADR 0032 §5).
    """
    try:
        hostname = socket.gethostname()
        user = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
        identity = session_identity(hostname, user)
        result = plane.drain_agentbus_inbox(identity["agent_id"])
        messages = list(result.get("messages") or [])
    except Exception:  # noqa: BLE001 - a deaf turn beats a blocked one
        logger.debug("cli-session hook drain failed", exc_info=True)
        messages = []
    if harness == "claude":
        return render_claude_hook_output(messages, event=event)
    return {}
