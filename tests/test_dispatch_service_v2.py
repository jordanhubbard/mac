from contextlib import contextmanager

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


def test_pull_claims_explicit_target_without_global_allocation_round(monkeypatch):
    cp = ControlPlane.in_memory()
    active_project(cp)
    target = worker(cp, "target")
    task = cp.create_task(
        "targeted",
        project="mac",
        required_capabilities=["python"],
        metadata={"target_agent_id": target.id},
    )

    def global_round_must_not_run(**_kwargs):
        raise AssertionError("explicit target should use the bounded direct path")

    monkeypatch.setattr(cp.dispatch, "_allocate_v2_round", global_round_must_not_run)

    assignment = cp.claim_next_for_agent(target.id)

    assert assignment is not None
    assert assignment["task"]["id"] == task.id
    assert assignment["agent"]["id"] == target.id
    assert cp.get_task(task.id).state == TaskState.CLAIMED.value


def test_targeted_task_ineligible_falls_through_to_the_global_round():
    """A target_agent_id task the agent is not actually eligible for (missing
    capability) must be skipped by the bounded direct path, not claimed
    incorrectly and not left stuck blocking the fallback global round."""
    cp = ControlPlane.in_memory()
    active_project(cp)
    target = worker(cp, "target", capabilities=["python"])
    ineligible_task = cp.create_task(
        "targeted but ineligible",
        project="mac",
        required_capabilities=["rust"],
        metadata={"target_agent_id": target.id},
    )
    fallback_task = cp.create_task(
        "untargeted fallback",
        project="mac",
        required_capabilities=["python"],
    )

    assignment = cp.claim_next_for_agent(target.id)

    assert assignment is not None
    assert assignment["task"]["id"] == fallback_task.id
    assert cp.get_task(ineligible_task.id).state == TaskState.OPEN.value


def test_targeted_claim_raising_falls_through_to_a_later_eligible_target(monkeypatch):
    """claim_task_v2 raising AuthorizationError/TransitionError/ValidationError
    for one targeted candidate (e.g. a lost claim race) must be swallowed and
    the loop must continue to the next targeted candidate rather than
    propagating or silently returning nothing."""
    from mac.models import ValidationError

    cp = ControlPlane.in_memory()
    active_project(cp)
    target = worker(cp, "target")
    first_task = cp.create_task(
        "targeted first",
        project="mac",
        required_capabilities=["python"],
        metadata={"target_agent_id": target.id},
    )
    second_task = cp.create_task(
        "targeted second",
        project="mac",
        required_capabilities=["python"],
        metadata={"target_agent_id": target.id},
    )

    real_claim = cp.claim_task_v2

    def flaky_claim(task_id, agent_id, **kwargs):
        if task_id == first_task.id:
            raise ValidationError("synthetic lost-race for test coverage")
        return real_claim(task_id, agent_id, **kwargs)

    monkeypatch.setattr(cp, "claim_task_v2", flaky_claim)

    assignment = cp.claim_next_for_agent(target.id)

    assert assignment is not None
    assert assignment["task"]["id"] == second_task.id
    assert cp.get_task(first_task.id).state == TaskState.OPEN.value


def test_no_targeted_tasks_returns_none_and_uses_the_global_round(monkeypatch):
    """An agent with no target_agent_id-tagged tasks at all must fall straight
    through the bounded direct path (empty result set) to the ordinary global
    allocation round, not error or stall."""
    cp = ControlPlane.in_memory()
    active_project(cp)
    target = worker(cp, "target")
    task = cp.create_task("untargeted", project="mac", required_capabilities=["python"])
    calls = []
    real_targeted = cp.dispatch._claim_targeted_task_for_agent

    def counted_targeted(agent, **kwargs):
        result = real_targeted(agent, **kwargs)
        calls.append(result)
        return result

    monkeypatch.setattr(cp.dispatch, "_claim_targeted_task_for_agent", counted_targeted)

    assignment = cp.claim_next_for_agent(target.id)

    assert calls == [None]
    assert assignment is not None
    assert assignment["task"]["id"] == task.id


