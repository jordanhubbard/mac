from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

import pytest

from mac.openshell_certifier import (
    CERTIFICATION_ISOLATION_SCHEMA,
    CERTIFICATION_RESULT_SCHEMA,
    CertificationCleanupError,
    CertificationPolicy,
    CertificationValidationError,
    CommandOutcome,
    ControllerCommand,
    OpenShellCertificationJob,
    OpenShellCertificationRunner,
)


POLICY_TEXT = """\
version: 1
filesystem_policy:
  include_workdir: true
  read_only:
    - /usr
    - /bin
    - /etc
  read_write:
    - /tmp
    - /dev
landlock:
  compatibility: hard_requirement
process:
  run_as_user: sandbox
  run_as_group: sandbox
network_policies: {}
"""


class RecordingRunner:
    def __init__(self, returncodes: Sequence[int]) -> None:
        self.returncodes = list(returncodes)
        self.calls: list[tuple[tuple[str, ...], Mapping[str, str], float]] = []

    def run(self, argv, *, env, timeout_seconds):
        command = tuple(argv)
        self.calls.append((command, dict(env), float(timeout_seconds)))
        if not self.returncodes:
            raise AssertionError("unexpected certifier command: %r" % (command,))
        returncode = self.returncodes.pop(0)
        stdout = "ok\n" if returncode == 0 else ""
        if "/opt/mac-certifier/bin/run-contract-tests" in command:
            stdout = _phase_output(command) + stdout
        return CommandOutcome(
            command,
            returncode,
            stdout=stdout,
            stderr="failed\n" if returncode else "",
            timed_out=returncode == 124,
        )


class MissingPhaseRunner(RecordingRunner):
    def run(self, argv, *, env, timeout_seconds):
        outcome = super().run(argv, env=env, timeout_seconds=timeout_seconds)
        if "/opt/mac-certifier/bin/run-contract-tests" not in outcome.argv:
            return outcome
        return CommandOutcome(
            outcome.argv,
            outcome.returncode,
            "ok\n",
            outcome.stderr,
            outcome.timed_out,
        )


def _digest_bytes(value: bytes) -> str:
    return "sha256:%s" % hashlib.sha256(value).hexdigest()


def _digest_json(value: object) -> str:
    return _digest_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    )


def _phase_output(command: tuple[str, ...]) -> str:
    base = command[command.index("--base-sha") + 1]
    changed = ["docs/canaries/test.md"]
    payload = {
        "schema": "mac.certifier_phase_manifest.v1",
        "trusted_source_revision": "f" * 40,
        "assembly_base_sha": base,
        "candidate_sha": "a" * 40,
        "changed_files": changed,
        "changed_file_count": 1,
        "changed_files_digest": _digest_json(changed),
        "selection_mode": "documentation_fast_lane",
        "authoritative": {
            "mode": "focused",
            "reason": "documentation_only_invariants",
            "tests": [
                "tests/test_openshell_certifier.py",
                "tests/test_publication_lane.py",
                "tests/test_repository_contract_certification.py",
            ],
        },
        "supplemental": {
            "mode": "skipped",
            "reason": "documentation_only",
            "tests": [],
        },
        "full_suite_count": 0,
    }
    payload["manifest_digest"] = _digest_json(payload)
    return "MAC_CERTIFIER_PHASE_MANIFEST_JSON=" + json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ) + "\n"


def _policy(text: str = POLICY_TEXT) -> CertificationPolicy:
    return CertificationPolicy(
        "trusted-repository-default",
        7,
        _digest_bytes(text.encode("utf-8")),
        text,
    )


def _bundle(tmp_path: Path, *, content: bytes | None = None) -> tuple[Path, str]:
    payload = content or (b"# v2 git bundle\n" + b"fixture-object-data\n")
    path = tmp_path / "candidate.bundle"
    path.write_bytes(payload)
    return path, _digest_bytes(payload)


