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
from urllib.parse import urlsplit

import yaml


CERTIFICATION_JOB_SCHEMA = "mac.openshell_certification_job.v2"
CERTIFICATION_RESULT_SCHEMA = "mac.openshell_certification_result.v1"
CERTIFICATION_ISOLATION_SCHEMA = "mac.certification_isolation.v1"
CERTIFIER_PHASE_MANIFEST_SCHEMA = "mac.certifier_phase_manifest.v1"
CERTIFIER_PHASE_PROFILE_SCHEMA = "mac.certifier_phase_profile.v1"
CLEANUP_ALERT_SCHEMA = "mac.openshell_certification_cleanup_alert.v1"

_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TREE_RE = re.compile(r"^git-tree:[0-9a-f]{40}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}$")
_REF_RE = re.compile(r"^refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]{0,240}$")
_SAFE_LAUNCH_ENV = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "OPENSHELL_GATEWAY_ENDPOINT",
        "PATH",
        "SYSTEMROOT",
        "TMPDIR",
        "TZ",
    }
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
CERTIFIER_PRIMARY_COMMAND = "/opt/mac-certifier/bin/run-contract-tests"
_CERTIFIER_PHASE_PREFIX = "MAC_CERTIFIER_PHASE_MANIFEST_JSON="
_PHASE_MANIFEST_KEYS = frozenset(
    {
        "schema",
        "trusted_source_revision",
        "assembly_base_sha",
        "candidate_sha",
        "changed_files",
        "changed_file_count",
        "changed_files_digest",
        "selection_mode",
        "authoritative",
        "supplemental",
        "full_suite_count",
        "manifest_digest",
    }
)
_PHASE_KEYS = frozenset({"mode", "reason", "tests"})
_PHASE_PROFILE_KEYS = frozenset(
    {
        "schema",
        "version",
        "checksum",
        "full_targets",
        "focused_required_tests",
        "selection_modes",
    }
)
_PHASE_PROFILE_SELECTION_KEYS = frozenset(
    {"authoritative", "supplemental", "expected_full_suite_count"}
)
_PHASE_PROFILE_EXPECTATION_KEYS = frozenset({"mode", "reason"})
_AUTHORITATIVE_PHASE_MODES = frozenset({"focused", "full", "rejected"})
_SUPPLEMENTAL_PHASE_MODES = frozenset({"skipped", "full"})


@dataclass(frozen=True)
class CertifierPhaseExpectation:
    """One exact image-owned phase outcome allowed by a selection mode."""

    mode: str
    reason: str

    def to_dict(self) -> Mapping[str, Any]:
        return {"mode": self.mode, "reason": self.reason}


@dataclass(frozen=True)
class CertifierPhaseSelection:
    """The exact two-phase shape and full-suite count for one selector result."""

    selection_mode: str
    authoritative: CertifierPhaseExpectation
    supplemental: CertifierPhaseExpectation
    expected_full_suite_count: int

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "authoritative": self.authoritative.to_dict(),
            "supplemental": self.supplemental.to_dict(),
            "expected_full_suite_count": int(self.expected_full_suite_count),
        }


