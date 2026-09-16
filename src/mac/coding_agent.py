"""Prefer an installed, authenticated coding-agent CLI over a direct LLM run.

For executor *coding* work, a coding-agent CLI (Claude Code, Codex, Cursor)
authenticates against a subscription / seat — Claude Pro/Max via
``~/.claude.json``, Codex via ``~/.codex/auth.json``, Cursor via ``~/.cursor`` —
rather than a metered API token. Routing the work through one of those CLIs is
therefore materially cheaper than driving the LLM gateway directly. This module
decides, from the same environment the executor runs in, *which* coding agent
(if any) is available **and** authenticated, and how to invoke it
non-interactively. When none qualifies the executor fails closed so work cannot
silently move to an unverified or retired runtime.

Detection uses priority order claude -> codex -> cursor.  The first qualifying
route wins unless a caller supplies an end-to-end verifier; verified resolution
falls through configured routes until one actually works.

* **claude**: ``claude`` on PATH *and* (``ANTHROPIC_API_KEY`` set *or*
  ``~/.claude.json`` carries a non-empty ``primary_key``).
* **codex**: ``codex`` on PATH *and* ``~/.codex/auth.json`` present and non-empty.
* **cursor**: ``cursor-agent`` (or ``cursor``) on PATH *and*
  ``CURSOR_AUTH_TOKEN``, ``CURSOR_API_KEY``, or ``~/.cursor`` exists.

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

import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "CodingAgentChoice",
    "resolve_coding_agent",
    "coding_agent_argv",
    "supports_per_invocation_mcp",
    "mcp_config_document",
    "PREFERENCE_ENV",
    "FORCE_ENV",
    "AGENT_PRIORITY",
]

#: Master on/off for coding-agent preference. Default ON. Falsy means no coding
#: agent is eligible and the executor fails closed.
PREFERENCE_ENV = "MAC_PREFER_CODING_AGENT"

#: Pin or disable selection. ``opencode``/``pi``/``claude``/``codex``/``cursor`` restrict
#: consideration to that one agent (still must be available + authed, else the
#: executor fails closed); legacy disable values remain accepted during rollout.
FORCE_ENV = "MAC_CODING_AGENT"

#: Per-agent explicit command template (shlex-split). When set it is used
#: verbatim and the prompt is appended as the trailing positional argument
#: (mirrors ``MAC_ACP_BACKEND_CMD``), insulating the fleet from upstream CLI
#: flag drift without a code change.
COMMAND_ENV = {
    "claude": "MAC_CODING_AGENT_CLAUDE_CMD",
    "codex": "MAC_CODING_AGENT_CODEX_CMD",
    "cursor": "MAC_CODING_AGENT_CURSOR_CMD",
    "opencode": "MAC_CODING_AGENT_OPENCODE_CMD",
    "pi": "MAC_CODING_AGENT_PI_CMD",
}

#: Resolution priority when the owner has published NO route ladder. ADR 0029
#: makes the fleet's search path an owner-authored ``mac.coding_route_ladder.v1``
#: document (see :mod:`mac.route_ladder`); when one is configured its order wins
#: here, and this tuple is the fallback for an unconfigured fleet. It is still
#: the provisioning list — :mod:`mac.sandbox_bom` installs every CLI named here
#: regardless of rank, because a route the ladder may promote tomorrow has to
#: already exist in the image.
#:
#: Earlier entries win when more than one qualifies.
#:
#: opencode is FIRST, and the reason is credential durability rather than model
#: quality. It authenticates from a portable on-disk API credential
#: (~/.local/share/opencode/auth.json) that can simply be copied to a worker
#: and does not expire.
#:
#: The others cannot be provisioned that way today. Anthropic does not offer an
#: API-key passthrough that would let a logged-in workstation hand its
#: credential to a worker, and the OAuth sessions claude and codex do use TIME
#: OUT -- so a remote node drifts back to unauthenticated with no local event
#: to notice. Logging in a headless worker is an unsolved problem on this
#: fleet, not a task anyone forgot to do.
#:
#: What that costs when it is not first: on 2026-08-19 every node reported all
#: three original CLIs unavailable AT ONCE -- claude with no credential, codex
#: denied by sandbox policy, cursor returning resource_exhausted -- and the
#: fleet completed exactly one task in twenty-four hours. The route that can be
#: provisioned belongs ahead of the routes that cannot.
#: pi sits second on the same reasoning that puts opencode first -- it
#: authenticates from a portable credential (an env key, or ~/.pi/agent/auth.json)
#: rather than an expiring OAuth session. It is behind opencode only because
#: opencode is proven end to end on this fleet and pi is not yet configured
#: here; that is an evidence ordering, not a preference, and it is meant to
#: change when pi has run real work.
AGENT_PRIORITY: Tuple[str, ...] = ("opencode", "pi", "claude", "codex", "cursor")

#: Sentinel the coding agent must echo back for the in-sandbox preflight to
#: pass. A correct echo proves, end-to-end *inside the sandbox*, that the binary
#: exists, the credentials resolve, and egress to the provider is permitted —
#: i.e. that routing a real task to this agent will actually work there.
PREFLIGHT_SENTINEL = "MAC_CODING_AGENT_SANDBOX_OK"
PREFLIGHT_PROMPT = "Respond with exactly this text and nothing else: " + PREFLIGHT_SENTINEL

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

    ``agent`` is ``""`` when no coding agent qualifies (the caller fails closed).
    No secret ever appears here — only the *name*
    of the env var / file that proved authentication (``auth_source``).
    """

    agent: str
    available: bool
    binary: str = ""
    auth_source: str = ""
    provider: str = ""
    protocol: str = ""
    auth_kind: str = ""
    endpoint: str = ""
    model: str = ""
    rationale: List[str] = field(default_factory=list)

    def route_fingerprint(self) -> str:
        """Stable, secret-free identity of the route that was actually checked."""
        payload = {
            "agent": self.agent,
            "binary": self.binary,
            "provider": self.provider,
            "protocol": self.protocol,
            "auth_kind": self.auth_kind,
            "auth_source": self.auth_source,
            "endpoint": self.endpoint,
            "model": self.model,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    def observable(self) -> Dict[str, object]:
        """Secret-free view for logs / the executor telemetry + observability."""
        return {
            "schema": "mac.coding_agent.choice.v2",
            "agent": self.agent or None,
            "available": self.available,
            "binary": self.binary or None,
            "auth_source": self.auth_source or None,
            "provider": self.provider or None,
            "protocol": self.protocol or None,
            "auth_kind": self.auth_kind or None,
            "endpoint": self.endpoint or None,
            "model": self.model or None,
            "route_fingerprint": self.route_fingerprint() if self.agent else None,
            "rationale": list(self.rationale),
        }


def _safe_endpoint(value: object, default: str) -> str:
    """Return a secret-free endpoint suitable for telemetry and fingerprints."""
    text = str(value or default).strip() or default
    try:
        parsed = urlsplit(text)
    except ValueError:
        return default
    if not parsed.scheme or not parsed.netloc:
        return default
    host = parsed.hostname or ""
    if not host:
        return default
    netloc = "[%s]" % host if ":" in host else host
    try:
        port = parsed.port
    except ValueError:
        return default
    if port is not None:
        netloc += ":%d" % port
    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))


