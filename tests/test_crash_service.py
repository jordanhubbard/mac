from __future__ import annotations

from mac.services import ControlPlane


STACK_A = """2026-07-12T01:02:03Z Fatal Python error: Segmentation fault
Traceback (most recent call last):
  File "/home/alice/mac/src/mac/worker.py", line 99, in run_once
    explode()
RuntimeError: observer proof at 0xabc123 pid=1234
"""

STACK_B = """2026-07-12T09:08:07Z Fatal Python error: Segmentation fault
Traceback (most recent call last):
  File "/Users/bob/mac/src/mac/worker.py", line 99, in run_once
    explode()
RuntimeError: observer proof at 0xdef456 pid=9999
"""


def _agent(cp: ControlPlane, name: str):
    machine = cp.register_machine("%s-host" % name)
    return cp.register_agent(
        machine.id, name, capabilities=["python", "ops", "testing"]
    )


def _payload(event_id: str, stack: str = STACK_A, revision: str = "abc123"):
    return {
        "event_id": event_id,
        "observed_at": "2026-07-12T01:02:03+00:00",
        "supervisor": "systemd",
        "process_name": "mac-agent-service",
        "pid": 1234,
        "signal": 11,
        "reason": "process terminated by signal SIGSEGV",
        "revision": revision,
        "tree_sha": "tree123",
        "stack_trace": stack,
        "stderr_tail": stack,
        "core_reference": "systemd-coredump:1234",
        "core_metadata": {"provider": "systemd-coredump"},
        "resource_snapshot": {"free_bytes": 123456},
        "metadata": {"observer_pid": 55},
    }


def test_crash_ingest_deduplicates_and_reassigns_repair_to_unaffected_peer():
    cp = ControlPlane.in_memory()
    crashed = _agent(cp, "crashed")
    first_peer = _agent(cp, "first-peer")

    first = cp.crashes.ingest(crashed.id, _payload("event-1"))
    assert first["occurrence_count"] == 1
    task = cp.get_task(first["repair_task_id"])
    assert task.owner_agent_id == first_peer.id
    assert task.metadata["excluded_agent_ids"] == [crashed.id]
    assert task.metadata.get("no_dispatch") is None

    second_peer = _agent(cp, "second-peer")
    repeated = cp.crashes.ingest(
        first_peer.id,
        _payload("event-2", stack=STACK_B),
    )
    assert repeated["id"] == first["id"]
    assert repeated["fingerprint"] == first["fingerprint"]
    assert repeated["occurrence_count"] == 2
    assert repeated["affected_agent_ids"] == sorted([crashed.id, first_peer.id])
    task = cp.get_task(first["repair_task_id"])
    assert task.owner_agent_id == second_peer.id
    assert task.metadata["excluded_agent_ids"] == sorted([crashed.id, first_peer.id])

    duplicate = cp.crashes.ingest(first_peer.id, _payload("event-2", stack=STACK_B))
    assert duplicate["duplicate"] is True
    assert duplicate["occurrence_count"] == 2


def test_crash_recurrence_after_completed_repair_creates_new_task():
    cp = ControlPlane.in_memory()
    crashed = _agent(cp, "crashed")
    _agent(cp, "repairer")
    first = cp.crashes.ingest(crashed.id, _payload("event-1"))
    old_task = cp.get_task(first["repair_task_id"])
    cp.force_complete_task(old_task.id, "test", "repair proof")

    recurring = cp.crashes.ingest(crashed.id, _payload("event-2"))
    assert recurring["repair_task_id"] != old_task.id
    new_task = cp.get_task(recurring["repair_task_id"])
    assert new_task.metadata["prior_repair_task_id"] == old_task.id
    assert "recurred after repair task" in new_task.description


def test_crash_fingerprint_includes_revision_and_resolve_is_durable():
    cp = ControlPlane.in_memory()
    crashed = _agent(cp, "crashed")
    _agent(cp, "repairer")
    first = cp.crashes.ingest(crashed.id, _payload("event-1", revision="rev-a"))
    second = cp.crashes.ingest(crashed.id, _payload("event-2", revision="rev-b"))
    assert first["fingerprint"] != second["fingerprint"]
    resolved = cp.crashes.resolve(first["id"], actor="test", reason="verified")
    assert resolved["status"] == "resolved"
    listed = cp.crashes.list_reports(status="resolved")
    assert [item["id"] for item in listed] == [resolved["id"]]


def test_crash_repair_tick_closes_incident_after_verified_task_completion():
    cp = ControlPlane.in_memory()
    crashed = _agent(cp, "crashed")
    _agent(cp, "repairer")
    report = cp.crashes.ingest(crashed.id, _payload("event-1"))
    assert report["repair_attempt_count"] == 1
    cp.force_complete_task(report["repair_task_id"], "test", "regression verified")
    tick = cp.crashes.tick()
    assert tick["resolved"] == 1
    assert cp.crashes.get_report(report["id"])["status"] == "resolved"


def test_crash_repair_tick_refiles_failed_repair_with_prior_evidence_link():
    cp = ControlPlane.in_memory()
    crashed = _agent(cp, "crashed")
    repairer = _agent(cp, "repairer")
    report = cp.crashes.ingest(crashed.id, _payload("event-1"))
    old_task = cp.get_task(report["repair_task_id"])
    assert old_task.owner_agent_id == repairer.id
    cp.transition_task(
        old_task.id,
        "failed",
        repairer.id,
        {"reason": "first repair approach did not hold"},
    )
    tick = cp.crashes.tick()
    assert tick["requeued"] == 1
    refreshed = cp.crashes.get_report(report["id"])
    assert refreshed["repair_attempt_count"] == 2
    assert refreshed["repair_task_id"] != old_task.id
    replacement = cp.get_task(refreshed["repair_task_id"])
    assert replacement.metadata["prior_repair_task_id"] == old_task.id
