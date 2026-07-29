"""Secret-free remote-execution interface for all fleet callers.

This module defines the single, provider-neutral surface every fleet caller
uses to run commands and move files on a remote agent.  It intentionally does
not re-derive SSH routes or option flags: route resolution and argv
construction already live in :mod:`mac.fleet_ssh`
(:class:`~mac.fleet_ssh.FleetSshSpec`, :func:`~mac.fleet_ssh.resolve_fleet_ssh`,
``_route_options``, :func:`~mac.fleet_ssh.ssh_argv`,
:func:`~mac.fleet_ssh.scp_argv`, :func:`~mac.fleet_ssh.route_argv`).  The SSH
adapter here is a thin, well-typed shell over those helpers.

Design contract
---------------
* :class:`RemoteEndpoint` is an immutable, secret-free identity plus a frozen
  capability set so callers *feature-test* rather than branch on provider type.
* :class:`RemoteTransport` is the provider-neutral protocol.  ``run`` takes an
  ``argv`` sequence and **never** joins it into a shell string; a separate,
  explicitly named :meth:`RemoteTransport.run_shell` escape hatch exists for the
  few callers that genuinely need remote shell semantics.
* Every operation takes a bounded timeout with a documented default; the SSH
  adapter always propagates a concrete timeout to ``subprocess``.
* Failures map into a typed hierarchy with a stable ``failure_class`` taxonomy.
* :meth:`RemoteTransport.attest` runs an unpredictable marker command and fails
  closed unless the exact marker is echoed back, mirroring
  :meth:`mac.hgx_provider.HgxProvider.attest_ssh`.
* Strict endpoint verification is never weakened here: host-key policy comes
  only from the resolved :class:`~mac.fleet_ssh.FleetSshSpec` via the
  ``fleet_ssh`` option builders.
"""

from __future__ import annotations

import secrets
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    FrozenSet,
    Optional,
    Protocol,
    Sequence,
    Union,
    runtime_checkable,
)

from mac.fleet_ssh import FleetSshSpec, scp_argv, ssh_argv

__all__ = [
    "REMOTE_SESSION_SCHEMA",
    "DEFAULT_OPERATION_TIMEOUT",
    "DEFAULT_CONNECT_TIMEOUT",
    "Capability",
    "RemoteEndpoint",
    "RemoteResult",
    "RemoteTransport",
    "RemoteTelemetry",
    "RemoteError",
    "RemoteAuthError",
    "RemoteHostKeyError",
    "RemoteConnectTimeout",
    "RemoteOperationTimeout",
    "RemoteTransportDead",
    "RemoteCommandError",
    "SshTransport",
]

# Versioned schema constant for any persisted/emitted structures.
REMOTE_SESSION_SCHEMA = "mac.remote_session.v1"

# Bounded default operation timeout (seconds).  Every op is bounded; callers may
# tighten it per call but can never disable it (see ``_bounded_timeout``).
DEFAULT_OPERATION_TIMEOUT: float = 120.0

# Default TCP connect timeout handed to the fleet_ssh option builder.
DEFAULT_CONNECT_TIMEOUT: int = 10


# Field-name hints that may carry credential material.  Mirrors the
# ``_has_secret_hint`` discipline in :mod:`mac.hgx_provider`: such fields are
# never copied into observable/persisted structures, only their presence.
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
    "identity_file",
    "identity_ref",
)


def _has_secret_hint(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SECRET_FIELD_KEYS)


# --------------------------------------------------------------------------- #
# Capabilities
# --------------------------------------------------------------------------- #
class Capability:
    """Named, feature-test capabilities a transport may advertise.

    Callers feature-test (``Capability.FILE_PUT in endpoint.capabilities``)
    instead of branching on the provider type, so new providers slot in without
    touching call sites.
    """

    ARGV_EXEC = "argv_exec"
    FILE_PUT = "file_put"
    FILE_GET = "file_get"
    STREAMING_TRANSFER = "streaming_transfer"
    MULTIPLEX = "multiplex"


