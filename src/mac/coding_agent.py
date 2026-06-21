"""Prefer an installed, authenticated coding-agent CLI over a direct LLM run.

For executor *coding* work, a coding-agent CLI (Claude Code, Codex, Cursor)
authenticates against a subscription / seat — Claude Pro/Max via
``~/.claude.json``, Codex via ``~/.codex/auth.json``, Cursor via ``~/.cursor`` —
rather than a metered API token. Routing the work through one of those CLIs is
therefore materially cheaper than driving the LLM gateway directly. This module
decides, from the same environment the executor runs in, *which* coding agent
(if any) is available **and** authenticated, and how to invoke it
non-interactively. When none qualifies the executor falls back to the vendored
Hermes runtime -> LLM gateway path (its prior behavior).

Detection (priority order claude -> codex -> cursor; first qualifying wins):

* **claude**: ``claude`` on PATH *and* (``ANTHROPIC_API_KEY`` set *or*
  ``~/.claude.json`` carries a non-empty ``primary_key``).
* **codex**: ``codex`` on PATH *and* ``~/.codex/auth.json`` present and non-empty.
* **cursor**: ``cursor-agent`` (or ``cursor``) on PATH *and* ``~/.cursor`` exists.

The decision is *legible* the same way :mod:`mac.agent_provider` is: every
resolution yields a secret-free :meth:`CodingAgentChoice.observable` plus a
human-readable ``rationale`` so an operator (or the agent itself) can answer
"why did this task run on Claude / on the gateway?" rather than facing an
inexplicable routing outcome.

The module is intentionally dependency-free (stdlib only) and has no import-time
side effects, so it is unit-testable without a live agent or filesystem
fixtures (``resolve_coding_agent`` takes injectable ``env``/``home``/``which``).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple

__all__ = [
    "CodingAgentChoice",
    "resolve_coding_agent",
    "coding_agent_argv",
    "messaging_mcp_enabled",
    "supports_per_invocation_mcp",
    "mcp_config_document",
    "PREFERENCE_ENV",
    "FORCE_ENV",
    "MESSAGING_MCP_ENV",
    "AGENT_PRIORITY",
]

#: Master on/off for coding-agent preference. Default ON ("use preferentially").
#: Falsy -> always use the Hermes -> gateway fallback.
PREFERENCE_ENV = "MAC_PREFER_CODING_AGENT"

#: Pin or disable selection. ``claude``/``codex``/``cursor`` restrict
#: consideration to that one agent (still must be available + authed, else we
#: fall back); ``off``/``none``/``hermes``/``gateway`` disable preference.
FORCE_ENV = "MAC_CODING_AGENT"

#: Per-agent explicit command template (shlex-split). When set it is used
#: verbatim and the prompt is appended as the trailing positional argument
#: (mirrors ``MAC_ACP_BACKEND_CMD``), insulating the fleet from upstream CLI
#: flag drift without a code change.
COMMAND_ENV = {
    "claude": "MAC_CODING_AGENT_CLAUDE_CMD",
    "codex": "MAC_CODING_AGENT_CODEX_CMD",
    "cursor": "MAC_CODING_AGENT_CURSOR_CMD",
}

#: Register the vendored messaging MCP server with the coding agent where the
#: CLI supports a per-invocation config (currently Claude Code). Default ON to
#: match the Hermes messaging tool surface ("full parity where supported").
MESSAGING_MCP_ENV = "MAC_CODING_AGENT_MESSAGING_MCP"

#: Resolution priority. Earlier entries win when more than one qualifies.
AGENT_PRIORITY: Tuple[str, ...] = ("claude", "codex", "cursor")

_DISABLE_VALUES = {"off", "none", "hermes", "gateway", "0", "false", "no"}


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _which(name: str, which: Callable[[str], Optional[str]]) -> Optional[str]:
    try:
        return which(name)
    except Exception:  # noqa: BLE001 - PATH probing must never raise into selection
        return None


def _read_json(path: Path) -> Optional[dict]:
    try:
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def messaging_mcp_enabled(env: Mapping[str, str]) -> bool:
    """Whether to register the messaging MCP server (default ON)."""
    return _truthy(env.get(MESSAGING_MCP_ENV, "1"))


def supports_per_invocation_mcp(agent: str) -> bool:
    """True when the agent CLI accepts an MCP config per run (no global state).

    Only Claude Code exposes a clean per-invocation ``--mcp-config <file>``.
    Codex and Cursor read MCP servers from their own on-disk config
    (``~/.codex/config.toml``, ``~/.cursor/mcp.json``); mac does not rewrite a
    user's config file, so their messaging-MCP parity is set up out of band.
    Hub parity (the ``mac`` CLI + runtime context) is automatic for all three.
    """
    return agent == "claude"


@dataclass(frozen=True)
class CodingAgentChoice:
    """The coding-agent routing decision plus the reason for it.

    ``agent`` is ``""`` when no coding agent qualifies (the caller then uses the
    Hermes -> gateway fallback). No secret ever appears here — only the *name*
    of the env var / file that proved authentication (``auth_source``).
    """

    agent: str
    available: bool
    binary: str = ""
    auth_source: str = ""
    rationale: List[str] = field(default_factory=list)

    def observable(self) -> Dict[str, object]:
        """Secret-free view for logs / the executor telemetry + observability."""
        return {
            "schema": "mac.coding_agent.choice.v1",
            "agent": self.agent or None,
            "available": self.available,
            "binary": self.binary or None,
            "auth_source": self.auth_source or None,
            "rationale": list(self.rationale),
        }


def _detect_claude(
    env: Mapping[str, str], home: Path, which: Callable[[str], Optional[str]]
) -> Tuple[bool, str, str, str]:
    """Return (available, binary, auth_source, reason)."""
    binary = _which("claude", which)
    if not binary:
        return False, "", "", "claude: not on PATH"
    if str(env.get("ANTHROPIC_API_KEY") or "").strip():
        return True, binary, "ANTHROPIC_API_KEY", "claude: authed via ANTHROPIC_API_KEY"
    config = _read_json(home / ".claude.json")
    if config and str(config.get("primary_key") or "").strip():
        return True, binary, "~/.claude.json:primary_key", "claude: authed via ~/.claude.json primary_key"
    return False, binary, "", "claude: on PATH but no ANTHROPIC_API_KEY and no ~/.claude.json primary_key"


def _detect_codex(
    env: Mapping[str, str], home: Path, which: Callable[[str], Optional[str]]
) -> Tuple[bool, str, str, str]:
    binary = _which("codex", which)
    if not binary:
        return False, "", "", "codex: not on PATH"
    auth = home / ".codex" / "auth.json"
    try:
        present = auth.is_file() and auth.stat().st_size > 0
    except OSError:
        present = False
    if present:
        return True, binary, "~/.codex/auth.json", "codex: authed via ~/.codex/auth.json"
    return False, binary, "", "codex: on PATH but ~/.codex/auth.json missing or empty"


def _detect_cursor(
    env: Mapping[str, str], home: Path, which: Callable[[str], Optional[str]]
) -> Tuple[bool, str, str, str]:
    binary = _which("cursor-agent", which) or _which("cursor", which)
    if not binary:
        return False, "", "", "cursor: cursor-agent/cursor not on PATH"
    if (home / ".cursor").exists():
        return True, binary, "~/.cursor", "cursor: authed via ~/.cursor"
    return False, binary, "", "cursor: on PATH but ~/.cursor missing"


_DETECTORS = {
    "claude": _detect_claude,
    "codex": _detect_codex,
    "cursor": _detect_cursor,
}


def resolve_coding_agent(
    env: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
) -> CodingAgentChoice:
    """Resolve which coding-agent CLI to prefer, or none (fall back to gateway).

    ``env``/``home``/``which`` are injectable for tests; they default to the
    live process environment, ``Path.home()`` and :func:`shutil.which`.
    """
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    which = shutil.which if which is None else which

    rationale: List[str] = []

    if not _truthy(env.get(PREFERENCE_ENV, "1")):
        rationale.append("%s is disabled; using Hermes -> gateway" % PREFERENCE_ENV)
        return CodingAgentChoice(agent="", available=False, rationale=rationale)

    forced = str(env.get(FORCE_ENV) or "").strip().lower()
    if forced in _DISABLE_VALUES:
        rationale.append("%s=%s disables coding-agent preference" % (FORCE_ENV, forced))
        return CodingAgentChoice(agent="", available=False, rationale=rationale)

    if forced and forced not in _DETECTORS:
        rationale.append("%s=%s is not a known agent; ignoring" % (FORCE_ENV, forced))
        forced = ""

    candidates = (forced,) if forced in _DETECTORS else AGENT_PRIORITY
    if forced in _DETECTORS:
        rationale.append("%s pins selection to %s" % (FORCE_ENV, forced))

    for agent in candidates:
        available, binary, auth_source, reason = _DETECTORS[agent](env, home, which)
        rationale.append(reason)
        if available:
            return CodingAgentChoice(
                agent=agent,
                available=True,
                binary=binary,
                auth_source=auth_source,
                rationale=rationale,
            )

    rationale.append("no coding agent available/authed; using Hermes -> gateway")
    return CodingAgentChoice(agent="", available=False, rationale=rationale)


def mcp_config_document(server_command: List[str], name: str = "hermes") -> Dict[str, object]:
    """An MCP client config registering a single stdio server ``name``.

    ``server_command`` is the argv that launches the (messaging) MCP server,
    e.g. ``[<python>, "-m", "hermes_cli.main", "mcp", "serve"]``.
    """
    command = server_command[0] if server_command else ""
    args = list(server_command[1:]) if len(server_command) > 1 else []
    return {"mcpServers": {name: {"command": command, "args": args}}}


def _default_argv(agent: str, binary: str, prompt: str) -> List[str]:
    """Non-interactive, approvals-bypassed invocation per agent.

    Approval bypass is intentional and *coupled to the executor's existing
    OpenShell/--yolo invariant*: these argvs are routed through the same
    sandbox-or-fail-closed gate as the Hermes ``--yolo`` invocation, so the
    coding-agent CLI is only run unconfined under the same escape hatch.
    """
    if agent == "claude":
        # `-p` = headless print mode; skip Claude's own permission prompts
        # (OpenShell is the real guardrail). Plain-text output for the audit log.
        return [binary, "--dangerously-skip-permissions", "--output-format", "text", "-p", prompt]
    if agent == "codex":
        # `exec` = non-interactive; bypass Codex's own approval + sandbox since
        # OpenShell provides confinement.
        return [binary, "exec", "--dangerously-bypass-approvals-and-sandbox", prompt]
    if agent == "cursor":
        return [binary, "-p", "--force", prompt]
    raise ValueError("unknown coding agent: %r" % agent)


def coding_agent_argv(
    choice: CodingAgentChoice,
    prompt: str,
    *,
    env: Optional[Mapping[str, str]] = None,
    mcp_config_path: Optional[str] = None,
) -> List[str]:
    """Build the argv to run ``prompt`` through the chosen coding-agent CLI.

    A ``MAC_CODING_AGENT_<AGENT>_CMD`` override (shlex-split) is honored verbatim
    with the prompt appended as the trailing positional argument. ``mcp_config_path``
    (when the agent supports per-invocation MCP) injects the messaging server.
    """
    if not choice.available or not choice.agent:
        raise ValueError("coding_agent_argv called without an available choice")
    env = os.environ if env is None else env

    override = str(env.get(COMMAND_ENV[choice.agent]) or "").strip()
    if override:
        return [*shlex.split(override), prompt]

    argv = _default_argv(choice.agent, choice.binary, prompt)
    if mcp_config_path and supports_per_invocation_mcp(choice.agent):
        # Insert right after the binary so it precedes the `-p <prompt>` tail.
        argv = [argv[0], "--mcp-config", mcp_config_path, *argv[1:]]
    return argv


def _describe(env: Optional[Mapping[str, str]] = None) -> str:
    choice = resolve_coding_agent(env=env)
    return json.dumps(choice.observable(), indent=2, sort_keys=True)


if __name__ == "__main__":  # pragma: no cover - operator debugging aid
    # `python -m mac.coding_agent` prints the (secret-free) routing decision for
    # the current environment, so a fleet operator can see why a node routes to
    # a coding agent or to the gateway.
    print(_describe())
