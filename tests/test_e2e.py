"""API integration tests crossing FastAPI, ControlPlane and PostgreSQL.

These exercise HTTP serialization, authentication, signed evidence, lifecycle,
acceptance and publication records against an isolated PostgreSQL schema.
The executor and forge results are fixtures: these are not a live coding-model,
OpenShell, or deployment canary. Real worker subprocess boundaries are covered
by test_worker_process_e2e.py; the operator workflow documents the live proof.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient

from mac.api import create_app
from mac.hermes_adapter import MacApiClient, MacApiError
from mac.models import TaskState
from mac.services import DEFAULT_HUB_REVIEWER_AGENT_ID, ControlPlane
from mac.test_support import ephemeral_store
from mac.worker import MacWorker, WorkerExecution

_SECRET_KEY = "test-key-with-enough-entropy-32+chars"


def _disk_app(tmp_path: Path) -> TestClient:
    """Build a FastAPI app against a real, durable database.

    Uses the same fixed secret_key as ControlPlane.in_memory so the
    secret encryption path works in tests without mutating the
    environment. The store is exposed on the client so a test can prove the
    request actually crossed a database rather than in-process state.
    """
    cp = ControlPlane(ephemeral_store(), secret_key=_SECRET_KEY)
    client = TestClient(create_app(control_plane=cp))
    client.mac_store = cp.store
    return client


def _api_transport(client: TestClient):
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


def _verified_execution(summary: str = "tests passed") -> WorkerExecution:
    return WorkerExecution(
        0,
        summary,
        stdout=summary + "\n",
        metadata={
            "verification": {
                "schema": "mac.worker_evidence.v1",
                "status": "complete",
                "evidence_type": "repo_change",
                "repo": {
                    "head_sha": "abcdef1234567890abcdef1234567890abcdef12",
                    "pushed": True,
                    "remote_ref": "refs/heads/task/example",
                    "dirty": False,
                    "files_changed": ["src/example.py"],
                },
                "tests": [{"command": "pytest", "returncode": 0}],
            }
        },
    )


# ---------------------------------------------------------------------------
# Test 1: full task lifecycle through HTTP against a real on-disk DB
# ---------------------------------------------------------------------------


def test_e2e_full_task_lifecycle_via_http_and_disk(tmp_path: Path):
    client = _disk_app(tmp_path)

    machine = client.post("/machines", json={"hostname": "host-e2e"}).json()
    worker = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "rocky", "capabilities": ["python"]},
    ).json()
    task = client.post(
        "/tasks",
        json={
            "title": "E2E task",
            "required_capabilities": ["python"],
            "metadata": {"publication_target": "test://e2e"},
        },
    ).json()

    # MacWorker drives claim → start → run → evidence → submit_for_review
    # through the same API surface. The attestation key returned by
    # /agents POST signs the verification manifest so the default-
    # review workflow accepts the evidence (mac-ng2).
    api = MacApiClient("http://mac.test", transport=_api_transport(client))
    macworker = MacWorker(
        api,
        worker["id"],
        tmp_path / "workspaces",
        lambda _t, _d: _verified_execution("tests passed"),
        attestation_key=worker["attestation_key"],
    )
    result = macworker.run_once()
    assert result.status == "submitted_for_review"
    assert result.task["id"] == task["id"]

    # The validated worker evidence is the review verdict: the hub-reviewer
    # approves it and the task publishes without a reviewer agent.
    pre = client.get("/tasks/%s" % task["id"]).json()
    executor_evidence_id = pre["evidence"][0]["id"]
    client.post("/reviews/default/tick")

    final = client.get("/tasks/%s" % task["id"]).json()
    assert final["task"]["state"] == TaskState.COMPLETED.value
    assert final["reviews"][0]["reviewer_agent_id"] == DEFAULT_HUB_REVIEWER_AGENT_ID
    assert final["reviews"][0]["status"] == "approved"
    assert final["publications"][0]["status"] == "published"

    # Publication is a durable result, not implicit request acceptance or
    # deployment. The operator verifies this exact result through HTTP.
    outcome = client.get("/tasks/%s/outcome" % task["id"]).json()
    assert outcome["tests"]["status"] == "reported_pass"
    assert outcome["publication"]["status"] == "published"
    assert outcome["acceptance"]["status"] == "unknown"
    assert outcome["deployment"]["status"] == "unknown"
    accepted = client.post(
        "/tasks/%s/acceptance" % task["id"],
        json={
            "evidence_id": executor_evidence_id,
            "reason": "Observed the expected fixture result",
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["acceptance"]["status"] == "accepted"
    cohort = client.get("/tasks/outcomes").json()
    assert cohort["accepted_completed_count"] == 1
    assert cohort["tasks"][0]["known_cost_usd"] is None

    # History shows every transition.
    history_events = {h["event_type"] for h in final["history"]}
    assert {
        "task.transitioned",
        "task.evidence_added",
        "task.review_requested",
        "task.review_completed",
    }.issubset(history_events)

    # The rows are really in the database — proves the lifecycle crossed a
    # durable store and not just in-process state. Under SQLite this asserted
    # that the on-disk file had grown; the Postgres equivalent is that the
    # authority can be read back independently of the request that wrote it.
    assert client.mac_store.query_one("SELECT COUNT(*) AS n FROM tasks")["n"] > 0


def _operator_execution(summary: str) -> WorkerExecution:
    """A non-repo operator_result execution (the planning/directive path the
    executor's fallback writer emits)."""
    return WorkerExecution(
        0,
        summary,
        stdout=summary + "\n",
        metadata={
            "verification": {
                "schema": "mac.worker_evidence.v1",
                "status": "complete",
                "evidence_type": "operator_result",
                "summary": summary,
            }
        },
    )


def test_e2e_chatter_evidence_fails_closed(tmp_path: Path):
    """autonomy-loop fix: an executor that only chats ('hello hello hello') —
    the exact jam shape — must fail closed at the verification gate, not get
    submitted for review or published. A *substantive* operator_result on the
    same kind of task still passes the gate, proving we didn't break the
    legitimate planning/directive path."""
    client = _disk_app(tmp_path)
    machine = client.post("/machines", json={"hostname": "host-fc"}).json()
    worker = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "rocky", "capabilities": ["python"]},
    ).json()
    api = MacApiClient("http://mac.test", transport=_api_transport(client))

    def _run(title: str, summary: str):
        task = client.post(
            "/tasks",
            json={
                "title": title,
                "required_capabilities": ["python"],
                "metadata": {"publication_target": "test://e2e"},
            },
        ).json()
        macworker = MacWorker(
            api,
            worker["id"],
            tmp_path / "workspaces",
            lambda _t, _d: _operator_execution(summary),
            attestation_key=worker["attestation_key"],
        )
        return task, macworker.run_once()

    # Chatter -> rejected at the gate, task blocks for repair, nothing published.
    task, result = _run("E2E chatter task", "hello hello hello")
    assert result.status == "blocked", result
    assert "substantive" in (result.error or "")
    final = client.get("/tasks/%s" % task["id"]).json()
    assert final["task"]["state"] == TaskState.BLOCKED.value
    assert not final.get("publications")

    # Substantive planning result → passes the gate (submitted for review).
    task2, result2 = _run(
        "E2E planning task",
        "Produced the rollout plan and identified the three blocking dependencies.",
    )
    assert result2.status == "submitted_for_review", result2


