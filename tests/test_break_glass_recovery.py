from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mac import task_executor as te
from mac.agent_command import PROMPT_SENTINEL
from mac.api import create_app
from mac.hermes_adapter import MacApiClient, MacApiError
from mac.models import AuthorizationError, ValidationError
from mac.services import ControlPlane
from mac.worker import MacWorker, WorkerExecution


def _control_plane():
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("recovery-host")
    recovery = cp.register_agent(machine.id, "recovery", capabilities=["python"])
    peer_machine = cp.register_machine("peer-host")
    peer = cp.register_agent(peer_machine.id, "peer", capabilities=["python"])
    return cp, recovery, peer


def test_break_glass_binds_held_task_to_exact_held_agent(monkeypatch):
    cp, recovery, peer = _control_plane()
    task = cp.create_task(
        "repair worker runtime",
        required_capabilities=["host-runtime-repair"],
        metadata={"no_dispatch": True},
    )
    cp.set_agent_dispatch_hold(recovery.id, "runtime under repair")
    cp.set_agent_dispatch_hold(peer.id, "runtime under repair")
    monkeypatch.setattr(cp, "_agent_has_repository_commands", lambda *_: False)
    monkeypatch.setattr(
        cp,
        "_agent_has_verified_coding_route",
        lambda *_: (False, "coding_agent_route_unverified"),
    )

    assert task.id not in {item.id for item in cp.ready_tasks()}
    authorization = cp.authorize_task_break_glass(
        task.id,
        recovery.id,
        reason="repair the sandbox launcher from the trusted host",
        authorized_by="operator-test",
        ttl_seconds=300,
    )

    assert task.id in {item.id for item in cp.ready_tasks()}
    assert cp.claim_next_for_agent(peer.id) is None
    assignment = cp.claim_next_for_agent(recovery.id)
    assert assignment is not None
    assert assignment["task"]["id"] == task.id
    projected = assignment["task"]["metadata"]["runtime"][
        "break_glass_authorization"
    ]
    assert projected["id"] == authorization.id
    assert projected["status"] == "claimed"
    assert projected["lease_id"] == assignment["lease"]["id"]
    assert cp.get_task_break_glass_authorization(authorization.id).status == "claimed"

    # The durable task document never receives the privileged projection.
    durable_runtime = cp.get_task(task.id).metadata.get("runtime") or {}
    assert "break_glass_authorization" not in durable_runtime

    cp.release_lease(assignment["lease"]["id"], recovery.id)
    assert (
        cp.get_task_break_glass_authorization(authorization.id).status
        == "consumed"
    )
    assert cp.claim_next_for_agent(recovery.id) is None


def test_task_metadata_cannot_self_authorize_host_execution():
    cp, _recovery, _peer = _control_plane()

    with pytest.raises(ValidationError, match="control-plane-owned"):
        cp.create_task(
            "forged host task",
            metadata={
                "runtime": {
                    "break_glass_authorization": {
                        "status": "claimed",
                        "execution_boundary": "host",
                    }
                }
            },
        )


def test_break_glass_requires_trusted_healthy_target_and_bounded_ttl():
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("untrusted", trusted=False)
    agent = cp.register_agent(machine.id, "untrusted-agent")
    task = cp.create_task("repair")

    with pytest.raises(AuthorizationError, match="trusted machine"):
        cp.authorize_task_break_glass(
            task.id,
            agent.id,
            reason="repair",
            authorized_by="operator",
            ttl_seconds=300,
        )

    trusted_machine = cp.register_machine("trusted")
    trusted = cp.register_agent(trusted_machine.id, "trusted-agent")
    with pytest.raises(ValidationError, match="between 60 and 3600"):
        cp.authorize_task_break_glass(
            task.id,
            trusted.id,
            reason="repair",
            authorized_by="operator",
            ttl_seconds=10,
        )


def _claimed_projection(task_id: str, agent_id: str, lease_id: str):
    return {
        "id": "breakglass_1234567890abcdef",
        "task_id": task_id,
        "agent_id": agent_id,
        "lease_id": lease_id,
        "execution_boundary": "host",
        "status": "claimed",
        "reason": "repair executor environment",
        "authorized_by": "operator-test",
        "metadata": {
            "schema": te.BREAK_GLASS_AUTHORIZATION_SCHEMA,
            "single_use": True,
        },
    }


