"""Secret-safe worker evidence keeps outcome and signature authority intact."""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import pytest
from fastapi.testclient import TestClient
from mac.api import create_app
from mac.hermes_adapter import MacApiClient, MacApiError
from mac.models import TaskState
from mac.services import (
    ControlPlane,
    sign_verification_manifest,
    verify_verification_manifest_signature,
)
from mac.worker import MacWorker, WorkerExecution


def api_transport(client: TestClient):
    def transport(method: str, path: str, payload: Optional[Dict[str, Any]]) -> Any:
        request = getattr(client, method.lower())
        kwargs: Dict[str, Any] = {}
        if payload is not None:
            kwargs["json"] = payload
        response = request(path, **kwargs)
        if response.status_code >= 400:
            raise MacApiError(response.text)
        return response.json() if response.content else None

    return transport


def register_worker_fixture(cp: ControlPlane):
    machine = cp.register_machine("worker-host")
    agent = cp.register_agent(machine.id, "worker", capabilities=["python"])
    return agent


def assert_secret_absent(secret: str, value: object, *, path: str) -> None:
    if secret in str(value):
        pytest.fail(f"credential fixture persisted at {path}", pytrace=False)


def assert_raises_safely(exception_type: type[BaseException], action: Callable[[], object]) -> None:
    try:
        action()
    except exception_type:
        return
    except BaseException:
        pytest.fail("unexpected exception type", pytrace=False)
    pytest.fail("expected exception was not raised", pytrace=False)


def call_safely(action: Callable[[], object], *, path: str) -> Any:
    try:
        return action()
    except BaseException:
        pytest.fail(f"unexpected exception at {path}", pytrace=False)


def evidence_artifact_text(cp: ControlPlane, evidence_id: str) -> Dict[str, str]:
    artifacts = cp.list_evidence_artifacts(evidence_id)
    return {
        artifact["name"]: base64.b64decode(
            cp.get_evidence_artifact(evidence_id, artifact["id"])["content_base64"]
        ).decode("utf-8", errors="replace")
        for artifact in artifacts
    }


def persisted_task_state(cp: ControlPlane, task_id: str) -> Dict[str, Any]:
    evidence = cp.list_evidence(task_id)
    return {
        "task": cp.get_task(task_id).to_dict(),
        "evidence": [item.to_dict() for item in evidence],
        "artifacts": {item.id: evidence_artifact_text(cp, item.id) for item in evidence},
        "history": [event.to_dict() for event in cp.task_history(task_id)],
        "observability": [
            item.to_dict()
            for item in cp.list_observability(subject_type="task", subject_id=task_id, limit=200)
        ],
    }


def test_record_execution_redacts_before_manifest_signing_and_artifact_capture(
    tmp_path: Path,
):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    task = cp.create_task("Secret-safe operator result")
    cp.claim_task(task.id, agent.id)
    cp.start_task(task.id, agent.id)
    client = TestClient(create_app(control_plane=cp))
    leaked = "never-persist-this-value"
    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path,
        lambda _task, _task_dir: WorkerExecution(0, "unused"),
        attestation_key=cp._agent_attestation_key(agent.id),
    )
    task_dir = tmp_path / task.id
    task_dir.mkdir()
    (task_dir / "task.json").write_text(
        json.dumps({"task": task.to_dict()}),
        encoding="utf-8",
    )
    execution = WorkerExecution(
        0,
        f"completed with CURSOR_AUTH_TOKEN={leaked}",
        stdout=f"MAC_ATTESTATION_KEY={leaked}\n",
        stderr=f"Authorization: Bearer {leaked}\n",
        metadata={
            "verification": {
                "schema": "mac.worker_evidence.v1",
                "status": "pass",
                "evidence_type": "operator_result",
                "result": f"MAC_CONTROL_PLANE_DB_PASSWORD={leaked}",
            }
        },
    )

    evidence_result = worker._record_execution(
        task.id,
        task_dir,
        execution,
        lease_id=cp.get_task(task.id).lease_id,
    )

    evidence = cp.get_evidence(evidence_result["id"])
    assert_secret_absent(leaked, evidence.to_dict(), path="evidence")
    assert_secret_absent(
        leaked,
        evidence_artifact_text(cp, evidence.id),
        path="evidence.artifacts",
    )
    verification = evidence.metadata["verification"]
    assert verify_verification_manifest_signature(
        cp._agent_attestation_key(agent.id),
        verification,
        verification["signature"],
    )