@dataclass(frozen=True)
class CertifierPhaseProfile:
    """Immutable, checksummed contract for interpreting certifier receipts.

    The profile is repository data, not a controller-side project-name switch.
    Its complete normalized form is included in the certification job digest so
    queued work cannot silently acquire different phase semantics after a MAC
    deployment.
    """

    schema: str
    version: int
    checksum: str
    full_targets: Tuple[str, ...]
    focused_required_tests: Tuple[str, ...]
    selection_modes: Tuple[CertifierPhaseSelection, ...]

    @classmethod
    def from_mapping(cls, value: Any) -> "CertifierPhaseProfile":
        if not isinstance(value, Mapping) or set(value) != _PHASE_PROFILE_KEYS:
            raise CertificationValidationError(
                "certifier phase profile fields are invalid"
            )
        raw_modes = value.get("selection_modes")
        if (
            not isinstance(raw_modes, Mapping)
            or not raw_modes
            or len(raw_modes) > 128
            or any(not isinstance(name, str) for name in raw_modes)
        ):
            raise CertificationValidationError(
                "certifier phase profile selection modes are invalid"
            )
        selections = []
        for selection_mode in sorted(raw_modes):
            raw_selection = raw_modes.get(selection_mode)
            if (
                not isinstance(selection_mode, str)
                or not isinstance(raw_selection, Mapping)
                or set(raw_selection) != _PHASE_PROFILE_SELECTION_KEYS
            ):
                raise CertificationValidationError(
                    "certifier phase profile selection is malformed"
                )
            phases = []
            for name in ("authoritative", "supplemental"):
                raw_phase = raw_selection.get(name)
                if (
                    not isinstance(raw_phase, Mapping)
                    or set(raw_phase) != _PHASE_PROFILE_EXPECTATION_KEYS
                    or not isinstance(raw_phase.get("mode"), str)
                    or not isinstance(raw_phase.get("reason"), str)
                ):
                    raise CertificationValidationError(
                        "certifier phase profile expectation is malformed"
                    )
                phases.append(
                    CertifierPhaseExpectation(
                        raw_phase["mode"],
                        raw_phase["reason"],
                    )
                )
            expected_count = raw_selection.get("expected_full_suite_count")
            if isinstance(expected_count, bool) or not isinstance(expected_count, int):
                raise CertificationValidationError(
                    "certifier phase profile full-suite count is invalid"
                )
            selections.append(
                CertifierPhaseSelection(
                    selection_mode,
                    phases[0],
                    phases[1],
                    expected_count,
                )
            )
        full_targets = value.get("full_targets")
        focused_required = value.get("focused_required_tests")
        if not isinstance(full_targets, list) or not isinstance(focused_required, list):
            raise CertificationValidationError(
                "certifier phase profile test inventories are malformed"
            )
        profile = cls(
            str(value.get("schema") or ""),
            value.get("version"),
            str(value.get("checksum") or ""),
            tuple(full_targets),
            tuple(focused_required),
            tuple(selections),
        )
        profile.validate()
        return profile

    def unsigned_dict(self) -> Mapping[str, Any]:
        return {
            "schema": self.schema,
            "version": int(self.version),
            "full_targets": list(self.full_targets),
            "focused_required_tests": list(self.focused_required_tests),
            "selection_modes": {
                item.selection_mode: item.to_dict() for item in self.selection_modes
            },
        }

    def to_dict(self) -> Mapping[str, Any]:
        value = dict(self.unsigned_dict())
        value["checksum"] = self.checksum
        return value

    def validate(self) -> None:
        if self.schema != CERTIFIER_PHASE_PROFILE_SCHEMA:
            raise CertificationValidationError(
                "certifier phase profile schema is invalid"
            )
        if type(self.version) is not int or self.version != 1:
            raise CertificationValidationError(
                "certifier phase profile version is unsupported"
            )
        _validate_profile_paths(
            self.full_targets,
            "full targets",
            allow_empty=False,
        )
        _validate_profile_paths(
            self.focused_required_tests,
            "focused required tests",
            allow_empty=True,
        )
        _require_sha256(self.checksum, "certifier phase profile checksum")
        if self.checksum != _sha256_json(self.unsigned_dict()):
            raise CertificationValidationError(
                "certifier phase profile checksum does not match its content"
            )
        if not self.selection_modes or len(self.selection_modes) > 128:
            raise CertificationValidationError(
                "certifier phase profile selection modes are invalid"
            )
        names = [item.selection_mode for item in self.selection_modes]
        if names != sorted(set(names)):
            raise CertificationValidationError(
                "certifier phase profile selection modes are not canonical"
            )
        focused_used = False
        for selection in self.selection_modes:
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", selection.selection_mode):
                raise CertificationValidationError(
                    "certifier phase profile selection mode is invalid"
                )
            self._validate_expectation(
                selection.authoritative,
                allowed=_AUTHORITATIVE_PHASE_MODES,
                label="authoritative",
            )
            self._validate_expectation(
                selection.supplemental,
                allowed=_SUPPLEMENTAL_PHASE_MODES,
                label="supplemental",
            )
            focused_used = focused_used or any(
                phase.mode == "focused"
                for phase in (selection.authoritative, selection.supplemental)
            )
            observed_full = sum(
                phase.mode == "full"
                for phase in (selection.authoritative, selection.supplemental)
            )
            if (
                isinstance(selection.expected_full_suite_count, bool)
                or selection.expected_full_suite_count != observed_full
                or not 0 <= selection.expected_full_suite_count <= 1
            ):
                raise CertificationValidationError(
                    "certifier phase profile full-suite count is incoherent"
                )
            rejected = selection.authoritative.mode == "rejected"
            if rejected != selection.selection_mode.endswith("_rejected"):
                raise CertificationValidationError(
                    "certifier phase profile rejected selection is incoherent"
                )
        if focused_used and not self.focused_required_tests:
            raise CertificationValidationError(
                "certifier phase profile lacks focused required tests"
            )

    @staticmethod
    def _validate_expectation(
        expectation: CertifierPhaseExpectation,
        *,
        allowed: frozenset[str],
        label: str,
    ) -> None:
        if expectation.mode not in allowed:
            raise CertificationValidationError(
                "certifier phase profile %s mode is invalid" % label
            )
        if (
            not expectation.reason
            or len(expectation.reason) > 256
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in expectation.reason
            )
        ):
            raise CertificationValidationError(
                "certifier phase profile %s reason is invalid" % label
            )

    def selection(self, selection_mode: str) -> CertifierPhaseSelection:
        for item in self.selection_modes:
            if item.selection_mode == selection_mode:
                return item
        raise CertificationValidationError(
            "certifier selection mode is not allowed by the phase profile"
        )


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