def _route_fields(
    agent: str,
    env: Mapping[str, str],
    auth_source: str,
) -> Dict[str, str]:
    """Describe provider/protocol/auth separately from credential presence."""
    if agent == "codex":
        endpoint = _safe_endpoint(
            env.get("MAC_CODEX_BASE_URL") or env.get("OPENAI_BASE_URL"),
            "https://api.openai.com/v1",
        )
        explicit_provider = str(env.get("MAC_CODEX_PROVIDER") or "").strip()
        host = (urlsplit(endpoint).hostname or "").lower()
        provider = explicit_provider or ("openai" if host.endswith("openai.com") else "mac-router")
        auth_kind = "oauth_file" if auth_source == "~/.codex/auth.json" else "bearer_env"
        configured_model = str(
            env.get("MAC_TASK_MODEL") or env.get("MAC_CODEX_MODEL") or ""
        ).strip()
        return {
            "provider": provider,
            "protocol": str(env.get("MAC_CODEX_WIRE_API") or "responses").strip().lower(),
            "auth_kind": auth_kind,
            "endpoint": endpoint,
            # Codex has its own product default (currently gpt-5.5).  Letting it
            # leak into the MAC router bypasses the router's measured wildcard
            # ladder and can select a model the configured provider cannot
            # serve.  An unpinned routed invocation therefore requests MAC's
            # canonical wildcard explicitly; a user/task pin still wins.
            "model": configured_model or ("*" if provider == "mac-router" else ""),
        }
    if agent == "claude":
        provider = "anthropic"
        auth_kind = "api_key"
        if str(env.get("CLAUDE_CODE_USE_BEDROCK") or "").strip():
            provider, auth_kind = "amazon-bedrock", "cloud_identity"
        elif str(env.get("CLAUDE_CODE_USE_VERTEX") or "").strip():
            provider, auth_kind = "google-vertex", "cloud_identity"
        elif str(env.get("CLAUDE_CODE_USE_FOUNDRY") or "").strip():
            provider, auth_kind = "microsoft-foundry", "cloud_identity"
        elif auth_source == "ANTHROPIC_AUTH_TOKEN":
            provider, auth_kind = "anthropic-gateway", "bearer_env"
        elif auth_source == "CLAUDE_CODE_OAUTH_TOKEN" or auth_source.startswith("~/.claude"):
            auth_kind = "oauth"
        elif auth_source == "apiKeyHelper":
            auth_kind = "credential_helper"
        return {
            "provider": provider,
            "protocol": "anthropic-messages",
            "auth_kind": auth_kind,
            "endpoint": _safe_endpoint(env.get("ANTHROPIC_BASE_URL"), "https://api.anthropic.com"),
            "model": str(env.get("MAC_TASK_MODEL") or env.get("ANTHROPIC_MODEL") or "").strip(),
        }
    if agent == "opencode":
        # opencode is a multi-provider client: the provider is chosen per model
        # in ~/.config/opencode/opencode.json, so there is no single endpoint to
        # report. Say so rather than inventing one -- an endpoint field that
        # names the wrong host is worse than an empty one, because the route
        # fingerprint is what an operator reads to see where a task actually
        # went.
        return {
            "provider": "opencode",
            "protocol": "opencode-run",
            "auth_kind": ("bearer_env" if auth_source == "OPENCODE_API_KEY" else "api_key_file"),
            "endpoint": "",
            "model": str(env.get("MAC_TASK_MODEL") or env.get("MAC_OPENCODE_MODEL") or "").strip(),
        }
    if agent == "pi":
        # Multi-provider, like opencode: pi resolves a provider per model from
        # its own catalog, so no single endpoint is implied by the CLI. Report
        # none rather than inventing one -- the route fingerprint is what an
        # operator reads to see where a task actually went.
        return {
            "provider": "pi",
            "protocol": "pi-print",
            "auth_kind": ("api_key_file" if auth_source.startswith("~/.pi") else "bearer_env"),
            "endpoint": "",
            "model": str(env.get("MAC_TASK_MODEL") or env.get("MAC_PI_MODEL") or "").strip(),
        }
    if agent == "cursor":
        return {
            "provider": "cursor",
            "protocol": "cursor-agent",
            "auth_kind": (
                "api_key"
                if auth_source == "CURSOR_API_KEY"
                else "bearer_env"
                if auth_source == "CURSOR_AUTH_TOKEN"
                else "browser_session"
            ),
            "endpoint": _safe_endpoint(
                env.get("MAC_CURSOR_ENDPOINT") or env.get("CURSOR_AGENT_ENDPOINT"),
                "https://api.cursor.com",
            ),
            "model": str(env.get("MAC_TASK_MODEL") or env.get("MAC_CURSOR_MODEL") or "").strip(),
        }
    # No silent fallback. This used to `return` cursor's fields for anything
    # unrecognized, so adding opencode produced a route that reported
    # provider=cursor, endpoint=https://api.cursor.com, protocol=cursor-agent
    # while running the opencode binary -- available and correct-looking, and
    # describing a provider it never contacts. A new agent must declare its own
    # identity or fail loudly here.
    raise ValueError("no route fields defined for coding agent: %r" % agent)


