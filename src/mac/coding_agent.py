"""MAC's coding CLIs: an ordered list the hub owns, driven through its router.

The fleet's coding CLIs are an ordered list, ``MAC_CODING_AGENTS`` (default
``opencode``; known CLIs: opencode, claude), configured on the hub. Workers do
not choose: the hub projects its list into every assignment
(``metadata.runtime.coding_agents``, exported to the executor as
``MAC_TASK_CODING_AGENTS``) and returns it on every heartbeat
(``MAC_HUB_CODING_AGENTS``), so a change on the hub reaches the whole fleet on
the next task. :func:`coding_agent_order` says where the order came from.

The executor takes the first CLI in the list that works here and moves to the
next only on a structured availability failure: the binary is missing, the
host cannot reach the hub router (no hub URL, no token), or the in-sandbox
preflight fails (:func:`resolve_coding_agent`). After a run, it also moves on
when the hub router recorded an auth, rate-limit or upstream failure for that
CLI's route (see :mod:`mac.executor_sandbox`). A failing test, a judge's
``not_met`` or a bad diff is a task outcome and never moves the list.

Both CLIs reach models through the hub with the task's inference-only token
(:mod:`mac.inference_tokens`): opencode through ``/v1/chat/completions`` via a
generated config whose only provider is ``machub``
(:func:`opencode_router_config`), and Claude Code through ``/v1/messages``
(:mod:`mac.anthropic_passthrough`) with MAC's board hooks
(:mod:`mac.claude_hooks`). Provider failover inside one route is the hub
router's job.

The decision is *legible*: every resolution yields a secret-free
:meth:`CodingAgentChoice.observable` plus a human-readable ``rationale`` and
the CLIs it skipped, with why.

The module is intentionally dependency-free (stdlib only) and has no import-time
side effects (``resolve_coding_agent`` takes injectable ``env``/``home``/``which``).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "CODING_AGENT",
    "CodingAgentChoice",
    "resolve_coding_agent",
    "coding_agent_argv",
    "route_status",
    "PREFERENCE_ENV",
    "FORCE_ENV",
    "AGENTS_ENV",
    "coding_agent_order",
]

#: The default coding CLI.
CODING_AGENT = "opencode"
#: Claude Code.
CLAUDE_AGENT = "claude"
SUPPORTED_AGENTS = (CODING_AGENT, CLAUDE_AGENT)

#: The fleet's ordered coding-CLI list. Configured on the hub; a worker reads
#: its own value only when the hub has issued none (and says so).
AGENTS_ENV = "MAC_CODING_AGENTS"
#: The hub's list for the task being run, from the assignment.
TASK_AGENTS_ENV = "MAC_TASK_CODING_AGENTS"
#: The hub's list as of the worker's last heartbeat (used outside a task).
HUB_AGENTS_ENV = "MAC_HUB_CODING_AGENTS"
#: The CLI the executor is running right now (set per run, never configured).
ACTIVE_AGENT_ENV = "MAC_ACTIVE_CODING_AGENT"
#: The order used when nothing is configured anywhere.
DEFAULT_AGENTS: Tuple[str, ...] = (CODING_AGENT,)
#: The Anthropic model Claude Code runs on unless a task pins a Claude model.
CLAUDE_MODEL_ENV = "MAC_CLAUDE_MODEL"
DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"
#: Upper bound on Claude Code's agentic turns in one run.
CLAUDE_MAX_TURNS_ENV = "MAC_CLAUDE_MAX_TURNS"
DEFAULT_CLAUDE_MAX_TURNS = 400

#: Master on/off for the coding route. Default ON. Falsy means no coding agent
#: is eligible and the executor fails closed.
PREFERENCE_ENV = "MAC_PREFER_CODING_AGENT"

#: Deprecated single-CLI switch. A disable value (``off``, ``none``, ``0`` ...)
#: still turns the coding route off so the executor fails closed. A CLI name is
#: honoured only when no list was configured anywhere, and is reported as
#: deprecated: the list is :data:`AGENTS_ENV`, set on the hub.
FORCE_ENV = "MAC_CODING_AGENT"

#: Sentinel the coding agent must echo back for the preflight to pass. A correct
#: echo proves, end-to-end, that the binary exists, the credential resolves, and
#: egress to the router is permitted.
PREFLIGHT_SENTINEL = "MAC_CODING_AGENT_SANDBOX_OK"
PREFLIGHT_PROMPT = "Respond with exactly this text and nothing else: " + PREFLIGHT_SENTINEL

_DISABLE_VALUES = {"off", "none", "hermes", "gateway", "0", "false", "no"}

#: opencode's provider id for the hub router in the generated config. Model
#: references on the command line are ``machub/<logical model>``.
ROUTER_PROVIDER_ID = "machub"
#: The sandbox credential the router route authenticates with. A per-task,
#: inference-only token (:mod:`mac.inference_tokens`), never the worker token.
ROUTER_AUTH_ENV = "MAC_INFERENCE_TOKEN"
#: Logical model names the generated config declares (comma separated).
CODING_MODELS_ENV = "MAC_CODING_MODELS"
#: The model a task runs on when it does not pin one with ``MAC_TASK_MODEL``.
CODING_DEFAULT_MODEL_ENV = "MAC_CODING_DEFAULT_MODEL"
DEFAULT_CODING_MODEL = "gpt-5.6-sol"
_HUB_URL_ENVS = ("MAC_HUB_URL", "MAC_URL")
_WORKER_TOKEN_ENVS = ("MAC_WORKER_TOKEN", "MAC_TOKEN", "MAC_API_TOKEN")


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _which(name: str, which: Callable[[str], Optional[str]]) -> Optional[str]:
    try:
        return which(name)
    except Exception:  # noqa: BLE001 - PATH probing must never raise into selection
        return None


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
    #: The ordered list this choice was made from, and where it came from.
    order: Tuple[str, ...] = ()
    order_source: str = ""
    #: CLIs passed over before this one: ``{"agent", "failure_class", "detail"}``.
    skipped: Tuple[Dict[str, str], ...] = ()

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
            "order": list(self.order),
            "order_source": self.order_source or None,
            "skipped": [dict(item) for item in self.skipped],
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


def _env_text(env: Mapping[str, str], *names: str) -> str:
    for name in names:
        value = str(env.get(name) or "").strip()
        if value:
            return value
    return ""


@dataclass(frozen=True)
class AgentOrder:
    """The ordered coding-CLI list in effect, where it came from, and notes."""

    agents: Tuple[str, ...]
    source: str
    notes: Tuple[str, ...] = ()

    def observable(self) -> Dict[str, object]:
        return {"agents": list(self.agents), "source": self.source, "notes": list(self.notes)}


def parse_agent_list(raw: object) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """``(known CLIs in order, unknown names)`` from a comma list or sequence."""
    if isinstance(raw, (list, tuple)):
        items = [str(item) for item in raw]
    else:
        items = str(raw or "").split(",")
    agents: List[str] = []
    unknown: List[str] = []
    for item in items:
        name = item.strip().lower()
        if not name:
            continue
        if name not in SUPPORTED_AGENTS:
            unknown.append(name)
        elif name not in agents:
            agents.append(name)
    return tuple(agents), tuple(unknown)


def coding_agent_order(env: Optional[Mapping[str, str]] = None) -> AgentOrder:
    """The ordered list of coding CLIs, hub first.

    Precedence: the hub's list for this task (``MAC_TASK_CODING_AGENTS``), the
    hub's list from the last heartbeat (``MAC_HUB_CODING_AGENTS``), this
    host's own ``MAC_CODING_AGENTS`` (only when the hub has issued nothing,
    and reported as a local override), the deprecated ``MAC_CODING_AGENT``,
    then :data:`DEFAULT_AGENTS`. Unknown names are dropped with a note.
    """
    env = os.environ if env is None else env
    notes: List[str] = []
    for name, source in (
        (TASK_AGENTS_ENV, "hub"),
        (HUB_AGENTS_ENV, "hub"),
        (AGENTS_ENV, "worker-local"),
    ):
        raw = _env_text(env, name)
        if not raw:
            continue
        agents, unknown = parse_agent_list(raw)
        if unknown:
            notes.append(
                "%s names unknown coding CLIs %s (known: %s); ignored"
                % (name, ", ".join(unknown), ", ".join(SUPPORTED_AGENTS))
            )
        if not agents:
            notes.append("%s lists no known coding CLI; ignored" % name)
            continue
        if source == "worker-local":
            notes.append(
                "no hub-issued list; using this host's own %s (a local override)" % AGENTS_ENV
            )
        return AgentOrder(agents, source, tuple(notes))
    forced = _env_text(env, FORCE_ENV).lower()
    if forced and forced not in SUPPORTED_AGENTS and forced not in _DISABLE_VALUES:
        notes.append(
            "%s=%s is not supported; MAC's coding CLIs are %s"
            % (FORCE_ENV, forced, ", ".join(SUPPORTED_AGENTS))
        )
    if forced in SUPPORTED_AGENTS:
        notes.append(
            "%s=%s is deprecated: set the ordered list %s on the hub"
            % (FORCE_ENV, forced, AGENTS_ENV)
        )
        return AgentOrder((forced,), "deprecated:%s" % FORCE_ENV, tuple(notes))
    return AgentOrder(DEFAULT_AGENTS, "default", tuple(notes))


#: Hub-owned model settings carried alongside the list. The worker exports
#: them under these names for the run, replacing any per-host value.
POLICY_MODEL_ENVS: Dict[str, str] = {
    "claude_model": CLAUDE_MODEL_ENV,
    "judge_model": "MAC_JUDGE_MODEL",
}
CODING_POLICY_SCHEMA = "mac.coding_policy.v1"


def hub_coding_policy(env: Optional[Mapping[str, str]] = None) -> Dict[str, object]:
    """The coding policy the hub issues to workers, from the hub's own config.

    ``agents`` is the hub's ``MAC_CODING_AGENTS`` (default opencode). The model
    keys appear only when the hub sets them, so a worker keeps the module
    defaults otherwise.
    """
    env = os.environ if env is None else env
    agents, _unknown = parse_agent_list(_env_text(env, AGENTS_ENV))
    policy: Dict[str, object] = {
        "schema": CODING_POLICY_SCHEMA,
        "agents": list(agents or DEFAULT_AGENTS),
    }
    for key, name in POLICY_MODEL_ENVS.items():
        value = _env_text(env, name)
        if value:
            policy[key] = value
    return policy


def coding_policy_env(policy: object, *, agents_env: str = TASK_AGENTS_ENV) -> Dict[str, str]:
    """Env assignments that carry a hub-issued policy into a process.

    ``agents_env`` is :data:`TASK_AGENTS_ENV` for a task run and
    :data:`HUB_AGENTS_ENV` for the worker's own (heartbeat) view. Anything
    malformed yields nothing, so a bad document never empties the list.
    """
    if not isinstance(policy, Mapping):
        return {}
    agents, _unknown = parse_agent_list(policy.get("agents") or ())
    if not agents:
        return {}
    values = {agents_env: ",".join(agents)}
    for key, name in POLICY_MODEL_ENVS.items():
        value = str(policy.get(key) or "").strip()
        if value:
            values[name] = value
    return values


def selected_agent(env: Optional[Mapping[str, str]] = None) -> str:
    """The coding CLI the current run uses.

    Inside a run the executor records the CLI it actually chose
    (``MAC_ACTIVE_CODING_AGENT``); outside one this is the first CLI on the
    list.
    """
    env = os.environ if env is None else env
    active = _env_text(env, ACTIVE_AGENT_ENV).lower()
    if active in SUPPORTED_AGENTS:
        return active
    return coding_agent_order(env).agents[0]


def claude_model(env: Mapping[str, str]) -> str:
    """The Anthropic model Claude Code runs on.

    A task pin wins when it names a Claude model (``MAC_TASK_MODEL``, with or
    without the ``machub/`` or ``azure/anthropic/`` prefixes the router knows);
    a pin for another family is ignored, since Claude Code cannot run it.
    """
    pinned = _env_text(env, "MAC_TASK_MODEL")
    for prefix in (ROUTER_PROVIDER_ID + "/",):
        if pinned.startswith(prefix):
            pinned = pinned[len(prefix) :]
    if pinned and "claude" in pinned.lower():
        return pinned
    return _env_text(env, CLAUDE_MODEL_ENV) or DEFAULT_CLAUDE_MODEL


def claude_max_turns(env: Mapping[str, str]) -> int:
    try:
        return max(1, int(_env_text(env, CLAUDE_MAX_TURNS_ENV) or DEFAULT_CLAUDE_MAX_TURNS))
    except ValueError:
        return DEFAULT_CLAUDE_MAX_TURNS


def router_hub_url(env: Mapping[str, str]) -> str:
    """The hub base URL the router route talks to, or ``""``."""
    return _env_text(env, *_HUB_URL_ENVS).rstrip("/")


def coding_model(env: Mapping[str, str]) -> str:
    """The logical router model a task runs on.

    ``MAC_TASK_MODEL`` (a per-task pin) wins, then ``MAC_CODING_DEFAULT_MODEL``,
    then :data:`DEFAULT_CODING_MODEL`. A ``machub/`` prefix is accepted and
    dropped so either spelling of a pin works.
    """
    model = _env_text(env, "MAC_TASK_MODEL", CODING_DEFAULT_MODEL_ENV) or DEFAULT_CODING_MODEL
    prefix = ROUTER_PROVIDER_ID + "/"
    return model[len(prefix) :] if model.startswith(prefix) else model


def coding_models(env: Mapping[str, str]) -> List[str]:
    """The logical models the generated opencode config declares.

    ``MAC_CODING_MODELS`` (comma separated, default :data:`DEFAULT_CODING_MODEL`)
    plus the task's own model, which opencode refuses unless it is declared.
    """
    raw = str(env.get(CODING_MODELS_ENV) or "").strip() or DEFAULT_CODING_MODEL
    models: List[str] = []
    for item in [*raw.split(","), coding_model(env)]:
        name = item.strip()
        if name and name not in models:
            models.append(name)
    return models


def opencode_router_config(env: Mapping[str, str]) -> Dict[str, object]:
    """opencode config with one provider, ``machub``: the hub's model router.

    ``env`` is the environment the CLI will run in (for a sandbox, the
    sandbox's view of ``MAC_HUB_URL``). The API key is an ``{env:...}``
    reference, so the file carries no secret. Task and lease ids, when known,
    ride along as router attribution headers; the agent is attributed from the
    token itself.
    """
    hub = router_hub_url(env)
    if not hub:
        raise ValueError("opencode router config needs MAC_HUB_URL")
    options: Dict[str, object] = {
        "baseURL": hub + "/v1",
        "apiKey": "{env:%s}" % ROUTER_AUTH_ENV,
    }
    headers = {
        header: value
        for header, value in (
            ("X-MAC-Task-ID", _env_text(env, "MAC_TASK_ID")),
            ("X-MAC-Lease-ID", _env_text(env, "MAC_LEASE_ID")),
        )
        if value
    }
    if headers:
        options["headers"] = headers
    return {
        "$schema": "https://opencode.ai/config.json",
        # Never reach opencode.ai for an update from inside a task.
        "autoupdate": False,
        "model": "%s/%s" % (ROUTER_PROVIDER_ID, coding_model(env)),
        "provider": {
            ROUTER_PROVIDER_ID: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "MAC hub model router",
                "options": options,
                "models": {name: {"tool_call": True} for name in coding_models(env)},
            }
        },
    }


def _route_fields(env: Mapping[str, str], agent: str = CODING_AGENT) -> Dict[str, str]:
    """The router route's identity: provider, protocol, auth and endpoint."""
    if agent == CLAUDE_AGENT:
        return {
            "provider": "mac-router",
            "protocol": "anthropic-messages",
            "auth_kind": "bearer_env",
            "endpoint": _safe_endpoint(router_hub_url(env) + "/v1/messages", ""),
            "model": claude_model(env),
        }
    return {
        "provider": "mac-router",
        "protocol": "openai-chat-completions",
        "auth_kind": "bearer_env",
        "endpoint": _safe_endpoint(router_hub_url(env) + "/v1", ""),
        "model": coding_model(env),
    }


def _choice(
    available: bool,
    binary: str,
    auth_source: str,
    rationale: List[str],
    env: Mapping[str, str],
    agent: str = CODING_AGENT,
) -> CodingAgentChoice:
    if not available and not binary:
        return CodingAgentChoice(agent="", available=False, rationale=rationale)
    return CodingAgentChoice(
        agent=agent,
        available=available,
        binary=binary,
        auth_source=auth_source,
        rationale=rationale,
        **_route_fields(env, agent),
    )


def _detect_opencode(
    env: Mapping[str, str], which: Callable[[str], Optional[str]]
) -> Tuple[bool, str, str, str]:
    """Return (available, binary, auth_source, reason).

    The route is the hub's model router. It needs the hub URL and either an
    inference token (a sandbox) or a worker token to mint one with (a worker
    host). It always reports ``MAC_INFERENCE_TOKEN`` as its auth source.
    """
    binary = _which(CODING_AGENT, which)
    if not binary:
        return False, "", "", "opencode: not on PATH"
    if not router_hub_url(env):
        return False, binary, "", "opencode: no hub URL (MAC_HUB_URL) to route through"
    if not _env_text(env, ROUTER_AUTH_ENV, *_WORKER_TOKEN_ENVS):
        return (
            False,
            binary,
            "",
            "opencode: no inference token or worker token to authenticate to the hub router",
        )
    return (
        True,
        binary,
        ROUTER_AUTH_ENV,
        "opencode: routed through the hub model router (%s)" % ROUTER_PROVIDER_ID,
    )


def _detect_claude(
    env: Mapping[str, str], which: Callable[[str], Optional[str]]
) -> Tuple[bool, str, str, str]:
    """Return (available, binary, auth_source, reason) for Claude Code.

    Same requirements as opencode: the binary, the hub URL, and a token to
    reach the hub's /v1/messages with.
    """
    binary = _which(CLAUDE_AGENT, which)
    if not binary:
        return False, "", "", "claude: not on PATH"
    if not router_hub_url(env):
        return False, binary, "", "claude: no hub URL (MAC_HUB_URL) to route through"
    if not _env_text(env, ROUTER_AUTH_ENV, *_WORKER_TOKEN_ENVS):
        return (
            False,
            binary,
            "",
            "claude: no inference token or worker token to authenticate to the hub router",
        )
    return True, binary, ROUTER_AUTH_ENV, "claude: routed through the hub's /v1/messages"


def _detect(
    agent: str, env: Mapping[str, str], which: Callable[[str], Optional[str]]
) -> Tuple[bool, str, str, str]:
    return (_detect_claude if agent == CLAUDE_AGENT else _detect_opencode)(env, which)


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


def route_status(
    env: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
    verification: Optional[Mapping[str, object]] = None,
    host_which: Optional[Callable[[str], Optional[str]]] = None,
    agent: str = "",
) -> Dict[str, object]:
    """Secret-free status of one coding CLI's route, for the worker heartbeat.

    Workers embed one of these per listed CLI as
    ``resources["coding_clis"]["clis"][<agent>]``. ``agent`` defaults to the
    first CLI on the list.
    Three facts are reported and MUST NOT be conflated:

    * **execution inventory** -- ``on_path`` and ``configured`` describe the
      environment selected by ``which`` (the task image's declared inventory
      for a sandboxed worker).
    * **host diagnostics** -- ``host_on_path``/``host_configured`` record the
      supervisor host's own view.
    * **executable proof** -- ``verified`` (and its alias ``available``) is only
      ``True`` when ``verification`` is a matching-route, ``verified: True``
      report from the live probe run in the environment tasks use.
    """
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    host_which = _service_augmented_which(env, home) if host_which is None else host_which
    if which is None:
        which = host_which
    agent = agent if agent in SUPPORTED_AGENTS else selected_agent(env)
    host_configured, host_binary, host_source, host_detail = _detect(agent, env, host_which)
    configured, binary, source, detail = _detect(agent, env, which)
    checked = dict(verification or {})
    reported_binary = str(checked.get("binary") or "").strip()
    if reported_binary:
        # A same-environment report is authoritative for the executable it
        # actually attempted, even when this host cannot resolve that path.
        reported = _detect(
            agent, env, lambda command: reported_binary if command == agent else None
        )
        reported_choice = _choice(
            reported[0], reported[1], reported[2], [reported[3]], env, agent
        )
        if checked.get("route_fingerprint") == reported_choice.route_fingerprint():
            configured, binary, source, detail = reported

    choice = _choice(configured, binary, source, [detail], env, agent)
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
    verified = bool(
        execution_configured
        and matches
        and binary_status == "present"
        and checked.get("verified") is True
    )
    return {
        "available": verified,
        "configured": execution_configured,
        "verified": verified,
        "verification_status": ("verified" if verified else "failed" if matches else "unverified"),
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


def _unavailable_class(binary: str, env: Mapping[str, str]) -> str:
    """Why a detector said no, as a closed failure class."""
    if not binary:
        return "agent_binary_missing"
    if not router_hub_url(env):
        return "not_configured"
    return "inference_token_unavailable"


def resolve_coding_agent(
    env: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
    accept: Optional[Callable[[CodingAgentChoice], bool]] = None,
    exclude: Tuple[str, ...] = (),
) -> CodingAgentChoice:
    """The first CLI on the ordered list that works here, or none.

    Each CLI is checked in order: installed, able to reach the hub router,
    and accepted by ``accept`` (the in-sandbox preflight) when one is given.
    A CLI that fails one of those is skipped with a failure class and the
    next is tried; when none qualifies the executor fails closed. ``exclude``
    names CLIs already tried in this attempt (post-run failover), which are
    skipped as ``already_failed``.

    ``env``/``home``/``which`` are injectable for tests; they default to the
    live process environment, ``Path.home()`` and the same service-augmented
    lookup used by :func:`route_status`.
    """
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    which = _service_augmented_which(env, home) if which is None else which

    order = coding_agent_order(env)
    rationale: List[str] = list(order.notes)
    rationale.append(
        "coding CLIs in order: %s (from %s)" % (", ".join(order.agents), order.source)
    )

    def _none(reason: str, skipped: List[Dict[str, str]]) -> CodingAgentChoice:
        rationale.append(reason)
        return CodingAgentChoice(
            agent="",
            available=False,
            rationale=rationale,
            order=order.agents,
            order_source=order.source,
            skipped=tuple(skipped),
        )

    if not _truthy(env.get(PREFERENCE_ENV, "1")):
        return _none("%s is disabled; executor will fail closed" % PREFERENCE_ENV, [])
    forced = _env_text(env, FORCE_ENV).lower()
    if forced in _DISABLE_VALUES:
        return _none("%s=%s disables the coding route" % (FORCE_ENV, forced), [])

    skipped: List[Dict[str, str]] = []
    for agent in order.agents:
        if agent in exclude:
            skipped.append(
                {
                    "agent": agent,
                    "failure_class": "already_failed",
                    "detail": "%s already failed this attempt" % agent,
                }
            )
            continue
        available, binary, auth_source, reason = _detect(agent, env, which)
        rationale.append(reason)
        if not available:
            skipped.append(
                {
                    "agent": agent,
                    "failure_class": _unavailable_class(binary, env),
                    "detail": reason,
                }
            )
            continue
        choice = _choice(True, binary, auth_source, rationale, env, agent)
        if accept is not None:
            try:
                accepted = bool(accept(choice))
            except Exception as exc:  # noqa: BLE001 - a verifier crash means "not verified"
                rationale.append("%s: verifier raised %s" % (agent, exc.__class__.__name__))
                accepted = False
            if not accepted:
                rationale.append("%s: route verification failed" % agent)
                skipped.append(
                    {
                        "agent": agent,
                        "failure_class": "preflight_failed",
                        "detail": "%s did not pass the in-sandbox preflight" % agent,
                    }
                )
                continue
        if skipped:
            rationale.append(
                "using %s after skipping %s"
                % (agent, ", ".join("%s (%s)" % (i["agent"], i["failure_class"]) for i in skipped))
            )
        return CodingAgentChoice(
            agent=choice.agent,
            available=True,
            binary=choice.binary,
            auth_source=choice.auth_source,
            provider=choice.provider,
            protocol=choice.protocol,
            auth_kind=choice.auth_kind,
            endpoint=choice.endpoint,
            model=choice.model,
            rationale=rationale,
            order=order.agents,
            order_source=order.source,
            skipped=tuple(skipped),
        )
    return _none("no coding route available; executor will fail closed", skipped)


def opencode_argv(binary: str, prompt: str, *, model: str = "") -> List[str]:
    """Non-interactive, approvals-bypassed opencode invocation.

    ``run`` is the non-interactive entry point; the bare ``opencode`` default
    subcommand starts a TUI and would hang a task run forever.

    ``--auto`` bypasses opencode's own permission prompts (e.g.
    "external_directory"): nothing answers an interactive prompt in a task run,
    so without it every filesystem permission request is auto-rejected.
    Confinement is the executor's OpenShell gate.
    """
    argv = [binary, "run", "--auto"]
    if model:
        argv += ["--model", model]
    return [*argv, prompt]


#: Where the executor writes Claude Code's settings and hooks, relative to the
#: task workspace (the agent's working directory).
CLAUDE_AGENT_DIR = ".mac-agent"
CLAUDE_SETTINGS_FILE = CLAUDE_AGENT_DIR + "/settings.json"


def claude_argv(
    binary: str,
    prompt: str,
    *,
    model: str,
    max_turns: int,
    session_id: str = "",
    resume: str = "",
    settings: str = CLAUDE_SETTINGS_FILE,
) -> List[str]:
    """Headless Claude Code with MAC's settings and hooks only.

    ``--setting-sources ""`` keeps a repository's own ``.claude`` settings and
    hooks from loading: the task repository is untrusted input. Permission
    prompts are bypassed because nobody can answer one in a task run;
    confinement is the executor's OpenShell gate, as with opencode's
    ``--auto``. ``session_id`` names a new session; ``resume`` continues an
    earlier one (the executor keeps sessions under the workspace).
    """
    argv = [
        binary,
        "-p",
        "--settings",
        settings,
        "--setting-sources",
        "",
        "--permission-mode",
        "bypassPermissions",
        "--model",
        model,
        "--max-turns",
        str(int(max_turns)),
        "--output-format",
        "text",
    ]
    if resume:
        argv += ["--resume", resume]
    elif session_id:
        argv += ["--session-id", session_id]
    return [*argv, prompt]


def coding_agent_argv(
    choice: CodingAgentChoice,
    prompt: str,
    *,
    env: Optional[Mapping[str, str]] = None,
    session_id: str = "",
    resume: str = "",
) -> List[str]:
    """Build the argv to run ``prompt`` through the chosen coding CLI."""
    if not choice.available or choice.agent not in SUPPORTED_AGENTS:
        raise ValueError("coding_agent_argv called without an available coding route")
    env = os.environ if env is None else env
    if choice.agent == CLAUDE_AGENT:
        return claude_argv(
            choice.binary,
            prompt,
            model=claude_model(env),
            max_turns=claude_max_turns(env),
            session_id=session_id,
            resume=resume,
        )
    task_model = str(env.get("MAC_TASK_MODEL") or choice.model or "").strip()
    # The generated config names the router provider `machub`; opencode needs
    # the provider prefix on every model reference.
    model = "%s/%s" % (ROUTER_PROVIDER_ID, coding_model({"MAC_TASK_MODEL": task_model}))
    return opencode_argv(choice.binary, prompt, model=model)


def _describe(env: Optional[Mapping[str, str]] = None) -> str:
    choice = resolve_coding_agent(env=env)
    return json.dumps(choice.observable(), indent=2, sort_keys=True)


if __name__ == "__main__":  # pragma: no cover - operator debugging aid
    # `python -m mac.coding_agent` prints the (secret-free) routing decision for
    # the current environment, so a fleet operator can see why a node selects
    # opencode or fails closed.
    print(_describe())
