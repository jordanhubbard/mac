"""Edge coverage for workflow runtime normalization and payload propagation."""

from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from mac.models import NotFoundError, ValidationError
from mac.workflow_runtime import WorkflowRuntime


class _Store:
    def __init__(self, rows=None) -> None:
        self.rows = list(rows or [])
        self.executed = []
        self.one = {}

    def query_all(self, sql, params=()):
        self.last_query = (sql, params)
        return self.rows

    def query_one(self, sql, params=()):
        if "evidence" in sql:
            return self.one.get("evidence")
        if "workflow_runs" in sql:
            return self.one.get("run")
        return self.one.get("other")

    def execute(self, sql, params=()):
        self.executed.append((sql, params))

    def transaction(self):
        return nullcontext(self)


def _runtime(store=None, **overrides):
    store = store or _Store()
    values = {
        "store": store,
        "observability": SimpleNamespace(),
        "workflows": SimpleNamespace(),
        "roles": SimpleNamespace(),
        "create_task": lambda *_a, **_k: SimpleNamespace(id="task"),
        "transition_task": lambda *_a, **_k: None,
        "get_task": lambda _id: SimpleNamespace(id="task"),
        "record_history": lambda *_a, **_k: None,
    }
    values.update(overrides)
    return WorkflowRuntime(**values)


def test_list_runs_builds_all_filters_and_clamps_limit() -> None:
    store = _Store()
    runtime = _runtime(store)
    assert runtime.list_runs(
        state="running", workflow_id="workflow", tenant_id="tenant", limit=5000
    ) == []
    sql, params = store.last_query
    assert "state = ?" in sql
    assert "workflow_id = ?" in sql
    assert "tenant_id = ?" in sql
    assert params == ("running", "workflow", "tenant", 1000)


def test_merge_plan_payloads_ignores_missing_and_malformed_evidence() -> None:
    store = _Store()
    runtime = _runtime(store)
    runtime._merge_plan_payloads_from_evidence("run", "task")
    assert store.executed == []
    store.one["evidence"] = {"metadata": "not-json"}
    with pytest.raises(json.JSONDecodeError):
        runtime._merge_plan_payloads_from_evidence("run", "task")
    store.one["evidence"] = {"metadata": json.dumps({"plan_payloads": []})}
    runtime._merge_plan_payloads_from_evidence("run", "task")
    store.one["evidence"] = {
        "metadata": json.dumps({"verification": {"plan_payloads": {"node": "bad"}}})
    }
    runtime._merge_plan_payloads_from_evidence("run", "task")
    assert store.executed == []


def test_merge_plan_payloads_filters_and_merges_existing_context() -> None:
    store = _Store()
    store.one["evidence"] = {
        "metadata": json.dumps(
            {
                "verification": {
                    "plan_payloads": {
                        "node": {
                            "instructions": "dynamic",
                            "metadata": {"owner": "planner"},
                            "required_capabilities": ["gpu"],
                            "ignored": "drop",
                        },
                        "bad": "drop",
                    }
                }
            }
        )
    }
    store.one["run"] = {"context": json.dumps({"plan_payloads": "bad", "keep": True})}
    runtime = _runtime(store)
    runtime._merge_plan_payloads_from_evidence("run", "task")
    context = json.loads(store.executed[-1][1][0])
    assert context["keep"] is True
    assert context["plan_payloads"]["node"] == {
        "instructions": "dynamic",
        "metadata": {"owner": "planner"},
        "required_capabilities": ["gpu"],
    }


