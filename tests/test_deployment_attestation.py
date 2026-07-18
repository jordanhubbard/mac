from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mac import deployment_attestation
from mac.api import create_app
from mac.deployment_attestation import (
    DeploymentAttestationError,
    build_key_probe,
    install_recovery_manifest,
    recovery_manifest,
)
from mac.models import (
    REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY,
    REPORT_REPOSITORY_EXECUTOR_ATTESTATION_KEY,
    ValidationError,
    agent_has_read_only_report_repository_executor,
    read_only_report_repository_executor_attestation,
)
from mac.services import ControlPlane


def _write_env(path: Path, key: str) -> None:
    path.write_text("MAC_ATTESTATION_KEY=%s\n" % key, encoding="utf-8")
    path.chmod(0o600)


def _write_manifest(path: Path, value) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def _report_attestation():
    return read_only_report_repository_executor_attestation(
        runtime_image_ref=(
            "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "1" * 64
        ),
        policy_sha256="sha256:" + "2" * 64,
        openshell_bin_path="/opt/openshell",
        openshell_bin_sha256="sha256:" + "3" * 64,
        executor_path="/opt/mac-task-executor",
        executor_sha256="sha256:" + "4" * 64,
        platform="linux",
        isolation_posture="landlock_enforced",
        python_path="/opt/python",
        python_sha256="sha256:" + "5" * 64,
        executor_script_path="/opt/mac-task-executor.py",
        executor_script_sha256="sha256:" + "6" * 64,
        source_root="/opt/mac",
        source_bundle_sha256="sha256:" + "7" * 64,
    )


def _startup(agent_id: str, attestation, timestamp="2026-07-18T12:00:00Z"):
    return {
        "schema": "mac.agent_startup_self_test.v1",
        "timestamp": timestamp,
        "status": "passed",
        "agent_id": agent_id,
        "checks": {
            "openshell_executor_config": True,
            "report_repository_executor_attestation": True,
        },
        "report_repository_executor_attestation": attestation,
        "blocking_problems": [],
    }


def test_probe_is_secret_free_and_recovery_rotates_only_stale(tmp_path):
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("probe-host")
    agent = cp.register_agent(machine.id, "probe-worker")
    original = agent.attestation_key
    env_file = tmp_path / "mac.env"
    _write_env(env_file, original)

    valid_probe = build_key_probe(
        agent.id, "deployment-1", env_file, nonce="n" * 40
    )
    assert original not in json.dumps(valid_probe)
    assert cp.verify_agent_attestation_challenge(
        agent.id, valid_probe["challenge"], valid_probe["signature"]
    )
    with pytest.raises(ValidationError, match="already valid"):
        cp.recover_agent_attestation_key(agent.id, valid_probe)

    _write_env(env_file, "stale-local-key-that-is-at-least-thirty-two-bytes")
    stale_probe = build_key_probe(
        agent.id, "deployment-1", env_file, nonce="s" * 40
    )
    replacement = cp.recover_agent_attestation_key(agent.id, stale_probe)
    assert replacement != original
    _write_env(env_file, replacement)
    proved = build_key_probe(
        agent.id, "deployment-1", env_file, nonce="p" * 40
    )
    assert cp.verify_agent_attestation_challenge(
        agent.id, proved["challenge"], proved["signature"]
    )


def test_recovery_manifest_install_is_owner_only_atomic_and_one_use(tmp_path):
    env_file = tmp_path / "mac.env"
    _write_env(env_file, "old-key-that-is-at-least-thirty-two-bytes")
    manifest_path = tmp_path / "recovery.json"
    manifest_path.write_text(
        json.dumps(
            recovery_manifest(
                "agent_worker", "deployment-2", "new-key-that-is-at-least-thirty-two-bytes"
            )
        ),
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)

    receipt = install_recovery_manifest(
        manifest_path,
        env_file,
        expected_agent_id="agent_worker",
        expected_deployment_id="deployment-2",
    )
    assert receipt["installed"] is True
    assert not manifest_path.exists()
    assert "new-key" in env_file.read_text(encoding="utf-8")
    assert os.stat(env_file).st_mode & 0o077 == 0


def test_recovery_install_rejects_public_or_symlink_manifest(tmp_path):
    with pytest.raises(DeploymentAttestationError, match="unreadable"):
        install_recovery_manifest(
            tmp_path / "missing.json",
            tmp_path / "env",
            expected_agent_id="agent_worker",
            expected_deployment_id="deployment-3",
        )

    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    source.chmod(0o644)
    with pytest.raises(DeploymentAttestationError, match="owner-only"):
        install_recovery_manifest(
            source,
            tmp_path / "env",
            expected_agent_id="agent_worker",
            expected_deployment_id="deployment-3",
        )
    source.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(source)
    with pytest.raises(DeploymentAttestationError, match="regular file"):
        install_recovery_manifest(
            link,
            tmp_path / "env",
            expected_agent_id="agent_worker",
            expected_deployment_id="deployment-3",
        )


