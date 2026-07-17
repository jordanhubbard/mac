"""External, credential-free OpenShell certification station primitive.

This module deliberately does not import the landing service and does not know
how to write to the hub.  It is the outer station boundary: a trusted controller
constructs an exact job, this runner validates and executes it in OpenShell, and
the caller may later submit the captured result through a separately authorised
hub API.

The sandbox never receives a hub token, repository credential, landing
credential, mutable policy path, or planner-provided shell command.  The only
repository input accepted here is a content-addressed Git bundle.  OpenShell is
invoked with a freshly materialised, checksum-verified lockdown policy and a
digest-pinned image.  All result authority comes from exit codes observed by the
outer process; candidate code cannot post its own certification.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import signal
import stat
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, Tuple

import yaml


CERTIFICATION_JOB_SCHEMA = "mac.openshell_certification_job.v1"
CERTIFICATION_RESULT_SCHEMA = "mac.openshell_certification_result.v1"
CERTIFICATION_ISOLATION_SCHEMA = "mac.certification_isolation.v1"
CLEANUP_ALERT_SCHEMA = "mac.openshell_certification_cleanup_alert.v1"

_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TREE_RE = re.compile(r"^git-tree:[0-9a-f]{40}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_IMAGE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}$"
)
_REF_RE = re.compile(r"^refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]{0,240}$")
_SAFE_LAUNCH_ENV = frozenset(
    {"HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "SYSTEMROOT", "TMPDIR", "TZ"}
)
_POLICY_KEYS = frozenset(
    {"version", "filesystem_policy", "landlock", "process", "network_policies"}
)
_FILESYSTEM_POLICY_KEYS = frozenset({"include_workdir", "read_only", "read_write"})
_LANDLOCK_POLICY_KEYS = frozenset({"compatibility"})
_PROCESS_POLICY_KEYS = frozenset({"run_as_user", "run_as_group"})
_POLICY_WRITABLE_ROOTS = frozenset({"/dev", "/sandbox", "/tmp"})
_DEFAULT_LAUNCH_ENV = {"HOME": "/tmp", "PATH": "/usr/bin:/bin"}
_DEFAULT_SANDBOX_PATH = "/opt/mac-venv/bin:/usr/local/bin:/usr/bin:/bin"
_OUTPUT_LIMIT = 16_000
_CERTIFIER_COMMAND_PREFIX = "/opt/mac-certifier/bin/"


class OpenShellCertificationError(RuntimeError):
    """Base error for invalid or unenforceable certification work."""


class CertificationValidationError(OpenShellCertificationError):
    """The controller job cannot be proved safe enough to execute."""


class CertificationCleanupError(OpenShellCertificationError):
    """The sandbox could not be removed, so success must not escape."""

    def __init__(self, message: str, *, alert: "CleanupAlert") -> None:
        super().__init__(message)
        self.alert = alert


def validate_certifier_image_ref(value: Any) -> str:
    """Return one immutable certifier image reference or fail closed.

    Repository-contract admission and concrete job execution deliberately use
    this same primitive. Keeping digest-pin validation here prevents an
    apparently ready release gate from moving product WIP only for job
    preparation to reject the image later.
    """

    image_ref = str(value or "").strip()
    if not _IMAGE_RE.fullmatch(image_ref):
        raise CertificationValidationError(
            "certifier image must be an immutable name@sha256:digest reference"
        )
    return image_ref


def validate_certifier_controller_command(command: "ControllerCommand") -> None:
    """Require an immutable-image-owned certification command executable."""

    command.validate()
    executable = command.argv[0]
    if (
        not executable.startswith(_CERTIFIER_COMMAND_PREFIX)
        or posixpath.normpath(executable) != executable
        or executable.endswith("/")
    ):
        raise CertificationValidationError(
            "certification command executable must be image-owned under "
            "/opt/mac-certifier/bin/"
        )


@dataclass(frozen=True)
class CommandOutcome:
    argv: Tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class CommandRunner(Protocol):
    """Injected host command boundary used by unit tests and production."""

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout_seconds: float,
    ) -> CommandOutcome: ...


class SubprocessCommandRunner:
    """Bounded subprocess runner that terminates the complete process group."""

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout_seconds: float,
    ) -> CommandOutcome:
        command = tuple(str(item) for item in argv)
        proc = subprocess.Popen(
            list(command),
            env=dict(env),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=max(0.1, float(timeout_seconds)))
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (AttributeError, ProcessLookupError, PermissionError, OSError):
                proc.kill()
            stdout, stderr = proc.communicate()
        return CommandOutcome(
            command,
            124 if timed_out else int(proc.returncode),
            _clip(stdout),
            _clip(stderr),
            timed_out,
        )


@dataclass(frozen=True)
class CertificationPolicy:
    policy_id: str
    version: int
    checksum: str
    policy_text: str = field(repr=False)

    def validate(self) -> Mapping[str, Any]:
        _require_identifier(self.policy_id, "policy_id")
        if isinstance(self.version, bool) or int(self.version) < 1:
            raise CertificationValidationError("policy version must be positive")
        _require_sha256(self.checksum, "policy checksum")
        observed_checksum = _sha256_bytes(self.policy_text.encode("utf-8"))
        if observed_checksum != self.checksum:
            raise CertificationValidationError("policy checksum does not match policy text")
        if not self.policy_text or len(self.policy_text.encode("utf-8")) > 1024 * 1024:
            raise CertificationValidationError("certifier policy text size is invalid")
        try:
            parsed = yaml.safe_load(self.policy_text)
        except yaml.YAMLError as exc:
            raise CertificationValidationError("certifier policy is invalid YAML") from exc
        if not isinstance(parsed, dict):
            raise CertificationValidationError("certifier policy must be a YAML object")
        unknown = sorted(set(parsed) - _POLICY_KEYS)
        if unknown:
            raise CertificationValidationError(
                "certifier policy has unreviewed top-level keys: %s" % ", ".join(unknown)
            )
        if parsed.get("version") != 1:
            raise CertificationValidationError("certifier policy version must be exactly 1")
        if parsed.get("network_policies") != {}:
            raise CertificationValidationError(
                "certifier policy must disable all network egress"
            )
        landlock = parsed.get("landlock")
        if not isinstance(landlock, dict) or landlock.get("compatibility") != "hard_requirement":
            raise CertificationValidationError(
                "certifier policy must require hard Landlock enforcement"
            )
        if set(landlock) - _LANDLOCK_POLICY_KEYS:
            raise CertificationValidationError(
                "certifier policy has unreviewed Landlock controls"
            )
        process = parsed.get("process")
        if not isinstance(process, dict):
            raise CertificationValidationError("certifier policy requires a process section")
        user = str(process.get("run_as_user") or "").strip()
        group = str(process.get("run_as_group") or "").strip()
        if not user or user.lower() == "root" or user == "0":
            raise CertificationValidationError("certifier policy must run as a non-root user")
        if not group or group.lower() == "root" or group == "0":
            raise CertificationValidationError("certifier policy must run as a non-root group")
        if set(process) - _PROCESS_POLICY_KEYS:
            raise CertificationValidationError(
                "certifier policy has unreviewed process controls"
            )
        filesystem = parsed.get("filesystem_policy")
        if not isinstance(filesystem, dict) or filesystem.get("include_workdir") is not True:
            raise CertificationValidationError(
                "certifier policy must confine and include the sandbox workdir"
            )
        if set(filesystem) - _FILESYSTEM_POLICY_KEYS:
            raise CertificationValidationError(
                "certifier policy has unreviewed filesystem controls"
            )
        read_only = filesystem.get("read_only")
        if not isinstance(read_only, list) or not all(
            isinstance(item, str) and item.startswith("/") for item in read_only
        ):
            raise CertificationValidationError(
                "certifier policy read_only paths must be an explicit absolute-path list"
            )
        writable = filesystem.get("read_write")
        if not isinstance(writable, list) or not all(
            isinstance(item, str) and item.startswith("/") for item in writable
        ):
            raise CertificationValidationError(
                "certifier policy read_write paths must be an explicit absolute-path list"
            )
        unexpected_writable = sorted(set(writable) - _POLICY_WRITABLE_ROOTS)
        if unexpected_writable:
            raise CertificationValidationError(
                "certifier policy has unreviewed writable roots: %s"
                % ", ".join(unexpected_writable)
            )
        return parsed

    def identity(self) -> Mapping[str, Any]:
        return {
            "policy_id": self.policy_id,
            "version": int(self.version),
            "checksum": self.checksum,
        }


@dataclass(frozen=True)
class ControllerCommand:
    command_id: str
    argv: Tuple[str, ...]
    timeout_seconds: int = 900

    def validate(self) -> None:
        _require_identifier(self.command_id, "command_id")
        if not self.argv:
            raise CertificationValidationError("controller command argv is empty")
        if len(self.argv) > 256:
            raise CertificationValidationError("controller command argv is too large")
        for index, item in enumerate(self.argv):
            if not isinstance(item, str) or not item or "\x00" in item or len(item) > 8192:
                raise CertificationValidationError("controller command has an invalid argv item")
            if index == 0 and item.startswith("-"):
                raise CertificationValidationError("controller command executable is invalid")
        if isinstance(self.timeout_seconds, bool) or not 1 <= int(self.timeout_seconds) <= 3600:
            raise CertificationValidationError(
                "controller command timeout must be between 1 and 3600 seconds"
            )

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "command_id": self.command_id,
            "argv": list(self.argv),
            "timeout_seconds": int(self.timeout_seconds),
        }


@dataclass(frozen=True)
class OpenShellCertificationJob:
    job_id: str
    batch_id: str
    package_id: str
    plan_version: int
    epoch: int
    candidate_sha: str
    candidate_tree_digest: str
    assembly_base_sha: str
    landing_base_sha: str
    target_ref: str
    policy: CertificationPolicy
    image_ref: str
    bundle_path: Path = field(repr=False)
    bundle_digest: str
    controller_commands: Tuple[ControllerCommand, ...]
    lifecycle_timeout_seconds: int = 120

    @property
    def image_digest(self) -> str:
        return self.image_ref.rsplit("@", 1)[1]

    @property
    def commands_digest(self) -> str:
        return _sha256_json([item.to_dict() for item in self.controller_commands])

    def validate(self) -> None:
        for name, value in (
            ("job_id", self.job_id),
            ("batch_id", self.batch_id),
            ("package_id", self.package_id),
        ):
            _require_identifier(value, name)
        if isinstance(self.plan_version, bool) or int(self.plan_version) < 1:
            raise CertificationValidationError("plan_version must be positive")
        if isinstance(self.epoch, bool) or int(self.epoch) < 1:
            raise CertificationValidationError("epoch must be positive")
        for name, value in (
            ("candidate_sha", self.candidate_sha),
            ("assembly_base_sha", self.assembly_base_sha),
            ("landing_base_sha", self.landing_base_sha),
        ):
            if not _SHA40_RE.fullmatch(str(value or "")):
                raise CertificationValidationError("%s must be an exact lowercase Git SHA" % name)
        if not _TREE_RE.fullmatch(str(self.candidate_tree_digest or "")):
            raise CertificationValidationError(
                "candidate_tree_digest must be git-tree plus an exact lowercase Git SHA"
            )
        if not _REF_RE.fullmatch(str(self.target_ref or "")) or any(
            token in self.target_ref for token in ("..", "//", "@{")
        ):
            raise CertificationValidationError("target_ref must be a safe refs/heads ref")
        self.policy.validate()
        validate_certifier_image_ref(self.image_ref)
        _require_sha256(self.bundle_digest, "bundle digest")
        if not self.controller_commands:
            raise CertificationValidationError("certification requires controller commands")
        seen = set()
        for command in self.controller_commands:
            validate_certifier_controller_command(command)
            if command.command_id in seen:
                raise CertificationValidationError("controller command ids must be unique")
            seen.add(command.command_id)
        if (
            isinstance(self.lifecycle_timeout_seconds, bool)
            or not 5 <= int(self.lifecycle_timeout_seconds) <= 600
        ):
            raise CertificationValidationError(
                "lifecycle timeout must be between 5 and 600 seconds"
            )

    def identity(self) -> Mapping[str, Any]:
        return {
            "schema": CERTIFICATION_JOB_SCHEMA,
            "job_id": self.job_id,
            "batch_id": self.batch_id,
            "package_id": self.package_id,
            "plan_version": int(self.plan_version),
            "epoch": int(self.epoch),
            "candidate_sha": self.candidate_sha,
            "candidate_tree_digest": self.candidate_tree_digest,
            "assembly_base_sha": self.assembly_base_sha,
            "landing_base_sha": self.landing_base_sha,
            "target_ref": self.target_ref,
            "policy": self.policy.identity(),
            "image_ref": self.image_ref,
            "image_digest": self.image_digest,
            "bundle_digest": self.bundle_digest,
            "bundle_format": "git_bundle",
            "commands_digest": self.commands_digest,
            "controller_commands": [item.to_dict() for item in self.controller_commands],
        }

    @property
    def job_digest(self) -> str:
        return _sha256_json(self.identity())


@dataclass(frozen=True)
class CertificationCheckResult:
    command_id: str
    argv: Tuple[str, ...]
    returncode: int
    status: str
    stdout: str
    stderr: str
    timed_out: bool

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "command_id": self.command_id,
            "argv": list(self.argv),
            "returncode": int(self.returncode),
            "status": self.status,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": bool(self.timed_out),
        }


@dataclass(frozen=True)
class OpenShellCertificationResult:
    job_id: str
    job_digest: str
    batch_id: str
    package_id: str
    plan_version: int
    epoch: int
    candidate_sha: str
    candidate_tree_digest: str
    assembly_base_sha: str
    landing_base_sha: str
    target_ref: str
    status: str
    policy: Mapping[str, Any]
    image_ref: str
    image_digest: str
    bundle_digest: str
    commands_digest: str
    sandbox_name: str
    checks: Tuple[CertificationCheckResult, ...]
    isolation: Mapping[str, Any]
    started_at: str
    completed_at: str
    cleanup_status: str
    failure_class: str = ""
    failure_reason: str = ""
    result_digest: str = ""

    def _payload(self, *, include_digest: bool) -> Mapping[str, Any]:
        payload = {
            "schema": CERTIFICATION_RESULT_SCHEMA,
            "job_id": self.job_id,
            "job_digest": self.job_digest,
            "batch_id": self.batch_id,
            "package_id": self.package_id,
            "plan_version": int(self.plan_version),
            "epoch": int(self.epoch),
            "candidate_sha": self.candidate_sha,
            "candidate_tree_digest": self.candidate_tree_digest,
            "assembly_base_sha": self.assembly_base_sha,
            "landing_base_sha": self.landing_base_sha,
            "target_ref": self.target_ref,
            "status": self.status,
            "policy": dict(self.policy),
            "image_ref": self.image_ref,
            "image_digest": self.image_digest,
            "bundle_digest": self.bundle_digest,
            "commands_digest": self.commands_digest,
            "sandbox_name": self.sandbox_name,
            "checks": [item.to_dict() for item in self.checks],
            "isolation": dict(self.isolation),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "cleanup_status": self.cleanup_status,
            "failure_class": self.failure_class,
            "failure_reason": self.failure_reason,
        }
        if include_digest:
            payload["result_digest"] = self.result_digest
        return payload

    def with_digest(self) -> "OpenShellCertificationResult":
        return replace(self, result_digest=_sha256_json(self._payload(include_digest=False)))

    def to_dict(self) -> Mapping[str, Any]:
        if not self.result_digest:
            return self.with_digest().to_dict()
        return self._payload(include_digest=True)


@dataclass(frozen=True)
class CleanupAlert:
    job_id: str
    sandbox_name: str
    returncode: int
    detail: str
    at: str

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "schema": CLEANUP_ALERT_SCHEMA,
            "job_id": self.job_id,
            "sandbox_name": self.sandbox_name,
            "returncode": int(self.returncode),
            "detail": self.detail,
            "at": self.at,
        }


class OpenShellCertificationRunner:
    """Run one exact certification job without hub or landing authority."""

    def __init__(
        self,
        *,
        command_runner: Optional[CommandRunner] = None,
        openshell_bin: str = "openshell",
        launcher_environment: Optional[Mapping[str, str]] = None,
        sandbox_path: str = _DEFAULT_SANDBOX_PATH,
        max_bundle_bytes: int = 2 * 1024 * 1024 * 1024,
        now: Optional[Callable[[], datetime]] = None,
        name_factory: Optional[Callable[[], str]] = None,
        cleanup_alert_sink: Optional[Callable[[CleanupAlert], None]] = None,
    ) -> None:
        if not openshell_bin or "\x00" in openshell_bin:
            raise CertificationValidationError("openshell binary is invalid")
        self.command_runner = command_runner or SubprocessCommandRunner()
        self.openshell_bin = openshell_bin
        self.launcher_environment = _sanitized_launcher_environment(
            launcher_environment
        )
        if not sandbox_path or "\x00" in sandbox_path:
            raise CertificationValidationError("sandbox PATH is invalid")
        self.sandbox_path = sandbox_path
        if max_bundle_bytes < 1024:
            raise CertificationValidationError("max_bundle_bytes is too small")
        self.max_bundle_bytes = int(max_bundle_bytes)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._name_factory = name_factory or (
            lambda: "mac-cert-" + uuid.uuid4().hex[:16]
        )
        self._cleanup_alert_sink = cleanup_alert_sink or (lambda _alert: None)

    def run(
        self,
        job: OpenShellCertificationJob,
        *,
        result_path: Path,
    ) -> OpenShellCertificationResult:
        job.validate()
        sandbox_name = self._name_factory()
        if not re.fullmatch(r"mac-cert-[A-Za-z0-9._-]{1,80}", sandbox_name):
            raise CertificationValidationError("sandbox name factory returned an unsafe name")
        started_at = _iso(self._now())
        checks: list[CertificationCheckResult] = []
        failure_class = ""
        failure_reason = ""
        create_attempted = False

        with tempfile.TemporaryDirectory(prefix="mac-certifier-") as temp_value:
            temp = Path(temp_value)
            staged_bundle = self._stage_bundle(job, temp)
            policy_path = temp / "policy.yaml"
            policy_path.write_text(job.policy.policy_text, encoding="utf-8")
            policy_path.chmod(0o600)

            try:
                create_attempted = True
                create = self._invoke(
                    self._create_argv(
                        job,
                        sandbox_name=sandbox_name,
                        policy_path=policy_path,
                        staged_bundle=staged_bundle,
                    ),
                    timeout_seconds=job.lifecycle_timeout_seconds,
                )
                if create.returncode != 0:
                    failure_class = "sandbox_create_failed"
                    failure_reason = _outcome_detail(create)
                else:
                    setup = self._invoke(
                        self._identity_exec_argv(job, sandbox_name, setup=True),
                        timeout_seconds=job.lifecycle_timeout_seconds,
                    )
                    if setup.returncode != 0:
                        failure_class = "candidate_identity_setup_failed"
                        failure_reason = _outcome_detail(setup)
                    else:
                        for command in job.controller_commands:
                            outcome = self._invoke(
                                self._command_exec_argv(sandbox_name, command),
                                timeout_seconds=command.timeout_seconds + 30,
                            )
                            checks.append(
                                CertificationCheckResult(
                                    command.command_id,
                                    command.argv,
                                    outcome.returncode,
                                    "pass" if outcome.returncode == 0 else "fail",
                                    _clip(outcome.stdout),
                                    _clip(outcome.stderr),
                                    outcome.timed_out,
                                )
                            )
                            if outcome.returncode != 0:
                                failure_class = (
                                    "controller_command_timed_out"
                                    if outcome.timed_out
                                    else "controller_command_failed"
                                )
                                failure_reason = "%s: %s" % (
                                    command.command_id,
                                    _outcome_detail(outcome),
                                )
                                break
                        if not failure_class:
                            postcheck = self._invoke(
                                self._identity_exec_argv(job, sandbox_name, setup=False),
                                timeout_seconds=job.lifecycle_timeout_seconds,
                            )
                            if postcheck.returncode != 0:
                                failure_class = "candidate_identity_changed"
                                failure_reason = _outcome_detail(postcheck)
            except Exception as exc:  # outer infrastructure failure is a failed result
                failure_class = "certifier_infrastructure_error"
                failure_reason = _clip(str(exc))

            result = self._build_result(
                job,
                sandbox_name=sandbox_name,
                checks=tuple(checks),
                started_at=started_at,
                cleanup_status="pending",
                failure_class=failure_class,
                failure_reason=failure_reason,
            )

            cleanup = None
            if create_attempted:
                try:
                    cleanup = self._invoke(
                        [self.openshell_bin, "sandbox", "delete", sandbox_name],
                        timeout_seconds=job.lifecycle_timeout_seconds,
                    )
                except Exception as exc:
                    cleanup = CommandOutcome(
                        (self.openshell_bin, "sandbox", "delete", sandbox_name),
                        1,
                        "",
                        str(exc),
                        False,
                    )
            if cleanup is None or cleanup.returncode != 0:
                detail = "cleanup was not attempted" if cleanup is None else _outcome_detail(cleanup)
                alert = CleanupAlert(
                    job.job_id,
                    sandbox_name,
                    1 if cleanup is None else cleanup.returncode,
                    detail,
                    _iso(self._now()),
                )
                try:
                    self._cleanup_alert_sink(alert)
                except Exception:
                    pass
                result = replace(
                    result,
                    status="failed",
                    cleanup_status="failed",
                    failure_class="sandbox_cleanup_failed",
                    failure_reason=detail,
                    completed_at=_iso(self._now()),
                    result_digest="",
                ).with_digest()
                _write_result(result_path, result)
                raise CertificationCleanupError(
                    "OpenShell certification sandbox cleanup failed",
                    alert=alert,
                )

            result = replace(
                result,
                cleanup_status="deleted",
                completed_at=_iso(self._now()),
                result_digest="",
            ).with_digest()
            _write_result(result_path, result)
            return result

    def _stage_bundle(
        self,
        job: OpenShellCertificationJob,
        temp: Path,
    ) -> Path:
        source = Path(job.bundle_path)
        try:
            before = source.lstat()
        except OSError as exc:
            raise CertificationValidationError("certification Git bundle is unavailable") from exc
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise CertificationValidationError("certification input must be a regular Git bundle")
        if before.st_size < 16 or before.st_size > self.max_bundle_bytes:
            raise CertificationValidationError("certification Git bundle size is invalid")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(source, flags)
        except OSError as exc:
            raise CertificationValidationError("certification Git bundle cannot be opened safely") from exc
        destination = temp / "candidate.bundle"
        digest = hashlib.sha256()
        header = b""
        total = 0
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise CertificationValidationError("certification input is not a regular file")
            with os.fdopen(descriptor, "rb", closefd=True) as reader:
                descriptor = -1
                with destination.open("xb") as writer:
                    while True:
                        chunk = reader.read(1024 * 1024)
                        if not chunk:
                            break
                        if not header:
                            header = chunk[:64]
                        total += len(chunk)
                        if total > self.max_bundle_bytes:
                            raise CertificationValidationError(
                                "certification Git bundle exceeds the size limit"
                            )
                        digest.update(chunk)
                        writer.write(chunk)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not header.startswith((b"# v2 git bundle\n", b"# v3 git bundle\n")):
            raise CertificationValidationError(
                "certification input is not a credential-free Git bundle"
            )
        observed_digest = "sha256:%s" % digest.hexdigest()
        if observed_digest != job.bundle_digest:
            raise CertificationValidationError("Git bundle digest does not match the job")
        destination.chmod(0o400)
        return destination

    def _invoke(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
    ) -> CommandOutcome:
        outcome = self.command_runner.run(
            tuple(str(item) for item in argv),
            env=self.launcher_environment,
            timeout_seconds=max(1.0, float(timeout_seconds)),
        )
        if not isinstance(outcome, CommandOutcome):
            raise TypeError("certifier command runner returned an invalid outcome")
        return outcome

    def _create_argv(
        self,
        job: OpenShellCertificationJob,
        *,
        sandbox_name: str,
        policy_path: Path,
        staged_bundle: Path,
    ) -> Tuple[str, ...]:
        return (
            self.openshell_bin,
            "sandbox",
            "create",
            "--no-auto-providers",
            "--policy",
            str(policy_path),
            "--name",
            sandbox_name,
            "--keep",
            "--label",
            "mac.owner=mac",
            "--label",
            "mac.kind=certifier",
            "--label",
            "mac.job=%s" % job.job_id,
            "--from",
            job.image_ref,
            "--env",
            "HOME=/tmp",
            "--env",
            "PATH=%s" % self.sandbox_path,
            "--upload",
            "%s:/sandbox/input/candidate.bundle" % staged_bundle,
            "--",
            "/bin/true",
        )

    def _identity_exec_argv(
        self,
        job: OpenShellCertificationJob,
        sandbox_name: str,
        *,
        setup: bool,
    ) -> Tuple[str, ...]:
        script = _SETUP_IDENTITY_SCRIPT if setup else _POSTCHECK_IDENTITY_SCRIPT
        argv = [
            self.openshell_bin,
            "sandbox",
            "exec",
            "--name",
            sandbox_name,
            "--timeout",
            str(int(job.lifecycle_timeout_seconds)),
            "--no-tty",
        ]
        if not setup:
            argv += ["--workdir", "/sandbox/work/repo"]
        argv += [
            "--",
            "/usr/bin/env",
            "-i",
            "HOME=/tmp",
            "PATH=%s" % self.sandbox_path,
            "python3",
            "-c",
            script,
            job.candidate_sha,
            job.candidate_tree_digest,
        ]
        return tuple(argv)

    def _command_exec_argv(
        self,
        sandbox_name: str,
        command: ControllerCommand,
    ) -> Tuple[str, ...]:
        return (
            self.openshell_bin,
            "sandbox",
            "exec",
            "--name",
            sandbox_name,
            "--workdir",
            "/sandbox/work/repo",
            "--timeout",
            str(int(command.timeout_seconds)),
            "--no-tty",
            "--",
            "/usr/bin/env",
            "-i",
            "HOME=/tmp",
            "PATH=%s" % self.sandbox_path,
            *command.argv,
        )

    def _build_result(
        self,
        job: OpenShellCertificationJob,
        *,
        sandbox_name: str,
        checks: Tuple[CertificationCheckResult, ...],
        started_at: str,
        cleanup_status: str,
        failure_class: str,
        failure_reason: str,
    ) -> OpenShellCertificationResult:
        status = "passed" if not failure_class and len(checks) == len(job.controller_commands) else "failed"
        isolation = {
            "schema": CERTIFICATION_ISOLATION_SCHEMA,
            "network": "disabled",
            "landing_credentials": "absent",
            "planner_commands": "rejected",
            "policy_source": "trusted_controller",
            "policy_id": job.policy.policy_id,
            "policy_version": int(job.policy.version),
            "policy_checksum": job.policy.checksum,
            "landlock": "hard_requirement",
            "run_as_user": "non_root",
            "launcher_environment": sorted(self.launcher_environment),
            "input_format": "credential_free_git_bundle",
        }
        return OpenShellCertificationResult(
            job_id=job.job_id,
            job_digest=job.job_digest,
            batch_id=job.batch_id,
            package_id=job.package_id,
            plan_version=job.plan_version,
            epoch=job.epoch,
            candidate_sha=job.candidate_sha,
            candidate_tree_digest=job.candidate_tree_digest,
            assembly_base_sha=job.assembly_base_sha,
            landing_base_sha=job.landing_base_sha,
            target_ref=job.target_ref,
            status=status,
            policy=job.policy.identity(),
            image_ref=job.image_ref,
            image_digest=job.image_digest,
            bundle_digest=job.bundle_digest,
            commands_digest=job.commands_digest,
            sandbox_name=sandbox_name,
            checks=checks,
            isolation=isolation,
            started_at=started_at,
            completed_at=_iso(self._now()),
            cleanup_status=cleanup_status,
            failure_class=failure_class,
            failure_reason=_clip(failure_reason),
        ).with_digest()


_SETUP_IDENTITY_SCRIPT = r'''import json, os, shutil, subprocess, sys
sha, expected_tree = sys.argv[1], sys.argv[2]
bundle = "/sandbox/input/candidate.bundle"
repo = "/sandbox/work/repo"
shutil.rmtree("/sandbox/work", ignore_errors=True)
os.makedirs("/sandbox/work", exist_ok=True)

def run(argv):
    return subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

clone = run(["git", "clone", "--no-checkout", "--no-local", "--", bundle, repo])
if clone.returncode:
    print(clone.stderr or clone.stdout, file=sys.stderr)
    raise SystemExit(61)
checkout = run(["git", "-C", repo, "checkout", "--detach", sha])
if checkout.returncode:
    print(checkout.stderr or checkout.stdout, file=sys.stderr)
    raise SystemExit(62)
run(["git", "-C", repo, "remote", "remove", "origin"])
head = run(["git", "-C", repo, "rev-parse", "HEAD"])
tree = run(["git", "-C", repo, "rev-parse", "HEAD^{tree}"])
if head.returncode or head.stdout.strip() != sha:
    raise SystemExit(63)
observed_tree = "git-tree:" + tree.stdout.strip()
if tree.returncode or observed_tree != expected_tree:
    raise SystemExit(64)
print(json.dumps({"candidate_sha": sha, "candidate_tree_digest": observed_tree}, sort_keys=True))
'''


_POSTCHECK_IDENTITY_SCRIPT = r'''import subprocess, sys
sha, expected_tree = sys.argv[1], sys.argv[2]

def value(argv):
    result = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        print(result.stderr or result.stdout, file=sys.stderr)
        raise SystemExit(71)
    return result.stdout.strip()

head = value(["git", "rev-parse", "HEAD"])
tree = "git-tree:" + value(["git", "rev-parse", "HEAD^{tree}"])
if head != sha or tree != expected_tree:
    raise SystemExit(72)
'''


def _sanitized_launcher_environment(
    supplied: Optional[Mapping[str, str]],
) -> Mapping[str, str]:
    if supplied is None:
        return dict(_DEFAULT_LAUNCH_ENV)
    unknown = sorted(set(supplied) - _SAFE_LAUNCH_ENV)
    if unknown:
        raise CertificationValidationError(
            "certifier launcher environment contains forbidden names: %s"
            % ", ".join(unknown)
        )
    values = {}
    for name, value in supplied.items():
        text = str(value)
        if "\x00" in text:
            raise CertificationValidationError("certifier launcher environment is invalid")
        values[name] = text
    for name, value in _DEFAULT_LAUNCH_ENV.items():
        values.setdefault(name, value)
    return values


def _write_result(path: Path, result: OpenShellCertificationResult) -> None:
    target = Path(path)
    if target.is_symlink():
        raise CertificationValidationError("certification result path may not be a symlink")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n"
    descriptor, temp_name = tempfile.mkstemp(
        prefix=".%s." % target.name,
        dir=str(target.parent),
    )
    temp_path = Path(temp_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temp_path.unlink(missing_ok=True)


def _require_identifier(value: str, label: str) -> None:
    if not _IDENTIFIER_RE.fullmatch(str(value or "")):
        raise CertificationValidationError("%s is invalid" % label)


def _require_sha256(value: str, label: str) -> None:
    if not _SHA256_RE.fullmatch(str(value or "")):
        raise CertificationValidationError("%s must be an exact sha256 digest" % label)


def _sha256_bytes(value: bytes) -> str:
    return "sha256:%s" % hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _clip(value: Any, limit: int = _OUTPUT_LIMIT) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    head = limit // 4
    tail = limit - head
    return "%s\n... %d characters omitted ...\n%s" % (
        text[:head],
        len(text) - limit,
        text[-tail:],
    )


def _outcome_detail(outcome: CommandOutcome) -> str:
    detail = (outcome.stderr or outcome.stdout or "").strip()
    if detail:
        return _clip(detail)
    return "command exited with status %d" % int(outcome.returncode)


def _iso(value: datetime) -> str:
    current = value
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="microseconds")


__all__ = [
    "CERTIFICATION_ISOLATION_SCHEMA",
    "CERTIFICATION_JOB_SCHEMA",
    "CERTIFICATION_RESULT_SCHEMA",
    "CLEANUP_ALERT_SCHEMA",
    "CertificationCheckResult",
    "CertificationCleanupError",
    "CertificationPolicy",
    "CertificationValidationError",
    "CleanupAlert",
    "CommandOutcome",
    "CommandRunner",
    "ControllerCommand",
    "OpenShellCertificationJob",
    "OpenShellCertificationResult",
    "OpenShellCertificationRunner",
    "OpenShellCertificationError",
    "SubprocessCommandRunner",
    "validate_certifier_controller_command",
    "validate_certifier_image_ref",
]