def test_record_execution_redacts_executor_authored_manifest_before_capture(
    tmp_path: Path,
):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    task = cp.create_task("Secret-safe executor manifest")
    cp.claim_task(task.id, agent.id)
    cp.start_task(task.id, agent.id)
    client = TestClient(create_app(control_plane=cp))
    secret = "opaque-credential-fixture"
    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path,
        lambda _task, _task_dir: WorkerExecution(0, "unused"),
        attestation_key=cp._agent_attestation_key(agent.id),
    )
    task_dir = tmp_path / task.id
    task_dir.mkdir()
    (task_dir / "task.json").write_text(
        json.dumps({"task": task.to_dict()}),
        encoding="utf-8",
    )
    manifest_path = task_dir / "mac-evidence.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "mac.worker_evidence.v1",
                "status": "pass",
                "evidence_type": "operator_result",
                "result": f"operator completed with CURSOR_AUTH_TOKEN={secret}",
            }
        ),
        encoding="utf-8",
    )

    evidence_result = worker._record_execution(
        task.id,
        task_dir,
        WorkerExecution(0, "operator completed"),
        lease_id=cp.get_task(task.id).lease_id,
    )

    evidence = cp.get_evidence(evidence_result["id"])
    assert_secret_absent(secret, evidence.to_dict(), path="evidence.verification")
    assert_secret_absent(
        secret,
        manifest_path.read_text(encoding="utf-8"),
        path="workspace.mac-evidence.json",
    )
    assert_secret_absent(
        secret,
        evidence_artifact_text(cp, evidence.id),
        path="evidence.artifacts.mac-evidence.json",
    )
    verification = evidence.metadata["verification"]
    assert verify_verification_manifest_signature(
        cp._agent_attestation_key(agent.id),
        verification,
        verification["signature"],
    )


def test_failed_execution_redacts_persisted_diagnostics_without_changing_returncode(
    tmp_path: Path,
):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    task = cp.create_task("Secret-safe failed result", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))
    leaked = "never-persist-this-value"

    def executor(_task_payload: Dict[str, Any], _task_dir: Path) -> WorkerExecution:
        return WorkerExecution(
            17,
            f"RuntimeError: CURSOR_AUTH_TOKEN={leaked}",
            stdout=f"build step failed\nMAC_ATTESTATION_KEY={leaked}\n",
            stderr=f"Authorization: Bearer {leaked}\n",
        )

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path,
        executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "blocked"
    evidence = cp.list_evidence(task.id)[0]
    persisted = persisted_task_state(cp, task.id)
    assert_secret_absent(leaked, persisted, path="task.persistence")
    assert evidence.metadata["returncode"] == 17
    assert "build step failed" in str(persisted)


def test_timeout_redacts_output_before_evidence_logs_and_history(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    task = cp.create_task("Secret-safe timeout", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))
    secret = "opaque-credential-fixture"

    def executor(_task_payload: Dict[str, Any], _task_dir: Path) -> WorkerExecution:
        raise subprocess.TimeoutExpired(
            cmd="opaque-command",
            timeout=1,
            output=f"build timed out\nMAC_ATTESTATION_KEY={secret}\n",
            stderr=f"Authorization: Bearer {secret}\n",
        )

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path,
        executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    result = worker.run_once()

    assert result.status == "blocked"
    evidence = cp.list_evidence(task.id)[0]
    persisted = persisted_task_state(cp, task.id)
    assert_secret_absent(secret, persisted, path="timeout.persistence")
    assert evidence.metadata["returncode"] == 124
    assert "build timed out" in str(persisted)


def test_worker_exception_redacts_message_and_traceback_before_persistence(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = register_worker_fixture(cp)
    task = cp.create_task("Secret-safe exception", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))
    secret = "opaque-credential-fixture"

    def executor(_task_payload: Dict[str, Any], _task_dir: Path) -> WorkerExecution:
        raise RuntimeError(f"worker failed with CURSOR_AUTH_TOKEN={secret}")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        agent.id,
        tmp_path,
        executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )

    assert_raises_safely(RuntimeError, worker.run_once)

    persisted = persisted_task_state(cp, task.id)
    assert_secret_absent(secret, persisted, path="exception.persistence")
    assert "worker failed" in str(persisted)


