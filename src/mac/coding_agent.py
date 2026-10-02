"""MAC's coding CLI: opencode, driven through the hub's model router.

MAC runs exactly one coding CLI. opencode gets its model from the hub's
OpenAI-compatible router through a generated config whose only provider is
``machub`` (:func:`opencode_router_config`), and it authenticates with a
per-task, inference-only token (:mod:`mac.inference_tokens`). Provider choice
and provider failover live in the hub router, not here: this module only
answers "is opencode installed and can it reach the router from here?".

The route is available when ``opencode`` is on PATH and this host can reach
the hub as a worker (a hub URL plus a worker token to mint the task's
inference token with), or already holds an inference token, as a sandbox does.
Otherwise the executor fails closed.

The decision is *legible*: every resolution yields a secret-free
:meth:`CodingAgentChoice.observable` plus a human-readable ``rationale``.

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
]

#: The coding CLI. There is no other.
CODING_AGENT = "opencode"

#: Master on/off for the coding route. Default ON. Falsy means no coding agent
#: is eligible and the executor fails closed.
PREFERENCE_ENV = "MAC_PREFER_CODING_AGENT"

#: ``opencode`` (or unset) selects opencode; a disable value (``off``, ``none``,
#: ``0`` ...) turns the coding route off so the executor fails closed. Any other
#: value is ignored with a rationale line: MAC has no other coding CLI.
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


def _env_text(env: Mapping[str, str], *names: str) -> str:
    for name in names:
        value = str(env.get(name) or "").strip()
        if value:
            return value
    return ""


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


def _route_fields(env: Mapping[str, str]) -> Dict[str, str]:
    """The router route's identity: provider, protocol, auth and endpoint."""
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
) -> CodingAgentChoice:
    if not available and not binary:
        return CodingAgentChoice(agent="", available=False, rationale=rationale)
    return CodingAgentChoice(
        agent=CODING_AGENT,
        available=available,
        binary=binary,
        auth_source=auth_source,
        rationale=rationale,
        **_route_fields(env),
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
) -> Dict[str, object]:
    """Secret-free status of the opencode route, for the worker heartbeat.

    Workers embed this as ``resources["coding_clis"]["clis"]["opencode"]``.
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
    host_configured, host_binary, host_source, host_detail = _detect_opencode(env, host_which)
    configured, binary, source, detail = _detect_opencode(env, which)
    checked = dict(verification or {})
    reported_binary = str(checked.get("binary") or "").strip()
    if reported_binary:
        # A same-environment report is authoritative for the executable it
        # actually attempted, even when this host cannot resolve that path.
        reported = _detect_opencode(
            env, lambda command: reported_binary if command == CODING_AGENT else None
        )
        reported_choice = _choice(reported[0], reported[1], reported[2], [reported[3]], env)
        if checked.get("route_fingerprint") == reported_choice.route_fingerprint():
            configured, binary, source, detail = reported

    choice = _choice(configured, binary, source, [detail], env)
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


def resolve_coding_agent(
    env: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
    accept: Optional[Callable[[CodingAgentChoice], bool]] = None,
) -> CodingAgentChoice:
    """Resolve the opencode route, or none (the executor fails closed).

    ``env``/``home``/``which`` are injectable for tests; they default to the
    live process environment, ``Path.home()`` and the same service-augmented
    lookup used by :func:`route_status`.

    ``accept`` is an end-to-end verifier (the in-sandbox preflight). When it
    rejects the route, or raises, there is no route: there is nothing to fall
    back to, and provider failover is the hub router's job.
    """
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    which = _service_augmented_which(env, home) if which is None else which

    rationale: List[str] = []
    if not _truthy(env.get(PREFERENCE_ENV, "1")):
        rationale.append("%s is disabled; executor will fail closed" % PREFERENCE_ENV)
        return _choice(False, "", "", rationale, env)

    forced = str(env.get(FORCE_ENV) or "").strip().lower()
    if forced in _DISABLE_VALUES:
        rationale.append("%s=%s disables the coding route" % (FORCE_ENV, forced))
        return _choice(False, "", "", rationale, env)
    if forced and forced != CODING_AGENT:
        rationale.append(
            "%s=%s is not supported; MAC's only coding CLI is %s"
            % (FORCE_ENV, forced, CODING_AGENT)
        )

    available, binary, auth_source, reason = _detect_opencode(env, which)
    rationale.append(reason)
    if not available:
        rationale.append("no coding route available; executor will fail closed")
        return _choice(False, "", "", rationale, env)
    choice = _choice(True, binary, auth_source, rationale, env)
    if accept is None:
        return choice
    try:
        accepted = bool(accept(choice))
    except Exception as exc:  # noqa: BLE001 - a verifier crash means "not verified"
        rationale.append("opencode: verifier raised %s" % exc.__class__.__name__)
        accepted = False
    if accepted:
        return choice
    rationale.append("opencode: route verification failed; executor will fail closed")
    return _choice(False, "", "", rationale, env)


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


def coding_agent_argv(
    choice: CodingAgentChoice,
    prompt: str,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> List[str]:
    """Build the argv to run ``prompt`` through opencode on a ``machub`` model."""
    if not choice.available or choice.agent != CODING_AGENT:
        raise ValueError("coding_agent_argv called without an available opencode choice")
    env = os.environ if env is None else env
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