def test_probe_reports_missing_key_and_rejects_untrusted_environment_files(tmp_path):
    missing = build_key_probe(
        "agent_worker",
        "deployment-missing",
        tmp_path / "absent.env",
        nonce="m" * 40,
    )
    assert missing == {
        "schema": deployment_attestation.PROBE_SCHEMA,
        "state": "missing",
        "agent_id": "agent_worker",
        "deployment_id": "deployment-missing",
        "challenge": {},
        "signature": "",
    }

    public_env = tmp_path / "public.env"
    _write_env(public_env, "x" * 32)
    public_env.chmod(0o644)
    with pytest.raises(DeploymentAttestationError, match="owner-only"):
        build_key_probe("agent_worker", "deployment-public", public_env)

    private_env = tmp_path / "private.env"
    _write_env(private_env, "x" * 32)
    linked_env = tmp_path / "linked.env"
    linked_env.symlink_to(private_env)
    with pytest.raises(DeploymentAttestationError, match="regular file"):
        build_key_probe("agent_worker", "deployment-linked", linked_env)


@pytest.mark.parametrize(
    ("agent_id", "deployment_id", "key", "message"),
    [
        ("agent_worker", "deployment-4", "short", "unsafe shape"),
        (
            "agent_worker",
            "deployment-4",
            "x" * 16 + " " + "x" * 16,
            "unsafe shape",
        ),
        ("", "deployment-4", "x" * 32, "agent id is required"),
        ("agent_worker", "bad\x00deployment", "x" * 32, "deployment id is required"),
    ],
)
def test_recovery_manifest_rejects_unsafe_fields(
    agent_id, deployment_id, key, message
):
    with pytest.raises(DeploymentAttestationError, match=message):
        recovery_manifest(agent_id, deployment_id, key)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("invalid-json", "unreadable"),
        ("not-object", "malformed"),
        ("extra-field", "malformed"),
        ("wrong-schema", "schema is unsupported"),
        ("wrong-agent", "agent does not match"),
        ("wrong-deployment", "deployment does not match"),
        ("short-key", "unsafe shape"),
        ("whitespace-key", "unsafe shape"),
    ],
)
def test_recovery_install_rejects_malformed_or_mismatched_manifest_without_consuming(
    tmp_path, mutation, message
):
    source = tmp_path / (mutation + ".json")
    manifest = recovery_manifest("agent_worker", "deployment-5", "x" * 32)
    if mutation == "invalid-json":
        source.write_text("{not-json", encoding="utf-8")
        source.chmod(0o600)
    elif mutation == "not-object":
        _write_manifest(source, [])
    else:
        if mutation == "extra-field":
            manifest["unexpected"] = True
        elif mutation == "wrong-schema":
            manifest["schema"] = "mac.agent_attestation_key_recovery.v0"
        elif mutation == "wrong-agent":
            manifest["agent_id"] = "agent_intruder"
        elif mutation == "wrong-deployment":
            manifest["deployment_id"] = "deployment-other"
        elif mutation == "short-key":
            manifest["attestation_key"] = "short"
        elif mutation == "whitespace-key":
            manifest["attestation_key"] = "x" * 16 + " " + "x" * 16
        _write_manifest(source, manifest)

    destination = tmp_path / "mac.env"
    with pytest.raises(DeploymentAttestationError, match=message):
        install_recovery_manifest(
            source,
            destination,
            expected_agent_id="agent_worker",
            expected_deployment_id="deployment-5",
        )
    assert source.exists(), "validation failure must not consume recovery authority"
    assert not destination.exists(), "validation failure must not install a key"


@pytest.mark.parametrize(
    ("expected_agent_id", "expected_deployment_id", "message"),
    [
        ("", "deployment-6", "expected agent id is required"),
        ("agent_worker", "bad\x00deployment", "expected deployment id is required"),
    ],
)
def test_recovery_install_rejects_invalid_expected_identity_without_consuming(
    tmp_path, expected_agent_id, expected_deployment_id, message
):
    source = tmp_path / "recovery.json"
    _write_manifest(
        source,
        recovery_manifest("agent_worker", "deployment-6", "x" * 32),
    )

    with pytest.raises(DeploymentAttestationError, match=message):
        install_recovery_manifest(
            source,
            tmp_path / "mac.env",
            expected_agent_id=expected_agent_id,
            expected_deployment_id=expected_deployment_id,
        )
    assert source.exists()


