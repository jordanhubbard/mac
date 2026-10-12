from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mac import task_executor
from mac.agent_command import PROMPT_SENTINEL
from mac.api import create_app
from mac.hermes_adapter import MacApiClient, MacApiError
from mac.models import ValidationError
from mac.services import ControlPlane
from mac.worker import MacWorker, WorkerExecution


def _control_plane():
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("recovery-host")
    recovery = cp.register_agent(machine.id, "recovery", capabilities=["python"])
    peer_machine = cp.register_machine("peer-host")
    peer = cp.register_agent(peer_machine.id, "peer", capabilities=["python"])
    return cp, recovery, peer


def test_control_plane_refuses_host_break_glass_authorization():
    cp, recovery, _peer = _control_plane()
    task = cp.create_task("repair worker runtime")

    with pytest.raises(ValidationError, match="sandbox-only"):
        cp.authorize_task_break_glass(
            task.id,
            recovery.id,
            reason="repair the sandbox launcher from the trusted host",
            authorized_by="operator-test",
            ttl_seconds=300,
        )


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


def test_executor_refuses_direct_host_path_for_valid_claimed_projection(
    monkeypatch, tmp_path: Path
):
    task_id = "task_break_glass"
    agent_id = "agent_recovery"
    lease_id = "lease_recovery"
    task = {
        "id": task_id,
        "metadata": {
            "runtime": {
                "break_glass_authorization": {
                    "id": "breakglass_1234567890abcdef",
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "lease_id": lease_id,
                    "execution_boundary": "host",
                    "status": "claimed",
                }
            }
        },
    }
    monkeypatch.setenv("MAC_AGENT_ID", agent_id)
    monkeypatch.setenv("MAC_LEASE_ID", lease_id)
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_OPENSHELL_REQUIRED", "1")
    monkeypatch.setattr(
        task_executor,
        "_agent_argv",
        lambda *_args, **_kwargs: ["agent", PROMPT_SENTINEL],
    )
    monkeypatch.setattr(
        task_executor,
        "_run_sandboxed",
        lambda runner, argv, workspace, audit_id, opts: runner(
            argv, workspace, audit_id, {**opts, "execution_boundary": "sandbox"}
        ),
    )
    calls = []

    def runner(argv, workspace, audit_id, opts):
        calls.append((argv, workspace, audit_id, opts))
        return type("Result", (), {"returncode": 0})()

    result = task_executor._invoke_agent(
        runner,
        "repair",
        tmp_path,
        task_id,
        {"task": task, "execution_kind": "task"},
    )

    assert result.returncode == 0
    assert calls[0][3]["execution_boundary"] == "sandbox"
    assert "break_glass_authorization_id" not in calls[0][3]


def test_break_glass_api_requires_admin_and_refuses_host_execution():
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
    assert allowed.status_code == 400
    assert "sandbox-only" in allowed.json()["detail"]
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
        event for event in cp.list_observability(limit=100) if event.name == "worker.dispatch_held"
    ]
    assert len(hold_logs) == 1