# --------------------------------------------------------------------------- #
# Typed failure hierarchy with a stable taxonomy
# --------------------------------------------------------------------------- #
class RemoteError(Exception):
    """Base class for all remote-session failures.

    ``failure_class`` is the stable taxonomy string callers and telemetry key
    on; it never changes value for a given subclass.  ``stderr`` is captured for
    diagnostics but is deliberately *not* echoed into any observable structure
    (provider stderr can carry secret-bearing hints), matching
    :class:`mac.hgx_provider.HgxCommandError`.
    """

    failure_class = "remote_error"

    def __init__(
        self,
        message: str,
        *,
        endpoint: Optional["RemoteEndpoint"] = None,
        returncode: Optional[int] = None,
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.returncode = returncode
        self.stderr = stderr


class RemoteAuthError(RemoteError):
    """Authentication was rejected by the remote endpoint."""

    failure_class = "auth"


class RemoteHostKeyError(RemoteError):
    """Host-key verification failed; the endpoint could not be trusted."""

    failure_class = "host_key"


class RemoteConnectTimeout(RemoteError):
    """The transport could not establish a connection in time."""

    failure_class = "connect_timeout"


class RemoteOperationTimeout(RemoteError):
    """A bounded operation exceeded its timeout."""

    failure_class = "operation_timeout"


class RemoteTransportDead(RemoteError):
    """The transport binary/channel was unusable (missing, killed, refused)."""

    failure_class = "transport_dead"


class RemoteCommandError(RemoteError):
    """The remote command ran but exited non-zero."""

    failure_class = "remote_nonzero"


# Stable ordered taxonomy of every failure class this module can report.
FAILURE_CLASSES: FrozenSet[str] = frozenset(
    {
        RemoteAuthError.failure_class,
        RemoteHostKeyError.failure_class,
        RemoteConnectTimeout.failure_class,
        RemoteOperationTimeout.failure_class,
        RemoteTransportDead.failure_class,
        RemoteCommandError.failure_class,
    }
)


# --------------------------------------------------------------------------- #
# Endpoint identity + capabilities
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RemoteEndpoint:
    """Resolved, secret-free endpoint for one logical fleet identity.

    For SSH this wraps the resolved :class:`~mac.fleet_ssh.FleetSshSpec`.  No
    field, ``repr``, or :meth:`to_dict` output ever contains secret material:
    the spec itself already excludes private-key bytes, and identity *paths*
    (``identity_file``/``identity_ref``) are scrubbed from observable output via
    the ``_has_secret_hint`` discipline.
    """

    logical_identity: str
    address: str
    transport: str
    capabilities: FrozenSet[str]
    spec: Optional[FleetSshSpec] = None

    @classmethod
    def for_ssh(
        cls,
        spec: FleetSshSpec,
        *,
        extra_capabilities: Sequence[str] = (),
    ) -> "RemoteEndpoint":
        """Build an SSH endpoint from a resolved :class:`FleetSshSpec`."""

        caps = {
            Capability.ARGV_EXEC,
            Capability.FILE_PUT,
            Capability.FILE_GET,
            Capability.STREAMING_TRANSFER,
        }
        caps.update(extra_capabilities)
        address = spec.target
        if spec.port is not None:
            address = "%s:%d" % (spec.target, spec.port)
        return cls(
            logical_identity=spec.agent,
            address=address,
            transport="ssh",
            capabilities=frozenset(caps),
            spec=spec,
        )

    @property
    def identity(self) -> str:
        """Compatibility name for the immutable resolved endpoint address."""

        return self.address

    @property
    def provider(self) -> str:
        """Compatibility name for the connection adapter."""

        return self.transport

    def has(self, capability: str) -> bool:
        """Feature-test a capability instead of branching on provider type."""

        return capability in self.capabilities

    def to_dict(self) -> Dict[str, Any]:
        """Return a secret-free, JSON-serialisable view of the endpoint."""

        payload: Dict[str, Any] = {
            "schema": REMOTE_SESSION_SCHEMA,
            "logical_identity": self.logical_identity,
            "address": self.address,
            "transport": self.transport,
            "capabilities": sorted(self.capabilities),
        }
        if self.spec is not None:
            spec_view = {
                key: value
                for key, value in self.spec.to_dict().items()
                if not _has_secret_hint(str(key))
            }
            payload["spec"] = spec_view
        return payload

    def __repr__(self) -> str:  # pragma: no cover - trivial, but secret-free
        return "RemoteEndpoint(logical_identity=%r, address=%r, transport=%r, capabilities=%r)" % (
            self.logical_identity,
            self.address,
            self.transport,
            sorted(self.capabilities),
        )


# --------------------------------------------------------------------------- #
# Result value object
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RemoteResult:
    """Outcome of a remote operation.

    Carries the process ``returncode``, ``stdout``/``stderr``, and the endpoint
    *identity* string.  It never carries credentials.
    """

    returncode: int
    stdout: str
    stderr: str
    endpoint: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": REMOTE_SESSION_SCHEMA,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "endpoint": self.endpoint,
        }