@pytest.mark.parametrize("destination_kind", ["public", "symlink"])
def test_recovery_install_rejects_untrusted_destination_without_consuming(
    tmp_path, destination_kind
):
    source = tmp_path / "recovery.json"
    _write_manifest(
        source,
        recovery_manifest("agent_worker", "deployment-7", "x" * 32),
    )
    destination = tmp_path / "mac.env"
    if destination_kind == "public":
        _write_env(destination, "old-key-that-is-at-least-thirty-two-bytes")
        destination.chmod(0o644)
        expected_message = "owner-only"
    else:
        actual = tmp_path / "actual.env"
        _write_env(actual, "old-key-that-is-at-least-thirty-two-bytes")
        destination.symlink_to(actual)
        expected_message = "regular file"

    with pytest.raises(DeploymentAttestationError, match=expected_message):
        install_recovery_manifest(
            source,
            destination,
            expected_agent_id="agent_worker",
            expected_deployment_id="deployment-7",
        )
    assert source.exists()


def test_recovery_install_replace_failure_is_atomic_and_preserves_manifest(
    tmp_path, monkeypatch
):
    source = tmp_path / "recovery.json"
    _write_manifest(
        source,
        recovery_manifest("agent_worker", "deployment-8", "x" * 32),
    )
    destination = tmp_path / "mac.env"
    original = "MAC_ATTESTATION_KEY=" + "o" * 32 + "\n"
    destination.write_text(original, encoding="utf-8")
    destination.chmod(0o600)

    def fail_replace(_source, _destination):
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(deployment_attestation.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated atomic replace failure"):
        install_recovery_manifest(
            source,
            destination,
            expected_agent_id="agent_worker",
            expected_deployment_id="deployment-8",
        )
    assert source.exists()
    assert destination.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("mac.env.*")) == []


def test_command_handoff_writes_private_probe_and_install_artifacts(tmp_path, capsys):
    assert deployment_attestation.main(
        [
            "probe",
            "--agent-id",
            "agent_worker",
            "--deployment-id",
            "deployment-stdout-only",
            "--env-file",
            str(tmp_path / "absent.env"),
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "missing"

    probe_output = tmp_path / "nested" / "probe.json"
    assert deployment_attestation.main(
        [
            "probe",
            "--agent-id",
            "agent_worker",
            "--deployment-id",
            "deployment-cli",
            "--env-file",
            str(tmp_path / "absent.env"),
            "--output",
            str(probe_output),
        ]
    ) == 0
    probe_stdout = json.loads(capsys.readouterr().out)
    assert json.loads(probe_output.read_text(encoding="utf-8")) == probe_stdout
    assert stat_mode(probe_output) == 0o600

    source = tmp_path / "recovery.json"
    _write_manifest(
        source,
        recovery_manifest("agent_worker", "deployment-cli", "x" * 32),
    )
    destination = tmp_path / "installed.env"
    receipt_output = tmp_path / "nested" / "receipt.json"
    assert deployment_attestation.main(
        [
            "install",
            "--manifest",
            str(source),
            "--env-file",
            str(destination),
            "--agent-id",
            "agent_worker",
            "--deployment-id",
            "deployment-cli",
            "--receipt-out",
            str(receipt_output),
        ]
    ) == 0
    receipt_stdout = json.loads(capsys.readouterr().out)
    assert json.loads(receipt_output.read_text(encoding="utf-8")) == receipt_stdout
    assert stat_mode(receipt_output) == 0o600


def test_command_reports_contract_errors_without_traceback(tmp_path, capsys):
    assert deployment_attestation.main(
        [
            "probe",
            "--agent-id",
            "",
            "--deployment-id",
            "deployment-cli-error",
            "--env-file",
            str(tmp_path / "absent.env"),
        ]
    ) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "deployment attestation error: agent id is required\n"


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def test_report_executor_approval_cas_binds_current_startup_and_revokes():
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("report-host")
    attestation = _report_attestation()
    agent_id = "agent_report-worker"
    agent = cp.register_agent(
        machine.id,
        "report-worker",
        agent_id=agent_id,
        resources={
            "openshell_required": True,
            REPORT_REPOSITORY_EXECUTOR_ATTESTATION_KEY: attestation,
            "startup_self_test": _startup(agent_id, attestation),
        },
    )
    assert not agent_has_read_only_report_repository_executor(agent.resources)

    with pytest.raises(ValidationError, match="startup proof"):
        cp.approve_agent_report_repository_executor(
            agent.id,
            attestation,
            "stale-timestamp",
        )
    approved = cp.approve_agent_report_repository_executor(
        agent.id,
        attestation,
        "2026-07-18T12:00:00Z",
    )
    assert agent_has_read_only_report_repository_executor(approved.resources)
    assert REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY in approved.resources

    revoked = cp.revoke_agent_report_repository_executor(
        agent.id, "deployment artifact changed"
    )
    assert not agent_has_read_only_report_repository_executor(revoked.resources)
    assert REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY not in revoked.resources


def test_recovery_and_report_approval_routes_are_admin_only(tmp_path):
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("api-host")
    agent = cp.register_agent(machine.id, "api-worker")
    env_file = tmp_path / "mac.env"
    _write_env(env_file, "stale-local-key-that-is-at-least-thirty-two-bytes")
    probe = build_key_probe(agent.id, "deployment-api", env_file, nonce="a" * 40)
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "worker": {
                    "scopes": ["agent", "read", "write"],
                    "agent_id": agent.id,
                },
                "admin": ["admin"],
            },
        )
    )
    path = "/agents/%s/attestation-key/recover" % agent.id
    assert client.post(
        path,
        headers={"Authorization": "Bearer worker"},
        json={"probe": probe},
    ).status_code == 403
    recovered = client.post(
        path,
        headers={"Authorization": "Bearer admin"},
        json={"probe": probe},
    )
    assert recovered.status_code == 200
    assert recovered.json()["attestation_key"]