def test_spawn_node_task_applies_snapshot_plan_override_and_metadata() -> None:
    captured = {}

    def create_task(title, **kwargs):
        captured.update(title=title, **kwargs)
        return SimpleNamespace(id="created")

    runtime = _runtime(
        store=_Store(),
        create_task=create_task,
        get_task=lambda task_id: SimpleNamespace(id=task_id, metadata=captured.get("metadata")),
    )
    node = {
        "node_key": "plan",
        "node_type": "plan",
        "role_required": "dev",
        "extra_capabilities": ["git"],
        "instructions": "static",
    }
    task = runtime._spawn_node_task(
        "run",
        node,
        workflow=SimpleNamespace(slug="flow"),
        started_by="actor",
        tenant_id="tenant",
        attempt=2,
        role_snapshots={
            "dev": {
                "slug": "snapshot-dev",
                "required_capabilities": ["python"],
                "default_capabilities": ["lint"],
                "hardware_requirements": {"gpu": True},
            }
        },
        plan_payloads={
            "plan": {
                "instructions": "dynamic",
                "required_capabilities": ["ignored-by-role"],
                "metadata": {"owner": "planner", "workflow_run_id": "cannot-overwrite"},
            }
        },
        run_input={"goal": "build"},
    )
    assert task.id == "created"
    assert captured["title"] == "flow :: plan"
    assert captured["description"] == "dynamic"
    assert captured["required_capabilities"] == ["git", "lint", "python"]
    metadata = captured["metadata"]
    assert metadata["hardware"] == {"gpu": True}
    assert metadata["origin"]["tenant_id"] == "tenant"
    assert metadata["is_plan_node"] is True
    assert metadata["plan_input"] == {"goal": "build"}
    assert metadata["owner"] == "planner"
    assert metadata["workflow_run_id"] == "run"


def test_spawn_node_task_uses_live_role_and_rejects_missing_role() -> None:
    role = SimpleNamespace(
        slug="dev", required_capabilities=["python"], default_capabilities=[], hardware_requirements={}
    )
    runtime = _runtime(roles=SimpleNamespace(get_role=lambda *_a, **_k: role))
    node = {"node_key": "gate", "node_type": "approval", "role_required": "dev"}
    runtime._spawn_node_task(
        "run", node, workflow=None, started_by="actor", tenant_id=None, attempt=1
    )

    runtime.roles = SimpleNamespace(
        get_role=lambda *_a, **_k: (_ for _ in ()).throw(NotFoundError("missing"))
    )
    with pytest.raises(ValidationError, match="references missing role"):
        runtime._spawn_node_task(
            "run", node, workflow=None, started_by="actor", tenant_id=None, attempt=1
        )


def test_runtime_lookup_and_condition_helpers_cover_fallbacks() -> None:
    runtime = _runtime()
    assert runtime._is_plan_node({}, None) is False
    assert runtime._is_plan_node({"nodes": [{"node_key": "x", "node_type": "task"}]}, "missing") is False
    assert runtime._terminal_to_condition("completed", metadata={"requires_approval": True, "approval_decision": "approved"}) == "approved"
    assert runtime._terminal_to_condition("completed", metadata={"requires_approval": True, "approval_decision": "rejected"}) == "rejected"
    assert runtime._terminal_to_condition("unknown", metadata={}) == "failure"
    with pytest.raises(ValidationError, match="missing start edge"):
        runtime._find_start_edge({"edges": []})
    assert runtime._node_by_key({"nodes": []}, "missing") is None


def test_walk_predecided_handles_nonapproval_missing_decision_and_terminal() -> None:
    runtime = _runtime()
    conn = _Store()
    task = {"node_key": "task", "node_type": "task"}
    assert runtime._walk_through_pre_decided(
        "run", {}, task, pre_decisions={}, actor="actor", conn=conn
    ) is task
    approval = {"node_key": "gate", "node_type": "approval"}
    assert runtime._walk_through_pre_decided(
        "run", {"nodes": [approval], "edges": []}, approval, pre_decisions={}, actor="actor", conn=conn
    ) is approval
    runtime._next_history_seq = lambda _run_id: 2
    assert runtime._walk_through_pre_decided(
        "run",
        {"nodes": [approval], "edges": []},
        approval,
        pre_decisions={"gate": "approved"},
        actor="actor",
        conn=conn,
    ) is None
    assert conn.executed
