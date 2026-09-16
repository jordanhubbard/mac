"""First-class, dependency-light adapter for the ``hgx`` provider CLI.

This module wraps the ``hgx`` CLI verbs ``create``, ``list``, ``status``,
``ssh``, ``stop``, ``resume`` and ``delete`` as structured, subprocess-based
calls, in the same spirit as the subprocess helpers in
:mod:`mac.fleet_deploy` and the secret-free, dataclass-first decision surface
in :mod:`mac.agent_provider`.

Design contract (why this exists)
---------------------------------
The HGX-backed surge autoscaler (the parent task) needs a legible, testable
way to talk to the provider without leaking the well-known dark spots of
ad-hoc CLI shelling:

- **Immutable-ID addressing only.** Every lifecycle operation (``status``,
  ``ssh``, ``stop``, ``resume``, ``delete``) addresses a session by its
  immutable provider session ID. A human-facing display *name* is never used
  to select a session directly; :func:`HgxProvider.resolve_session_id` refuses
  when a name maps to zero or multiple sessions and only proceeds when it
  resolves to a single unique ID. This removes the "operated on the wrong box
  because two sessions shared a name" failure mode.
- **No ``hgx info``, ever.** ``hgx info`` can echo a fallback bootstrap
  password on stdout. This adapter never invokes it, and it never stores raw
  provider stdout that could carry a credential in any returned/observable
  structure. Results are secret-free dataclasses; any credential-bearing field
  is reduced to an env-var *name* plus a presence flag, mirroring
  :meth:`mac.agent_provider.ProviderDecision.observable`.
- **HGX login is interactive, not token provisioning.** HGX owns its own
  browser-backed session.  When it expires, the operator runs ``hgx login``
  once in the account that runs HGX; MAC neither requests nor stores an HGX
  API token.
- **``standard-dind`` is an explicit path.** OpenShell / Docker execution needs
  the ``standard-dind`` flavor; creating one is a first-class, parameterized
  option rather than a magic string callers must remember.
- **Real SSH attestation.** ``attest_ssh`` executes a nonce-bearing remote
  command through the current ``hgx ssh <id> -- <command>`` contract. Merely
  finding a session in ``hgx list`` or parsing an endpoint is never treated as
  reachability proof.

It is intentionally stdlib-only and does not import anything heavier than
:mod:`mac.fleet_deploy`, so it is unit-testable in mac's own venv.
"""

from __future__ import annotations

import json
import re
import secrets
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from mac.fleet_deploy import SshTarget, parse_ssh_target

__all__ = [
    "HGX_PROVIDER_SCHEMA",
    "STANDARD_DIND_FLAVOR",
    "HGX_LOGIN_GUIDANCE",
    "HgxError",
    "HgxCommandError",
    "HgxSessionNotFoundError",
    "HgxAmbiguousSessionError",
    "HgxSession",
    "HgxSshEndpoint",
    "HgxProvider",
]

# Versioned schema constant for any persisted/emitted structures.
HGX_PROVIDER_SCHEMA = "mac.hgx_provider.v1"

# The session flavor required when OpenShell/Docker (docker-in-docker)
# execution is needed. Kept as a named constant so callers never hand-type it.
STANDARD_DIND_FLAVOR = "standard-dind"

# HGX authenticates through its own browser-backed login session.  Keep this
# user-facing wording here so commands which surface an expired HGX session
# give operators the right recovery, rather than suggesting token setup.
HGX_LOGIN_GUIDANCE = (
    "HGX uses a one-time interactive `hgx login` browser bounce, not a MAC or "
    "provider API token. Run `hgx login` once in this operating account, then retry."
)

# hgx info can print a fallback bootstrap password; it is banned outright.
_FORBIDDEN_VERB = "info"

# Field names that may carry a credential in provider payloads. They are never
# copied into returned/observable structures; only presence is recorded.
_SECRET_FIELD_KEYS = (
    "password",
    "passwd",
    "fallback_password",
    "bootstrap_password",
    "root_password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "private_key",
    "ssh_private_key",
)