# --------------------------------------------------------------------------- #
# Telemetry
# --------------------------------------------------------------------------- #
@dataclass
class RemoteTelemetry:
    """Injectable counters recording transport activity.

    Kept as a plain, mutable object so tests can pass one in and assert on the
    recorded values.  ``failure_classes`` maps each stable taxonomy string to a
    count.
    """

    operations: int = 0
    pool_hits: int = 0
    reconnects: int = 0
    timeouts: int = 0
    failure_classes: Dict[str, int] = field(default_factory=dict)

    def record_operation(self) -> None:
        self.operations += 1

    def record_pool_hit(self) -> None:
        self.pool_hits += 1

    def record_reconnect(self) -> None:
        self.reconnects += 1

    def record_timeout(self) -> None:
        self.timeouts += 1

    def record_failure(self, failure_class: str) -> None:
        self.failure_classes[failure_class] = self.failure_classes.get(failure_class, 0) + 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "operations": self.operations,
            "pool_hits": self.pool_hits,
            "reconnects": self.reconnects,
            "timeouts": self.timeouts,
            "failure_classes": dict(self.failure_classes),
        }


# --------------------------------------------------------------------------- #
# Transport protocol
# --------------------------------------------------------------------------- #
@runtime_checkable
class RemoteTransport(Protocol):
    """Provider-neutral remote-execution surface.

    Implementations MUST honour these invariants:

    * ``run`` takes an ``argv`` *sequence* and never joins it into a shell
      string implicitly.  Shell semantics require the explicit
      :meth:`run_shell` escape hatch.
    * Every method accepts a bounded ``timeout`` with a documented default and
      always propagates a concrete bound to the underlying transport.
    * :meth:`attest` proves live execution and fails closed on a missing or
      partial marker.
    """

    endpoint: RemoteEndpoint

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: Union[bytes, str, None] = None,
        timeout: Optional[float] = None,
    ) -> RemoteResult:
        """Run ``argv`` remotely without any implicit shell interpolation."""

    def run_shell(
        self,
        script: str,
        *,
        stdin: Union[bytes, str, None] = None,
        timeout: Optional[float] = None,
    ) -> RemoteResult:
        """Explicit escape hatch: run a remote shell ``script``."""

    def put(
        self,
        local_path: str,
        remote_path: str,
        *,
        timeout: Optional[float] = None,
    ) -> RemoteResult:
        """Copy a local file to the remote endpoint."""

    def get(
        self,
        remote_path: str,
        local_path: str,
        *,
        timeout: Optional[float] = None,
    ) -> RemoteResult:
        """Copy a remote file to the local host."""

    def open_read(self, remote_path: str, *, timeout: Optional[float] = None) -> bytes:
        """Stream a remote file's bytes into memory."""

    def open_write(
        self,
        remote_path: str,
        data: Union[bytes, str],
        *,
        timeout: Optional[float] = None,
    ) -> RemoteResult:
        """Stream bytes into a remote file."""

    def attest(self, *, timeout: Optional[float] = None) -> str:
        """Prove live remote execution; fail closed on a missing marker."""

    def close(self) -> None:
        """Release any transport resources."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _bounded_timeout(timeout: Optional[float]) -> float:
    """Resolve an operation timeout to a concrete, positive bound.

    ``None`` selects :data:`DEFAULT_OPERATION_TIMEOUT`.  A non-positive value is
    rejected so no operation can run unbounded.
    """

    if timeout is None:
        return DEFAULT_OPERATION_TIMEOUT
    value = float(timeout)
    if value <= 0:
        raise ValueError("operation timeout must be positive")
    return value


def _stderr_failure_class(stderr: str) -> Optional[type]:
    """Classify an SSH failure from stderr into a taxonomy error type."""

    lowered = (stderr or "").lower()
    if (
        "host key verification failed" in lowered
        or "remote host identification has changed" in lowered
    ):
        return RemoteHostKeyError
    if (
        "permission denied" in lowered
        or "authentication failed" in lowered
        or "no such identity" in lowered
        or "too many authentication failures" in lowered
    ):
        return RemoteAuthError
    if "connection timed out" in lowered or "operation timed out" in lowered:
        return RemoteConnectTimeout
    if (
        "connection refused" in lowered
        or "connection closed" in lowered
        or "connection reset" in lowered
        or "could not resolve hostname" in lowered
        or "no route to host" in lowered
        or "broken pipe" in lowered
    ):
        return RemoteTransportDead
    return None


# --------------------------------------------------------------------------- #
# SSH adapter
# --------------------------------------------------------------------------- #
class SshTransport:
    """:class:`RemoteTransport` over OpenSSH, built on :mod:`mac.fleet_ssh`.

    All argv/option construction is delegated to
    :func:`mac.fleet_ssh.ssh_argv`/:func:`mac.fleet_ssh.scp_argv`, so strict
    host-key policy and known_hosts handling come only from the resolved
    :class:`~mac.fleet_ssh.FleetSshSpec` and are never weakened here.
    """

    def __init__(
        self,
        spec: FleetSshSpec,
        *,
        default_timeout: float = DEFAULT_OPERATION_TIMEOUT,
        connect_timeout: int = DEFAULT_CONNECT_TIMEOUT,
        telemetry: Optional[RemoteTelemetry] = None,
    ) -> None:
        self._spec = spec
        self.endpoint = RemoteEndpoint.for_ssh(spec)
        self._default_timeout = _bounded_timeout(default_timeout)
        self._connect_timeout = max(1, int(connect_timeout))
        self.telemetry = telemetry if telemetry is not None else RemoteTelemetry()

    # -- subprocess plumbing ------------------------------------------------
    def _execute(
        self,
        argv: Sequence[str],
        *,
        stdin: Union[bytes, str, None],
        timeout: Optional[float],
    ) -> RemoteResult:
        bound = timeout if timeout is not None else self._default_timeout
        bound = _bounded_timeout(bound)
        input_bytes: Optional[bytes]
        if stdin is None:
            input_bytes = None
        elif isinstance(stdin, str):
            input_bytes = stdin.encode("utf-8")
        else:
            input_bytes = bytes(stdin)
        completed = self._run_subprocess(argv, input_bytes=input_bytes, timeout=bound)
        stdout = _as_text(completed.stdout)
        stderr = _as_text(completed.stderr)
        return RemoteResult(
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            endpoint=self.endpoint.identity,
        )

    def _run_subprocess(
        self,
        argv: Sequence[str],
        *,
        input_bytes: Optional[bytes],
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]:
        self.telemetry.record_operation()
        try:
            completed = subprocess.run(
                list(argv),
                input=input_bytes,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            self.telemetry.record_failure(RemoteTransportDead.failure_class)
            raise RemoteTransportDead(
                "ssh transport binary not found",
                endpoint=self.endpoint,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            self.telemetry.record_timeout()
            self.telemetry.record_failure(RemoteOperationTimeout.failure_class)
            raise RemoteOperationTimeout(
                "remote operation timed out after %ss" % timeout,
                endpoint=self.endpoint,
            ) from exc
        stderr = _as_text(completed.stderr)
        if completed.returncode != 0:
            self._raise_for_returncode(completed.returncode, stderr)
        return completed

    def _raise_for_returncode(self, returncode: int, stderr: str) -> None:
        # OpenSSH uses exit code 255 for its own transport-level failures; the
        # specific class is disambiguated from stderr.  Any other non-zero code
        # is the remote command's own exit status.
        error_type = (
            _stderr_failure_class(stderr) or RemoteTransportDead
            if returncode == 255
            else RemoteCommandError
        )
        self.telemetry.record_failure(error_type.failure_class)
        raise error_type(
            "remote operation failed (exit %d)" % returncode,
            endpoint=self.endpoint,
            returncode=returncode,
            stderr=stderr,
        )

    # -- exec ---------------------------------------------------------------
    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: Union[bytes, str, None] = None,
        timeout: Optional[float] = None,
    ) -> RemoteResult:
        """Run ``argv`` remotely, never joining it into a shell string.

        The argv is quoted element-by-element with :func:`shlex.quote` so the
        remote shell reconstructs exactly the intended tokens; callers never get
        implicit word-splitting or interpolation.
        """

        if isinstance(argv, (str, bytes)):
            raise TypeError("run() requires an argv sequence, not a string")
        items = [str(item) for item in argv]
        if not items:
            raise ValueError("run() requires a non-empty argv")
        remote_command = " ".join(shlex.quote(item) for item in items)
        full_argv = ssh_argv(
            self._spec,
            remote_command,
            connect_timeout=self._connect_timeout,
        )
        return self._execute(full_argv, stdin=stdin, timeout=timeout)

    def run_shell(
        self,
        script: str,
        *,
        stdin: Union[bytes, str, None] = None,
        timeout: Optional[float] = None,
    ) -> RemoteResult:
        """Explicit escape hatch for callers needing remote shell semantics.

        Unlike :meth:`run`, ``script`` is handed to the remote shell verbatim
        (e.g. ``soul_snapshot`` building ``if [ -f ... ]; then ...`` probes).
        This is deliberately separate and loudly named so shell interpolation is
        always an explicit caller choice, never a default of :meth:`run`.
        """

        if not isinstance(script, str):
            raise TypeError("run_shell() requires a shell script string")
        full_argv = ssh_argv(
            self._spec,
            script,
            connect_timeout=self._connect_timeout,
        )
        return self._execute(full_argv, stdin=stdin, timeout=timeout)

    # -- file transfer ------------------------------------------------------
    def put(
        self,
        local_path: str,
        remote_path: str,
        *,
        timeout: Optional[float] = None,
    ) -> RemoteResult:
        """Copy ``local_path`` to ``remote_path`` on the endpoint via scp."""

        destination = "%s:%s" % (self._spec.target, shlex.quote(remote_path))
        full_argv = scp_argv(
            self._spec,
            [str(local_path)],
            destination,
            connect_timeout=self._connect_timeout,
        )
        return self._execute(full_argv, stdin=None, timeout=timeout)

    def get(
        self,
        remote_path: str,
        local_path: str,
        *,
        timeout: Optional[float] = None,
    ) -> RemoteResult:
        """Copy ``remote_path`` from the endpoint to ``local_path`` via scp."""

        source = "%s:%s" % (self._spec.target, shlex.quote(remote_path))
        full_argv = scp_argv(
            self._spec,
            [source],
            str(local_path),
            connect_timeout=self._connect_timeout,
        )
        return self._execute(full_argv, stdin=None, timeout=timeout)

    def open_read(self, remote_path: str, *, timeout: Optional[float] = None) -> bytes:
        """Stream a remote file's bytes back through the exec channel."""

        bound = _bounded_timeout(timeout if timeout is not None else self._default_timeout)
        remote_command = " ".join(shlex.quote(item) for item in ("cat", "--", str(remote_path)))
        completed = self._run_subprocess(
            ssh_argv(
                self._spec,
                remote_command,
                connect_timeout=self._connect_timeout,
            ),
            input_bytes=None,
            timeout=bound,
        )
        return bytes(completed.stdout or b"")

    def open_write(
        self,
        remote_path: str,
        data: Union[bytes, str],
        *,
        timeout: Optional[float] = None,
    ) -> RemoteResult:
        """Stream ``data`` into a remote file through the exec channel."""

        payload = data if isinstance(data, (bytes, str)) else bytes(data)
        return self.run(
            ["tee", "--", str(remote_path)],
            stdin=payload,
            timeout=timeout,
        )

    # -- attestation --------------------------------------------------------
    def attest(self, *, timeout: Optional[float] = None) -> str:
        """Prove live remote execution and return the endpoint identity.

        A successful SSH exit alone is insufficient: the remote side must echo
        an unpredictable marker back exactly, otherwise attestation fails
        closed.  Same contract as
        :meth:`mac.hgx_provider.HgxProvider.attest_ssh`.
        """

        marker = "mac-remote-attest-" + secrets.token_hex(16)
        result = self.run(["printf", "%s", marker], timeout=timeout)
        if marker not in result.stdout.splitlines() and result.stdout.strip() != marker:
            raise RemoteError(
                "remote attestation marker was not echoed back",
                endpoint=self.endpoint,
            )
        return self.endpoint.identity

    def close(self) -> None:
        """No persistent channel is held; present for protocol completeness."""

        return None


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


# Static protocol conformance check (documents intent; costs nothing at import).
_: type = SshTransport