# ---------------------------------------------------------------------------
# Test 2: two workers race for one task; exactly one wins
# ---------------------------------------------------------------------------


def test_e2e_two_workers_race_for_one_task_serializes(tmp_path: Path):
    client = _disk_app(tmp_path)

    m1 = client.post("/machines", json={"hostname": "host-a"}).json()
    m2 = client.post("/machines", json={"hostname": "host-b"}).json()
    a1 = client.post(
        "/agents",
        json={"machine_id": m1["id"], "name": "rocky", "capabilities": ["python"]},
    ).json()
    a2 = client.post(
        "/agents",
        json={"machine_id": m2["id"], "name": "natasha", "capabilities": ["python"]},
    ).json()
    task = client.post(
        "/tasks",
        json={
            "title": "race",
            "required_capabilities": ["python"],
            "metadata": {"publication_target": "test://race"},
        },
    ).json()

    api = MacApiClient("http://mac.test", transport=_api_transport(client))

    keys = {a1["id"]: a1["attestation_key"], a2["id"]: a2["attestation_key"]}

    def make_worker(agent_id: str) -> MacWorker:
        return MacWorker(
            api,
            agent_id,
            tmp_path / ("ws-%s" % agent_id),
            lambda _t, _d: _verified_execution("ok"),
            attestation_key=keys[agent_id],
        )

    results: Dict[str, Any] = {}

    def run_worker(name: str, worker: MacWorker) -> None:
        # Tiny stagger so both threads are actually contending. The
        # store's BEGIN IMMEDIATE + RLock is what serializes the race.
        time.sleep(0.01)
        results[name] = worker.run_once()

    t1 = threading.Thread(target=run_worker, args=("a1", make_worker(a1["id"])))
    t2 = threading.Thread(target=run_worker, args=("a2", make_worker(a2["id"])))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    statuses = sorted(r.status for r in results.values())
    assert statuses == ["no_task", "submitted_for_review"], statuses

    client.post("/reviews/default/tick")

    final = client.get("/tasks/%s" % task["id"]).json()
    assert final["task"]["state"] == TaskState.COMPLETED.value
    # NEEDS_REVIEW releases the lease + clears owner_agent_id on the task,
    # then the default review workflow can complete it. Exactly one
    # transition row of `running -> needs_review` still proves the race
    # serialized: the losing worker never claimed, so it never transitioned
    # the task.
    needs_review_transitions = [
        h
        for h in final["history"]
        if h["event_type"] == "task.transitioned" and h["to_state"] == TaskState.NEEDS_REVIEW.value
    ]
    assert len(needs_review_transitions) == 1
    assert needs_review_transitions[0]["from_state"] == TaskState.RUNNING.value
    assert needs_review_transitions[0]["actor"] in {a1["id"], a2["id"]}
    assert len(final["reviews"]) == 1
    # The approval identity is the virtual hub-reviewer, never the executor.
    assert final["reviews"][0]["reviewer_agent_id"] != needs_review_transitions[0]["actor"]
    assert len(final["publications"]) == 1