class HgxError(Exception):
    """Base class for all ``hgx`` provider adapter failures."""


class HgxCommandError(HgxError):
    """The ``hgx`` CLI exited non-zero or could not be executed.

    ``stderr`` is captured for diagnostics but is deliberately *not* echoed into
    any observable/persisted structure, since provider stderr can contain
    secret-bearing hints.
    """

    def __init__(
        self,
        message: str,
        *,
        argv: Sequence[str],
        returncode: Optional[int] = None,
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.argv: List[str] = list(argv)
        self.returncode = returncode
        self.stderr = stderr


class HgxSessionNotFoundError(HgxError):
    """No session matched the requested selector."""


class HgxAmbiguousSessionError(HgxError):
    """A display name mapped to more than one session; refuse to guess.

    Selecting by an ambiguous name is unsafe because the operation could hit the
    wrong box. Callers must disambiguate with the immutable session ID.
    """

    def __init__(self, name: str, session_ids: Sequence[str]) -> None:
        self.name = name
        self.session_ids: List[str] = list(session_ids)
        super().__init__(
            "session name %r is ambiguous; it maps to %d sessions: %s. "
            "Select by immutable session id instead."
            % (name, len(self.session_ids), ", ".join(sorted(self.session_ids)))
        )


def _has_secret_hint(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SECRET_FIELD_KEYS)


@dataclass(frozen=True)
class HgxSshEndpoint:
    """A validated SSH endpoint for an HGX session.

    ``target`` is always a validated :class:`mac.fleet_deploy.SshTarget`; the
    ``raw`` provider string is retained only for provenance and is guaranteed
    secret-free (an SSH target is host/user/port, never a credential).
    """

    target: SshTarget
    raw: str = ""

    @property
    def user_host(self) -> str:
        return self.target.user_host

    @property
    def port(self) -> Optional[int]:
        return self.target.port

    def observable(self) -> Dict[str, Any]:
        return {
            "user_host": self.target.user_host,
            "port": self.target.port,
        }


@dataclass(frozen=True)
class HgxSession:
    """A secret-free, structured view of one HGX session.

    The immutable ``session_id`` is the only safe selector for lifecycle
    operations. ``name`` is a human-facing display label and must never be used
    to address a session directly. Credential-bearing provider fields are never
    stored here; ``credential_env_var``/``credential_present`` expose only the
    env var *name* that would supply a credential plus a presence flag, exactly
    like :meth:`mac.agent_provider.ProviderDecision.observable`.
    """

    session_id: str
    name: str = ""
    flavor: str = ""
    state: str = ""
    ssh: Optional[HgxSshEndpoint] = None
    credential_env_var: Optional[str] = None
    credential_present: bool = False
    scrubbed_fields: List[str] = field(default_factory=list)

    @property
    def is_dind(self) -> bool:
        return self.flavor == STANDARD_DIND_FLAVOR

    def observable(self) -> Dict[str, Any]:
        """Secret-free dict suitable for logs, evidence and observability."""
        return {
            "schema": HGX_PROVIDER_SCHEMA,
            "session_id": self.session_id,
            "name": self.name or None,
            "flavor": self.flavor or None,
            "state": self.state or None,
            "ssh": self.ssh.observable() if self.ssh else None,
            "credential_env_var": self.credential_env_var,
            "credential_present": self.credential_present,
            "scrubbed_fields": list(self.scrubbed_fields),
        }


# hgx ssh / status / list human output may carry an SSH endpoint in a few
# shapes; capture the target token after an "ssh"/"host"/"endpoint" label or a
# bare "ssh user@host -p 2201" invocation.
_SSH_LABELLED = re.compile(
    r"(?:ssh\s+target|ssh\s+endpoint|endpoint|host)\s*[:=]\s*(?P<value>\S+)",
    re.IGNORECASE,
)
_SSH_INVOCATION = re.compile(
    r"\bssh\b(?P<args>(?:\s+-\S+(?:\s+\S+)?|\s+[^\s-]\S*)+)",
    re.IGNORECASE,
)


def _parse_ssh_from_text(text: str) -> Optional[HgxSshEndpoint]:
    """Extract and validate an SSH endpoint from free-form ``hgx`` output."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        labelled = _SSH_LABELLED.search(stripped)
        if labelled:
            candidate = labelled.group("value")
            endpoint = _try_endpoint(candidate)
            if endpoint is not None:
                return endpoint
        invocation = _SSH_INVOCATION.search(stripped)
        if invocation:
            endpoint = _endpoint_from_ssh_args(invocation.group("args"))
            if endpoint is not None:
                return endpoint
    return None


def _endpoint_from_ssh_args(args: str) -> Optional[HgxSshEndpoint]:
    tokens = shlex.split(args)
    port: Optional[int] = None
    host_token: Optional[str] = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "-p" and index + 1 < len(tokens) and tokens[index + 1].isdigit():
            port = int(tokens[index + 1])
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        host_token = token
        index += 1
    if host_token is None:
        return None
    return _try_endpoint(host_token, port=port)


def _try_endpoint(candidate: str, *, port: Optional[int] = None) -> Optional[HgxSshEndpoint]:
    try:
        target = parse_ssh_target(candidate, port=port)
    except ValueError:
        return None
    return HgxSshEndpoint(target=target, raw=candidate)


class HgxProvider:
    """Structured, subprocess-based wrapper around the ``hgx`` CLI.

    All methods shell out to ``hgx`` with a fixed argv (never through a shell),
    request JSON when the verb supports it, and translate output into secret-free
    dataclasses.
    """

    def __init__(
        self,
        *,
        binary: str = "hgx",
        timeout: float = 120.0,
        env: Optional[Mapping[str, str]] = None,
        credential_env_var: Optional[str] = None,
    ) -> None:
        self._binary = binary
        self._timeout = timeout
        self._env = dict(env) if env is not None else None
        # Env var name (never value) that supplies a session credential, if any.
        self._credential_env_var = credential_env_var

    # -- subprocess plumbing -------------------------------------------------
    def _run(self, args: Sequence[str]) -> str:
        """Run ``hgx <args>`` and return stdout, guarding the banned verb."""
        verb = next((arg for arg in args if not arg.startswith("-")), "")
        if verb == _FORBIDDEN_VERB:
            raise HgxError("hgx info is forbidden: it can echo a fallback password")
        argv = [self._binary, *args]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
                env=self._env,
            )
        except FileNotFoundError as exc:
            raise HgxCommandError("hgx binary %r not found" % self._binary, argv=argv) from exc
        except subprocess.TimeoutExpired as exc:
            raise HgxCommandError(
                "hgx %s timed out after %ss" % (verb, self._timeout),
                argv=argv,
            ) from exc
        if completed.returncode != 0:
            message = "hgx %s failed (exit %d)" % (verb, completed.returncode)
            stderr = completed.stderr or ""
            auth_markers = (
                "not authenticated",
                "login token expired",
                "session revoked",
                "run: hgx login",
            )
            if any(marker in stderr.lower() for marker in auth_markers):
                message = "%s. %s" % (message, HGX_LOGIN_GUIDANCE)
            raise HgxCommandError(
                message,
                argv=argv,
                returncode=completed.returncode,
                stderr=stderr,
            )
        return completed.stdout or ""

    # -- payload -> dataclass ------------------------------------------------
    def _session_from_payload(self, payload: Mapping[str, Any]) -> HgxSession:
        session_id = _first_str(payload, ("id", "session_id", "sessionId", "uuid"))
        if not session_id:
            raise HgxError("hgx session payload is missing an immutable id")
        name = _first_str(payload, ("name", "display_name", "displayName", "label"))
        flavor = _first_str(payload, ("agent_type", "agentType", "flavor", "type", "image", "kind"))
        state = _first_str(payload, ("state", "status", "phase"))

        scrubbed = sorted(key for key in payload if _has_secret_hint(str(key)))
        credential_present = bool(scrubbed) or self._credential_env_var is not None

        ssh = self._ssh_from_payload(payload)

        return HgxSession(
            session_id=session_id,
            name=name,
            flavor=flavor,
            state=state,
            ssh=ssh,
            credential_env_var=self._credential_env_var,
            credential_present=credential_present,
            scrubbed_fields=scrubbed,
        )

    def _ssh_from_payload(self, payload: Mapping[str, Any]) -> Optional[HgxSshEndpoint]:
        port = _coerce_port(payload)
        raw = _first_str(payload, ("ssh", "ssh_target", "sshTarget", "endpoint", "host", "address"))
        if raw:
            endpoint = _try_endpoint(raw, port=port)
            if endpoint is not None:
                return endpoint
        # Fall back to a user + host composition if provided separately.
        host = _first_str(payload, ("hostname", "ip", "ipv4"))
        user = _first_str(payload, ("user", "username", "login"))
        if host:
            candidate = "%s@%s" % (user, host) if user else host
            return _try_endpoint(candidate, port=port)
        return None

    def _sessions_from_output(self, stdout: str) -> List[HgxSession]:
        payload = _loads_or_none(stdout)
        rows = _iter_session_payloads(payload)
        if rows is not None:
            return [self._session_from_payload(row) for row in rows]
        # Non-JSON output: parse a single-session SSH endpoint at best-effort.
        return []

    # -- verbs ---------------------------------------------------------------
    def create(
        self,
        *,
        flavor: str,
        name: Optional[str] = None,
        extra_args: Optional[Sequence[str]] = None,
    ) -> HgxSession:
        """Create a session of ``flavor`` and return its structured view."""
        # hgx 0.9 uses a global --json flag and names the session execution
        # shape --type. Keep ``flavor`` in this Python API for compatibility
        # with callers and persisted HgxSession records.
        args: List[str] = ["--json", "create", "--type", flavor]
        if name:
            args += ["--name", name]
        if extra_args:
            args += list(extra_args)
        stdout = self._run(args)
        payload = _loads_or_none(stdout)
        row = _single_session_payload(payload)
        if row is None:
            raise HgxError("hgx create did not return a JSON session object")
        return self._session_from_payload(row)

    def create_standard_dind(
        self,
        *,
        name: Optional[str] = None,
        extra_args: Optional[Sequence[str]] = None,
    ) -> HgxSession:
        """Create a ``standard-dind`` session (OpenShell/Docker execution)."""
        session = self.create(flavor=STANDARD_DIND_FLAVOR, name=name, extra_args=extra_args)
        if session.flavor and not session.is_dind:
            raise HgxError(
                "requested standard-dind but provider returned flavor %r" % session.flavor
            )
        return session

    def list(self) -> List[HgxSession]:
        """List all sessions as structured, secret-free views."""
        stdout = self._run(["--json", "list"])
        return self._sessions_from_output(stdout)

    def status(self, session_id: str) -> HgxSession:
        """Return status for the session addressed by its immutable ID."""
        sid = _require_session_id(session_id)
        stdout = self._run(["--json", "status", sid])
        payload = _loads_or_none(stdout)
        row = _single_session_payload(payload)
        if row is None:
            raise HgxSessionNotFoundError("no hgx session with id %r" % sid)
        session = self._session_from_payload(row)
        if session.session_id != sid:
            raise HgxSessionNotFoundError(
                "hgx status returned id %r for requested %r" % (session.session_id, sid)
            )
        return session

    def ssh_target(self, session_id: str) -> HgxSshEndpoint:
        """Return a structured endpoint when current status exposes one.

        Current hgx versions no longer advertise ``ssh --print`` as a public
        endpoint-discovery contract. Call :meth:`attest_ssh` when the caller
        needs proof that the session is actually reachable.
        """
        sid = _require_session_id(session_id)
        endpoint = self.status(sid).ssh
        if endpoint is None:
            raise HgxError(
                "hgx status did not expose an SSH target for session %r; "
                "use attest_ssh() to prove reachability" % sid
            )
        return endpoint

    def run_ssh_command(self, session_id: str, command: Sequence[str]) -> str:
        """Run a non-interactive command through a session's real SSH path.

        The immutable session ID and explicit ``--`` separator are always
        supplied by this adapter. A command is required so callers can never
        accidentally open an interactive SSH session and hang a controller.
        """

        sid = _require_session_id(session_id)
        if isinstance(command, (str, bytes)) or not command:
            raise HgxError("hgx ssh requires a non-empty argv sequence")
        argv: List[str] = []
        for item in command:
            if not isinstance(item, str) or not item:
                raise HgxError("hgx ssh command items must be non-empty strings")
            argv.append(item)
        return self._run(["ssh", sid, "--", *argv])

    def attest_ssh(self, session_id: str) -> str:
        """Prove real SSH command execution and return the immutable session ID.

        A successful CLI exit alone is insufficient: hgx may print local
        connection diagnostics before SSH starts. The remote side must echo an
        unpredictable marker exactly, otherwise attestation fails closed.
        """

        sid = _require_session_id(session_id)
        marker = "mac-hgx-ssh-attest-" + secrets.token_hex(16)
        stdout = self.run_ssh_command(sid, ["printf", marker])
        if marker not in stdout.splitlines():
            raise HgxError("hgx ssh attestation marker was not returned for session %r" % sid)
        return sid

    def stop(self, session_id: str) -> str:
        """Stop the session addressed by its immutable ID; returns its ID."""
        sid = _require_session_id(session_id)
        self._run(["stop", sid])
        return sid

    def resume(self, session_id: str) -> str:
        """Resume the session addressed by its immutable ID; returns its ID."""
        sid = _require_session_id(session_id)
        self._run(["resume", sid])
        return sid

    def delete(self, session_id: str) -> str:
        """Delete the session addressed by its immutable ID; returns its ID."""
        sid = _require_session_id(session_id)
        self._run(["delete", sid])
        return sid

    # -- name -> immutable id resolver --------------------------------------
    def resolve_session_id(self, name: str) -> str:
        """Resolve a display ``name`` to a single immutable session ID.

        Raises :class:`HgxSessionNotFoundError` when no session matches and
        :class:`HgxAmbiguousSessionError` when more than one matches. Never
        guesses; the caller must supply an unambiguous name (or use the ID
        directly).
        """
        target = (name or "").strip()
        if not target:
            raise HgxSessionNotFoundError("a non-empty session name is required")
        matches = [s.session_id for s in self.list() if s.name == target]
        # An exact session-id match is unambiguous even if a name collides.
        if not matches:
            id_matches = [s.session_id for s in self.list() if s.session_id == target]
            if len(id_matches) == 1:
                return id_matches[0]
            raise HgxSessionNotFoundError("no hgx session named %r" % target)
        if len(set(matches)) > 1:
            raise HgxAmbiguousSessionError(target, sorted(set(matches)))
        return matches[0]


# -- module-level helpers ---------------------------------------------------
def _require_session_id(session_id: str) -> str:
    sid = (session_id or "").strip()
    if not sid:
        raise HgxSessionNotFoundError("an immutable session id is required")
    return sid


def _first_str(payload: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int,)) and not isinstance(value, bool):
            return str(value)
    return ""


def _coerce_port(payload: Mapping[str, Any]) -> Optional[int]:
    for key in ("port", "ssh_port", "sshPort"):
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def _loads_or_none(text: str) -> Any:
    stripped = (text or "").strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except (TypeError, ValueError):
        return None


def _iter_session_payloads(payload: Any) -> Optional[List[Mapping[str, Any]]]:
    if payload is None:
        return None
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, Mapping)]
    if isinstance(payload, Mapping):
        # Current ``hgx --json status <id>`` wraps the provider record with
        # restore and event details.
        if "session" in payload:
            session = payload.get("session")
            return [session] if isinstance(session, Mapping) else []
        for key in ("sessions", "items", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, Mapping)]
        # A single session object.
        return [payload]
    return None


def _single_session_payload(payload: Any) -> Optional[Mapping[str, Any]]:
    rows = _iter_session_payloads(payload)
    if not rows:
        return None
    return rows[0]
