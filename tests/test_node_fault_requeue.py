"""A node fault benches the node and gives the task its attempt back.

On 2026-09-23..28 a disk-full worker failed 55 tasks and a broken worker
install on two nodes failed 40 more. Each failure was charged to the task
(usually terminally) while the faulty node kept claiming new work.
"""

import pytest

import mac.services as services
from mac.models import TaskState
from mac.services import ControlPlane

BROKEN_INSTALL = {
    "reason": "executor_failed",
    "failure": "executor_failed",
    "manual_repair_required": True,
    "output_tail": (
        "Traceback (most recent call last):\n"
        '  File "/home/jkh/.mac/bin/mac-task-executor.py", line 6, in <module>\n'
        "    from mac.task_executor import main\n"
        "ModuleNotFoundError: No module named 'mac'\n"
    ),
}
DISK_FULL = {
    "reason": "worker_exception",
    "diagnosis": {
        "failure": "worker_exception",
        "output_tail": (
            "OSError: [Errno 28] No space left on device: "
            "'/home/horde/.mac/agent-workspaces/task_x'"
        ),
    },
}


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _worker(cp, name):
    machine = cp.register_machine("host-%s" % name)
    return cp.register_agent(machine.id, name, capabilities=["python"])


def _fail_attempt(cp, task, agent, detail, *, attempt=1):
    cp._transition_task_internal(task.id, TaskState.BLOCKED.value, agent.id, detail)
    cp.store.execute(
        "UPDATE tasks SET attempt_count = ?, updated_at = ? WHERE id = ?",
        (attempt, services.utcnow(), task.id),
    )


def test_node_fault_signatures():
    assert services._node_fault(BROKEN_INSTALL) == "broken_worker_install"
    assert services._node_fault(DISK_FULL) == "disk_full"
    assert services._node_fault({"reason": "executor_failed", "output_tail": "3 tests failed"}) is None


def test_broken_install_requeues_task_and_benches_the_node(cp):
    agent = _worker(cp, "natasha")
    task = cp.create_task("real work", required_capabilities=["python"], max_attempts=3)
    _fail_attempt(cp, task, agent, BROKEN_INSTALL)

    result = cp.tick(limit=0)

    reopened = cp.get_task(task.id)
    assert reopened.state == TaskState.OPEN.value
    assert reopened.attempt_count == 0
    assert reopened.metadata["retry_excluded_agent_ids"] == [agent.id]
    assert reopened.metadata["retry_failure_kind"] == "node_fault"
    assert [item["id"] for item in result["auto_reopened"]] == [task.id]
    held = cp.get_agent(agent.id)
    assert held.dispatch_hold
    assert held.dispatch_hold_reason == "auto_quarantine:node_fault:broken_worker_install"
    reopen = [e for e in cp.task_history(task.id) if e.event_type == "task.auto_reopened"][-1]
    assert reopen.detail["node_fault"] == "broken_worker_install"
    assert reopen.detail["attempt_refunded"] is True


def test_disk_full_on_a_held_node_still_requeues_without_rehold(cp):
    agent = _worker(cp, "canary")
    first = cp.create_task("first", required_capabilities=["python"], max_attempts=3)
    second = cp.create_task("second", required_capabilities=["python"], max_attempts=3)
    _fail_attempt(cp, first, agent, DISK_FULL, attempt=3)
    _fail_attempt(cp, second, agent, DISK_FULL, attempt=3)

    cp.tick(limit=0)  # the retry sweep takes one blocked task per tick here
    cp.tick(limit=0)

    for task in (first, second):
        reopened = cp.get_task(task.id)
        # Even a task on its last attempt is not failed by a node fault.
        assert reopened.state == TaskState.OPEN.value
        assert reopened.attempt_count == 2
    assert cp.get_agent(agent.id).dispatch_hold_reason == "auto_quarantine:node_fault:disk_full"
    quarantined = [
        [e for e in cp.task_history(t.id) if e.event_type == "task.auto_reopened"][-1].detail[
            "node_quarantined"
        ]
        for t in (first, second)
    ]
    assert sorted(quarantined) == [False, True]


def test_operator_hold_reason_is_not_overwritten(cp):
    agent = _worker(cp, "bullwinkle")
    cp.set_agent_dispatch_hold(agent.id, "stabilization: operator pause")
    task = cp.create_task("work", required_capabilities=["python"], max_attempts=3)
    _fail_attempt(cp, task, agent, BROKEN_INSTALL)

    cp.tick(limit=0)

    assert cp.get_task(task.id).state == TaskState.OPEN.value
    assert cp.get_agent(agent.id).dispatch_hold_reason == "stabilization: operator pause"


def test_missing_mac_module_inside_the_task_is_the_tasks_problem(cp):
    """An agent's own test run failing to import mac is not a broken node."""
    agent = _worker(cp, "rocky")
    task = cp.create_task("work", required_capabilities=["python"], max_attempts=3)
    _fail_attempt(
        cp,
        task,
        agent,
        {
            "reason": "executor_failed",
            "manual_repair_required": True,
            "output_tail": "tests/test_x.py:1: in <module>\nE ModuleNotFoundError: No module named 'mac'",
        },
    )

    cp.tick(limit=0)

    assert cp.get_task(task.id).state == TaskState.FAILED.value
    assert not cp.get_agent(agent.id).dispatch_hold


def test_operator_non_retryable_marker_still_wins(cp):
    agent = _worker(cp, "rocky")
    task = cp.create_task("work", required_capabilities=["python"], max_attempts=3)
    _fail_attempt(cp, task, agent, {**BROKEN_INSTALL, "diagnosis_code": "needs-operator"})

    cp.tick(limit=0)

    assert cp.get_task(task.id).state != TaskState.OPEN.value
    assert not cp.get_agent(agent.id).dispatch_hold