def validate_certifier_phase_manifest(
    value: Any,
    *,
    assembly_base_sha: str,
    candidate_sha: str,
    phase_profile: CertifierPhaseProfile,
) -> Mapping[str, Any]:
    """Validate the image-owned proportional-execution receipt exactly."""

    phase_profile.validate()
    if not isinstance(value, Mapping) or set(value) != _PHASE_MANIFEST_KEYS:
        raise CertificationValidationError(
            "certifier phase manifest fields are invalid"
        )
    manifest = dict(value)
    if manifest.get("schema") != CERTIFIER_PHASE_MANIFEST_SCHEMA:
        raise CertificationValidationError("certifier phase manifest schema is invalid")
    if manifest.get("assembly_base_sha") != assembly_base_sha:
        raise CertificationValidationError("certifier phase manifest base changed")
    if manifest.get("candidate_sha") != candidate_sha:
        raise CertificationValidationError("certifier phase manifest candidate changed")
    if not _SHA40_RE.fullmatch(str(manifest.get("trusted_source_revision") or "")):
        raise CertificationValidationError(
            "certifier phase manifest trusted revision is invalid"
        )

    changed = manifest.get("changed_files")
    if (
        not isinstance(changed, list)
        or not changed
        or len(changed) > 4096
        or any(not isinstance(path, str) for path in changed)
        or changed != sorted(set(changed))
    ):
        raise CertificationValidationError(
            "certifier changed-file inventory is invalid"
        )
    for path in changed:
        if (
            not isinstance(path, str)
            or not path
            or len(path.encode("utf-8")) > 1024
            or path.startswith("/")
            or ".." in path.split("/")
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
        ):
            raise CertificationValidationError(
                "certifier changed-file inventory is unsafe"
            )
    count = manifest.get("changed_file_count")
    if isinstance(count, bool) or not isinstance(count, int) or count != len(changed):
        raise CertificationValidationError("certifier changed-file count is invalid")
    if manifest.get("changed_files_digest") != _sha256_json(changed):
        raise CertificationValidationError("certifier changed-file digest is invalid")

    selection_mode = manifest.get("selection_mode")
    if not isinstance(selection_mode, str) or not re.fullmatch(
        r"[a-z][a-z0-9_]{0,79}", selection_mode
    ):
        raise CertificationValidationError("certifier selection mode is invalid")
    selection = phase_profile.selection(selection_mode)

    phases: list[Mapping[str, Any]] = []
    for name in ("authoritative", "supplemental"):
        phase = manifest.get(name)
        if not isinstance(phase, Mapping) or set(phase) != _PHASE_KEYS:
            raise CertificationValidationError("certifier %s phase is malformed" % name)
        mode = phase.get("mode")
        expected = (
            selection.authoritative
            if name == "authoritative"
            else selection.supplemental
        )
        if mode != expected.mode:
            raise CertificationValidationError(
                "certifier %s phase mode does not match its selection" % name
            )
        reason = phase.get("reason")
        if reason != expected.reason:
            raise CertificationValidationError(
                "certifier %s phase reason does not match its selection" % name
            )
        tests = phase.get("tests")
        if (
            not isinstance(tests, list)
            or len(tests) > 4096
            or any(not isinstance(path, str) for path in tests)
            or tests != sorted(set(tests))
            or any(
                not path or path.startswith("/") or ".." in path.split("/")
                for path in tests
            )
        ):
            raise CertificationValidationError("certifier %s tests are invalid" % name)
        if mode in {"focused", "full"} and not tests:
            raise CertificationValidationError("certifier %s phase has no tests" % name)
        if mode in {"skipped", "rejected"} and tests:
            raise CertificationValidationError(
                "certifier inactive phase unexpectedly names tests"
            )
        if mode == "full" and tests != list(phase_profile.full_targets):
            raise CertificationValidationError(
                "certifier full phase does not name the exact frozen suite"
            )
        if mode == "focused" and not set(phase_profile.focused_required_tests).issubset(
            tests
        ):
            raise CertificationValidationError(
                "certifier focused phase lacks root-owned invariants"
            )
        phases.append(phase)

    full_count = manifest.get("full_suite_count")
    observed_full = sum(phase.get("mode") == "full" for phase in phases)
    if (
        isinstance(full_count, bool)
        or not isinstance(full_count, int)
        or full_count != observed_full
        or full_count > 1
        or full_count != selection.expected_full_suite_count
    ):
        raise CertificationValidationError(
            "certifier phase manifest full-suite count does not match its selection"
        )
    rejected = phases[0].get("mode") == "rejected"
    if rejected != selection_mode.endswith("_rejected"):
        raise CertificationValidationError("certifier rejected selection is incoherent")

    claimed_digest = str(manifest.get("manifest_digest") or "")
    unsigned = dict(manifest)
    unsigned.pop("manifest_digest", None)
    if claimed_digest != _sha256_json(unsigned):
        raise CertificationValidationError("certifier phase manifest digest is invalid")
    return manifest


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
            raise CertificationValidationError(
                "policy checksum does not match policy text"
            )
        if not self.policy_text or len(self.policy_text.encode("utf-8")) > 1024 * 1024:
            raise CertificationValidationError("certifier policy text size is invalid")
        try:
            parsed = yaml.safe_load(self.policy_text)
        except yaml.YAMLError as exc:
            raise CertificationValidationError(
                "certifier policy is invalid YAML"
            ) from exc
        if not isinstance(parsed, dict):
            raise CertificationValidationError("certifier policy must be a YAML object")
        unknown = sorted(set(parsed) - _POLICY_KEYS)
        if unknown:
            raise CertificationValidationError(
                "certifier policy has unreviewed top-level keys: %s"
                % ", ".join(unknown)
            )
        if parsed.get("version") != 1:
            raise CertificationValidationError(
                "certifier policy version must be exactly 1"
            )
        if parsed.get("network_policies") != {}:
            raise CertificationValidationError(
                "certifier policy must disable all network egress"
            )
        landlock = parsed.get("landlock")
        if (
            not isinstance(landlock, dict)
            or landlock.get("compatibility") != "hard_requirement"
        ):
            raise CertificationValidationError(
                "certifier policy must require hard Landlock enforcement"
            )
        if set(landlock) - _LANDLOCK_POLICY_KEYS:
            raise CertificationValidationError(
                "certifier policy has unreviewed Landlock controls"
            )
        process = parsed.get("process")
        if not isinstance(process, dict):
            raise CertificationValidationError(
                "certifier policy requires a process section"
            )
        user = str(process.get("run_as_user") or "").strip()
        group = str(process.get("run_as_group") or "").strip()
        if not user or user.lower() == "root" or user == "0":
            raise CertificationValidationError(
                "certifier policy must run as a non-root user"
            )
        if not group or group.lower() == "root" or group == "0":
            raise CertificationValidationError(
                "certifier policy must run as a non-root group"
            )
        if set(process) - _PROCESS_POLICY_KEYS:
            raise CertificationValidationError(
                "certifier policy has unreviewed process controls"
            )
        filesystem = parsed.get("filesystem_policy")
        if (
            not isinstance(filesystem, dict)
            or filesystem.get("include_workdir") is not True
        ):
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
            if (
                not isinstance(item, str)
                or not item
                or "\x00" in item
                or len(item) > 8192
            ):
                raise CertificationValidationError(
                    "controller command has an invalid argv item"
                )
            if index == 0 and item.startswith("-"):
                raise CertificationValidationError(
                    "controller command executable is invalid"
                )
        if (
            isinstance(self.timeout_seconds, bool)
            or not 1 <= int(self.timeout_seconds) <= 3600
        ):
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
    phase_profile: CertifierPhaseProfile
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
                raise CertificationValidationError(
                    "%s must be an exact lowercase Git SHA" % name
                )
        if not _TREE_RE.fullmatch(str(self.candidate_tree_digest or "")):
            raise CertificationValidationError(
                "candidate_tree_digest must be git-tree plus an exact lowercase Git SHA"
            )
        if not _REF_RE.fullmatch(str(self.target_ref or "")) or any(
            token in self.target_ref for token in ("..", "//", "@{")
        ):
            raise CertificationValidationError(
                "target_ref must be a safe refs/heads ref"
            )
        self.policy.validate()
        self.phase_profile.validate()
        validate_certifier_image_ref(self.image_ref)
        _require_sha256(self.bundle_digest, "bundle digest")
        if not self.controller_commands:
            raise CertificationValidationError(
                "certification requires controller commands"
            )
        seen = set()
        primary_commands = 0
        for command in self.controller_commands:
            validate_certifier_controller_command(command)
            if command.command_id in seen:
                raise CertificationValidationError(
                    "controller command ids must be unique"
                )
            seen.add(command.command_id)
            base_flags = [
                index
                for index, item in enumerate(command.argv)
                if item == "--base-sha" or item.startswith("--base-sha=")
            ]
            if base_flags != [len(command.argv) - 2] or command.argv[-2:] != (
                "--base-sha",
                self.assembly_base_sha,
            ):
                raise CertificationValidationError(
                    "controller command must end with the exact controller-owned assembly base"
                )
            if command.argv[0] == CERTIFIER_PRIMARY_COMMAND:
                primary_commands += 1
                if command.argv != (
                    CERTIFIER_PRIMARY_COMMAND,
                    "--base-sha",
                    self.assembly_base_sha,
                ):
                    raise CertificationValidationError(
                        "primary certifier command accepts only the controller-owned base"
                    )
        if primary_commands != 1:
            raise CertificationValidationError(
                "certification requires exactly one primary frozen test command"
            )
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
            "phase_profile": self.phase_profile.to_dict(),
            "image_ref": self.image_ref,
            "image_digest": self.image_digest,
            "bundle_digest": self.bundle_digest,
            "bundle_format": "git_bundle",
            "commands_digest": self.commands_digest,
            "controller_commands": [
                item.to_dict() for item in self.controller_commands
            ],
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
    phase_manifest: Mapping[str, Any]
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
            "phase_manifest": dict(self.phase_manifest),
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
        return replace(
            self, result_digest=_sha256_json(self._payload(include_digest=False))
        )

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

    @classmethod
    def from_environment(
        cls,
        environ: Optional[Mapping[str, str]] = None,
        *,
        command_runner: Optional[CommandRunner] = None,
        **kwargs: Any,
    ) -> "OpenShellCertificationRunner":
        """Bind production execution to the deployed OpenShell service identity.

        Fleet bootstrap publishes an exact CLI path through
        ``MAC_OPENSHELL_BIN``.  Do not fall back to ambient ``PATH`` lookup: the
        certifier deliberately replaces its subprocess environment, and such a
        fallback previously made a configured ``~/.mac/bin/openshell``
        unreachable.  The host CLI keeps the service user's real ``HOME`` so it
        can resolve the selected gateway; candidate processes still receive the
        isolated ``HOME=/tmp`` environment built below.
        """

        source = os.environ if environ is None else environ
        raw_binary = str(source.get("MAC_OPENSHELL_BIN") or "").strip()
        if not raw_binary or "\x00" in raw_binary:
            raise CertificationValidationError(
                "MAC_OPENSHELL_BIN must name an absolute executable"
            )
        configured_binary = Path(raw_binary)
        if not configured_binary.is_absolute():
            raise CertificationValidationError(
                "MAC_OPENSHELL_BIN must name an absolute executable"
            )
        try:
            binary = configured_binary.resolve(strict=True)
        except OSError as exc:
            raise CertificationValidationError(
                "MAC_OPENSHELL_BIN is unavailable"
            ) from exc
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise CertificationValidationError(
                "MAC_OPENSHELL_BIN is not an executable file"
            )

        raw_home = str(source.get("HOME") or "").strip()
        if not raw_home or "\x00" in raw_home:
            raise CertificationValidationError(
                "OpenShell host launcher HOME must be an absolute directory"
            )
        configured_home = Path(raw_home)
        if not configured_home.is_absolute():
            raise CertificationValidationError(
                "OpenShell host launcher HOME must be an absolute directory"
            )
        try:
            home = configured_home.resolve(strict=True)
        except OSError as exc:
            raise CertificationValidationError(
                "OpenShell host launcher HOME is unavailable"
            ) from exc
        if not home.is_dir():
            raise CertificationValidationError(
                "OpenShell host launcher HOME must be an absolute directory"
            )

        launch_environment = {
            "HOME": str(home),
            "PATH": str(source.get("PATH") or "/usr/bin:/bin"),
        }
        for name in ("LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "TMPDIR", "TZ"):
            value = source.get(name)
            if value not in (None, ""):
                launch_environment[name] = str(value)

        # A Darwin control plane cannot meet the certifier's hard-Landlock
        # contract through Docker Desktop's LinuxKit VM. Production may point
        # only the certifier CLI at a loopback endpoint backed by a durable SSH
        # tunnel to a Linux OpenShell gateway. Keep this separate from the
        # ordinary agent gateway and reject arbitrary plaintext remote URLs.
        raw_gateway_endpoint = str(
            source.get("MAC_CERTIFIER_OPENSHELL_GATEWAY_ENDPOINT") or ""
        ).strip()
        if raw_gateway_endpoint:
            launch_environment["OPENSHELL_GATEWAY_ENDPOINT"] = (
                _validated_loopback_gateway_endpoint(raw_gateway_endpoint)
            )

        runner = cls(
            command_runner=command_runner,
            openshell_bin=str(binary),
            launcher_environment=launch_environment,
            **kwargs,
        )
        runner.preflight()
        return runner

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
            raise CertificationValidationError(
                "sandbox name factory returned an unsafe name"
            )
        started_at = _iso(self._now())
        checks: list[CertificationCheckResult] = []
        phase_manifest: Mapping[str, Any] = {}
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
                            effective_returncode = outcome.returncode
                            if command.argv[0] == CERTIFIER_PRIMARY_COMMAND:
                                try:
                                    if phase_manifest:
                                        raise CertificationValidationError(
                                            "certifier emitted more than one phase manifest"
                                        )
                                    phase_manifest = _phase_manifest_from_output(
                                        outcome.stdout,
                                        assembly_base_sha=job.assembly_base_sha,
                                        candidate_sha=job.candidate_sha,
                                        phase_profile=job.phase_profile,
                                    )
                                    if (
                                        outcome.returncode == 0
                                        and phase_manifest["authoritative"]["mode"]
                                        == "rejected"
                                    ):
                                        raise CertificationValidationError(
                                            "certifier returned success for a rejected selection"
                                        )
                                except CertificationValidationError as exc:
                                    effective_returncode = 78
                                    failure_class = "certifier_phase_manifest_invalid"
                                    failure_reason = str(exc)
                            checks.append(
                                CertificationCheckResult(
                                    command.command_id,
                                    command.argv,
                                    effective_returncode,
                                    "pass" if effective_returncode == 0 else "fail",
                                    _clip(outcome.stdout),
                                    _clip(outcome.stderr),
                                    outcome.timed_out and effective_returncode == 124,
                                )
                            )
                            if effective_returncode != 0:
                                if not failure_class:
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
                        if not failure_class and not phase_manifest:
                            failure_class = "certifier_phase_manifest_missing"
                            failure_reason = (
                                "primary frozen test command emitted no phase manifest"
                            )
                        if not failure_class:
                            postcheck = self._invoke(
                                self._identity_exec_argv(
                                    job, sandbox_name, setup=False
                                ),
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
                phase_manifest=phase_manifest,
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
                detail = (
                    "cleanup was not attempted"
                    if cleanup is None
                    else _outcome_detail(cleanup)
                )
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

    def preflight(self) -> None:
        """Prove the bound host CLI can execute before claiming product work.

        This proves both the binary binding and the selected/direct gateway is
        reachable. A particular certifier image and hard isolation policy are
        still covered by the real sandbox create/delete canary.
        """

        try:
            outcome = self._invoke(
                (self.openshell_bin, "--version"), timeout_seconds=15
            )
        except OSError as exc:
            raise CertificationValidationError(
                "OpenShell certifier CLI preflight could not execute"
            ) from exc
        if outcome.returncode != 0 or outcome.timed_out:
            raise CertificationValidationError(
                "OpenShell certifier CLI preflight failed"
            )
        try:
            gateway = self._invoke((self.openshell_bin, "status"), timeout_seconds=15)
        except OSError as exc:
            raise CertificationValidationError(
                "OpenShell certifier gateway preflight could not execute"
            ) from exc
        if gateway.returncode != 0 or gateway.timed_out:
            raise CertificationValidationError(
                "OpenShell certifier gateway preflight failed"
            )

    def _stage_bundle(
        self,
        job: OpenShellCertificationJob,
        temp: Path,
    ) -> Path:
        source = Path(job.bundle_path)
        try:
            before = source.lstat()
        except OSError as exc:
            raise CertificationValidationError(
                "certification Git bundle is unavailable"
            ) from exc
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise CertificationValidationError(
                "certification input must be a regular Git bundle"
            )
        if before.st_size < 16 or before.st_size > self.max_bundle_bytes:
            raise CertificationValidationError(
                "certification Git bundle size is invalid"
            )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(source, flags)
        except OSError as exc:
            raise CertificationValidationError(
                "certification Git bundle cannot be opened safely"
            ) from exc
        destination = temp / "candidate.bundle"
        digest = hashlib.sha256()
        header = b""
        total = 0
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise CertificationValidationError(
                    "certification input is not a regular file"
                )
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
            raise CertificationValidationError(
                "Git bundle digest does not match the job"
            )
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
        phase_manifest: Mapping[str, Any],
        started_at: str,
        cleanup_status: str,
        failure_class: str,
        failure_reason: str,
    ) -> OpenShellCertificationResult:
        status = (
            "passed"
            if not failure_class and len(checks) == len(job.controller_commands)
            else "failed"
        )
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
            "assembly_base_transport": "controller_bound_argv",
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
            phase_manifest=phase_manifest,
            isolation=isolation,
            started_at=started_at,
            completed_at=_iso(self._now()),
            cleanup_status=cleanup_status,
            failure_class=failure_class,
            failure_reason=_clip(failure_reason),
        ).with_digest()