def test_explain_reuses_a_caller_supplied_sync_states_without_rebuilding(monkeypatch):
    """A caller explaining many tasks in one pass (task_flow_report) builds
    sync_states once and passes it explicitly; explain_task_dispatch must not
    rebuild it in that case."""
    cp = ControlPlane.in_memory()
    active_project(cp)
    worker(cp, "first")
    task = cp.create_task("explain", project="mac", required_capabilities=["python"])
    sync_states = cp.dispatch._sync_barrier_states()
    calls = 0
    real_non_terminal_tasks = cp._non_terminal_tasks

    def counted_non_terminal_tasks():
        nonlocal calls
        calls += 1
        return real_non_terminal_tasks()

    monkeypatch.setattr(cp, "_non_terminal_tasks", counted_non_terminal_tasks)

    explanation = cp.dispatch.explain_task_dispatch(task.id, sync_states=sync_states)

    assert explanation["candidate_count"] == 1
    assert calls == 0


def test_idle_pull_is_write_free_and_does_not_claim_reconciliation_leases():
    cp = ControlPlane.in_memory()
    active_project(cp)
    idle_worker = worker(cp, "idle")
    # Recorded at the store API rather than through sqlite3's trace callback,
    # which only exists on one backend. Both `store.execute` and the connection
    # handed out by `store.transaction` are wrapped, since a write can go
    # through either.
    statements: list[str] = []
    real_execute = cp.store.execute
    real_transaction = cp.store.transaction

    def recording_execute(sql, params=(), *args, **kwargs):
        statements.append(str(sql))
        return real_execute(sql, params, *args, **kwargs)

    @contextmanager
    def recording_transaction(*args, **kwargs):
        with real_transaction(*args, **kwargs) as conn:
            real_conn_execute = conn.execute

            def conn_execute(sql, params=(), *inner, **inner_kwargs):
                statements.append(str(sql))
                return real_conn_execute(sql, params, *inner, **inner_kwargs)

            conn.execute = conn_execute  # type: ignore[method-assign]
            statements.append("BEGIN")
            yield conn

    cp.store.execute = recording_execute  # type: ignore[method-assign]
    cp.store.transaction = recording_transaction  # type: ignore[method-assign]
    try:
        assert cp.claim_next_for_agent(idle_worker.id) is None
    finally:
        cp.store.execute = real_execute  # type: ignore[method-assign]
        cp.store.transaction = real_transaction  # type: ignore[method-assign]

    writes = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(("BEGIN", "INSERT", "UPDATE", "DELETE", "REPLACE"))
    ]
    assert writes == []
    assert cp.store.query_all("SELECT * FROM reconciliation_state") == []
    assert cp.store.query_all("SELECT * FROM dispatch_rounds") == []


def test_pull_provisions_unmatched_work_once_and_deduplicates_rounds():
    cp = ControlPlane.in_memory()
    active_project(cp)
    python_worker = worker(cp, "python-only", capabilities=["python"])
    task = cp.create_task(
        "needs gpu",
        project="mac",
        required_capabilities=["gpu"],
    )
    emitted = []
    cp.dispatch._empty_pull_round_interval_seconds = 0
    cp.dispatch._emit_dispatch_provisioning_signal = emitted.append

    assert cp.claim_next_for_agent(python_worker.id) is None
    assert cp.claim_next_for_agent(python_worker.id) is None

    assert [item.id for item in emitted] == [task.id]
    rows = cp.store.query_all("SELECT unmatched_count, assignment_count FROM dispatch_rounds")
    assert len(rows) == 1
    assert rows[0]["unmatched_count"] == 1
    assert rows[0]["assignment_count"] == 0


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


def test_explain_builds_sync_barrier_state_once_for_all_agents(monkeypatch):
    cp = ControlPlane.in_memory()
    active_project(cp)
    worker(cp, "first")
    worker(cp, "second")
    task = cp.create_task("explain", project="mac", required_capabilities=["python"])
    calls = 0
    real_non_terminal_tasks = cp._non_terminal_tasks

    def counted_non_terminal_tasks():
        nonlocal calls
        calls += 1
        return real_non_terminal_tasks()

    monkeypatch.setattr(cp, "_non_terminal_tasks", counted_non_terminal_tasks)

    explanation = cp.dispatch.explain_task_dispatch(task.id)

    assert explanation["candidate_count"] == 2
    assert calls == 1
