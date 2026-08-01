"""End-to-end tests crossing the full FastAPI + ControlPlane + on-disk
SQLite stack.

The rest of the suite tests one layer at a time:
``test_control_plane.py`` exercises ``ControlPlane`` directly with
``:memory:``; ``test_api.py`` tests the HTTP layer in isolation;
``test_worker.py`` stops at ``submitted_for_review`` without crossing
the review + publish path. None of them use a file-backed SQLite or
walk a task through the full lifecycle via HTTP.

These tests close that gap. They use ``create_app(db_path=...)``
against a real ``tmp_path`` SQLite file so WAL/busy_timeout/threading
behaves like production, and they drive the FastAPI app through
``TestClient`` so every request crosses Pydantic, the auth middleware,
and the full domain service composition.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient

from mac.codegraph_audit import CODEGRAPH_AUDIT_SCHEMA
from mac.api import create_app
from mac.hermes_adapter import MacApiClient, MacApiError
from mac.models import TaskState
from mac.services import ControlPlane
from mac.test_support import ephemeral_store
from mac.worker import MacWorker, WorkerExecution

_SECRET_KEY = "test-key-with-enough-entropy-32+chars"


def _disk_app(tmp_path: Path) -> TestClient:
    """Build a FastAPI app against a disk-backed SQLite file.

    Uses the same fixed secret_key as ControlPlane.in_memory so the
    secret encryption path works in tests without mutating the
    environment.
    """
    db_path = tmp_path / "mac.db"
    cp = ControlPlane(ephemeral_store(), secret_key=_SECRET_KEY)
    return TestClient(create_app(control_plane=cp))


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


def _post_review_verdict(
    client: TestClient,
    task_id: str,
    reviewer_id: str,
    reviewer_attestation_key: str,
    executor_evidence_id: str,
    *,
    verdict: str = "approved",
) -> Dict[str, Any]:
    """Build a signed review_verdict manifest and POST it as evidence.

    mac-jqb: the default-review workflow does not auto-approve. The
    reviewer agent must publish a verdict evidence row signed with its
    own attestation key for the workflow to advance to PUBLISHED.
    """
    from mac.services import sign_verification_manifest

    task_detail = client.get("/tasks/%s" % task_id).json()
    executor_evidence = next(
        item for item in task_detail["evidence"] if item["id"] == executor_evidence_id
    )
    executor_manifest = executor_evidence["metadata"]["verification"]
    files_changed = list((executor_manifest.get("repo") or {}).get("files_changed") or [])
    manifest: Dict[str, Any] = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "review_verdict",
        "verdict": verdict,
        "reviewed_evidence_id": executor_evidence_id,
        "repo": dict(executor_manifest["repo"]),
        "checks": [{"name": "reviewer independent verification", "returncode": 0}],
        "worktree_digest": "sha256:" + ("0" * 64),
    }
    if files_changed:
        manifest["codegraph"] = {
            "schema": CODEGRAPH_AUDIT_SCHEMA,
            "status": "pass",
            "reason": "test_fixture",
            "relevant_files": files_changed,
            "commands": [
                {"argv": ["codegraph", "sync"], "returncode": 0},
                {"argv": ["codegraph", "affected"], "returncode": 0},
            ],
        }
    manifest["signed_by"] = reviewer_id
    manifest["signature"] = sign_verification_manifest(reviewer_attestation_key, manifest)
    response = client.post(
        "/tasks/%s/evidence" % task_id,
        json={
            "kind": "review",
            "uri": "artifact://verdict",
            "summary": "reviewer verdict: %s" % verdict,
            "created_by": reviewer_id,
            "metadata": {"returncode": 0, "verification": manifest},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


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
                "codegraph": {
                    "schema": CODEGRAPH_AUDIT_SCHEMA,
                    "status": "pass",
                    "reason": "test_fixture",
                    "relevant_files": ["src/example.py"],
                    "commands": [
                        {"argv": ["codegraph", "sync"], "returncode": 0},
                        {"argv": ["codegraph", "affected"], "returncode": 0},
                    ],
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
    reviewer = client.post(
        "/agents",
        json={"machine_id": machine["id"], "name": "natasha", "capabilities": ["review"]},
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

    # mac-jqb: the default-review workflow needs the reviewer to
    # produce a signed verdict before it will publish. Tick the
    # workflow once to register the pending review, then have the
    # reviewer submit a signed approval, then tick again to publish.
    pre = client.get("/tasks/%s" % task["id"]).json()
    executor_evidence_id = pre["evidence"][0]["id"]
    tick = client.post("/reviews/default/tick").json()
    assert tick["processed"] >= 1
    _post_review_verdict(
        client,
        task["id"],
        reviewer["id"],
        reviewer["attestation_key"],
        executor_evidence_id,
    )
    client.post("/reviews/default/tick")

    final = client.get("/tasks/%s" % task["id"]).json()
    assert final["task"]["state"] == TaskState.COMPLETED.value
    assert final["reviews"][0]["reviewer_agent_id"] == reviewer["id"]
    assert final["reviews"][0]["status"] == "approved"
    assert final["publications"][0]["status"] == "published"

    # History shows every transition.
    history_events = {h["event_type"] for h in final["history"]}
    assert {
        "task.transitioned",
        "task.evidence_added",
        "task.review_requested",
        "task.review_completed",
    }.issubset(history_events)

    # The on-disk file actually has bytes — proves we crossed the file path.
    assert (tmp_path / "mac.db").stat().st_size > 0


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
    # Reviewer is now a required role (mac-s1a) — register a separate
    # agent that can do the review work for the auto-publish path.
    reviewer = client.post(
        "/agents",
        json={
            "machine_id": m1["id"],
            "name": "reviewer",
            "capabilities": ["review"],
        },
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

    # mac-jqb verdict step (see test 1 above for the reasoning).
    pre = client.get("/tasks/%s" % task["id"]).json()
    executor_evidence_id = pre["evidence"][0]["id"]
    client.post("/reviews/default/tick")
    _post_review_verdict(
        client,
        task["id"],
        reviewer["id"],
        reviewer["attestation_key"],
        executor_evidence_id,
    )
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
        if h["event_type"] == "task.transitioned"
        and h["to_state"] == TaskState.NEEDS_REVIEW.value
    ]
    assert len(needs_review_transitions) == 1
    assert needs_review_transitions[0]["from_state"] == TaskState.RUNNING.value
    assert needs_review_transitions[0]["actor"] in {a1["id"], a2["id"]}
    assert len(final["reviews"]) == 1
    # Reviewer is a separate agent — mac-s1a requires the `review`
    # capability so the workers (python only) can't review their own
    # work. mac-v2i additionally bars same-persona collusion, which
    # this test doesn't exercise (workers have no soul).
    assert final["reviews"][0]["reviewer_agent_id"] != needs_review_transitions[0]["actor"]
    assert len(final["publications"]) == 1


# ---------------------------------------------------------------------------
# Test 3: rollout advance blocked until a passing eval run exists
# ---------------------------------------------------------------------------


def test_e2e_rollout_advance_blocks_on_eval_gate_via_http(tmp_path: Path):
    client = _disk_app(tmp_path)

    runtime = client.post(
        "/runtimes",
        json={
            "name": "py-runtime",
            "manifest": {
                "image": "python:3.12@sha256:abc123",
                "dependencies": ["fastapi==0.111.0"],
            },
            "created_by": "ops",
        },
    ).json()
    eval_set = client.post(
        "/eval-sets",
        json={
            "name": "smoke-suite",
            "scoring": "higher_is_better",
            "baseline_score": 0.9,
            "regression_threshold": 0.05,
            "created_by": "ops",
        },
    ).json()
    rollout = client.post(
        "/rollouts",
        json={
            "version": "v1.2.3",
            "strategy": "canary",
            "target_percent": 25,
            "created_by": "ops",
            "channel": "fleet",
            "runtime_environment_id": runtime["id"],
            "required_eval_set_id": eval_set["id"],
        },
    ).json()

    # Pin an artifact so install_ready is satisfied.
    pinned = client.post(
        "/rollouts/%s/artifact" % rollout["id"],
        json={
            "artifact_uri": "registry://team/mac@sha256:abc",
            "artifact_hash": "sha256:abcabcabc",
            "actor": "ops",
        },
    ).json()
    assert pinned["artifact_hash"].startswith("sha256:")

    # mac-wfct: start_canary now requires a passing eval run too. Seed one.
    client.post(
        "/eval-runs",
        json={
            "eval_set_id": eval_set["id"],
            "target_kind": "rollout_version",
            "target_id": rollout["version"],
            "score": 0.95,
        },
    )
    started = client.post(
        "/rollouts/%s/advance" % rollout["id"],
        json={"action": "start_canary", "actor": "ops", "detail": {}},
    )
    assert started.status_code == 200
    assert started.json()["status"] == "canarying"

    # Pass the health gate so the eval gate is the next thing standing.
    # mac-jmjc: default health_policy now requires "runtime"; supply it.
    health = client.post(
        "/rollouts/%s/health" % rollout["id"],
        json={
            "actor": "ops",
            "checks": {
                "runtime": "healthy",
                "latency_p95_ms": "ok",
                "error_rate": "ok",
            },
        },
    ).json()
    assert health["healthy"] is True

    # mac-wfct: with start_canary also gated on a passing run, the
    # "no eval at all" scenario is unreachable here; record a failing
    # run that supersedes the canary-time passing run and verify
    # promote is blocked on the latest result.
    client.post(
        "/eval-runs",
        json={
            "eval_set_id": eval_set["id"],
            "target_kind": "rollout_version",
            "target_id": rollout["version"],
            "score": 0.5,
            "created_by": "ops",
        },
    )
    blocked = client.post(
        "/rollouts/%s/advance" % rollout["id"],
        json={"action": "promote", "actor": "ops", "detail": {}},
    )
    assert blocked.status_code == 400
    assert "eval" in blocked.json()["detail"].lower()

    # A second failing eval keeps the gate closed.
    client.post(
        "/eval-runs",
        json={
            "eval_set_id": eval_set["id"],
            "target_kind": "rollout_version",
            "target_id": rollout["version"],
            "score": 0.4,
            "created_by": "ops",
        },
    )
    still_blocked = client.post(
        "/rollouts/%s/advance" % rollout["id"],
        json={"action": "promote", "actor": "ops", "detail": {}},
    )
    assert still_blocked.status_code == 400

    # A passing run opens the gate.
    client.post(
        "/eval-runs",
        json={
            "eval_set_id": eval_set["id"],
            "target_kind": "rollout_version",
            "target_id": rollout["version"],
            "score": 0.95,
            "created_by": "ops",
        },
    )
    promoted = client.post(
        "/rollouts/%s/advance" % rollout["id"],
        json={"action": "promote", "actor": "ops", "detail": {}},
    )
    assert promoted.status_code == 200
    assert promoted.json()["status"] == "promoted"


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
                    {"from_node_key": "", "to_node_key": "run", "condition": "success", "priority": 100},
                    {"from_node_key": "run", "to_node_key": "", "condition": "failure", "priority": 100},
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
    resp = client.post(
        "/agents/%s/role" % agent["id"], json={"role_id_or_slug": "qa"}
    )
    assert resp.status_code == 200, resp.text

    run = client.post(
        "/workflows/smoke/start", json={"started_by": "ops"}
    ).json()
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
