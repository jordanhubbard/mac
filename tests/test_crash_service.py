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
    return cp.register_agent(machine.id, name, capabilities=["python", "ops", "testing"])


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


def _crash_notifications(cp: ControlPlane, report_id: str):
    return [
        item
        for item in cp.list_notifications(subject_type="crash_report", subject_id=report_id)
        if item.event_type == "agent.crash.observed"
    ]


def test_crash_ingest_deduplicates_records_and_files_no_task():
    cp = ControlPlane.in_memory()
    cp.create_project("mac", dispatch_paused=False)
    crashed = _agent(cp, "crashed")
    peer = _agent(cp, "peer")

    first = cp.crashes.ingest(crashed.id, _payload("event-1"))
    assert first["occurrence_count"] == 1
    assert first["status"] == "open"
    assert first["repair_task_id"] is None
    assert first["repair_attempt_count"] == 0
    assert len(first["occurrences"]) == 1

    repeated = cp.crashes.ingest(peer.id, _payload("event-2", stack=STACK_B))
    assert repeated["id"] == first["id"]
    assert repeated["fingerprint"] == first["fingerprint"]
    assert repeated["occurrence_count"] == 2
    assert repeated["affected_agent_ids"] == sorted([crashed.id, peer.id])

    duplicate = cp.crashes.ingest(peer.id, _payload("event-2", stack=STACK_B))
    assert duplicate["duplicate"] is True
    assert duplicate["occurrence_count"] == 2

    # Crashes are recorded and surfaced; they no longer become self-filed work.
    assert cp.list_tasks() == []
    assert [item["id"] for item in cp.crashes.list_reports(status="open")] == [first["id"]]
    # One operator notification per incident, not one per occurrence.
    assert len(_crash_notifications(cp, first["id"])) == 1


def test_crash_recurrence_after_resolution_reopens_and_notifies_again():
    cp = ControlPlane.in_memory()
    crashed = _agent(cp, "crashed")
    first = cp.crashes.ingest(crashed.id, _payload("event-1"))
    cp.crashes.resolve(first["id"], actor="test", reason="fixed")

    recurring = cp.crashes.ingest(crashed.id, _payload("event-2"))
    assert recurring["id"] == first["id"]
    assert recurring["status"] == "open"
    assert recurring["repair_task_id"] is None
    assert len(_crash_notifications(cp, first["id"])) == 2
    assert cp.list_tasks() == []


def test_crash_fingerprint_includes_revision_and_resolve_is_durable():
    cp = ControlPlane.in_memory()
    crashed = _agent(cp, "crashed")
    first = cp.crashes.ingest(crashed.id, _payload("event-1", revision="rev-a"))
    second = cp.crashes.ingest(crashed.id, _payload("event-2", revision="rev-b"))
    assert first["fingerprint"] != second["fingerprint"]
    resolved = cp.crashes.resolve(first["id"], actor="test", reason="verified")
    assert resolved["status"] == "resolved"
    listed = cp.crashes.list_reports(status="resolved")
    assert [item["id"] for item in listed] == [resolved["id"]]