# ---------------------------------------------------------------------------
# Test 4: secret handle is single-use
# ---------------------------------------------------------------------------


def test_e2e_secret_handle_is_single_use_via_http(tmp_path: Path):
    client = _disk_app(tmp_path)
    machine = client.post("/machines", json={"hostname": "host-secret"}).json()
    agent = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "ops", "capabilities": ["deploy"]},
    ).json()
    secret = client.post(
        "/secrets",
        json={
            "name": "deploy-token",
            "value": "plaintext-only-revealed-once",
            "scopes": {"capabilities": ["deploy"]},
            "created_by": "ops",
        },
    ).json()
    handle = client.post(
        "/secrets/%s/access" % secret["id"],
        json={"accessor_agent_id": agent["id"], "purpose": "deploy"},
    ).json()
    assert handle["handle"].startswith("secret://")

    first = client.post(
        "/secrets/%s/reveal" % secret["id"],
        json={"audit_id": handle["audit_id"], "accessor_agent_id": agent["id"]},
    )
    assert first.status_code == 200
    assert first.json()["value"] == "plaintext-only-revealed-once"

    # Second reveal with the same handle is refused — single-use.
    second = client.post(
        "/secrets/%s/reveal" % secret["id"],
        json={"audit_id": handle["audit_id"], "accessor_agent_id": agent["id"]},
    )
    assert second.status_code == 403


# ---------------------------------------------------------------------------
# Test 5: workflow drives a task end-to-end
# ---------------------------------------------------------------------------


def test_e2e_workflow_runtime_drives_task_via_http(tmp_path: Path):
    client = _disk_app(tmp_path)
    project = client.post("/projects", json={"name": "workflow"})
    assert project.status_code == 200, project.text

    # Roles + a minimal one-node workflow that ends after a single success.
    client.post(
        "/roles",
        json={
            "slug": "qa",
            "name": "QA",
            "description": "checks things",
            "system_prompt": "Run the tests.",
            "level": "ic",
            "default_capabilities": ["python", "qa"],
        },
    )
    workflow = client.post(
        "/workflows",
        json={
            "slug": "smoke",
            "name": "Smoke",
            "description": "single-node",
            "workflow_type": "smoke",
            "created_by": "ops",
            "definition": {
                "nodes": [
                    {
                        "node_key": "run",
                        "node_type": "task",
                        "role_required": "qa",
                        "max_attempts": 1,
                    }
                ],
                "edges": [
                    {
                        "from_node_key": "",
                        "to_node_key": "run",
                        "condition": "success",
                        "priority": 100,
                    },
                    {
                        "from_node_key": "run",
                        "to_node_key": "",
                        "condition": "failure",
                        "priority": 100,
                    },
                ],
            },
        },
    ).json()
    assert workflow["slug"] == "smoke"

    machine = client.post("/machines", json={"hostname": "host-wf"}).json()
    # Bind a soul before role assignment — soul takes precedence over role.
    tenant = client.post("/tenants", json={"name": "wf-team"}).json()
    persona = client.post(
        "/personas",
        json={
            "tenant_id": tenant["id"],
            "name": "QA Soul",
            "soul_ref": "h://wf/qa/SOUL.md",
            "memory_scope": "h://wf/qa/mem",
            "metadata": {"role_slugs": ["qa"]},
        },
    ).json()
    instance = client.post(
        "/persona-instances",
        json={
            "tenant_id": tenant["id"],
            "name": "qa-instance",
            "persona_id": persona["id"],
        },
    ).json()
    agent = client.post(
        "/agents",
        json={
            "machine_id": machine["id"],
            "name": "wf-runner",
            "capabilities": ["python"],
            "hermes_instance_id": instance["id"],
        },
    ).json()
    # Assign role so dispatcher accepts the workflow's required_role pin.
    resp = client.post("/agents/%s/role" % agent["id"], json={"role_id_or_slug": "qa"})
    assert resp.status_code == 200, resp.text

    run = client.post("/workflows/smoke/start", json={"started_by": "ops"}).json()
    assert run["state"] == "running"
    assert run["current_node_key"] == "run"

    # Worker drives the task to failure (we only wired a failure→end edge
    # in this minimal workflow, so failure leads to a terminal state).
    api = MacApiClient("http://mac.test", transport=_api_transport(client))
    macworker = MacWorker(
        api,
        agent["id"],
        tmp_path / "ws-wf",
        lambda _t, _d: WorkerExecution(2, "boom", stderr="boom\n"),
    )
    macworker.run_once()

    fresh = client.get("/workflows/runs/%s" % run["id"]).json()
    assert fresh["state"] == "failed"
    assert fresh["completed_at"] is not None