def _job(tmp_path: Path, **changes) -> OpenShellCertificationJob:
    if "bundle_path" in changes:
        bundle_path = Path(changes["bundle_path"])
        bundle_digest = str(changes.get("bundle_digest") or "")
    else:
        bundle_path, bundle_digest = _bundle(tmp_path)
    values = {
        "job_id": "certjob-1",
        "batch_id": "batch-1",
        "package_id": "package-1",
        "plan_version": 3,
        "epoch": 5,
        "candidate_sha": "a" * 40,
        "candidate_tree_digest": "git-tree:" + "b" * 40,
        "assembly_base_sha": "c" * 40,
        "landing_base_sha": "d" * 40,
        "target_ref": "refs/heads/main",
        "policy": _policy(),
        "image_ref": "registry.invalid/mac-certifier@sha256:" + "e" * 64,
        "bundle_path": bundle_path,
        "bundle_digest": bundle_digest,
        "controller_commands": (
            ControllerCommand(
                "contract-tests",
                (
                    "/opt/mac-certifier/bin/run-contract-tests",
                    "--base-sha",
                    "c" * 40,
                ),
                300,
            ),
        ),
        "lifecycle_timeout_seconds": 90,
    }
    values.update(changes)
    return OpenShellCertificationJob(**values)


def _runner(command_runner, **changes) -> OpenShellCertificationRunner:
    values = {
        "command_runner": command_runner,
        "openshell_bin": "/usr/bin/openshell",
        "launcher_environment": {"HOME": "/tmp", "PATH": "/safe/bin", "LANG": "C"},
        "name_factory": lambda: "mac-cert-focused",
        "now": lambda: datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc),
    }
    values.update(changes)
    return OpenShellCertificationRunner(**values)


