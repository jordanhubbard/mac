"""Tests for src/mac/openclaw_fleet_rollout.py.

Covers:
- RolloutPlanStep, RolloutPlan, RolloutResult data-class helpers
- build_staged_rollout_plan(): empty version, empty targets,
  canary_count < 1, canary_count > len(targets), missing node_id,
  missing host, valid single-node, valid multi-node canary/promote
  stage assignment, canary_count > 1
- execute_staged_rollout(): simulate mode (all succeed, no deploy_fn),
  deploy failure halts and marks remaining skipped, canary health-check
  failure halts and skips remaining, health_fn only called for canary
  steps (not promote), full success path with and without health_fn
"""

from __future__ import annotations

import pytest

from mac.openclaw_fleet_rollout import (
    RolloutPlan,
    RolloutPlanStep,
    RolloutResult,
    build_staged_rollout_plan,
    execute_staged_rollout,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _targets(*node_ids: str) -> list[dict[str, str]]:
    """Build a list of target dicts from a sequence of node IDs."""
    return [{"node_id": nid, "host": f"{nid}.example.com"} for nid in node_ids]


def _always_ok(_step: RolloutPlanStep) -> bool:
    return True


def _always_fail(_step: RolloutPlanStep) -> bool:
    return False


# ---------------------------------------------------------------------------
# RolloutPlanStep data-class
# ---------------------------------------------------------------------------


def test_rollout_plan_step_fields() -> None:
    step = RolloutPlanStep(node_id="n1", host="h1", stage="canary", status="planned")
    assert step.node_id == "n1"
    assert step.host == "h1"
    assert step.stage == "canary"
    assert step.status == "planned"


# ---------------------------------------------------------------------------
# RolloutPlan helpers
# ---------------------------------------------------------------------------


def test_rollout_plan_canary_steps_filters_correctly() -> None:
    plan = build_staged_rollout_plan("v1", _targets("a", "b", "c"), canary_count=1)
    assert len(plan.canary_steps) == 1
    assert plan.canary_steps[0].node_id == "a"


def test_rollout_plan_promote_steps_filters_correctly() -> None:
    plan = build_staged_rollout_plan("v1", _targets("a", "b", "c"), canary_count=1)
    assert len(plan.promote_steps) == 2
    assert {s.node_id for s in plan.promote_steps} == {"b", "c"}


def test_rollout_plan_step_for_node_returns_correct_step() -> None:
    plan = build_staged_rollout_plan("v1", _targets("x", "y"), canary_count=1)
    step = plan.step_for_node("y")
    assert step is not None
    assert step.node_id == "y"
    assert step.stage == "promote"


def test_rollout_plan_step_for_node_returns_none_for_missing() -> None:
    plan = build_staged_rollout_plan("v1", _targets("x"), canary_count=1)
    assert plan.step_for_node("nonexistent") is None


def test_rollout_plan_version_is_stored() -> None:
    plan = build_staged_rollout_plan("  2.0.1  ", _targets("n1"), canary_count=1)
    assert plan.version == "2.0.1"


# ---------------------------------------------------------------------------
# RolloutResult helpers
# ---------------------------------------------------------------------------


def test_rollout_result_ok_true_when_no_failures() -> None:
    result = RolloutResult(version="v1", succeeded=["n1", "n2"])
    assert result.ok is True


def test_rollout_result_ok_false_when_failures_present() -> None:
    result = RolloutResult(version="v1", failed=["n1"])
    assert result.ok is False


def test_rollout_result_default_lists_are_empty() -> None:
    result = RolloutResult(version="v1")
    assert result.succeeded == []
    assert result.failed == []
    assert result.skipped == []


# ---------------------------------------------------------------------------
# build_staged_rollout_plan – validation errors
# ---------------------------------------------------------------------------


def test_build_plan_rejects_empty_version() -> None:
    with pytest.raises(ValueError, match="version"):
        build_staged_rollout_plan("", _targets("n1"))


def test_build_plan_rejects_whitespace_only_version() -> None:
    with pytest.raises(ValueError, match="version"):
        build_staged_rollout_plan("   ", _targets("n1"))


def test_build_plan_rejects_empty_targets() -> None:
    with pytest.raises(ValueError, match="target"):
        build_staged_rollout_plan("v1", [])


def test_build_plan_rejects_canary_count_below_one() -> None:
    with pytest.raises(ValueError, match="canary_count"):
        build_staged_rollout_plan("v1", _targets("n1"), canary_count=0)


def test_build_plan_rejects_canary_count_exceeds_targets() -> None:
    with pytest.raises(ValueError, match="canary_count"):
        build_staged_rollout_plan("v1", _targets("n1"), canary_count=2)


def test_build_plan_rejects_missing_node_id() -> None:
    targets = [{"node_id": "", "host": "h.example.com"}]
    with pytest.raises(ValueError, match="node_id"):
        build_staged_rollout_plan("v1", targets)


def test_build_plan_rejects_none_node_id() -> None:
    targets = [{"host": "h.example.com"}]
    with pytest.raises(ValueError, match="node_id"):
        build_staged_rollout_plan("v1", targets)


def test_build_plan_rejects_missing_host() -> None:
    targets = [{"node_id": "n1", "host": ""}]
    with pytest.raises(ValueError, match="host"):
        build_staged_rollout_plan("v1", targets)


def test_build_plan_rejects_none_host() -> None:
    targets = [{"node_id": "n1"}]
    with pytest.raises(ValueError, match="host"):
        build_staged_rollout_plan("v1", targets)


# ---------------------------------------------------------------------------
# build_staged_rollout_plan – stage assignment
# ---------------------------------------------------------------------------


def test_build_plan_single_node_is_canary() -> None:
    plan = build_staged_rollout_plan("v1", _targets("only"))
    assert len(plan.steps) == 1
    assert plan.steps[0].stage == "canary"
    assert plan.steps[0].status == "planned"


def test_build_plan_two_nodes_default_canary_count() -> None:
    plan = build_staged_rollout_plan("v1", _targets("c1", "p1"))
    assert plan.steps[0].stage == "canary"
    assert plan.steps[1].stage == "promote"


def test_build_plan_canary_count_two() -> None:
    plan = build_staged_rollout_plan("v1", _targets("a", "b", "c", "d"), canary_count=2)
    assert plan.steps[0].stage == "canary"
    assert plan.steps[1].stage == "canary"
    assert plan.steps[2].stage == "promote"
    assert plan.steps[3].stage == "promote"


def test_build_plan_all_nodes_canary_when_canary_count_equals_len() -> None:
    plan = build_staged_rollout_plan("v1", _targets("a", "b"), canary_count=2)
    assert all(s.stage == "canary" for s in plan.steps)
    assert plan.promote_steps == []


def test_build_plan_preserves_node_id_and_host() -> None:
    targets = [{"node_id": "edge-01", "host": "10.0.0.1"}]
    plan = build_staged_rollout_plan("v2", targets)
    assert plan.steps[0].node_id == "edge-01"
    assert plan.steps[0].host == "10.0.0.1"


def test_build_plan_all_steps_start_as_planned() -> None:
    plan = build_staged_rollout_plan("v1", _targets("a", "b", "c"), canary_count=2)
    assert all(s.status == "planned" for s in plan.steps)


def test_build_plan_strips_version_whitespace() -> None:
    plan = build_staged_rollout_plan("  v3.1  ", _targets("n1"))
    assert plan.version == "v3.1"


# ---------------------------------------------------------------------------
# execute_staged_rollout – simulate mode
# ---------------------------------------------------------------------------


def test_execute_simulate_all_succeed_without_deploy_fn() -> None:
    plan = build_staged_rollout_plan("v1", _targets("a", "b", "c"), canary_count=1)
    called: list[str] = []

    def deploy_fn(step: RolloutPlanStep) -> bool:
        called.append(step.node_id)
        return True

    result = execute_staged_rollout(plan, deploy_fn=deploy_fn, simulate=True)
    assert result.ok
    assert set(result.succeeded) == {"a", "b", "c"}
    assert result.failed == []
    assert result.skipped == []
    assert called == [], "deploy_fn must not be called in simulate mode"


def test_execute_simulate_health_fn_not_called() -> None:
    plan = build_staged_rollout_plan("v1", _targets("a", "b"), canary_count=1)
    health_called: list[str] = []

    def health_fn(step: RolloutPlanStep) -> bool:
        health_called.append(step.node_id)
        return True

    result = execute_staged_rollout(
        plan, deploy_fn=_always_ok, health_fn=health_fn, simulate=True
    )
    assert result.ok
    assert health_called == [], "health_fn must not be called in simulate mode"


def test_execute_simulate_step_status_set_to_succeeded() -> None:
    plan = build_staged_rollout_plan("v1", _targets("n1", "n2"), canary_count=1)
    execute_staged_rollout(plan, deploy_fn=_always_ok, simulate=True)
    assert all(s.status == "succeeded" for s in plan.steps)


def test_execute_simulate_version_propagated() -> None:
    plan = build_staged_rollout_plan("3.7.1", _targets("n1"))
    result = execute_staged_rollout(plan, deploy_fn=_always_ok, simulate=True)
    assert result.version == "3.7.1"


# ---------------------------------------------------------------------------
# execute_staged_rollout – deploy failure halts and skips
# ---------------------------------------------------------------------------


def test_execute_canary_deploy_failure_halts_and_skips_rest() -> None:
    plan = build_staged_rollout_plan("v1", _targets("c1", "p1", "p2"), canary_count=1)
    result = execute_staged_rollout(plan, deploy_fn=_always_fail)
    assert "c1" in result.failed
    assert set(result.skipped) == {"p1", "p2"}
    assert result.succeeded == []
    assert not result.ok


def test_execute_promote_deploy_failure_halts_remaining_promote_steps() -> None:
    plan = build_staged_rollout_plan("v1", _targets("c1", "p1", "p2"), canary_count=1)
    fail_nodes = {"p1"}

    def deploy_fn(step: RolloutPlanStep) -> bool:
        return step.node_id not in fail_nodes

    result = execute_staged_rollout(plan, deploy_fn=deploy_fn)
    assert "c1" in result.succeeded
    assert "p1" in result.failed
    assert "p2" in result.skipped
    assert not result.ok


def test_execute_deploy_failure_sets_step_status_failed() -> None:
    plan = build_staged_rollout_plan("v1", _targets("c1", "p1"), canary_count=1)
    execute_staged_rollout(plan, deploy_fn=_always_fail)
    assert plan.steps[0].status == "failed"


def test_execute_skipped_steps_have_status_skipped() -> None:
    plan = build_staged_rollout_plan("v1", _targets("c1", "p1", "p2"), canary_count=1)
    execute_staged_rollout(plan, deploy_fn=_always_fail)
    assert plan.steps[1].status == "skipped"
    assert plan.steps[2].status == "skipped"


# ---------------------------------------------------------------------------
# execute_staged_rollout – canary health-check failure
# ---------------------------------------------------------------------------


def test_execute_canary_health_failure_halts_and_skips_promote() -> None:
    plan = build_staged_rollout_plan("v1", _targets("c1", "p1", "p2"), canary_count=1)
    result = execute_staged_rollout(
        plan, deploy_fn=_always_ok, health_fn=_always_fail
    )
    assert "c1" in result.failed
    assert set(result.skipped) == {"p1", "p2"}
    assert result.succeeded == []
    assert not result.ok


def test_execute_canary_health_failure_step_status_is_failed() -> None:
    plan = build_staged_rollout_plan("v1", _targets("c1", "p1"), canary_count=1)
    execute_staged_rollout(plan, deploy_fn=_always_ok, health_fn=_always_fail)
    assert plan.steps[0].status == "failed"


def test_execute_canary_health_failure_with_multiple_canaries_halts_early() -> None:
    plan = build_staged_rollout_plan("v1", _targets("c1", "c2", "p1"), canary_count=2)
    call_count = [0]

    def health_fn(step: RolloutPlanStep) -> bool:
        call_count[0] += 1
        return False  # fail first canary

    result = execute_staged_rollout(plan, deploy_fn=_always_ok, health_fn=health_fn)
    assert "c1" in result.failed
    assert call_count[0] == 1, "health_fn should be called once then halt"
    assert set(result.skipped) == {"c2", "p1"}


# ---------------------------------------------------------------------------
# execute_staged_rollout – health_fn only called for canary steps
# ---------------------------------------------------------------------------


def test_execute_health_fn_not_called_for_promote_steps() -> None:
    plan = build_staged_rollout_plan("v1", _targets("c1", "p1", "p2"), canary_count=1)
    health_calls: list[str] = []

    def health_fn(step: RolloutPlanStep) -> bool:
        health_calls.append(step.node_id)
        return True

    result = execute_staged_rollout(plan, deploy_fn=_always_ok, health_fn=health_fn)
    assert result.ok
    assert health_calls == ["c1"], "health_fn must only be called for canary steps"


def test_execute_health_fn_called_for_each_canary_step() -> None:
    plan = build_staged_rollout_plan("v1", _targets("c1", "c2", "p1"), canary_count=2)
    health_calls: list[str] = []

    def health_fn(step: RolloutPlanStep) -> bool:
        health_calls.append(step.node_id)
        return True

    result = execute_staged_rollout(plan, deploy_fn=_always_ok, health_fn=health_fn)
    assert result.ok
    assert health_calls == ["c1", "c2"]


# ---------------------------------------------------------------------------
# execute_staged_rollout – full success path
# ---------------------------------------------------------------------------


def test_execute_full_success_all_nodes_succeed() -> None:
    plan = build_staged_rollout_plan("v1", _targets("c1", "p1", "p2", "p3"), canary_count=1)
    result = execute_staged_rollout(plan, deploy_fn=_always_ok, health_fn=_always_ok)
    assert result.ok
    assert set(result.succeeded) == {"c1", "p1", "p2", "p3"}
    assert result.failed == []
    assert result.skipped == []


def test_execute_full_success_without_health_fn() -> None:
    plan = build_staged_rollout_plan("v1", _targets("c1", "p1"), canary_count=1)
    result = execute_staged_rollout(plan, deploy_fn=_always_ok)
    assert result.ok
    assert set(result.succeeded) == {"c1", "p1"}


def test_execute_full_success_step_statuses_all_succeeded() -> None:
    plan = build_staged_rollout_plan("v1", _targets("c1", "p1"), canary_count=1)
    execute_staged_rollout(plan, deploy_fn=_always_ok, health_fn=_always_ok)
    assert all(s.status == "succeeded" for s in plan.steps)


def test_execute_full_success_result_version_matches_plan() -> None:
    plan = build_staged_rollout_plan("edge-2026.7", _targets("c1"))
    result = execute_staged_rollout(plan, deploy_fn=_always_ok)
    assert result.version == "edge-2026.7"


# ---------------------------------------------------------------------------
# execute_staged_rollout – canary-only plan
# ---------------------------------------------------------------------------


def test_execute_canary_only_plan_all_succeed() -> None:
    """A plan where canary_count == len(targets) has no promote steps."""
    plan = build_staged_rollout_plan("v1", _targets("c1", "c2"), canary_count=2)
    assert plan.promote_steps == []
    result = execute_staged_rollout(plan, deploy_fn=_always_ok, health_fn=_always_ok)
    assert result.ok
    assert set(result.succeeded) == {"c1", "c2"}
    assert result.skipped == []
    assert result.failed == []


def test_execute_canary_only_plan_simulate() -> None:
    """Simulate mode works correctly for canary-only plans."""
    plan = build_staged_rollout_plan("v2", _targets("c1"), canary_count=1)
    assert plan.promote_steps == []
    result = execute_staged_rollout(plan, deploy_fn=_always_ok, simulate=True)
    assert result.ok
    assert result.succeeded == ["c1"]


def test_execute_canary_only_plan_health_failure() -> None:
    """Health check failure in canary-only plan skips no promote but marks failed."""
    plan = build_staged_rollout_plan("v1", _targets("c1", "c2"), canary_count=2)
    result = execute_staged_rollout(plan, deploy_fn=_always_ok, health_fn=_always_fail)
    assert "c1" in result.failed
    assert "c2" in result.skipped
    assert not result.ok


# ---------------------------------------------------------------------------
# execute_staged_rollout – mixed success/failure ordering
# ---------------------------------------------------------------------------


def test_execute_mixed_success_first_promote_fails_second_skipped() -> None:
    """Among promote steps, failure at first skips subsequent ones."""
    plan = build_staged_rollout_plan("v1", _targets("c1", "p1", "p2", "p3"), canary_count=1)
    fail_nodes = {"p1"}

    def deploy_fn(step: RolloutPlanStep) -> bool:
        return step.node_id not in fail_nodes

    result = execute_staged_rollout(plan, deploy_fn=deploy_fn)
    assert "c1" in result.succeeded
    assert "p1" in result.failed
    assert set(result.skipped) == {"p2", "p3"}
    assert not result.ok


def test_execute_mixed_success_canary_succeeds_first_promote_fails() -> None:
    """Canary succeeds but first promote node fails; remaining promote skipped."""
    plan = build_staged_rollout_plan("v1", _targets("c1", "p1", "p2"), canary_count=1)
    fail_nodes = {"p1"}

    def deploy_fn(step: RolloutPlanStep) -> bool:
        return step.node_id not in fail_nodes

    result = execute_staged_rollout(plan, deploy_fn=deploy_fn, health_fn=_always_ok)
    assert result.succeeded == ["c1"]
    assert result.failed == ["p1"]
    assert result.skipped == ["p2"]
    assert not result.ok


# ---------------------------------------------------------------------------
# RolloutResult – accumulation
# ---------------------------------------------------------------------------


def test_rollout_result_accumulation_succeeded_list_ordered() -> None:
    """Succeeded nodes are accumulated in rollout execution order."""
    plan = build_staged_rollout_plan("v1", _targets("c1", "p1", "p2"), canary_count=1)
    result = execute_staged_rollout(plan, deploy_fn=_always_ok)
    assert result.succeeded == ["c1", "p1", "p2"]


def test_rollout_result_accumulation_failed_and_skipped() -> None:
    """Failed node is recorded first, then skipped nodes accumulate in order."""
    plan = build_staged_rollout_plan("v1", _targets("c1", "p1", "p2", "p3"), canary_count=1)
    fail_nodes = {"p1"}

    def deploy_fn(step: RolloutPlanStep) -> bool:
        return step.node_id not in fail_nodes

    result = execute_staged_rollout(plan, deploy_fn=deploy_fn)
    assert result.failed == ["p1"]
    assert result.skipped == ["p2", "p3"]


def test_rollout_result_accumulation_version_preserved_throughout() -> None:
    """Version attribute on RolloutResult matches plan version after execution."""
    plan = build_staged_rollout_plan("fleet-v9.0", _targets("c1", "p1", "p2"), canary_count=1)
    result = execute_staged_rollout(plan, deploy_fn=_always_ok)
    assert result.version == "fleet-v9.0"


def test_rollout_result_accumulation_empty_failed_means_ok() -> None:
    """RolloutResult.ok reflects the emptiness of the failed list post-execution."""
    plan = build_staged_rollout_plan("v1", _targets("c1", "p1"), canary_count=1)
    result = execute_staged_rollout(plan, deploy_fn=_always_ok)
    assert result.failed == []
    assert result.ok is True
