"""Tests for the auto-decompose guardrails (T1 — bound runaway decomposition).

The live fleet once auto-decomposed a handoff backlog into runaway child tasks.
``ControlPlane.add_child_tasks`` now enforces three server-side caps that NO
decomposition path can bypass:

1. ``no_decompose`` — handoff/plan-note tasks refuse decomposition outright.
2. depth cap (``MAC_MAX_DECOMPOSE_DEPTH``, default 2) — a deep child-of-child
   chain can't recurse forever.
3. cumulative child-count cap (``MAC_MAX_CHILD_TASKS_PER_PARENT``, default 10).
"""

from __future__ import annotations

import pytest

from mac.models import ValidationError
from mac.services import ControlPlane


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _kids(n, prefix="child"):
    return [{"title": "%s %d" % (prefix, i)} for i in range(n)]


# -- no_decompose guard -----------------------------------------------------


def test_no_decompose_refuses_children(cp):
    parent = cp.create_task("handoff note", metadata={"no_decompose": True})
    with pytest.raises(ValidationError, match="no_decompose"):
        cp.add_child_tasks(parent.id, _kids(2))


def test_normal_task_can_decompose(cp):
    parent = cp.create_task("real plan")
    result = cp.add_child_tasks(parent.id, _kids(2))
    assert len(result["children"]) == 2


# -- depth cap --------------------------------------------------------------


def test_depth_helper_counts_ancestors(cp):
    root = cp.create_task("root")
    child_id = cp.add_child_tasks(root.id, _kids(1))["children"][0]["id"]
    grandchild_id = cp.add_child_tasks(child_id, _kids(1))["children"][0]["id"]
    assert cp._task_decompose_depth(cp.get_task(root.id)) == 0
    assert cp._task_decompose_depth(cp.get_task(child_id)) == 1
    assert cp._task_decompose_depth(cp.get_task(grandchild_id)) == 2


def test_depth_cap_blocks_third_level(cp):
    # default MAC_MAX_DECOMPOSE_DEPTH=2: root(0)->child(1)->grandchild(2) ok,
    # but a grandchild (depth 2) may NOT decompose further.
    root = cp.create_task("root")
    child_id = cp.add_child_tasks(root.id, _kids(1))["children"][0]["id"]
    grandchild_id = cp.add_child_tasks(child_id, _kids(1))["children"][0]["id"]
    with pytest.raises(ValidationError, match="depth limit"):
        cp.add_child_tasks(grandchild_id, _kids(1))


def test_depth_cap_env_override(cp, monkeypatch):
    monkeypatch.setenv("MAC_MAX_DECOMPOSE_DEPTH", "1")
    root = cp.create_task("root")
    child_id = cp.add_child_tasks(root.id, _kids(1))["children"][0]["id"]
    # depth-1 child can no longer decompose when the cap is tightened to 1.
    with pytest.raises(ValidationError, match="depth limit"):
        cp.add_child_tasks(child_id, _kids(1))


# -- count cap --------------------------------------------------------------


def test_count_cap_single_call(cp):
    parent = cp.create_task("big plan")
    with pytest.raises(ValidationError, match="child task limit"):
        cp.add_child_tasks(parent.id, _kids(11))


def test_count_cap_at_limit_ok(cp):
    parent = cp.create_task("ten-step plan")
    result = cp.add_child_tasks(parent.id, _kids(10))
    assert len(result["children"]) == 10


def test_count_cap_cumulative(cp):
    parent = cp.create_task("growing plan")
    cp.add_child_tasks(parent.id, _kids(6, "a"))
    # 6 existing + 6 new = 12 > 10 → refuse on the second call.
    with pytest.raises(ValidationError, match="child task limit"):
        cp.add_child_tasks(parent.id, _kids(6, "b"))


def test_count_cap_env_override(cp, monkeypatch):
    monkeypatch.setenv("MAC_MAX_CHILD_TASKS_PER_PARENT", "3")
    parent = cp.create_task("small plan")
    with pytest.raises(ValidationError, match="child task limit"):
        cp.add_child_tasks(parent.id, _kids(4))


def test_bad_env_value_falls_back_to_default(cp, monkeypatch):
    # A garbage/zero env value must NOT disable the cap.
    monkeypatch.setenv("MAC_MAX_CHILD_TASKS_PER_PARENT", "not-a-number")
    parent = cp.create_task("plan")
    with pytest.raises(ValidationError, match="child task limit"):
        cp.add_child_tasks(parent.id, _kids(11))
