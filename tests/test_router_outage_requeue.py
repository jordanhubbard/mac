"""A model-router outage during an attempt gives the task its attempt back.

On 2026-10-03 and 10-05, every upstream provider dropped for 4-7 minutes. One
coding agent exited on the router's 503, and one attempt never started because
its in-sandbox route probe timed out. Both were charged to the task.
"""

from datetime import timedelta

import pytest

import mac.services as services
from mac.models import TaskState, parse_time
from mac.services import ControlPlane

# What the 10-05 attempt reported: the router error itself never appeared.
PROBE_TIMED_OUT = {
    "reason": "verification_contract_failed",
    "diagnosis": {
        "failure": "verification_contract_failed",
        "output_tail": (
            "[executor] coding-agent sandbox preflight (opencode): FAILED (rc=124, class=timeout)\n"
            "task execution requires an available coding agent and, when confined, a verified "
            "in-sandbox route; repo evidence requires changed files"
        ),
    },
}


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _attempt(cp, *, max_attempts=1):
    machine = cp.register_machine("host-natasha")
    agent = cp.register_agent(machine.id, "natasha", capabilities=["python"])
    task = cp.create_task("work", required_capabilities=["python"], max_attempts=max_attempts)
    cp.claim_task(task.id, agent.id)
    return agent, task


def _route(cp, task, outcome):
    cp.record_log(
        "llm.route",
        level="error",
        layer="router",
        source="agent_natasha",
        subject_type="task",
        subject_id=task.id,
        detail={"schema": "mac.llm_route.v1", "status_code": 503, "outcome": outcome},
    )


def _block(cp, task, agent, *, minutes_ago=10):
    cp._transition_task_internal(task.id, TaskState.BLOCKED.value, agent.id, PROBE_TIMED_OUT)
    then = (parse_time(services.utcnow()) - timedelta(minutes=minutes_ago)).isoformat()
    cp.store.execute(
        "UPDATE tasks SET attempt_count = 1, updated_at = ? WHERE id = ?", (then, task.id)
    )


def test_an_attempt_that_hit_a_router_outage_is_requeued_without_charge(cp):
    agent, task = _attempt(cp)
    _route(cp, task, "all_providers_unavailable")
    _block(cp, task, agent)

    cp.tick(limit=0)

    reopened = cp.get_task(task.id)
    assert reopened.state == TaskState.OPEN.value
    assert reopened.attempt_count == 0
    assert reopened.metadata["router_outage_requeues"] == 1
    assert "retry_excluded_agent_ids" not in reopened.metadata  # not the worker's fault
    assert not cp.get_agent(agent.id).dispatch_hold
    reopen = [e for e in cp.task_history(task.id) if e.event_type == "task.auto_reopened"][-1]
    assert reopen.detail["router_outage"] is True and reopen.detail["attempt_refunded"] is True


def test_the_requeue_waits_out_the_retry_backoff(cp):
    agent, task = _attempt(cp)
    _route(cp, task, "all_providers_unavailable")
    _block(cp, task, agent, minutes_ago=0)

    cp.tick(limit=0)

    assert cp.get_task(task.id).state == TaskState.BLOCKED.value


def test_a_failure_without_a_router_outage_is_charged_as_before(cp):
    agent, task = _attempt(cp)
    _route(cp, task, "success")
    _block(cp, task, agent)

    cp.tick(limit=0)

    assert cp.get_task(task.id).state == TaskState.FAILED.value


def test_an_outage_before_this_attempt_does_not_count(cp):
    machine = cp.register_machine("host-natasha")
    agent = cp.register_agent(machine.id, "natasha", capabilities=["python"])
    task = cp.create_task("work", required_capabilities=["python"], max_attempts=1)
    _route(cp, task, "all_providers_unavailable")
    cp.store.execute(
        "UPDATE observability_events SET created_at = ? WHERE subject_id = ?",
        ("2000-01-01T00:00:00+00:00", task.id),
    )
    cp.claim_task(task.id, agent.id)
    _block(cp, task, agent)

    cp.tick(limit=0)

    assert cp.get_task(task.id).state == TaskState.FAILED.value


def test_refunds_stop_after_the_limit(cp, monkeypatch):
    monkeypatch.setenv("MAC_ROUTER_OUTAGE_REQUEUES", "1")
    agent, task = _attempt(cp)
    _route(cp, task, "all_providers_unavailable")
    _block(cp, task, agent)
    cp.tick(limit=0)
    assert cp.get_task(task.id).state == TaskState.OPEN.value

    cp.claim_task(task.id, agent.id)
    _route(cp, task, "all_providers_unavailable")
    _block(cp, task, agent)
    cp.tick(limit=0)

    assert cp.get_task(task.id).state == TaskState.FAILED.value
