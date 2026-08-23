"""Isolation and compatibility tests for :mod:`mac.executor_scope`."""

from __future__ import annotations

import json

from mac import executor_scope as scope
from mac import task_executor as te


def test_task_executor_reexports_scope_surface() -> None:
    names = (
        "compute_scope_estimate",
        "needs_scope_estimate",
        "maybe_preflight_scope_estimate",
        "is_planning_phase",
        "should_enter_planning_phase",
        "build_planning_prompt",
        "is_plan_decomposed_evidence",
        "maybe_auto_decompose",
    )
    for name in names:
        assert getattr(te, name) is getattr(scope, name)


def test_repository_breadth_no_longer_classifies_task_scope() -> None:
    """INVERTED by task_0ee0b7ce, because it was asserting the defect.

    This paired a long title with the repository contract's required_commands
    to reach the two-signal threshold. required_commands comes from the
    PROJECT's contract, so it is identical for every task in a project
    (measured 2026-08-07: ['python3','git','gh'] for every mac task,
    ['python3','git','gh','make','cc'] for every nanolang task). A signal that
    cannot be false for one task and true for another says nothing about
    either, and this one voted "large" on all of them -- which is how twelve
    self-contained, dependency-free tasks were decomposed on 2026-08-02, none
    of which completed.

    A long title on its own is weak evidence, and now correctly sizes small.
    """
    task = {
        "title": "A" * 101,
        "description": "small",
        "metadata": {
            "execution_contract": {
                "repository_contract": {
                    "toolchain": {"required_commands": ["git", "python", "codegraph"]}
                }
            }
        },
    }
    estimate = scope.compute_scope_estimate(task)
    assert estimate["size"] == "small"
    assert estimate["estimated_units"] == 1
    assert not any("repo_required_cmds" in s for s in estimate["signals"])


def test_is_planning_phase_excludes_child_tasks() -> None:
    task = {
        "attempt_count": 1,
        "metadata": {
            "plan_first": True,
            "decomposition": {"max_children": 3},
            "relationships": {"parent_task_id": "task_parent"},
        },
    }
    assert scope.is_planning_phase(task) is False


def test_build_planning_prompt_describes_symbolic_dependencies(tmp_path) -> None:
    task = {
        "id": "task_plan",
        "attempt_count": 1,
        "metadata": {"plan_first": True, "decomposition": {"max_children": 3}},
    }
    prompt = scope.build_planning_prompt(task, tmp_path / "task.json")
    assert "PLANNING MODE" in prompt
    assert "depends_on" in prompt
    assert "List order alone NEVER implies a dependency" in prompt


def test_plan_decomposed_evidence_handles_missing_and_valid_manifest(tmp_path) -> None:
    assert scope.is_plan_decomposed_evidence(tmp_path) is False
    (tmp_path / "mac-evidence.json").write_text(
        json.dumps({"evidence_type": "plan_decomposed"}), encoding="utf-8"
    )
    assert scope.is_plan_decomposed_evidence(tmp_path) is False
    (tmp_path / "mac-evidence.json").write_text(
        json.dumps(
            {
                "evidence_type": "plan_decomposed",
                "children": [{"title": "Inspect"}, {"title": "Repair"}],
            }
        ),
        encoding="utf-8",
    )
    assert scope.is_plan_decomposed_evidence(tmp_path) is True


def test_maybe_auto_decompose_returns_false_without_manifest(tmp_path) -> None:
    assert scope.maybe_auto_decompose(tmp_path, {"id": "task_plan"}) is False


def test_maybe_auto_decompose_preserves_dependency_graph(tmp_path, monkeypatch) -> None:
    (tmp_path / "mac-evidence.json").write_text(
        json.dumps(
            {
                "plan_steps": [
                    {"node_id": "worker", "title": "Change worker"},
                    {
                        "node_id": "tests",
                        "title": "Add tests",
                        "depends_on": ["worker"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    captured = {}
    monkeypatch.setattr(
        scope,
        "_hub_post_child_tasks",
        lambda task_id, children: captured.update(
            task_id=task_id, children=children
        )
        or {"ok": True},
    )

    assert scope.maybe_auto_decompose(tmp_path, {"id": "task_plan"}) is True
    assert captured["children"][1]["depends_on"] == ["worker"]