def test_stale_exception_redacts_reason_before_observability_and_result(tmp_path: Path):
    cp = ControlPlane.in_memory()
    first = register_worker_fixture(cp)
    machine = cp.register_machine("stale-exception-worker-host")
    second = cp.register_agent(machine.id, "second-worker", capabilities=["python"])
    task = cp.create_task("Secret-safe stale exception", required_capabilities=["python"])
    cp.claim_task(task.id, first.id)
    client = TestClient(create_app(control_plane=cp))
    secret = "opaque-credential-fixture"

    def executor(_task_payload: Dict[str, Any], _task_dir: Path) -> WorkerExecution:
        current_lease_id = cp.get_task(task.id).lease_id
        assert current_lease_id is not None
        expired_at = "2000-01-01T00:00:00+00:00"
        cp.store.execute(
            "UPDATE leases SET expires_at = ?, updated_at = ? WHERE id = ?",
            (expired_at, expired_at, current_lease_id),
        )
        cp.store.execute(
            "UPDATE tasks SET leased_until = ?, updated_at = ? WHERE id = ?",
            (expired_at, expired_at, task.id),
        )
        cp.expire_leases()
        _, second_lease = cp.claim_task(task.id, second.id)
        cp.start_task(task.id, second.id, lease_id=second_lease.id)
        raise RuntimeError(f"stale executor failed with CURSOR_AUTH_TOKEN={secret}")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        first.id,
        tmp_path,
        executor,
    )

    result = call_safely(worker.run_once, path="stale_result")

    assert result.status == "stale_result"
    assert_secret_absent(secret, result.error, path="stale_result.error")
    observations = [
        item.to_dict()
        for item in cp.list_observability(
            layer="worker",
            name="worker.execution.stale_result",
            subject_id=task.id,
            limit=20,
        )
    ]
    assert_secret_absent(secret, observations, path="stale_result.observability")
    assert "stale executor failed" in str(observations)


@pytest.mark.parametrize(
    "kind", ["symlink", "hardlink", "fifo", "oversized", "malformed", "directory"]
)
def test_unsafe_manifest_is_invalid_and_outside_data_is_preserved(tmp_path, monkeypatch, kind):
    import os
    from mac.worker import _write_host_control_text

    workspace = tmp_path / "task"
    workspace.mkdir()
    outside = tmp_path / "outside.json"
    original = b'{"status":"pass","password":"synthetic-credential"}'
    outside.write_bytes(original)
    manifest = workspace / "mac-evidence.json"
    monkeypatch.setattr("mac.worker._MAX_EVIDENCE_INPUT_BYTES", 100)
    if kind == "symlink":
        manifest.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, manifest)
    elif kind == "fifo":
        os.mkfifo(manifest)
    elif kind == "oversized":
        manifest.write_text('{"result":"' + "x" * 200 + '"}')
    elif kind == "directory":
        manifest.mkdir()
    else:
        manifest.write_text('{"password":"synthetic-credential')
    worker = object.__new__(MacWorker)
    result = worker._load_verification_manifest(workspace)
    assert result["status"] == "invalid"
    assert outside.read_bytes() == original
    assert_secret_absent("synthetic-credential", result, path="invalid manifest")
    if kind != "directory":
        assert manifest.is_file() and not manifest.is_symlink()
        assert json.loads(manifest.read_text())["status"] == "invalid"
    # The writer must replace a hard-linked leaf, never truncate its target.
    linked = workspace / "stdout.txt"
    os.link(outside, linked)
    _write_host_control_text(linked, "safe diagnostic", workspace)
    assert outside.read_bytes() == original
    assert linked.read_text() == "safe diagnostic"


def test_manifest_fifo_replacement_race_does_not_block(tmp_path, monkeypatch):
    import os
    import mac.worker as worker_module

    manifest = tmp_path / "mac-evidence.json"
    manifest.write_text('{"status":"pass"}')
    original_open = os.open
    replaced = False

    def raced_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if Path(path) == manifest and not replaced:
            replaced = True
            manifest.unlink()
            os.mkfifo(manifest)
            assert flags & os.O_NONBLOCK
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(worker_module.os, "open", raced_open)
    worker = object.__new__(MacWorker)
    assert worker._load_verification_manifest(tmp_path)["status"] == "invalid"
    assert replaced


def test_manifest_digest_matches_sanitized_bytes(tmp_path):
    import hashlib

    manifest = tmp_path / "mac-evidence.json"
    manifest.write_text(
        json.dumps({"status": "pass", "sha256": "forged", "password": "synthetic-credential"})
    )
    result = object.__new__(MacWorker)._load_verification_manifest(tmp_path)
    assert result["sha256"] == "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert_secret_absent("synthetic-credential", manifest.read_text(), path="manifest bytes")


def test_secondary_artifact_redaction_preserves_input_and_hashes_captured_bytes(tmp_path):
    import hashlib
    from mac.worker import _durable_evidence_artifacts

    raw = json.dumps(
        {
            "password": "synthetic-credential",
            "credential_source": "fleet registry",
            "token_count": 42,
        }
    ).encode()
    source = tmp_path / "executor-task.json"
    source.write_bytes(raw)
    result = tmp_path / "worker-result.json"
    result.write_text('{"returncode":0}')
    artifact = next(
        a for a in _durable_evidence_artifacts(tmp_path, result) if a["name"] == source.name
    )
    content = base64.b64decode(artifact["content_base64"])
    assert_secret_absent("synthetic-credential", content, path="secondary artifact")
    assert json.loads(content)["credential_source"] == "fleet registry"
    assert json.loads(content)["token_count"] == 42
    assert source.read_bytes() == raw
    assert artifact["sha256"] == "sha256:" + hashlib.sha256(content).hexdigest()
    assert artifact["metadata"]["source_sha256"] == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert artifact["metadata"]["redacted"] is True