def _choice(
    agent: str,
    available: bool,
    binary: str,
    auth_source: str,
    rationale: List[str],
    env: Mapping[str, str],
) -> CodingAgentChoice:
    route = _route_fields(agent, env, auth_source) if agent else {}
    return CodingAgentChoice(
        agent=agent,
        available=available,
        binary=binary,
        auth_source=auth_source,
        rationale=rationale,
        **route,
    )


def _detect_claude(
    env: Mapping[str, str], home: Path, which: Callable[[str], Optional[str]]
) -> Tuple[bool, str, str, str]:
    """Return (available, binary, auth_source, reason)."""
    binary = _which("claude", which)
    if not binary:
        return False, "", "", "claude: not on PATH"
    for cloud_flag, source in (
        ("CLAUDE_CODE_USE_BEDROCK", "aws_default_chain"),
        ("CLAUDE_CODE_USE_VERTEX", "google_default_chain"),
        ("CLAUDE_CODE_USE_FOUNDRY", "azure_default_chain"),
    ):
        if _truthy(env.get(cloud_flag)):
            return True, binary, source, "claude: configured via %s" % cloud_flag
    if str(env.get("ANTHROPIC_AUTH_TOKEN") or "").strip():
        return True, binary, "ANTHROPIC_AUTH_TOKEN", "claude: configured via ANTHROPIC_AUTH_TOKEN"
    if str(env.get("ANTHROPIC_API_KEY") or "").strip():
        return True, binary, "ANTHROPIC_API_KEY", "claude: configured via ANTHROPIC_API_KEY"
    settings = _read_json(home / ".claude" / "settings.json") or {}
    if str(settings.get("apiKeyHelper") or "").strip():
        return True, binary, "apiKeyHelper", "claude: configured via apiKeyHelper"
    if str(env.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip():
        return (
            True,
            binary,
            "CLAUDE_CODE_OAUTH_TOKEN",
            "claude: configured via CLAUDE_CODE_OAUTH_TOKEN",
        )
    credentials = home / ".claude" / ".credentials.json"
    try:
        if credentials.is_file() and credentials.stat().st_size > 0:
            return (
                True,
                binary,
                "~/.claude/.credentials.json",
                "claude: configured via ~/.claude/.credentials.json",
            )
    except OSError:
        pass
    config = _read_json(home / ".claude.json")
    if config and str(config.get("primary_key") or "").strip():
        return (
            True,
            binary,
            "~/.claude.json:primary_key",
            "claude: configured via ~/.claude.json primary_key",
        )
    return False, binary, "", "claude: on PATH but no supported credential configuration"


def _detect_codex(
    env: Mapping[str, str], home: Path, which: Callable[[str], Optional[str]]
) -> Tuple[bool, str, str, str]:
    binary = _which("codex", which)
    if not binary:
        return False, "", "", "codex: not on PATH"
    # Prefer non-rotating environment auth when available.  The OpenShell
    # executor transfers the named credential + endpoint through its
    # private mode-0600 environment file, so this route can be verified inside
    # an ephemeral sandbox without copying Codex's rotating OAuth refresh-token
    # store and potentially leaving the host copy stale.
    for key in ("MAC_CODEX_TOKEN", "CODEX_API_KEY", "OPENAI_API_KEY"):
        if str(env.get(key) or "").strip():
            return True, binary, key, "codex: configured via %s" % key
    auth = home / ".codex" / "auth.json"
    try:
        present = auth.is_file() and auth.stat().st_size > 0
    except OSError:
        present = False
    if present:
        return True, binary, "~/.codex/auth.json", "codex: configured via ~/.codex/auth.json"
    return (
        False,
        binary,
        "",
        "codex: on PATH but no supported credential configuration",
    )


def _detect_cursor(
    env: Mapping[str, str], home: Path, which: Callable[[str], Optional[str]]
) -> Tuple[bool, str, str, str]:
    binary = _which("cursor-agent", which) or _which("cursor", which)
    if not binary:
        return False, "", "", "cursor: cursor-agent/cursor not on PATH"
    # cursor-agent itself gives CURSOR_AUTH_TOKEN precedence over
    # CURSOR_API_KEY. Mirror that order so the route fingerprint describes the
    # credential the executable will really consume.
    if str(env.get("CURSOR_AUTH_TOKEN") or "").strip():
        return (
            True,
            binary,
            "CURSOR_AUTH_TOKEN",
            "cursor: configured via CURSOR_AUTH_TOKEN",
        )
    if str(env.get("CURSOR_API_KEY") or "").strip():
        return True, binary, "CURSOR_API_KEY", "cursor: configured via CURSOR_API_KEY"
    if (home / ".cursor").exists():
        return True, binary, "~/.cursor", "cursor: configured via ~/.cursor"
    return False, binary, "", "cursor: on PATH but no supported credential configuration"


def _detect_opencode(
    env: Mapping[str, str], home: Path, which: Callable[[str], Optional[str]]
) -> Tuple[bool, str, str, str]:
    """Return (available, binary, auth_source, reason).

    opencode keeps provider credentials in ``~/.local/share/opencode/auth.json``
    -- NOT in ``~/.config/opencode``, which holds only model and provider
    settings. Both must be present on a worker for a task run to succeed, and
    the distinction is easy to miss when copying a working setup between hosts.
    """
    binary = _which("opencode", which)
    if not binary:
        return False, "", "", "opencode: not on PATH"
    if str(env.get("OPENCODE_API_KEY") or "").strip():
        return True, binary, "OPENCODE_API_KEY", "opencode: configured via OPENCODE_API_KEY"
    auth = home / ".local" / "share" / "opencode" / "auth.json"
    try:
        if auth.is_file() and auth.stat().st_size > 0:
            return (
                True,
                binary,
                "~/.local/share/opencode/auth.json",
                "opencode: configured via ~/.local/share/opencode/auth.json",
            )
    except OSError:
        pass
    if (home / ".config" / "opencode").exists():
        # Config without credentials is the copy-the-wrong-directory mistake.
        # Name it, rather than reporting a bare "no credential".
        return (
            False,
            binary,
            "",
            "opencode: ~/.config/opencode exists but no credential "
            "(~/.local/share/opencode/auth.json is missing -- it is a separate directory)",
        )
    return False, binary, "", "opencode: on PATH but no supported credential configuration"


def _detect_pi(
    env: Mapping[str, str], home: Path, which: Callable[[str], Optional[str]]
) -> Tuple[bool, str, str, str]:
    """Return (available, binary, auth_source, reason).

    pi resolves a provider credential from environment variables, or from
    ``~/.pi/agent/auth.json`` for providers it has logged into.

    That file is NOT evidence on its own. pi writes it as an empty ``{}`` the
    first time it runs ANYTHING -- ``pi auth check`` alone creates it -- so
    testing for existence, the way the claude and opencode detectors reasonably
    test for theirs, would report every node that has ever invoked pi as
    configured. It is checked for content instead.

    pi can answer this authoritatively itself:

        pi auth check --provider <p> --json
        -> {"status": "ready", "provider": "anthropic", "authType": "api_key"}

    That is a real readiness probe rather than the inference the other
    detectors make, and it is the right answer -- but it costs a subprocess per
    heartbeat, and the executor's in-sandbox preflight already proves the route
    end to end where it matters. So detection stays cheap and the proof stays
    where it belongs.
    """
    binary = _which("pi", which)
    if not binary:
        return False, "", "", "pi: not on PATH"
    for name in ("INFERENCE_HUB_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        if str(env.get(name) or "").strip():
            return True, binary, name, "pi: configured via %s" % name
    auth = home / ".pi" / "agent" / "auth.json"
    data = _read_json(auth)
    if data:
        return (
            True,
            binary,
            "~/.pi/agent/auth.json",
            "pi: configured via ~/.pi/agent/auth.json",
        )
    if auth.exists():
        return (
            False,
            binary,
            "",
            "pi: ~/.pi/agent/auth.json is empty -- pi writes it on first run, so "
            "its presence is not a credential; set a provider key or run `pi auth`",
        )
    return False, binary, "", "pi: on PATH but no supported credential configuration"


_DETECTORS = {
    "claude": _detect_claude,
    "codex": _detect_codex,
    "cursor": _detect_cursor,
    "opencode": _detect_opencode,
    "pi": _detect_pi,
}


def _service_augmented_which(env: Mapping[str, str], home: Path) -> Callable[[str], Optional[str]]:
    """``shutil.which`` over the service PATH plus standard user install dirs.

    The worker daemon runs under a minimal supervisor PATH (systemd/launchd/
    supervisord), while the CLIs are installed into login-shell locations —
    so a bare which() under-reports "not installed" for binaries the task
    executor (which sources the login env) can see perfectly well. Heartbeat
    status must reflect what task runs will actually find.
    """
    extra = [
        str(home / ".local" / "bin"),
        str(home / "bin"),
        str(home / ".npm-global" / "bin"),
        # opencode's official installer writes here and ignores XDG_BIN_DIR.
        # It is on no default PATH, so a worker with opencode correctly
        # installed reported "not on PATH" from the heartbeat while a login
        # shell on the same host found it -- the inventory disagreeing with
        # reality is precisely what this function exists to prevent.
        str(home / ".opencode" / "bin"),
        "/usr/local/bin",
        "/opt/homebrew/bin",
    ]
    base = str(env.get("PATH") or "")
    search = os.pathsep.join(
        [p for p in extra if p not in base] + [p for p in base.split(os.pathsep) if p]
    )

    def _which_augmented(name: str) -> Optional[str]:
        import shutil as _shutil

        return _shutil.which(name, path=search)

    return _which_augmented


def detect_all(
    env: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
    verification: Optional[Mapping[str, Mapping[str, object]]] = None,
    host_which: Optional[Callable[[str], Optional[str]]] = None,
) -> Dict[str, Dict[str, object]]:
    """Secret-free per-CLI status for every known coding agent.

    Unlike :func:`resolve_coding_agent` (first-qualifying-wins routing), this
    reports ALL of them — the shape workers embed in their heartbeat
    ``resources["coding_clis"]`` so the hub (and ``mac admin fleet creds status``)
    can tell which agents have lost or never had CLI credentials.

    Three orthogonal facts are reported per agent and MUST NOT be conflated:

    * **execution inventory** — ``on_path`` and ``configured`` describe the
      environment selected by ``which``.  A worker whose tasks run in OpenShell
      passes the image-owned command inventory here; host PATH is not allowed to
      veto a CLI that exists only in the task image.
    * **host diagnostics** — ``host_on_path`` records the supervisor host's
      independent view.  This is useful for repair without conflating the host
      with the environment that actually executes tasks.
    * **executable-proof** — ``verified`` (and its alias ``available``) is only
      ``True`` when ``verification`` carries a *matching-route*, ``verified:
      True`` report from a live, non-mutating probe run in the SAME execution
      environment tasks use (the executor's in-sandbox preflight). A CLI that is
      on PATH and configured but was never proven to launch/authenticate there
      is ``available=False``.

    ``available`` deliberately tracks ``verified`` — never mere inventory — so a
    binary that only exists in a host-only directory the task sandbox cannot
    launch is never advertised as available.
    """
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    host_which = _service_augmented_which(env, home) if host_which is None else host_which
    if which is None:
        which = host_which
    out: Dict[str, Dict[str, object]] = {}
    for name, detect in _DETECTORS.items():
        host_configured, host_binary, host_source, host_detail = detect(env, home, host_which)
        configured, binary, source, detail = detect(env, home, which)
        checked = dict((verification or {}).get(name) or {})
        reported_binary = str(checked.get("binary") or "").strip()

        # A same-environment report is authoritative for the executable it
        # actually attempted.  This is what makes a sandbox-only Cursor install
        # visible even though the worker supervisor host cannot resolve it.
        if reported_binary:
            reported_name = Path(reported_binary).name

            def _reported_which(command: str) -> Optional[str]:
                return reported_binary if command == reported_name else None

            reported_configured, reported_path, reported_source, reported_detail = detect(
                env, home, _reported_which
            )
            reported_choice = _choice(
                name,
                reported_configured,
                reported_path,
                reported_source,
                [reported_detail],
                env,
            )
            if checked.get("route_fingerprint") == reported_choice.route_fingerprint():
                configured = reported_configured
                binary = reported_path
                source = reported_source
                detail = reported_detail

        choice = _choice(name, configured, binary, source, [detail], env)
        route = choice.observable()
        matches = bool(
            checked.get("route_fingerprint")
            and checked.get("route_fingerprint") == choice.route_fingerprint()
        )
        binary_status = (
            str(
                checked.get("binary_status")
                or ("present" if checked.get("verified") is True else "unverified")
            )
            if matches
            else "unverified"
        )
        execution_on_path = bool(binary)
        execution_configured = bool(configured)
        if matches and binary_status == "missing":
            execution_on_path = False
            execution_configured = False
        # Executable-proof: only a matching, successful same-environment probe
        # of a configured route counts.  Inventory alone can never set it.
        verified = bool(
            execution_configured
            and matches
            and binary_status == "present"
            and checked.get("verified") is True
        )
        out[name] = {
            # available == executable-proof (verified), NOT inventory.  Never
            # advertise a host-only binary the task sandbox cannot launch.
            "available": verified,
            "configured": execution_configured,
            "verified": verified,
            "verification_status": (
                "verified" if verified else "failed" if matches else "unverified"
            ),
            "on_path": execution_on_path,
            "binary_status": binary_status,
            "host_on_path": bool(host_binary),
            "host_configured": bool(host_configured),
            "host_auth_source": host_source,
            "host_detail": host_detail,
            "auth_source": source,
            "detail": detail,
            "provider": route.get("provider"),
            "protocol": route.get("protocol"),
            "auth_kind": route.get("auth_kind"),
            "endpoint": route.get("endpoint"),
            "model": route.get("model"),
            "route_fingerprint": route.get("route_fingerprint"),
            "verification": checked if matches else {},
        }
    return out


def _ladder_order(env: Mapping[str, str], rationale: List[str]) -> Tuple[str, ...]:
    """Candidate CLI order: the owner's route ladder when one is configured.

    ADR 0029 makes the route search path a fleet-wide contract instead of a
    per-worker environment accident. When the owner has published a
    ``mac.coding_route_ladder.v1`` document, its harness order is the order —
    the same document the executor, the reviewer and the hub read, so no two of
    them can disagree about which route is cheapest.

    :data:`AGENT_PRIORITY` remains the fallback for an unconfigured fleet. When
    a ladder DOES apply, the rationale says so by name, because "why did this
    task run on Claude?" has to stay answerable and "an ordering you cannot see
    chose it" is the exact dark spot this module exists to remove.

    A ladder that is present but unusable is NOT silently ignored — it is
    reported and the built-in order is used, because a fleet running on a route
    order nobody wrote is the failure mode worth being loud about.
    """
    try:
        from mac.route_ladder import LADDER_ENV, LadderConfigError, load_ladder
    except Exception:  # pragma: no cover - stdlib-only module; import cannot fail
        return AGENT_PRIORITY
    try:
        loaded = load_ladder(env)
    except LadderConfigError as exc:
        rationale.append(
            "%s is configured but unusable (%s); falling back to the built-in order"
            % (LADDER_ENV, exc)
        )
        return AGENT_PRIORITY
    if not loaded:
        # Silence, not a line: an unconfigured fleet gets exactly the rationale
        # it got before this existed. A note on every single resolution saying
        # a thing is absent is noise that buries the lines that matter.
        return AGENT_PRIORITY
    routes, _policy = loaded
    from mac.route_ladder import ladder_harness_order

    ordered = tuple(a for a in ladder_harness_order(routes) if a in _DETECTORS)
    unknown = [a for a in ladder_harness_order(routes) if a not in _DETECTORS]
    if unknown:
        rationale.append(
            "route ladder names %s, for which this build has no detector; skipped"
            % ", ".join(sorted(unknown))
        )
    if not ordered:
        rationale.append(
            "route ladder names no CLI this build can detect; falling back to the built-in order"
        )
        return AGENT_PRIORITY
    rationale.append("route ladder order applies: %s" % ", ".join(ordered))
    return ordered


def resolve_coding_agent(
    env: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
    accept: Optional[Callable[[CodingAgentChoice], bool]] = None,
    verify_all: bool = False,
    exclude: Optional[Iterable[str]] = None,
) -> CodingAgentChoice:
    """Resolve which coding-agent CLI to prefer, or none (fail closed).

    ``env``/``home``/``which`` are injectable for tests; they default to the
    live process environment, ``Path.home()`` and the same service-augmented
    lookup used by :func:`detect_all`. Selection and heartbeat inventory must
    not disagree merely because a supervisor starts with a minimal PATH.

    When ``accept`` is supplied, configured candidates are passed to it in
    priority order and selection continues after a rejection.  With
    ``verify_all=True`` the first accepted route remains selected while later
    configured fallbacks are also checked.  Heartbeats use this mode so a
    working primary route cannot leave every fallback permanently unverified.
    An explicit :data:`FORCE_ENV` pin remains strict because it limits the
    candidate set to that one agent.

    ``exclude`` drops agents from the candidate set outright. It exists for
    failover: a route that just exhausted its credits or met a provider outage
    mid-task is still perfectly "available" and would be re-selected on the
    spot, so the caller that watched it fail has to be able to say "not that
    one". An excluded pin degrades to no route rather than silently ignoring
    the pin.
    """
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    which = _service_augmented_which(env, home) if which is None else which

    rationale: List[str] = []

    if not _truthy(env.get(PREFERENCE_ENV, "1")):
        rationale.append("%s is disabled; executor will fail closed" % PREFERENCE_ENV)
        return _choice("", False, "", "", rationale, env)

    forced = str(env.get(FORCE_ENV) or "").strip().lower()
    if forced in _DISABLE_VALUES:
        rationale.append("%s=%s disables coding-agent preference" % (FORCE_ENV, forced))
        return _choice("", False, "", "", rationale, env)

    if forced and forced not in _DETECTORS:
        rationale.append("%s=%s is not a known agent; ignoring" % (FORCE_ENV, forced))
        forced = ""

    if forced in _DETECTORS:
        candidates = (forced,)
        rationale.append("%s pins selection to %s" % (FORCE_ENV, forced))
    else:
        candidates = _ladder_order(env, rationale)
    excluded = frozenset(str(name).strip().lower() for name in (exclude or ()) if name)
    if excluded:
        candidates = tuple(agent for agent in candidates if agent not in excluded)
        rationale.append(
            "excluding %s (failed this task); trying the remaining routes"
            % ", ".join(sorted(excluded))
        )

    selected: Optional[CodingAgentChoice] = None
    for agent in candidates:
        available, binary, auth_source, reason = _DETECTORS[agent](env, home, which)
        rationale.append(reason)
        if available:
            choice = _choice(agent, True, binary, auth_source, rationale, env)
            if accept is None:
                return choice
            try:
                accepted = bool(accept(choice))
            except Exception as exc:  # noqa: BLE001
                # One broken route must not shadow configured peers.
                rationale.append(
                    "%s: verifier raised %s; trying next configured agent"
                    % (agent, exc.__class__.__name__)
                )
                continue
            if accepted:
                if selected is None:
                    selected = choice
                if not verify_all:
                    return choice
                rationale.append("%s: route verified; continuing fallback verification" % agent)
                continue
            rationale.append("%s: route verification failed; trying next configured agent" % agent)

    if selected is not None:
        rationale.append("%s selected after checking all configured routes" % selected.agent)
        return replace(selected, rationale=list(rationale))

    rationale.append("no acceptable coding agent available/authed; executor will fail closed")
    return _choice("", False, "", "", rationale, env)


def mcp_config_document(server_command: List[str], name: str = "server") -> Dict[str, object]:
    """An MCP client config registering a single stdio server ``name``.

    ``server_command`` is the argv that launches a caller-selected MCP server.
    """
    command = server_command[0] if server_command else ""
    args = list(server_command[1:]) if len(server_command) > 1 else []
    return {"mcpServers": {name: {"command": command, "args": args}}}


def _default_argv(agent: str, binary: str, prompt: str, *, model: str = "") -> List[str]:
    """Non-interactive, approvals-bypassed invocation per agent.

    Approval bypass is intentional and coupled to the executor's OpenShell
    invariant: these argvs are routed through the sandbox-or-fail-closed gate,
    so a coding-agent CLI is only run unconfined under explicit break glass.

    ``model`` (per-task override, from ``MAC_TASK_MODEL``) maps to each CLI's
    own model flag so a task can pin a cheaper or stronger model without
    touching fleet config.
    """
    if agent == "claude":
        # `-p` = headless print mode; skip Claude's own permission prompts
        # (OpenShell is the real guardrail). Plain-text output for the audit log.
        argv = [binary, "--dangerously-skip-permissions", "--output-format", "text"]
        if model:
            argv += ["--model", model]
        return [*argv, "-p", prompt]
    if agent == "codex":
        # `exec` = non-interactive; bypass Codex's own approval + sandbox since
        # OpenShell provides confinement. The executor's OpenShell preflight runs
        # without uploading a git worktree, so allow that probe to reach the real
        # auth/provider check instead of failing on Codex's repo guard first.
        argv = [
            binary,
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
        ]
        if model:
            argv += ["--model", model]
        return [*argv, prompt]
    if agent == "cursor":
        argv = [binary, "-p", "--force"]
        if model:
            argv += ["--model", model]
        return [*argv, prompt]
    if agent == "opencode":
        # `run` is the non-interactive entry point; the bare `opencode` default
        # subcommand starts a TUI and would hang a task run forever.
        #
        # `--auto` bypasses opencode's own permission prompts (e.g.
        # "external_directory"). The comment this replaced claimed opencode
        # never prompts in `run` mode -- false against current opencode:
        # confirmed live, every real task execution was silently failing
        # with every filesystem permission request auto-rejected (nothing
        # answers an interactive prompt in a non-interactive task run),
        # blocking agents from reading even their own task worktree.
        # Confinement is still the executor's OpenShell gate, exactly as for
        # the others (--dangerously-skip-permissions / --dangerously-bypass-
        # approvals-and-sandbox above).
        #
        # Model names MUST carry the provider prefix that `opencode models`
        # prints -- "nvidia-inference/switchyard/openai/gpt-5.6-sol", not
        # "switchyard/openai/gpt-5.6-sol". The unprefixed form is accepted by
        # the CLI and then fails at the server as an opaque
        # "Unexpected server error", which is what a misconfigured node looked
        # like before this was understood.
        argv = [binary, "run", "--auto"]
        if model:
            argv += ["--model", model]
        return [*argv, prompt]
    if agent == "pi":
        # `--print`/`-p` is pi's non-interactive mode; without it the CLI opens
        # a TUI and a task run would hang forever, the same trap as bare
        # `opencode`. `--mode text` pins plain output for the audit log rather
        # than accepting whatever the default becomes.
        #
        # No approval-bypass flag is passed: pi has none, and confinement is
        # the executor's OpenShell gate as it is for every other route.
        argv = [binary, "--print", "--mode", "text"]
        if model:
            # pi's --model takes "provider/id" (and an optional ":<thinking>"
            # suffix), so a MAC_TASK_MODEL pin passes through unchanged.
            argv += ["--model", model]
        return [*argv, prompt]
    raise ValueError("unknown coding agent: %r" % agent)


def _codex_provider_config_argv(choice: CodingAgentChoice) -> List[str]:
    """Per-run custom-provider config, with credentials referenced by env name."""
    if choice.agent != "codex" or not choice.endpoint:
        return []
    built_in_openai = (
        choice.provider == "openai"
        and choice.endpoint.rstrip("/") == "https://api.openai.com/v1"
        and choice.auth_source in {"OPENAI_API_KEY", "~/.codex/auth.json"}
    )
    if built_in_openai:
        return []
    provider_name = "mac-openai" if choice.provider == "openai" else choice.provider
    provider_id = re.sub(r"[^a-z0-9_-]+", "-", provider_name.lower()).strip("-") or "mac"
    auth_env = choice.auth_source if re.fullmatch(r"[A-Z][A-Z0-9_]*", choice.auth_source) else ""
    args = [
        "-c",
        "model_provider=%s" % json.dumps(provider_id),
        "-c",
        "model_providers.%s.name=%s" % (provider_id, json.dumps("MAC %s route" % choice.provider)),
        "-c",
        "model_providers.%s.base_url=%s" % (provider_id, json.dumps(choice.endpoint)),
        "-c",
        "model_providers.%s.wire_api=%s"
        % (
            provider_id,
            json.dumps(choice.protocol or "responses"),
        ),
    ]
    if auth_env:
        args += [
            "-c",
            "model_providers.%s.env_key=%s" % (provider_id, json.dumps(auth_env)),
        ]
    if choice.provider == "mac-router":
        # Codex supports environment-backed custom-provider headers.  Stamp the
        # execution context already supplied by the worker into every router
        # request so the resolved provider/model and usage can be attributed to
        # the durable task ledger.  Referencing env names here keeps values and
        # credentials out of argv, logs, and process listings.
        context_headers = {
            "X-MAC-Agent-ID": "MAC_AGENT_ID",
            "X-MAC-Task-ID": "MAC_TASK_ID",
            "X-MAC-Lease-ID": "MAC_LEASE_ID",
            "X-MAC-Command-ID": "MAC_COMMAND_ID",
            "X-MAC-Persona-Instance-ID": "MAC_PERSONA_INSTANCE_ID",
            "X-MAC-Fleet": "MAC_FLEET",
        }
        inline_table = "{ %s }" % ", ".join(
            "%s = %s" % (json.dumps(header), json.dumps(env_name))
            for header, env_name in context_headers.items()
        )
        args += [
            "-c",
            "model_providers.%s.env_http_headers=%s" % (provider_id, inline_table),
        ]
    return args


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

    task_model = str(env.get("MAC_TASK_MODEL") or choice.model or "").strip()
    argv = _default_argv(choice.agent, choice.binary, prompt, model=task_model)
    if choice.agent == "codex":
        # Provider config is inserted before the `exec` subcommand. Tokens are
        # never put in argv; Codex reads the named environment variable.
        argv = [argv[0], *_codex_provider_config_argv(choice), *argv[1:]]
    if mcp_config_path and supports_per_invocation_mcp(choice.agent):
        # Insert right after the binary so it precedes the `-p <prompt>` tail.
        argv = [argv[0], "--mcp-config", mcp_config_path, *argv[1:]]
    return argv


def _describe(env: Optional[Mapping[str, str]] = None) -> str:
    choice = resolve_coding_agent(env=env)
    return json.dumps(choice.observable(), indent=2, sort_keys=True)


if __name__ == "__main__":  # pragma: no cover - operator debugging aid
    # `python -m mac.coding_agent` prints the (secret-free) routing decision for
    # the current environment, so a fleet operator can see why a node selects a
    # coding agent or fails closed.
    print(_describe())
