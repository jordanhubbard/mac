"""CLI contracts for deployment-owned agent attestation controls."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from mac.cli import main
from mac.models import read_only_report_repository_executor_attestation


def _run(tmp_path: Path, *args: str) -> tuple[int, Any]:
    """Run ``mac --db <tmp> <args>`` and return its JSON result."""

    out = io.StringIO()
    previous = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", str(tmp_path / "mac.db"), *args])
    finally:
        sys.stdout = previous
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None


class _RecordingPlane:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def recover_agent_attestation_key(
        self, agent_id: str, probe: dict[str, Any]
    ) -> str:
        self.calls.append(("recover", agent_id, probe))
        return "replacement-attestation-key-" + "x" * 40

    def approve_agent_report_repository_executor(
        self,
        agent_id: str,
        attestation: dict[str, Any],
        startup_timestamp: str,
        *,
        actor: str,
    ) -> dict[str, Any]:
        self.calls.append(("approve", agent_id, attestation, startup_timestamp, actor))
        return {
            "id": agent_id,
            "report_repository_executor": "approved",
            "actor": actor,
        }

    def revoke_agent_report_repository_executor(
        self,
        agent_id: str,
        reason: str,
        *,
        actor: str,
    ) -> dict[str, Any]:
        self.calls.append(("revoke", agent_id, reason, actor))
        return {
            "id": agent_id,
            "report_repository_executor": "revoked",
            "reason": reason,
            "actor": actor,
        }


def test_agent_attestation_recover_sends_exact_probe_and_writes_private_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = {
        "schema": "mac.agent_attestation_key_probe.v1",
        "state": "missing",
        "agent_id": "agent_worker",
        "deployment_id": "deployment-42",
        "challenge": {},
        "signature": "",
    }
    probe_path = tmp_path / "probe.json"
    probe_path.write_text(json.dumps(probe), encoding="utf-8")
    manifest_path = tmp_path / "handoff" / "recovery.json"
    plane = _RecordingPlane()
    monkeypatch.setattr("mac.cli._plane", lambda _args: plane)

    rc, result = _run(
        tmp_path,
        "agent",
        "attestation-recover",
        "agent_worker",
        "--probe-file",
        str(probe_path),
        "--manifest-out",
        str(manifest_path),
    )

    assert rc == 0
    assert plane.calls == [("recover", "agent_worker", probe)]
    assert result == {
        "status": "rotation_manifest_written",
        "agent_id": "agent_worker",
        "deployment_id": "deployment-42",
        "manifest": str(manifest_path),
    }
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest == {
        "schema": "mac.agent_attestation_key_recovery.v1",
        "agent_id": "agent_worker",
        "deployment_id": "deployment-42",
        "attestation_key": "replacement-attestation-key-" + "x" * 40,
        "issued_at": manifest["issued_at"],
    }
    assert os.stat(manifest_path).st_mode & 0o077 == 0
    assert "attestation_key" not in result


def test_agent_report_executor_approve_sends_exact_controller_cas_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attestation = read_only_report_repository_executor_attestation(
        runtime_image_ref="registry.example/mac@sha256:" + "a" * 64,
        policy_sha256="sha256:" + "b" * 64,
        openshell_bin_path="/opt/openshell",
        openshell_bin_sha256="sha256:" + "c" * 64,
        executor_path="/opt/mac-task-executor",
        executor_sha256="sha256:" + "d" * 64,
        platform="linux",
        isolation_posture="landlock_enforced",
        python_path="/opt/python",
        python_sha256="sha256:" + "e" * 64,
        executor_script_path="/opt/mac-task-executor.py",
        executor_script_sha256="sha256:" + "f" * 64,
        source_root="/opt/mac",
        source_bundle_sha256="sha256:" + "0" * 64,
    )
    attestation_path = tmp_path / "report-attestation.json"
    attestation_path.write_text(json.dumps(attestation), encoding="utf-8")
    plane = _RecordingPlane()
    monkeypatch.setattr("mac.cli._plane", lambda _args: plane)

    rc, result = _run(
        tmp_path,
        "agent",
        "report-executor-approve",
        "agent_worker",
        "--attestation-file",
        str(attestation_path),
        "--startup-timestamp",
        "2026-07-18T12:34:56Z",
        "--actor",
        "cutover-controller",
    )

    assert rc == 0
    assert plane.calls == [
        (
            "approve",
            "agent_worker",
            attestation,
            "2026-07-18T12:34:56Z",
            "cutover-controller",
        )
    ]
    assert result == {
        "id": "agent_worker",
        "report_repository_executor": "approved",
        "actor": "cutover-controller",
    }


def test_agent_report_executor_revoke_sends_exact_reason_and_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plane = _RecordingPlane()
    monkeypatch.setattr("mac.cli._plane", lambda _args: plane)

    rc, result = _run(
        tmp_path,
        "agent",
        "report-executor-revoke",
        "agent_worker",
        "--reason",
        "executor artifact drift",
        "--actor",
        "security-operator",
    )

    assert rc == 0
    assert plane.calls == [
        (
            "revoke",
            "agent_worker",
            "executor artifact drift",
            "security-operator",
        )
    ]
    assert result == {
        "id": "agent_worker",
        "report_repository_executor": "revoked",
        "reason": "executor artifact drift",
        "actor": "security-operator",
    }


@pytest.mark.parametrize(
    ("argv", "missing_option"),
    [
        (
            [
                "agent",
                "attestation-recover",
                "agent_worker",
                "--probe-file",
                "probe.json",
            ],
            "--manifest-out",
        ),
        (
            [
                "agent",
                "report-executor-approve",
                "agent_worker",
                "--attestation-file",
                "attestation.json",
            ],
            "--startup-timestamp",
        ),
        (
            ["agent", "report-executor-revoke", "agent_worker"],
            "--reason",
        ),
    ],
)
def test_agent_deployment_control_required_arguments_fail_closed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    missing_option: str,
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--db", str(tmp_path / "mac.db"), *argv])

    assert exc_info.value.code == 2
    assert missing_option in capsys.readouterr().err


@pytest.mark.parametrize(
    ("command", "file_option"),
    [
        ("attestation-recover", "--probe-file"),
        ("report-executor-approve", "--attestation-file"),
    ],
)
def test_agent_deployment_control_rejects_non_object_evidence_before_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    file_option: str,
) -> None:
    malformed = tmp_path / (command + ".json")
    malformed.write_text("[]", encoding="utf-8")
    plane = _RecordingPlane()
    monkeypatch.setattr("mac.cli._plane", lambda _args: plane)
    trailing = (
        ["--manifest-out", str(tmp_path / "must-not-exist.json")]
        if command == "attestation-recover"
        else ["--startup-timestamp", "2026-07-18T12:34:56Z"]
    )

    rc, result = _run(
        tmp_path,
        "agent",
        command,
        "agent_worker",
        file_option,
        str(malformed),
        *trailing,
    )

    assert rc == 1
    assert result is None
    assert plane.calls == []
    assert "must be a JSON object" in capsys.readouterr().err
    assert not (tmp_path / "must-not-exist.json").exists()