def test_review_redacts_durable_evidence_and_keeps_signed_verdict(
    tmp_path: Path, semantic_reviewer_on
):
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("review-host")
    executor_agent = cp.register_agent(machine.id, "executor", capabilities=["python"])
    reviewer = cp.register_agent(machine.id, "reviewer", capabilities=["review"])
    task = cp.create_task(
        "Reviewable repo task",
        required_capabilities=["python"],
        metadata={"publication_target": "test://publish"},
    )
    cp.claim_task(task.id, executor_agent.id)
    cp.start_task(task.id, executor_agent.id)
    executor_manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "repo": {
            "head_sha": "abc123abc123abc123abc123abc123abc123abcd",
            "remote_ref": "origin/main",
            "pushed": True,
            "dirty": False,
            "files_changed": ["src/example.py"],
        },
        "checks": [{"name": "pytest", "status": "passed", "returncode": 0}],
        "signed_by": executor_agent.id,
    }
    executor_manifest["signature"] = sign_verification_manifest(
        cp._agent_attestation_key(executor_agent.id), executor_manifest
    )
    evidence = cp.add_evidence(
        task.id,
        "log",
        "file:///tmp/executor-result.json",
        "executor completed",
        executor_agent.id,
        metadata={"returncode": 0, "verification": executor_manifest},
    )
    cp.submit_for_review(task.id, executor_agent.id)
    first = cp.advance_default_review_workflow(task.id)
    assert first["status"] == "waiting_for_reviewer_verdict"
    assert first["reviewer_agent_id"] == reviewer.id
    client = TestClient(create_app(control_plane=cp))
    secret = "opaque-credential-fixture"

    def review_executor(task_payload: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        context = task_payload["metadata"]["review_context"]
        assert context["task_id"] == task.id
        assert context["review_id"] == first["review_id"]
        assert context["executor_evidence_id"] == evidence.id
        assert context["review_claim"]["review_id"] == first["review_id"]
        assert context["review_claim"]["reviewer_agent_id"] == reviewer.id
        assert context["review_claim"]["executor_evidence_id"] == evidence.id
        manifest = {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "review_verdict",
            "verdict": "approved",
            "review_id": context["review_id"],
            "reviewed_evidence_id": context["executor_evidence_id"],
            "repo": dict(executor_manifest["repo"]),
            "checks": [{"name": "reviewer independent verification", "returncode": 0}],
            "worktree_digest": "sha256:" + ("0" * 64),
            "findings": [f"review completed with CURSOR_AUTH_TOKEN={secret}"],
        }
        (task_dir / "mac-evidence.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return WorkerExecution(
            0,
            f"review approved with CURSOR_AUTH_TOKEN={secret}",
            stdout=f"approved\nMAC_ATTESTATION_KEY={secret}\n",
            stderr=f"Authorization: Bearer {secret}\n",
        )

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=api_transport(client)),
        reviewer.id,
        tmp_path,
        review_executor,
        attestation_key=cp._agent_attestation_key(reviewer.id),
    )

    result = worker.run_once()

    assert result.status == "review_verdict_recorded"
    verdict_evidence = cp.list_evidence(task.id)[-1]
    manifest = verdict_evidence.metadata["verification"]
    assert verdict_evidence.kind == "review"
    assert manifest["evidence_type"] == "review_verdict"
    assert manifest["signed_by"] == reviewer.id
    assert manifest["reviewed_evidence_id"] == evidence.id
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value
    assert cp.get_agent(reviewer.id).status == "idle"
    task_metadata = cp.get_task(task.id).metadata
    assert task_metadata["review_claims"][first["review_id"]]["reviewer_agent_id"] == reviewer.id
    assert "task.review_claimed" in {event.event_type for event in cp.task_history(task.id)}
    assert_secret_absent(
        secret,
        persisted_task_state(cp, task.id),
        path="review.persistence",
    )


def test_disabling_artifact_upload_does_not_invalidate_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_EVIDENCE_ARTIFACT_MAX_BYTES", "0")
    (tmp_path / "mac-evidence.json").write_text(
        '{"status":"complete","evidence_type":"operator_result"}'
    )
    result = object.__new__(MacWorker)._load_verification_manifest(tmp_path)
    assert result["status"] == "complete"


def test_invalid_file_cannot_be_hidden_by_inline_success(tmp_path):
    (tmp_path / "mac-evidence.json").write_text("invalid JSON")
    worker = MacWorker(
        object(), "agent_test", tmp_path, lambda _task, _directory: WorkerExecution(0, "unused")
    )
    metadata = worker._execution_metadata(
        tmp_path, WorkerExecution(0, "done", metadata={"verification": {"status": "complete"}})
    )
    assert metadata["verification"]["status"] == "invalid"