_SETUP_IDENTITY_SCRIPT = r"""import json, os, shutil, subprocess, sys
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
"""


_POSTCHECK_IDENTITY_SCRIPT = r"""import subprocess, sys
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
"""


def _phase_manifest_from_output(
    stdout: str,
    *,
    assembly_base_sha: str,
    candidate_sha: str,
    phase_profile: CertifierPhaseProfile,
) -> Mapping[str, Any]:
    lines = [
        line[len(_CERTIFIER_PHASE_PREFIX) :]
        for line in str(stdout or "").splitlines()
        if line.startswith(_CERTIFIER_PHASE_PREFIX)
    ]
    if len(lines) != 1:
        raise CertificationValidationError(
            "primary certifier output must contain exactly one phase manifest"
        )
    try:
        payload = json.loads(lines[0])
    except (TypeError, json.JSONDecodeError) as exc:
        raise CertificationValidationError(
            "primary certifier phase manifest is not valid JSON"
        ) from exc
    return validate_certifier_phase_manifest(
        payload,
        assembly_base_sha=assembly_base_sha,
        candidate_sha=candidate_sha,
        phase_profile=phase_profile,
    )


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
            raise CertificationValidationError(
                "certifier launcher environment is invalid"
            )
        if name == "OPENSHELL_GATEWAY_ENDPOINT":
            text = _validated_loopback_gateway_endpoint(text)
        values[name] = text
    for name, value in _DEFAULT_LAUNCH_ENV.items():
        values.setdefault(name, value)
    return values


