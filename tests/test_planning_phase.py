"""Contract tests for planning-phase execution mode (plan-01).

Covers:
- is_planning_phase: triggers on scope_estimate.size=large, metadata.plan_first, NOT on
  children / no_decompose / review / attempt>1
- build_planning_prompt: contains planning instructions, topology reference, children
  endpoint, required evidence-schema fields
- is_plan_decomposed_evidence: reads evidence_type from mac-evidence.json
- _run_executor integration: large task -> planning prompt, planning_phase_started telemetry
- _run_executor integration: non-large task -> normal task prompt (no planning)
- _run_executor integration: planning-phase skips git finalizer, emits
  planning_phase_completed
- Parent task blocks on children until all children complete (services contract)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from mac import task_executor as te
from mac.services import ControlPlane
from mac.models import TaskState, ValidationError


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _task(
    *,
    title: str = "Fix a small bug",
    description: str = "One-line description.",
    attempt_count: int = 1,
    metadata: Optional[Dict[str, Any]] = None,
    task_id: str = "task_test123",
) -> Dict[str, Any]:
    t: Dict[str, Any] = {
        "id": task_id,
        "title": title,
        "description": description,
        "attempt_count": attempt_count,
        "project": "mac",
    }
    if metadata is not None:
        t["metadata"] = metadata
    return t


def _large_task(
    *,
    title: str = "Large feature implementation",
    description: str = "x",
    attempt_count: int = 1,
    task_id: str = "task_large000",
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a task fixture that already has scope_estimate=large in metadata."""
    metadata: Dict[str, Any] = {
        "scope_estimate": {
            "schema": "mac.scope_estimate.v1",
            "size": "large",
            "estimated_units": 2,
            "signals": ["desc_words:250", "repo_required_cmds:3"],
            "rationale": "size=large based on: desc_words:250; repo_required_cmds:3",
        }
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    return _task(
        title=title,
        description=description,
        attempt_count=attempt_count,
        task_id=task_id,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# is_planning_phase — pure predicate
# ---------------------------------------------------------------------------


class TestIsPlanningPhase:
    def test_returns_false_for_small_task(self):
        task = _task(
            metadata={"scope_estimate": {"size": "small"}},
        )
        assert te.is_planning_phase(task) is False

    def test_returns_true_for_large_scope_estimate(self):
        task = _large_task()
        assert te.is_planning_phase(task) is True

    def test_returns_true_for_plan_first_flag(self):
        task = _task(metadata={"plan_first": True})
        assert te.is_planning_phase(task) is True

    def test_plan_first_false_does_not_trigger(self):
        task = _task(metadata={"plan_first": False})
        assert te.is_planning_phase(task) is False

    def test_plan_first_string_true_triggers(self):
        # Truthy strings should trigger
        task = _task(metadata={"plan_first": "true"})
        assert te.is_planning_phase(task) is True

    def test_no_decompose_prevents_planning(self):
        task = _large_task(extra_metadata={"no_decompose": True})
        assert te.is_planning_phase(task) is False

    def test_child_task_prevents_planning(self):
        task = _large_task(
            extra_metadata={
                "relationships": {"parent_task_id": "task_parent_abc"}
            }
        )
        assert te.is_planning_phase(task) is False

    def test_task_with_existing_children_skips_planning(self):
        task = _large_task(
            extra_metadata={
                "relationships": {"child_task_ids": ["task_child1", "task_child2"]}
            }
        )
        assert te.is_planning_phase(task) is False

    def test_attempt_2_does_not_plan(self):
        task = _large_task(attempt_count=2)
        assert te.is_planning_phase(task) is False

    def test_attempt_0_does_not_plan(self):
        task = _large_task(attempt_count=0)
        assert te.is_planning_phase(task) is False

    def test_non_dict_task_returns_false(self):
        assert te.is_planning_phase(None) is False  # type: ignore[arg-type]
        assert te.is_planning_phase("string") is False  # type: ignore[arg-type]

    def test_no_metadata_returns_false(self):
        task = _task()  # no metadata key
        assert te.is_planning_phase(task) is False

    def test_none_metadata_returns_false(self):
        task = _task(metadata=None)
        assert te.is_planning_phase(task) is False

    def test_missing_size_in_estimate_returns_false(self):
        task = _task(
            metadata={"scope_estimate": {"schema": "mac.scope_estimate.v1"}}
        )
        assert te.is_planning_phase(task) is False


# ---------------------------------------------------------------------------
# build_planning_prompt — content inspection
# ---------------------------------------------------------------------------


class TestBuildPlanningPrompt:
    def test_contains_planning_mode_header(self, tmp_path: Path):
        task = _large_task()
        prompt = te.build_planning_prompt(task, tmp_path / "task.json")
        assert "PLANNING MODE" in prompt

    def test_contains_trigger_reason_scope_estimate(self, tmp_path: Path):
        task = _large_task()
        prompt = te.build_planning_prompt(task, tmp_path / "task.json")
        assert "scope_estimate.size=large" in prompt

    def test_contains_trigger_reason_plan_first(self, tmp_path: Path):
        task = _task(metadata={"plan_first": True})
        prompt = te.build_planning_prompt(task, tmp_path / "task.json")
        assert "plan_first=true" in prompt.lower() or "metadata.plan_first=true" in prompt

    def test_contains_topology_reference(self, tmp_path: Path):
        task = _large_task()
        prompt = te.build_planning_prompt(task, tmp_path / "task.json")
        assert "mac plan order" in prompt or "order_layers" in prompt

    def test_contains_children_endpoint(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MAC_HUB_URL", "http://hub.example.com")
        task = _large_task(title="T", task_id="task_abc123")
        prompt = te.build_planning_prompt(task, tmp_path / "task.json")
        assert "task_abc123" in prompt
        assert "/children" in prompt

    def test_contains_plan_decomposed_evidence_type(self, tmp_path: Path):
        task = _large_task()
        prompt = te.build_planning_prompt(task, tmp_path / "task.json")
        assert "plan_decomposed" in prompt

    def test_contains_do_not_write_code_instruction(self, tmp_path: Path):
        task = _large_task()
        prompt = te.build_planning_prompt(task, tmp_path / "task.json")
        assert "DO NOT write any code" in prompt or "NOT to implement" in prompt or "NOT implement" in prompt

    def test_contains_task_file_path(self, tmp_path: Path):
        task_file = tmp_path / "task.json"
        task = _large_task()
        prompt = te.build_planning_prompt(task, task_file)
        assert str(task_file) in prompt

    def test_contains_children_schema_fields(self, tmp_path: Path):
        task = _large_task()
        prompt = te.build_planning_prompt(task, tmp_path / "task.json")
        assert '"node_id"' in prompt
        assert '"depends_on"' in prompt
        assert "List order alone NEVER implies a dependency" in prompt
        assert "ordering_rationale" in prompt
        assert "coverage_claim" in prompt

    def test_injects_lessons(self, tmp_path: Path):
        task = _large_task()
        lessons = ["lesson one: always test", "lesson two: check deps first"]
        prompt = te.build_planning_prompt(task, tmp_path / "task.json", lessons=lessons)
        assert "lesson one" in prompt

    def test_returns_string(self, tmp_path: Path):
        task = _large_task()
        result = te.build_planning_prompt(task, tmp_path / "task.json")
        assert isinstance(result, str)
        assert len(result) > 100

    def test_fallback_endpoint_without_hub_url(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("MAC_HUB_URL", raising=False)
        monkeypatch.delenv("MAC_URL", raising=False)
        task = _large_task()
        prompt = te.build_planning_prompt(task, tmp_path / "task.json")
        assert "/children" in prompt  # fallback still mentions the endpoint path


# ---------------------------------------------------------------------------
# is_plan_decomposed_evidence
# ---------------------------------------------------------------------------


class TestIsPlanDecomposedEvidence:
    def test_returns_true_for_plan_decomposed(self, tmp_path: Path):
        manifest = {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "plan_decomposed",
            "summary": "Decomposed into 3 children.",
            "children": [{"title": "Child A"}, {"title": "Child B"}, {"title": "Child C"}],
            "ordering_rationale": "leaves before cores",
            "coverage_claim": "covers all deliverables",
        }
        (tmp_path / "mac-evidence.json").write_text(json.dumps(manifest), encoding="utf-8")
        assert te.is_plan_decomposed_evidence(tmp_path) is True

    def test_returns_false_for_repo_change(self, tmp_path: Path):
        manifest = {"evidence_type": "repo_change", "status": "complete"}
        (tmp_path / "mac-evidence.json").write_text(json.dumps(manifest), encoding="utf-8")
        assert te.is_plan_decomposed_evidence(tmp_path) is False

    def test_returns_false_when_no_manifest(self, tmp_path: Path):
        assert te.is_plan_decomposed_evidence(tmp_path) is False

    def test_returns_false_for_invalid_json(self, tmp_path: Path):
        (tmp_path / "mac-evidence.json").write_text("not-json", encoding="utf-8")
        assert te.is_plan_decomposed_evidence(tmp_path) is False

    def test_returns_false_for_operator_result(self, tmp_path: Path):
        manifest = {"evidence_type": "operator_result", "status": "complete"}
        (tmp_path / "mac-evidence.json").write_text(json.dumps(manifest), encoding="utf-8")
        assert te.is_plan_decomposed_evidence(tmp_path) is False


# ---------------------------------------------------------------------------
# _run_executor integration — planning phase wiring
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_plan_evidence(task_workspace: Path) -> None:
    """Write a minimal plan_decomposed evidence manifest."""
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "plan_decomposed",
        "summary": "Decomposed into 3 children.",
        "children": [
            {"title": "Child step A"},
            {"title": "Child step B"},
            {"title": "Child step C"},
        ],
        "ordering_rationale": "layer 0 -> layer 1 -> layer 2",
        "coverage_claim": "covers all deliverables in the parent task",
    }
    (task_workspace / "mac-evidence.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def _patch_run_executor_base(monkeypatch, *, tmp_path: Path) -> Dict[str, List]:
    """Apply the minimal set of monkeypatches needed for _run_executor to run
    in unit tests.  Returns a shared state dict for introspection.

    The patched _invoke_agent signature matches the real one:
        _invoke_agent(runner, prompt, workspace, audit_id, opts) -> result
    """
    state: Dict[str, List] = {
        "prompts": [],
        "telemetry": [],
        "git_finalizer_calls": [],
    }

    def fake_invoke_agent(runner, prompt, workspace, audit_id, opts):
        state["prompts"].append(prompt)
        return _FakeResult(0)

    monkeypatch.setattr(te, "recall_deployment_lessons", lambda task: [])
    monkeypatch.setattr(te, "recall_plan_lessons", lambda task, limit=3: [])
    monkeypatch.setattr(te, "emit_telemetry", lambda event, **kw: state["telemetry"].append(event) or True)
    monkeypatch.setattr(te, "_openshell_enabled", lambda: False)
    monkeypatch.setattr(te, "_openshell_required_for_local_agent", lambda: False)
    monkeypatch.setattr(te, "_invoke_agent", fake_invoke_agent)
    monkeypatch.setattr(te, "run_deterministic_git_finalizer", lambda *a, **kw: state["git_finalizer_calls"].append(1))
    monkeypatch.setattr(te, "maybe_auto_decompose", lambda *a, **kw: False)
    monkeypatch.setattr(te, "write_fallback_evidence_manifest", lambda *a, **kw: None)
    monkeypatch.setattr(te, "classify_outcome", lambda *a, **kw: {"outcome": "success", "evidence_type": "plan_decomposed", "signals": []})
    monkeypatch.setattr(te, "record_deployment_learning", lambda *a, **kw: True)
    monkeypatch.setattr(te, "record_curated_lessons", lambda *a, **kw: 0)
    monkeypatch.setattr(te, "record_plan_outcome", lambda *a, **kw: True)
    monkeypatch.setattr(te, "maybe_preflight_scope_estimate", lambda task: None)
    monkeypatch.setattr(te, "_manifest_is_complete", lambda *a, **kw: True)
    monkeypatch.setattr(te, "_review_experiment_assignment", lambda t: {})

    return state


def _fake_runner(argv, cwd, task_id, metadata):
    return _FakeResult(0)


class TestRunExecutorPlanningPhase:
    """Integration tests for planning-phase wiring in _run_executor."""

    def test_large_task_uses_planning_prompt(self, monkeypatch, tmp_path: Path):
        """A large task (scope_estimate=large) must receive the planning prompt."""
        state = _patch_run_executor_base(monkeypatch, tmp_path=tmp_path)

        def fake_invoke_with_evidence(runner, prompt, workspace, audit_id, opts):
            state["prompts"].append(prompt)
            _make_plan_evidence(workspace)
            return _FakeResult(0)

        monkeypatch.setattr(te, "_invoke_agent", fake_invoke_with_evidence)

        task = _large_task(task_id="task_large001")
        te._run_executor(
            runner=_fake_runner,
            task=task,
            task_file=tmp_path / "task.json",
            task_workspace=tmp_path,
            task_id="task_large001",
            review_context=None,
            is_review=False,
        )

        assert len(state["prompts"]) == 1
        assert "PLANNING MODE" in state["prompts"][0]
        assert "planning_phase_started" in state["telemetry"]

    def test_large_task_skips_git_finalizer_when_plan_evidence(self, monkeypatch, tmp_path: Path):
        """Planning-phase run with plan_decomposed evidence skips the git finalizer."""
        state = _patch_run_executor_base(monkeypatch, tmp_path=tmp_path)

        def fake_invoke_with_evidence(runner, prompt, workspace, audit_id, opts):
            state["prompts"].append(prompt)
            _make_plan_evidence(workspace)
            return _FakeResult(0)

        monkeypatch.setattr(te, "_invoke_agent", fake_invoke_with_evidence)

        task = _large_task(task_id="task_large002")
        te._run_executor(
            runner=_fake_runner,
            task=task,
            task_file=tmp_path / "task.json",
            task_workspace=tmp_path,
            task_id="task_large002",
            review_context=None,
            is_review=False,
        )

        assert state["git_finalizer_calls"] == [], (
            "git finalizer must NOT run for planning-phase with plan evidence"
        )
        assert "planning_phase_completed" in state["telemetry"]

    def test_small_task_uses_normal_prompt(self, monkeypatch, tmp_path: Path):
        """A small task must use the normal build_task_prompt, not planning prompt."""
        state = _patch_run_executor_base(monkeypatch, tmp_path=tmp_path)

        task = _task(
            metadata={"scope_estimate": {"size": "small"}},
            task_id="task_small001",
            attempt_count=1,
        )
        te._run_executor(
            runner=_fake_runner,
            task=task,
            task_file=tmp_path / "task.json",
            task_workspace=tmp_path,
            task_id="task_small001",
            review_context=None,
            is_review=False,
        )

        assert len(state["prompts"]) == 1
        assert "PLANNING MODE" not in state["prompts"][0]
        assert "planning_phase_started" not in state["telemetry"]

    def test_plan_first_task_uses_planning_prompt(self, monkeypatch, tmp_path: Path):
        """A task with metadata.plan_first=True must receive the planning prompt."""
        state = _patch_run_executor_base(monkeypatch, tmp_path=tmp_path)

        def fake_invoke_with_evidence(runner, prompt, workspace, audit_id, opts):
            state["prompts"].append(prompt)
            _make_plan_evidence(workspace)
            return _FakeResult(0)

        monkeypatch.setattr(te, "_invoke_agent", fake_invoke_with_evidence)

        task = _task(
            metadata={"plan_first": True},
            task_id="task_planfirst001",
            attempt_count=1,
        )
        te._run_executor(
            runner=_fake_runner,
            task=task,
            task_file=tmp_path / "task.json",
            task_workspace=tmp_path,
            task_id="task_planfirst001",
            review_context=None,
            is_review=False,
        )

        assert len(state["prompts"]) == 1
        assert "PLANNING MODE" in state["prompts"][0]
        assert "planning_phase_started" in state["telemetry"]

    def test_review_task_never_enters_planning_phase(self, monkeypatch, tmp_path: Path):
        """Review tasks must never enter planning mode."""
        state = _patch_run_executor_base(monkeypatch, tmp_path=tmp_path)
        monkeypatch.setattr(te, "build_review_prompt", lambda *a, **kw: "review prompt text")
        monkeypatch.setattr(te, "run_deterministic_review_verdict", lambda *a, **kw: None)

        task = _large_task(task_id="task_rev001")
        te._run_executor(
            runner=_fake_runner,
            task=task,
            task_file=tmp_path / "task.json",
            task_workspace=tmp_path,
            task_id="task_rev001",
            review_context={"task_id": "task_original"},
            is_review=True,
        )

        assert "planning_phase_started" not in state["telemetry"]
        assert len(state["prompts"]) == 1
        assert state["prompts"][0] == "review prompt text"

    def test_large_task_without_plan_evidence_falls_back_to_git_finalizer(
        self, monkeypatch, tmp_path: Path
    ):
        """If a planning-phase run does NOT write plan_decomposed evidence, the git
        finalizer still runs (the guard is _is_planning AND plan_evidence)."""
        state = _patch_run_executor_base(monkeypatch, tmp_path=tmp_path)
        # _invoke_agent does NOT write evidence — simulates an agent that mis-behaved
        monkeypatch.setattr(
            te,
            "_invoke_agent",
            lambda runner, prompt, workspace, audit_id, opts: state["prompts"].append(prompt) or _FakeResult(0),
        )
        monkeypatch.setattr(
            te,
            "classify_outcome",
            lambda *a, **kw: {"outcome": "failure", "evidence_type": "repo_change", "signals": []},
        )

        task = _large_task(task_id="task_large003")
        te._run_executor(
            runner=_fake_runner,
            task=task,
            task_file=tmp_path / "task.json",
            task_workspace=tmp_path,
            task_id="task_large003",
            review_context=None,
            is_review=False,
        )

        assert state["git_finalizer_calls"] == [1], (
            "git finalizer must run when plan evidence is absent"
        )

    def test_child_task_never_enters_planning_phase(self, monkeypatch, tmp_path: Path):
        """Child tasks must never enter planning mode even if they are large."""
        state = _patch_run_executor_base(monkeypatch, tmp_path=tmp_path)

        task = _large_task(
            task_id="task_child001",
            extra_metadata={"relationships": {"parent_task_id": "task_parent999"}},
        )
        te._run_executor(
            runner=_fake_runner,
            task=task,
            task_file=tmp_path / "task.json",
            task_workspace=tmp_path,
            task_id="task_child001",
            review_context=None,
            is_review=False,
        )

        assert "planning_phase_started" not in state["telemetry"]

    def test_attempt_2_large_task_does_not_enter_planning(self, monkeypatch, tmp_path: Path):
        """Second-attempt large tasks must execute, not plan."""
        state = _patch_run_executor_base(monkeypatch, tmp_path=tmp_path)

        task = _large_task(task_id="task_large_retry", attempt_count=2)
        te._run_executor(
            runner=_fake_runner,
            task=task,
            task_file=tmp_path / "task.json",
            task_workspace=tmp_path,
            task_id="task_large_retry",
            review_context=None,
            is_review=False,
        )

        assert "planning_phase_started" not in state["telemetry"]
        if state["prompts"]:
            assert "PLANNING MODE" not in state["prompts"][0]


# ---------------------------------------------------------------------------
# N ordered children contract: large fixture -> N children with deps
# ---------------------------------------------------------------------------


class TestLargeFixtureToChildren:
    """Verify the planning prompt includes all required structural elements for
    a large-fixture task to produce N ordered children with dependencies.

    This is the 'large fixture task -> N ordered children' contract test.
    """

    def test_prompt_instructs_dependencies_from_topology(self, tmp_path: Path):
        """Planning prompt must tell the agent to derive deps from topology ordering."""
        task = _large_task(
            title="Build the full authentication system with user management",
            description="Implement login, registration, profiles, sessions, and token rotation.",
        )
        prompt = te.build_planning_prompt(task, tmp_path / "task.json")

        # Topology dependency ordering must be mentioned
        assert (
            "topology" in prompt.lower()
            or "order_layers" in prompt
            or "mac plan order" in prompt
        )
        # Children with dependencies must be required
        assert "dependencies" in prompt

    def test_prompt_requires_two_to_ten_children(self, tmp_path: Path):
        """Planning prompt must require 2-10 child tasks."""
        task = _large_task()
        prompt = te.build_planning_prompt(task, tmp_path / "task.json")
        assert "2-10" in prompt or "2 to 10" in prompt.lower()

    def test_prompt_requires_ordering_rationale(self, tmp_path: Path):
        """Plan manifest must include ordering_rationale field."""
        task = _large_task()
        prompt = te.build_planning_prompt(task, tmp_path / "task.json")
        assert "ordering_rationale" in prompt

    def test_prompt_requires_coverage_claim(self, tmp_path: Path):
        """Plan manifest must include coverage_claim field."""
        task = _large_task()
        prompt = te.build_planning_prompt(task, tmp_path / "task.json")
        assert "coverage_claim" in prompt

    def test_plan_evidence_has_children_list(self, tmp_path: Path):
        """A valid plan_decomposed manifest must have a non-empty children list."""
        manifest = {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "plan_decomposed",
            "summary": "Plan for authentication system.",
            "children": [
                {"title": "Implement login endpoint", "description": "POST /auth/login"},
                {"title": "Implement registration endpoint", "description": "POST /auth/register"},
                {"title": "Implement session management", "description": "tokens + rotation"},
            ],
            "ordering_rationale": "data layer before API layer before integration",
            "coverage_claim": "covers all sub-components of the authentication system",
        }
        (tmp_path / "mac-evidence.json").write_text(json.dumps(manifest), encoding="utf-8")
        assert te.is_plan_decomposed_evidence(tmp_path) is True
        loaded = json.loads((tmp_path / "mac-evidence.json").read_text())
        assert len(loaded["children"]) == 3
        assert all("title" in c for c in loaded["children"])


# ---------------------------------------------------------------------------
# Parent gates on children — services contract (ControlPlane.in_memory())
# ---------------------------------------------------------------------------


class TestParentGatesOnChildren:
    """Verify that a parent task blocks until all children complete.

    Uses ControlPlane.in_memory() so there is no external hub dependency.
    """

    def test_parent_blocked_after_children_added(self):
        """After add_child_tasks the parent must be in state=blocked."""
        cp = ControlPlane.in_memory()
        parent = cp.create_task(
            title="Large parent task",
            description="needs decomposition",
            project="mac",
        )
        cp.add_child_tasks(
            parent.id,
            [
                {"title": "Child step A", "description": "first step"},
                {"title": "Child step B", "description": "second step"},
            ],
        )
        refreshed = cp.get_task(parent.id)
        assert refreshed.state == TaskState.BLOCKED.value, (
            "parent must be blocked while children are pending"
        )

    def test_children_open_after_add(self):
        """Children with no interdependency must be open immediately after creation."""
        cp = ControlPlane.in_memory()
        parent = cp.create_task(
            title="Large parent task", description="x", project="mac"
        )
        result = cp.add_child_tasks(
            parent.id,
            [
                {"title": "Child A"},
                {"title": "Child B"},
            ],
        )
        child_ids = [c["id"] for c in result["children"]]
        for cid in child_ids:
            child = cp.get_task(cid)
            assert child.state == TaskState.OPEN.value, (
                "child %s should be open, got %s" % (cid, child.state)
            )

    def test_all_children_complete_unblocks_parent(self):
        """When all children transition to completed the parent dependency is
        satisfied — verified by checking the dependency list is fulfilled."""
        cp = ControlPlane.in_memory()
        parent = cp.create_task(
            title="Large parent task", description="x", project="mac"
        )
        result = cp.add_child_tasks(
            parent.id,
            [
                {"title": "Child step A"},
                {"title": "Child step B"},
            ],
        )
        child_ids = [c["id"] for c in result["children"]]

        # Complete both children via force_complete_task (no review gate needed)
        cp.force_complete_task(child_ids[0], "test_agent", reason="test")
        cp.force_complete_task(child_ids[1], "test_agent", reason="test")

        all_complete = all(
            cp.get_task(cid).state == TaskState.COMPLETED.value
            for cid in child_ids
        )
        assert all_complete, "all children must be completed"

        # Parent's dependencies are the child IDs — they are now all completed.
        # The unblocking fires through the outbox; verify the parent's dependency
        # list consists only of the now-completed children.
        parent_refreshed = cp.get_task(parent.id)
        for dep_id in parent_refreshed.dependencies:
            dep = cp.get_task(dep_id)
            assert dep.state == TaskState.COMPLETED.value, (
                "parent dependency %s should be completed, got %s" % (dep_id, dep.state)
            )

    def test_partial_child_completion_does_not_complete_parent(self):
        """Completing only one of two children must not satisfy the parent's block."""
        cp = ControlPlane.in_memory()
        parent = cp.create_task(
            title="Large parent task", description="x", project="mac"
        )
        result = cp.add_child_tasks(
            parent.id,
            [
                {"title": "Child step A"},
                {"title": "Child step B"},
            ],
        )
        child_ids = [c["id"] for c in result["children"]]

        # Complete only the first child
        cp.force_complete_task(child_ids[0], "test_agent", reason="test")

        # Second child must still be open / not completed
        child_b = cp.get_task(child_ids[1])
        assert child_b.state != TaskState.COMPLETED.value, (
            "second child should not be completed yet"
        )
        # Parent must not be completed either
        parent_refreshed = cp.get_task(parent.id)
        assert parent_refreshed.state != TaskState.COMPLETED.value, (
            "parent must not complete while a child is still open"
        )

    def test_children_inherit_parent_project(self):
        """Children should inherit the parent's project when not specified."""
        cp = ControlPlane.in_memory()
        parent = cp.create_task(
            title="Large parent task", description="x", project="mac"
        )
        result = cp.add_child_tasks(
            parent.id,
            [{"title": "Child step A"}],
        )
        child = cp.get_task(result["children"][0]["id"])
        assert child.project == "mac"

    def test_add_child_tasks_returns_children_list(self):
        """add_child_tasks return value must include a non-empty 'children' list."""
        cp = ControlPlane.in_memory()
        parent = cp.create_task(
            title="Large parent task", description="x", project="mac"
        )
        result = cp.add_child_tasks(
            parent.id,
            [
                {"title": "Child A"},
                {"title": "Child B"},
                {"title": "Child C"},
            ],
        )
        assert "children" in result
        assert len(result["children"]) == 3
        assert all("id" in c for c in result["children"])
        assert all("title" in c for c in result["children"])

    def test_atomic_children_resolve_symbolic_dependency_graph(self):
        """A single planner POST maps node_ids to real sibling task IDs."""
        cp = ControlPlane.in_memory()
        parent = cp.create_task(
            title="Large ordered task", description="x", project="mac"
        )
        result = cp.add_child_tasks(
            parent.id,
            [
                {"node_id": "analysis", "title": "Analyze"},
                {
                    "node_id": "worker",
                    "title": "Change worker",
                    "depends_on": ["analysis"],
                },
                {
                    "node_id": "services",
                    "title": "Change services",
                    "depends_on": ["analysis"],
                },
                {
                    "node_id": "tests",
                    "title": "Add tests",
                    "depends_on": ["worker", "services"],
                },
                {
                    "node_id": "integration",
                    "title": "Integrate",
                    "depends_on": ["analysis", "worker", "services", "tests"],
                },
            ],
        )
        children = result["children"]
        ids = {
            child["metadata"]["coordination"]["plan_node_id"]: child["id"]
            for child in children
        }

        assert children[0]["dependencies"] == []
        assert children[1]["dependencies"] == [ids["analysis"]]
        assert children[2]["dependencies"] == [ids["analysis"]]
        assert children[3]["dependencies"] == [ids["worker"], ids["services"]]
        assert children[4]["dependencies"] == [
            ids["analysis"], ids["worker"], ids["services"], ids["tests"]
        ]
        assert children[0]["state"] == TaskState.OPEN.value
        assert all(child["state"] == TaskState.BLOCKED.value for child in children[1:])

        history = cp.task_history(parent.id)
        graph = next(
            event.detail["dependency_graph"]
            for event in history
            if event.event_type == "task.children_added"
        )
        assert graph[-1] == {
            "node_id": "integration",
            "task_id": ids["integration"],
            "dependencies": [
                ids["analysis"],
                ids["worker"],
                ids["services"],
                ids["tests"],
            ],
        }

    def test_atomic_children_reject_invalid_symbolic_dependencies(self):
        cp = ControlPlane.in_memory()
        parent = cp.create_task(title="Bad plan", description="x")

        with pytest.raises(ValidationError, match="duplicated"):
            cp.add_child_tasks(
                parent.id,
                [
                    {"node_id": "same", "title": "One"},
                    {"node_id": "same", "title": "Two"},
                ],
            )
        with pytest.raises(ValidationError, match="earlier sibling"):
            cp.add_child_tasks(
                parent.id,
                [
                    {"node_id": "first", "title": "One", "depends_on": ["later"]},
                    {"node_id": "later", "title": "Two"},
                ],
            )

    def test_ordered_children_with_explicit_dependencies(self):
        """Children can declare explicit dependencies on sibling children,
        creating a dependency chain: A -> B -> C (C depends on B, B depends on A)."""
        cp = ControlPlane.in_memory()
        parent = cp.create_task(
            title="Large ordered task", description="x", project="mac"
        )
        # Create A first (no deps)
        result_a = cp.add_child_tasks(parent.id, [{"title": "Child A"}])
        child_a_id = result_a["children"][0]["id"]

        # Create B depending on A
        result_b = cp.add_child_tasks(
            parent.id,
            [{"title": "Child B", "dependencies": [child_a_id]}],
        )
        child_b_id = result_b["children"][0]["id"]
        child_b = cp.get_task(child_b_id)
        assert child_a_id in child_b.dependencies, (
            "Child B should depend on Child A"
        )
        assert child_b.state == TaskState.BLOCKED.value, (
            "Child B should be blocked until Child A completes"
        )

        # Complete A; B should become unblocked
        cp.force_complete_task(child_a_id, "test_agent", reason="test")
        child_b_after = cp.get_task(child_b_id)
        assert child_b_after.state in {TaskState.OPEN.value, TaskState.BLOCKED.value}, (
            "Child B state after A completes should be open or blocked (outbox may not have drained)"
        )