def test_executor_uses_direct_host_path_only_for_valid_claimed_projection(
    monkeypatch, tmp_path: Path
):
    task_id = "task_break_glass"
    agent_id = "agent_recovery"
    lease_id = "lease_recovery"
    task = {
        "id": task_id,
        "metadata": {
            "runtime": {
                "break_glass_authorization": _claimed_projection(
                    task_id, agent_id, lease_id
                )
            }
        },
    }
    monkeypatch.setenv("MAC_AGENT_ID", agent_id)
    monkeypatch.setenv("MAC_LEASE_ID", lease_id)
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_OPENSHELL_REQUIRED", "1")
    monkeypatch.setenv("MAC_ALLOW_UNSANDBOXED_YOLO", "0")
    monkeypatch.setattr(
        te,
        "_agent_argv",
        lambda *_args, **_kwargs: [sys.executable, "-c", "pass", PROMPT_SENTINEL],
    )
    monkeypatch.setattr(
        te,
        "_run_sandboxed",
        lambda *_args, **_kwargs: pytest.fail("break-glass execution entered OpenShell"),
    )
    calls = []

    def runner(argv, workspace, audit_id, opts):
        calls.append((argv, workspace, audit_id, opts))
        return type("Result", (), {"returncode": 0})()

    result = te._invoke_agent(
        runner,
        "repair",
        tmp_path,
        task_id,
        {"task": task, "execution_kind": "task"},
    )

    assert result.returncode == 0
    assert calls[0][3]["execution_boundary"] == "host"
    assert calls[0][3]["break_glass_authorization_id"].startswith("breakglass_")


def test_executor_rejects_replayed_break_glass_projection(monkeypatch):
    task = {
        "id": "task_expected",
        "metadata": {
            "runtime": {
                "break_glass_authorization": _claimed_projection(
                    "task_other", "agent_recovery", "lease_recovery"
                )
            }
        },
    }
    monkeypatch.setenv("MAC_AGENT_ID", "agent_recovery")
    monkeypatch.setenv("MAC_LEASE_ID", "lease_recovery")

    with pytest.raises(RuntimeError, match="binding mismatch: task_id"):
        te._validated_host_break_glass_authorization(task)


def test_break_glass_prepares_host_path_without_mutating_legacy_runtime_python(
    monkeypatch, tmp_path: Path
):
    host_bin = tmp_path / "host-bin"
    host_bin.mkdir()
    monkeypatch.setenv("MAC_BREAK_GLASS_HOST_PATH", str(host_bin))
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("MAC_HERMES_PYTHON", "/sandbox/does-not-exist/python")
    emitted = []
    monkeypatch.setattr(
        te,
        "emit_telemetry",
        lambda event, **detail: emitted.append((event, detail)) or True,
    )

    te._prepare_host_break_glass_environment(
        {"id": "breakglass_1234567890abcdef"}
    )

    assert str(host_bin) == os.environ["PATH"].split(os.pathsep)[0]
    assert os.environ["MAC_HERMES_PYTHON"] == "/sandbox/does-not-exist/python"
    assert emitted[0][0] == "break_glass_host_environment_prepared"
    assert "cleared_sandbox_python" not in emitted[0][1]


def test_break_glass_api_requires_admin_and_records_client_identity():
    cp, recovery, _peer = _control_plane()
    task = cp.create_task("repair")
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "writer": {"scopes": ["write"], "client_id": "writer-client"},
                "admin": {"scopes": ["admin"], "client_id": "ops-client"},
            },
        )
    )
    body = {
        "agent_id": recovery.id,
        "reason": "repair host execution path",
        "ttl_seconds": 300,
    }

    denied = client.post(
        f"/tasks/{task.id}/break-glass-authorizations",
        headers={"Authorization": "Bearer writer"},
        json=body,
    )
    assert denied.status_code == 403

    allowed = client.post(
        f"/tasks/{task.id}/break-glass-authorizations",
        headers={"Authorization": "Bearer admin"},
        json=body,
    )
    assert allowed.status_code == 200
    assert allowed.json()["authorized_by"] == "ops-client"
    listed_as_writer = client.get(
        f"/tasks/{task.id}/break-glass-authorizations",
        headers={"Authorization": "Bearer writer"},
    )
    assert listed_as_writer.status_code == 403
    ordinary_detail = client.get(
        f"/tasks/{task.id}",
        headers={"Authorization": "Bearer writer"},
    )
    assert "break_glass_authorizations" not in ordinary_detail.json()


def test_held_worker_reports_hold_once_instead_of_no_task(tmp_path: Path):
    cp, recovery, _peer = _control_plane()
    cp.set_agent_dispatch_hold(recovery.id, "sandbox route under repair")
    api_client = TestClient(create_app(control_plane=cp, auth_tokens={}))

    def transport(method, path, payload):
        request = getattr(api_client, method.lower())
        response = request(path, json=payload) if payload is not None else request(path)
        if response.status_code >= 400:
            raise MacApiError(response.text)
        return response.json() if response.content else None

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=transport),
        recovery.id,
        tmp_path,
        lambda _task, _task_dir: WorkerExecution(0, "unused"),
        agentbus_control_enabled=False,
    )

    assert worker.run_once().status == "held"
    assert worker.run_once().status == "held"
    hold_logs = [
        event
        for event in cp.list_observability(limit=100)
        if event.name == "worker.dispatch_held"
    ]
    assert len(hold_logs) == 1