def _validated_loopback_gateway_endpoint(value: str) -> str:
    if not value or "\x00" in value or any(character.isspace() for character in value):
        raise CertificationValidationError(
            "certifier OpenShell gateway endpoint must be a loopback HTTP URL"
        )
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise CertificationValidationError(
            "certifier OpenShell gateway endpoint must be a loopback HTTP URL"
        ) from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not (1 <= port <= 65535)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise CertificationValidationError(
            "certifier OpenShell gateway endpoint must be a loopback HTTP URL"
        )
    return "http://127.0.0.1:%d" % port


def _write_result(path: Path, result: OpenShellCertificationResult) -> None:
    target = Path(path)
    if target.is_symlink():
        raise CertificationValidationError(
            "certification result path may not be a symlink"
        )
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


def _validate_profile_paths(
    values: Tuple[str, ...],
    label: str,
    *,
    allow_empty: bool,
) -> None:
    if (
        (not values and not allow_empty)
        or len(values) > 4096
        or any(not isinstance(path, str) for path in values)
        or list(values) != sorted(set(values))
    ):
        raise CertificationValidationError(
            "certifier phase profile %s are invalid" % label
        )
    for path in values:
        if (
            not path
            or len(path.encode("utf-8")) > 1024
            or path.startswith("/")
            or path.endswith("/")
            or posixpath.normpath(path) != path
            or ".." in path.split("/")
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
        ):
            raise CertificationValidationError(
                "certifier phase profile %s are unsafe" % label
            )


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
    "CERTIFIER_PHASE_PROFILE_SCHEMA",
    "CLEANUP_ALERT_SCHEMA",
    "CertificationCheckResult",
    "CertificationCleanupError",
    "CertificationPolicy",
    "CertificationValidationError",
    "CertifierPhaseExpectation",
    "CertifierPhaseProfile",
    "CertifierPhaseSelection",
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
    "validate_certifier_phase_manifest",
]