def test_runner_enforces_exact_identity_and_captures_result(tmp_path: Path) -> None:
    commands = RecordingRunner([0, 0, 0, 0, 0])
    runner = _runner(commands)
    job = _job(tmp_path)
    result_path = tmp_path / "result.json"

    result = runner.run(job, result_path=result_path)

    assert result.status == "passed"
    assert result.cleanup_status == "deleted"
    assert result.candidate_sha == job.candidate_sha
    assert result.candidate_tree_digest == job.candidate_tree_digest
    assert result.policy == job.policy.identity()
    assert result.image_digest == "sha256:" + "e" * 64
    assert result.bundle_digest == job.bundle_digest
    assert result.commands_digest == job.commands_digest
    assert result.isolation == {
        "schema": CERTIFICATION_ISOLATION_SCHEMA,
        "network": "disabled",
        "landing_credentials": "absent",
        "planner_commands": "rejected",
        "policy_source": "trusted_controller",
        "policy_id": job.policy.policy_id,
        "policy_version": job.policy.version,
        "policy_checksum": job.policy.checksum,
        "landlock": "hard_requirement",
        "run_as_user": "non_root",
        "launcher_environment": ["HOME", "LANG", "PATH"],
        "input_format": "credential_free_git_bundle",
        "assembly_base_transport": "controller_bound_argv",
    }
    captured = json.loads(result_path.read_text(encoding="utf-8"))
    assert captured == result.to_dict()
    assert captured["schema"] == CERTIFICATION_RESULT_SCHEMA
    digest_payload = dict(captured)
    digest = digest_payload.pop("result_digest")
    encoded = json.dumps(
        digest_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    assert digest == _digest_bytes(encoded)

    assert len(commands.calls) == 5
    create, setup, check, postcheck, cleanup = [item[0] for item in commands.calls]
    assert create[:4] == (
        "/usr/bin/openshell",
        "sandbox",
        "create",
        "--no-auto-providers",
    )
    assert create[create.index("--from") + 1] == job.image_ref
    assert "--policy" in create
    upload = create[create.index("--upload") + 1]
    assert upload.endswith(":/sandbox/input/candidate.bundle")
    assert not upload.startswith(str(job.bundle_path) + ":")
    assert setup[1:3] == ("sandbox", "exec")
    assert check[check.index("--") + 1 : check.index("--") + 6] == (
        "/usr/bin/env",
        "-i",
        "HOME=/tmp",
        "PATH=/opt/mac-venv/bin:/usr/local/bin:/usr/bin:/bin",
        "/opt/mac-certifier/bin/run-contract-tests",
    )
    assert check[-2:] == ("--base-sha", job.assembly_base_sha)
    assert "/bin/bash" not in check
    assert postcheck[1:3] == ("sandbox", "exec")
    assert cleanup == (
        "/usr/bin/openshell",
        "sandbox",
        "delete",
        "mac-cert-focused",
    )
    assert [item[2] for item in commands.calls] == [90.0, 90.0, 330.0, 90.0, 90.0]
    assert all(item[1] == {"HOME": "/tmp", "PATH": "/safe/bin", "LANG": "C"} for item in commands.calls)


@pytest.mark.parametrize(
    "old,new,problem",
    [
        ("network_policies: {}", "network_policies:\n  outbound: {}", "network"),
        ("compatibility: hard_requirement", "compatibility: best_effort", "Landlock"),
        ("run_as_user: sandbox", "run_as_user: root", "non-root user"),
        ("    - /dev", "    - /home", "writable roots"),
        (
            "  run_as_group: sandbox",
            "  run_as_group: sandbox\n  privileged: true",
            "process controls",
        ),
    ],
)
def test_policy_semantics_fail_closed(old: str, new: str, problem: str) -> None:
    text = POLICY_TEXT.replace(old, new)
    with pytest.raises(CertificationValidationError, match=problem):
        _policy(text).validate()


def test_policy_identity_is_exact() -> None:
    policy = replace(_policy(), checksum="sha256:" + "0" * 64)
    with pytest.raises(CertificationValidationError, match="checksum"):
        policy.validate()
    with pytest.raises(CertificationValidationError, match="version"):
        replace(_policy(), version=0).validate()


def test_bundled_lockdown_policy_satisfies_certifier_contract() -> None:
    path = Path(__file__).resolve().parents[1] / "src" / "mac" / "openshell" / "default-policy.yaml"
    text = path.read_text(encoding="utf-8")
    policy = CertificationPolicy(
        "bundled-lockdown",
        1,
        _digest_bytes(text.encode("utf-8")),
        text,
    )

    parsed = policy.validate()

    assert parsed["network_policies"] == {}
    assert parsed["landlock"]["compatibility"] == "hard_requirement"


@pytest.mark.parametrize(
    "field,value,problem",
    [
        ("image_ref", "registry.invalid/mac-certifier:latest", "immutable"),
        ("candidate_sha", "A" * 40, "candidate_sha"),
        ("candidate_tree_digest", "sha256:" + "b" * 64, "candidate_tree_digest"),
        ("target_ref", "refs/heads/../main", "target_ref"),
    ],
)
def test_job_rejects_mutable_or_inexact_identity(
    tmp_path: Path, field: str, value: str, problem: str
) -> None:
    with pytest.raises(CertificationValidationError, match=problem):
        _job(tmp_path, **{field: value}).validate()


@pytest.mark.parametrize(
    "executable",
    [
        "scripts/run-contract-tests.sh",
        "pytest",
        "/opt/mac-certifier/bin/../candidate-script",
        "/opt/mac-certifier/bin-not-trusted/run-tests",
    ],
)
def test_job_rejects_candidate_owned_controller_commands(
    tmp_path: Path, executable: str
) -> None:
    command = ControllerCommand("untrusted", (executable,), 30)

    with pytest.raises(CertificationValidationError, match="image-owned"):
        _job(tmp_path, controller_commands=(command,)).validate()


@pytest.mark.parametrize(
    "argv",
    [
        ("/opt/mac-certifier/bin/run-contract-tests",),
        (
            "/opt/mac-certifier/bin/run-contract-tests",
            "--base-sha",
            "d" * 40,
        ),
        (
            "/opt/mac-certifier/bin/run-contract-tests",
            "--base-sha",
            "c" * 40,
            "--base-sha",
            "c" * 40,
        ),
        (
            "/opt/mac-certifier/bin/run-contract-tests",
            "--base-sha=" + "c" * 40,
        ),
    ],
)
def test_job_requires_one_exact_controller_owned_base_argument(
    tmp_path: Path, argv: tuple[str, ...]
) -> None:
    command = ControllerCommand("contract-tests", argv, 30)

    with pytest.raises(CertificationValidationError, match="controller-owned assembly base"):
        _job(tmp_path, controller_commands=(command,)).validate()


def test_launcher_environment_rejects_credentials() -> None:
    with pytest.raises(CertificationValidationError, match="forbidden names"):
        OpenShellCertificationRunner(
            command_runner=RecordingRunner([]),
            launcher_environment={"PATH": "/safe", "LANDING_PUSH_SECRET": "secret"},
        )

    with pytest.raises(CertificationValidationError, match="loopback HTTP URL"):
        OpenShellCertificationRunner(
            command_runner=RecordingRunner([]),
            launcher_environment={
                "PATH": "/safe",
                "OPENSHELL_GATEWAY_ENDPOINT": "http://10.0.0.4:17670",
            },
        )


def test_production_binding_uses_absolute_cli_and_service_home_but_isolates_candidate(
    tmp_path: Path,
) -> None:
    home = tmp_path / "service-home"
    home.mkdir()
    binary = tmp_path / "openshell"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    commands = RecordingRunner([0, 0, 0, 0, 0, 0, 0])

    runner = OpenShellCertificationRunner.from_environment(
        {
            "MAC_OPENSHELL_BIN": str(binary),
            "MAC_CERTIFIER_OPENSHELL_GATEWAY_ENDPOINT": "http://127.0.0.1:17671/",
            "HOME": str(home),
            "PATH": "/managed/service/path:/usr/bin:/bin",
            "LANG": "C",
        },
        command_runner=commands,
        name_factory=lambda: "mac-cert-production-binding",
    )
    result = runner.run(_job(tmp_path), result_path=tmp_path / "bound-result.json")

    assert result.status == "passed"
    assert runner.openshell_bin == str(binary.resolve())
    assert runner.launcher_environment["HOME"] == str(home.resolve())
    assert (
        runner.launcher_environment["OPENSHELL_GATEWAY_ENDPOINT"]
        == "http://127.0.0.1:17671"
    )
    assert commands.calls[0][0] == (str(binary.resolve()), "--version")
    assert commands.calls[1][0] == (str(binary.resolve()), "status")
    assert all(call[1]["HOME"] == str(home.resolve()) for call in commands.calls)
    assert all(
        call[1]["OPENSHELL_GATEWAY_ENDPOINT"] == "http://127.0.0.1:17671"
        for call in commands.calls
    )
    create = commands.calls[2][0]
    assert create[create.index("--env") + 1] == "HOME=/tmp"
    check = commands.calls[4][0]
    assert check[check.index("--") + 1 : check.index("--") + 4] == (
        "/usr/bin/env",
        "-i",
        "HOME=/tmp",
    )


@pytest.mark.parametrize(
    ("binary_value", "problem"),
    [
        ("", "absolute executable"),
        ("relative/openshell", "absolute executable"),
        ("missing", "unavailable"),
        ("present", "not an executable"),
    ],
)
def test_production_binding_rejects_invalid_cli(
    tmp_path: Path,
    binary_value: str,
    problem: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    if binary_value in {"missing", "present"}:
        binary = tmp_path / binary_value
        if binary_value == "present":
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary_value = str(binary)

    with pytest.raises(CertificationValidationError, match=problem):
        OpenShellCertificationRunner.from_environment(
            {
                "MAC_OPENSHELL_BIN": binary_value,
                "HOME": str(home),
                "PATH": "/usr/bin:/bin",
            }
        )


def test_production_binding_fails_closed_when_cli_preflight_fails(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    binary = tmp_path / "openshell"
    binary.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
    binary.chmod(0o755)

    with pytest.raises(CertificationValidationError, match="preflight failed"):
        OpenShellCertificationRunner.from_environment(
            {
                "MAC_OPENSHELL_BIN": str(binary),
                "HOME": str(home),
                "PATH": "/usr/bin:/bin",
            }
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:17671",
        "http://localhost:17671",
        "http://100.121.27.109:17670",
        "http://user@127.0.0.1:17671",
        "http://127.0.0.1:17671/path",
        "http://127.0.0.1:17671?token=secret",
        "http://127.0.0.1",
        "http://127.0.0.1:99999",
    ],
)
def test_production_binding_rejects_non_loopback_gateway_endpoint(
    tmp_path: Path, endpoint: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    binary = tmp_path / "openshell"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)

    with pytest.raises(CertificationValidationError, match="loopback HTTP URL"):
        OpenShellCertificationRunner.from_environment(
            {
                "MAC_OPENSHELL_BIN": str(binary),
                "MAC_CERTIFIER_OPENSHELL_GATEWAY_ENDPOINT": endpoint,
                "HOME": str(home),
                "PATH": "/usr/bin:/bin",
            }
        )


def test_bundle_must_be_content_addressed_git_bundle(tmp_path: Path) -> None:
    bad_path, bad_digest = _bundle(tmp_path, content=b"not a tar or git bundle payload")
    commands = RecordingRunner([])
    with pytest.raises(CertificationValidationError, match="credential-free Git bundle"):
        _runner(commands).run(
            _job(tmp_path, bundle_path=bad_path, bundle_digest=bad_digest),
            result_path=tmp_path / "bad-result.json",
        )
    assert commands.calls == []

    real_path, _real_digest = _bundle(tmp_path)
    symlink = tmp_path / "candidate-link.bundle"
    symlink.symlink_to(real_path)
    with pytest.raises(CertificationValidationError, match="regular Git bundle"):
        _runner(commands).run(
            _job(
                tmp_path,
                bundle_path=symlink,
                bundle_digest=_digest_bytes(real_path.read_bytes()),
            ),
            result_path=tmp_path / "link-result.json",
        )


def test_bundle_digest_mismatch_stops_before_openshell(tmp_path: Path) -> None:
    commands = RecordingRunner([])
    with pytest.raises(CertificationValidationError, match="digest"):
        _runner(commands).run(
            _job(tmp_path, bundle_digest="sha256:" + "f" * 64),
            result_path=tmp_path / "result.json",
        )
    assert commands.calls == []


def test_controller_failure_is_a_captured_failed_certification(tmp_path: Path) -> None:
    commands = RecordingRunner([0, 0, 9, 0])
    result_path = tmp_path / "failed.json"

    result = _runner(commands).run(_job(tmp_path), result_path=result_path)

    assert result.status == "failed"
    assert result.failure_class == "controller_command_failed"
    assert result.cleanup_status == "deleted"
    assert len(result.checks) == 1
    assert result.checks[0].returncode == 9
    assert json.loads(result_path.read_text())["status"] == "failed"
    assert len(commands.calls) == 4  # post-identity check is not trusted after failure


def test_missing_phase_manifest_fails_even_when_tests_return_zero(tmp_path: Path) -> None:
    commands = MissingPhaseRunner([0, 0, 0, 0])

    result = _runner(commands).run(
        _job(tmp_path), result_path=tmp_path / "missing-phase.json"
    )

    assert result.status == "failed"
    assert result.failure_class == "certifier_phase_manifest_invalid"
    assert result.checks[0].returncode == 78
    assert result.phase_manifest == {}


def test_identity_mismatch_is_captured_and_never_runs_controller_commands(
    tmp_path: Path,
) -> None:
    commands = RecordingRunner([0, 64, 0])
    result = _runner(commands).run(
        _job(tmp_path), result_path=tmp_path / "identity-failed.json"
    )

    assert result.status == "failed"
    assert result.failure_class == "candidate_identity_setup_failed"
    assert result.checks == ()
    assert len(commands.calls) == 3


def test_cleanup_failure_alerts_records_failure_and_raises(tmp_path: Path) -> None:
    commands = RecordingRunner([0, 0, 0, 0, 17])
    alerts = []
    result_path = tmp_path / "cleanup-failed.json"

    with pytest.raises(CertificationCleanupError) as caught:
        _runner(commands, cleanup_alert_sink=alerts.append).run(
            _job(tmp_path), result_path=result_path
        )

    assert caught.value.alert.returncode == 17
    assert alerts == [caught.value.alert]
    captured = json.loads(result_path.read_text(encoding="utf-8"))
    assert captured["status"] == "failed"
    assert captured["cleanup_status"] == "failed"
    assert captured["failure_class"] == "sandbox_cleanup_failed"


def test_command_timeout_is_bounded_and_captured(tmp_path: Path) -> None:
    commands = RecordingRunner([0, 0, 124, 0])
    command = ControllerCommand(
        "bounded",
        ("/opt/mac-certifier/bin/run-bounded-tests", "--base-sha", "c" * 40),
        11,
    )
    primary = ControllerCommand(
        "contract-tests",
        ("/opt/mac-certifier/bin/run-contract-tests", "--base-sha", "c" * 40),
        300,
    )

    result = _runner(commands).run(
        _job(tmp_path, controller_commands=(command, primary)),
        result_path=tmp_path / "timeout.json",
    )

    assert result.status == "failed"
    assert result.failure_class == "controller_command_timed_out"
    assert result.checks[0].timed_out is True
    check_argv, _env, outer_timeout = commands.calls[2]
    assert check_argv[check_argv.index("--timeout") + 1] == "11"
    assert outer_timeout == 41.0