def test_atomic_release_revalidates_report_executor_proof():
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("release-host")
    attestation = _report_attestation()
    agent_id = "agent_release-worker"
    resources = {
        "openshell_required": True,
        "deployment_generation": "generation-1",
        REPORT_REPOSITORY_EXECUTOR_ATTESTATION_KEY: attestation,
        "startup_self_test": _startup(agent_id, attestation),
    }
    cp.register_agent(
        machine.id,
        "release-worker",
        agent_id=agent_id,
        resources=resources,
    )
    cp.approve_agent_report_repository_executor(
        agent_id, attestation, "2026-07-18T12:00:00Z"
    )
    cp.set_agent_dispatch_hold(agent_id, "deployment-one")
    released = cp.release_agent_dispatch_holds_batch(
        [(agent_id, "deployment-one")],
        epoch_id="epoch-one",
        expectations={
            agent_id: {
                "generation": "generation-1",
                "baseline_seen": "2020-01-01T00:00:00+00:00",
                "require_authenticated": False,
                "require_report_executor": True,
            }
        },
    )
    assert released[0].dispatch_hold is False

    cp.set_agent_dispatch_hold(agent_id, "deployment-two")
    cp.revoke_agent_report_repository_executor(agent_id, "artifact drift")
    with pytest.raises(ValidationError, match="lost report executor proof"):
        cp.release_agent_dispatch_holds_batch(
            [(agent_id, "deployment-two")],
            epoch_id="epoch-two",
            expectations={
                agent_id: {
                    "generation": "generation-1",
                    "baseline_seen": "2020-01-01T00:00:00+00:00",
                    "require_authenticated": False,
                    "require_report_executor": True,
                }
            },
        )
    assert cp.get_agent(agent_id).dispatch_hold is True


def test_atomic_successor_hold_revalidates_report_executor_proof():
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("transition-host")
    attestation = _report_attestation()
    agent_id = "agent_transition-worker"
    resources = {
        "openshell_required": True,
        "deployment_generation": "generation-transition",
        REPORT_REPOSITORY_EXECUTOR_ATTESTATION_KEY: attestation,
        "startup_self_test": _startup(agent_id, attestation),
    }
    cp.register_agent(
        machine.id,
        "transition-worker",
        agent_id=agent_id,
        resources=resources,
    )
    cp.approve_agent_report_repository_executor(
        agent_id, attestation, "2026-07-18T12:00:00Z"
    )
    cp.set_agent_dispatch_hold(agent_id, "deployment-transition-one")
    transitioned = cp.release_agent_dispatch_holds_batch(
        [(agent_id, "deployment-transition-one")],
        epoch_id="epoch-transition-one",
        successor_reason="successor-transition-one",
        expectations={
            agent_id: {
                "generation": "generation-transition",
                "baseline_seen": "2020-01-01T00:00:00+00:00",
                "require_authenticated": False,
                "require_report_executor": True,
            }
        },
    )
    assert transitioned[0].dispatch_hold is True
    assert transitioned[0].dispatch_hold_reason == "successor-transition-one"

    cp.revoke_agent_report_repository_executor(agent_id, "artifact drift")
    with pytest.raises(ValidationError, match="lost report executor proof"):
        cp.release_agent_dispatch_holds_batch(
            [(agent_id, "successor-transition-one")],
            epoch_id="epoch-transition-two",
            successor_reason="successor-transition-two",
            expectations={
                agent_id: {
                    "generation": "generation-transition",
                    "baseline_seen": "2020-01-01T00:00:00+00:00",
                    "require_authenticated": False,
                    "require_report_executor": True,
                }
            },
        )
    held = cp.get_agent(agent_id)
    assert held.dispatch_hold is True
    assert held.dispatch_hold_reason == "successor-transition-one"
