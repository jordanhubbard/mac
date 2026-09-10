"""Outcome projection and operator acceptance across the real HTTP/DB boundary."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from mac.allocator import AllocationAgent, summarize_execution_capacity
from mac.api import create_app, TokenPrincipal
from mac.models import json_dumps, new_id, utcnow
from mac.services import ControlPlane


def _reviewing(cp):
    task = cp.create_task("Verify the requested behavior", project="trust")
    eid = new_id("ev")
    with cp.store.transaction() as conn:
        conn.execute(
            "INSERT INTO evidence (id,task_id,kind,uri,summary,metadata,created_by,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                eid,
                task.id,
                "repo_change",
                "test://result",
                "Fixture result",
                json_dumps({"verification": {"tests": [{"returncode": 0}]}}),
                "executor",
                utcnow(),
            ),
        )
        conn.execute(
            "UPDATE tasks SET state='reviewing', metadata=?, attempt_count=1 WHERE id=?",
            (json_dumps({"review_target": {"executor_evidence_id": eid}}), task.id),
        )
    return task.id, eid


def _client(cp):
    return TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "operator": TokenPrincipal(
                    scopes=frozenset({"read", "write"}), human_id="human_test"
                ),
                "worker": TokenPrincipal(scopes=frozenset({"read", "write"}), agent_id="executor"),
                "reader": TokenPrincipal(scopes=frozenset({"read"})),
            },
        )
    )


def test_acceptance_is_operator_authored_exact_evidence_and_attempt_bound():
    cp = ControlPlane.in_memory()
    tid, eid = _reviewing(cp)
    client = _client(cp)
    url = f"/tasks/{tid}"
    body = {"evidence_id": eid, "reason": "Ran the requested scenario and inspected its output"}
    for token in ("worker", "reader"):
        assert (
            client.post(
                url + "/acceptance", json=body, headers={"Authorization": f"Bearer {token}"}
            ).status_code
            == 403
        )
    client.headers["Authorization"] = "Bearer operator"
    before = client.get(url + "/outcome").json()
    assert before["tests"]["status"] == "reported_pass"
    assert before["acceptance"]["status"] == "unknown"
    assert before["deployment"]["status"] == "unknown"
    assert (
        client.post(url + "/acceptance", json={**body, "evidence_id": "wrong"}).status_code == 400
    )
    result = client.post(url + "/acceptance", json=body)
    assert result.status_code == 200, result.text
    assert result.json()["acceptance"]["actor"] == "human_test"
    assert result.json()["acceptance"]["status"] == "accepted"
    assert cp.get_task(tid).state == "reviewing"  # acceptance does not publish
    assert (
        client.post(url + "/acceptance", json={**body, "accepted": False}).json()["acceptance"][
            "status"
        ]
        == "rejected"
    )
    cp.store.execute("UPDATE tasks SET attempt_count=2 WHERE id=?", (tid,))
    assert client.get(url + "/outcome").json()["acceptance"]["status"] == "unknown"
    assert client.get("/tasks/outcomes?project=trust").status_code == 200


def test_creation_cohort_keeps_unfinished_tasks_and_unknown_costs():
    cp = ControlPlane.in_memory()
    tid, eid = _reviewing(cp)
    cp.create_task("Unfinished", project="trust")
    cp.record_task_acceptance(tid, evidence_id=eid, reason="Observed the result", actor="operator")
    cp.store.execute(
        "UPDATE tasks SET state='completed', completed_at=? WHERE id=?", (utcnow(), tid)
    )
    cohort = cp.task_outcome_cohort(project="trust")
    assert cohort["count"] == 2
    assert cohort["accepted_completed_count"] == 1
    assert cohort["accepted_completion_fraction"] == 0.5
    item = next(x for x in cohort["tasks"] if x["task_id"] == tid)
    assert item["known_cost_usd"] is None
    assert item["recorded_operator_interventions"] == 1
    assert cp.task_outcome_cohort(project="trust", limit=1)["truncated"] is True
    assert cp.task_outcome_cohort(project="absent")["accepted_completion_fraction"] is None


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"operator_persona": True}, "agent_operator_persona"),
        ({"dispatch_held": True}, "agent_held"),
        ({"online": False}, "agent_offline"),
        ({"healthy": False}, "agent_unhealthy"),
        ({"execution_boundary_verified": False}, "agent_no_execution_boundary"),
        ({"active_leases": 1}, "agent_capacity_full"),
        ({"sync_queue_head_task_id": "task_update"}, "agent_sync_barrier:draining"),
    ],
)
def test_capacity_uses_allocator_exclusions(changes, reason):
    worker = AllocationAgent(id="worker")
    report = summarize_execution_capacity([replace(worker, **changes)])
    assert report["executable_idle_worker_count"] == 0
    assert reason in report["excluded"][0]["reasons"]
    assert summarize_execution_capacity([worker])["eligible_worker_ids"] == ["worker"]


def test_capacity_counts_authorized_tenant_without_claiming_all_tasks_match():
    worker = AllocationAgent(id="tenant-worker", authorized_tenants=frozenset({"tenant-a"}))
    assert summarize_execution_capacity([worker])["executable_idle_worker_count"] == 1
    assert (
        summarize_execution_capacity([replace(worker, denied_tenants=frozenset({"tenant-a"}))])[
            "executable_idle_worker_count"
        ]
        == 0
    )


def test_cohort_preserves_partial_price_coverage_and_origin_counts():
    cp = ControlPlane.in_memory()
    task = cp.create_task(
        "Generated unfinished work", project="trust", metadata={"origin": {"type": "dream"}}
    )
    for detail in (
        {"cost_usd": 0.25},
        {"usage": {"cost_usd": 0.5}},
        {"usage": {"input_tokens": 20}},
    ):
        cp.record_log(
            "llm.route",
            layer="router",
            source="fixture",
            subject_type="task",
            subject_id=task.id,
            detail=detail,
        )
    result = cp.task_outcome_cohort(project="trust")
    item = result["tasks"][0]
    assert item["known_cost_usd"] == 0.75
    assert item["cost_coverage"] == "partial"
    assert item["priced_route_count"] == 2
    assert item["observed_route_count"] == 3
    assert result["origins"][0]["origin_type"] == "dream"
    assert result["origins"][0]["accepted_completed_count"] == 0
    assert result["origins"][0]["median_completed_seconds"] is None


def test_new_executor_evidence_invalidates_acceptance_and_reported_tests():
    cp = ControlPlane.in_memory()
    tid, eid = _reviewing(cp)
    cp.record_task_acceptance(
        tid, evidence_id=eid, actor="operator", reason="Observed this version"
    )
    cp.store.execute(
        "UPDATE tasks SET metadata=? WHERE id=?",
        (json_dumps({"review_target": {"executor_evidence_id": "new-result"}}), tid),
    )
    result = cp.task_outcome(tid)
    assert result["acceptance"]["status"] == "unknown"
    assert result["tests"]["status"] == "unknown"


def test_throughput_does_not_count_idle_operator_or_held_worker():
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("host")
    cp.register_agent(
        machine.id,
        "operator",
        agent_id="agent_operator",
        capabilities=[],
        resources={"operator_persona": True},
    )
    held = cp.register_agent(machine.id, "held-worker", capabilities=["python"])
    cp.set_agent_dispatch_hold(held.id, reason="operator hold")
    report = cp.task_flow_report()
    assert report["active"]["idle_worker_count"] == 0
    assert report["active"]["execution_capacity"]["idle_identity_count"] >= 1


def test_deployment_after_publication_uses_a_dependent_tasks_evidence_boundary():
    from mac.models import AuthorizationError

    cp = ControlPlane.in_memory()
    tid, eid = _reviewing(cp)
    cp.store.execute(
        "UPDATE tasks SET state='completed', completed_at=? WHERE id=?", (utcnow(), tid)
    )
    machine = cp.register_machine("deployment-host")
    agent = cp.register_agent(machine.id, "deployment-operator")
    rollout = cp.create_task("Deploy the published result", project="trust", dependencies=[tid])
    _, lease = cp.claim_task(rollout.id, agent.id)
    cp.start_task(rollout.id, agent.id, lease_id=lease.id)
    with pytest.raises(AuthorizationError):
        cp.add_evidence(
            tid, "deployment", "test://runtime", "Observed runtime", agent.id, lease_id=lease.id
        )
    cp.add_evidence(
        rollout.id,
        "deployment",
        "test://old-runtime",
        "Old result",
        agent.id,
        lease_id=lease.id,
        metadata={"executor_evidence_id": "old-evidence"},
    )
    assert cp.task_outcome(tid)["deployment"]["status"] == "unknown"
    evidence = cp.add_evidence(
        rollout.id,
        "deployment",
        "test://runtime",
        "Observed exact result",
        agent.id,
        lease_id=lease.id,
        metadata={"executor_evidence_id": eid},
    )
    outcome = cp.task_outcome(tid)
    assert outcome["deployment"] == {
        "status": "recorded",
        "task_id": rollout.id,
        "evidence_id": evidence.id,
    }
    assert any(action["command"] == f"mac task show {rollout.id}" for action in outcome["actions"])
    assert cp.get_task(tid).state == "completed"
    # Merely naming the evidence from an unrelated task is not a linkage.
    cp.store.execute("DELETE FROM task_edges WHERE task_id=?", (rollout.id,))
    assert cp.task_outcome(tid)["deployment"]["status"] == "unknown"
