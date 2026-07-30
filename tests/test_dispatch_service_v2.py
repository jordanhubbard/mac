from mac.models import TaskState
from mac.services import ControlPlane


def worker(cp: ControlPlane, name: str, capabilities=None):
    machine = cp.register_machine("%s-host" % name)
    return cp.register_agent(
        machine.id,
        name,
        capabilities=capabilities or ["python"],
    )


def active_project(cp: ControlPlane, name: str = "mac"):
    return cp.create_project(name, dispatch_paused=False)


def test_push_dispatch_uses_one_global_allocator_round_for_parallel_capacity():
    cp = ControlPlane.in_memory()
    active_project(cp)
    first_worker = worker(cp, "first")
    second_worker = worker(cp, "second")
    first_task = cp.create_task("first task", project="mac", required_capabilities=["python"])
    second_task = cp.create_task("second task", project="mac", required_capabilities=["python"])

    assignments = cp.dispatch._dispatch_batch_impl(
        lease_seconds=120,
        limit=2,
        run_maintenance=False,
    )

    assert {item["task"]["id"] for item in assignments} == {
        first_task.id,
        second_task.id,
    }
    assert {item["agent"]["id"] for item in assignments} == {
        first_worker.id,
        second_worker.id,
    }
    assert all(item["lease"]["status"] == "active" for item in assignments)


def test_pull_claim_triggers_global_round_and_ignores_request_filters():
    cp = ControlPlane.in_memory()
    active_project(cp)
    first_worker = worker(cp, "first")
    second_worker = worker(cp, "second")
    tasks = [
        cp.create_task(
            "task-%d" % index,
            project="mac",
            required_capabilities=["python"],
        )
        for index in range(2)
    ]

    assignment = cp.claim_next_for_agent(
        first_worker.id,
        allowed_projects=["not-mac"],
        required_metadata={"canary": True},
        claim_only_canary_tasks=True,
        capabilities=["not-python"],
    )

    assert assignment is not None
    assert assignment["agent"]["id"] == first_worker.id
    assert {cp.get_task(task.id).state for task in tasks} == {TaskState.CLAIMED.value}
    assert cp._active_assignment_for_agent(second_worker) is not None


def test_pull_dry_run_uses_global_snapshot_without_creating_a_lease():
    cp = ControlPlane.in_memory()
    active_project(cp)
    target = worker(cp, "target")
    task = cp.create_task(
        "ordinary non-canary",
        project="mac",
        required_capabilities=["python"],
    )

    candidate = cp.claim_next_for_agent(
        target.id,
        allowed_projects=["wrong"],
        claim_only_canary_tasks=True,
        dry_run=True,
    )

    assert candidate is not None
    assert candidate["task"]["id"] == task.id
    assert candidate["agent"]["id"] == target.id
    assert candidate["lease"] is None
    assert candidate["dry_run"] is True
    assert cp.get_task(task.id).state == TaskState.OPEN.value
    assert (
        cp.store.query_one(
            "SELECT id FROM leases WHERE task_id = ?",
            (task.id,),
        )
        is None
    )


def test_only_runnable_but_unmatched_work_emits_provisioning_signal():
    cp = ControlPlane.in_memory()
    active_project(cp)
    worker(cp, "python-only", capabilities=["python"])
    dependency = cp.create_task(
        "dependency",
        project="mac",
        required_capabilities=["python"],
        metadata={"no_dispatch": True},
    )
    blocked = cp.create_task(
        "blocked",
        project="mac",
        dependencies=[dependency.id],
        required_capabilities=["gpu"],
    )
    unmatched = cp.create_task(
        "needs gpu",
        project="mac",
        required_capabilities=["gpu"],
    )

    assignments = cp.dispatch._dispatch_batch_impl(
        limit=10,
        run_maintenance=False,
    )

    assert assignments == []
    requests = cp.list_provisioning_requests()
    assert [request.task_id for request in requests] == [unmatched.id]
    assert blocked.id not in {request.task_id for request in requests}


def test_named_target_resolves_to_unique_agent_id():
    cp = ControlPlane.in_memory()
    active_project(cp)
    target = worker(cp, "target")
    worker(cp, "other")
    task = cp.create_task(
        "named target",
        project="mac",
        required_capabilities=["python"],
        metadata={"target_agent_name": "target"},
    )

    assignment = cp.dispatch_once()

    assert assignment is not None
    assert assignment["task"]["id"] == task.id
    assert assignment["agent"]["id"] == target.id


def test_ready_tasks_and_explain_share_allocator_v2_task_gates():
    cp = ControlPlane.in_memory()
    active_project(cp)
    target = worker(cp, "target")
    dependency = cp.create_task("dependency", project="mac", required_capabilities=["python"])
    blocked = cp.create_task(
        "blocked",
        project="mac",
        dependencies=[dependency.id],
        required_capabilities=["python"],
    )
    ready = cp.create_task(
        "ready",
        project="mac",
        required_capabilities=["python"],
        metadata={"target_agent_name": "target"},
    )

    ready_tasks = cp.dispatch.ready_tasks(project="mac", limit=10)
    explanation = cp.dispatch.explain_task_dispatch(ready.id)

    assert [task.id for task in ready_tasks] == [dependency.id, ready.id]
    assert blocked.id not in {task.id for task in ready_tasks}
    assert explanation["task_ready"] is True
    assert explanation["dispatchable"] is True
    assert explanation["eligible_agent_count"] == 1
    assert explanation["candidates"][0]["agent_id"] == target.id


def test_explain_non_open_task_uses_v2_task_rejection():
    cp = ControlPlane.in_memory()
    active_project(cp)
    worker(cp, "worker")
    task = cp.create_task("done", project="mac", required_capabilities=["python"])
    cp.force_complete_task(task.id, "test", reason="test fixture")

    explanation = cp.dispatch.explain_task_dispatch(task.id)

    assert explanation["task_ready"] is False
    assert explanation["dispatchable"] is False
    assert [reason["code"] for reason in explanation["task_reasons"]] == ["task_not_open"]
