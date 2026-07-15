"""Tests for src/mac/fleet_node_install.py.

Covers:
- InstallPhase, NodeInstallPlan, NodeInstallResult data-class helpers
- build_node_install_plan(): empty version, empty phases, missing name,
  duplicate name, invalid order, duplicate order, non-list depends_on,
  unknown dependency, self dependency, explicit + implicit ordering
- execute_node_install(): simulate mode, missing run_fn guard, full
  success, failure halts + skips remaining, dependency-driven skip
"""

from __future__ import annotations

import pytest

from mac.fleet_node_install import (
    NODE_INSTALL_PLAN_SCHEMA,
    PHASE_STATUSES,
    InstallPhase,
    NodeInstallPlan,
    NodeInstallResult,
    build_node_install_plan,
    execute_node_install,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _phase(name: str, order: int | None = None, **kwargs: object) -> dict[str, object]:
    entry: dict[str, object] = {"name": name}
    if order is not None:
        entry["order"] = order
    entry.update(kwargs)
    return entry


def _always_ok(_phase: InstallPhase) -> bool:
    return True


def _always_fail(_phase: InstallPhase) -> bool:
    return False


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------


def test_schema_and_statuses() -> None:
    assert NODE_INSTALL_PLAN_SCHEMA == "mac.fleet_node_install.v1"
    assert set(PHASE_STATUSES) == {
        "planned",
        "active",
        "succeeded",
        "failed",
        "skipped",
    }


# ---------------------------------------------------------------------------
# InstallPhase data-class
# ---------------------------------------------------------------------------


def test_install_phase_defaults_and_terminal() -> None:
    phase = InstallPhase(name="bootstrap", order=1)
    assert phase.description == ""
    assert phase.status == "planned"
    assert phase.command is None
    assert phase.depends_on == []
    assert phase.is_terminal is False
    phase.status = "succeeded"
    assert phase.is_terminal is True
    phase.status = "active"
    assert phase.is_terminal is False


# ---------------------------------------------------------------------------
# NodeInstallPlan helpers
# ---------------------------------------------------------------------------


def test_plan_ordered_and_lookup_helpers() -> None:
    plan = build_node_install_plan(
        "1.0.0",
        [_phase("b", 2), _phase("a", 1), _phase("c", 3)],
    )
    assert [p.name for p in plan.ordered_phases] == ["a", "b", "c"]
    assert plan.phase_for_name("b").order == 2
    assert plan.phase_for_name("missing") is None
    # Nothing has run yet.
    assert [p.name for p in plan.pending_phases] == ["a", "b", "c"]
    assert plan.completed_phases == []


def test_plan_completed_and_pending_reflect_status() -> None:
    plan = build_node_install_plan("1", [_phase("a", 1), _phase("b", 2)])
    plan.phase_for_name("a").status = "succeeded"
    assert [p.name for p in plan.completed_phases] == ["a"]
    assert [p.name for p in plan.pending_phases] == ["b"]


# ---------------------------------------------------------------------------
# NodeInstallResult helper
# ---------------------------------------------------------------------------


def test_result_ok_property() -> None:
    assert NodeInstallResult(version="1").ok is True
    assert NodeInstallResult(version="1", failed=["x"]).ok is False


# ---------------------------------------------------------------------------
# build_node_install_plan validation
# ---------------------------------------------------------------------------


def test_build_rejects_empty_version() -> None:
    with pytest.raises(ValueError, match="version is required"):
        build_node_install_plan("  ", [_phase("a")])


def test_build_rejects_empty_phases() -> None:
    with pytest.raises(ValueError, match="at least one install phase"):
        build_node_install_plan("1", [])


def test_build_rejects_missing_name() -> None:
    with pytest.raises(ValueError, match="missing 'name'"):
        build_node_install_plan("1", [{"order": 1}])


def test_build_rejects_duplicate_name() -> None:
    with pytest.raises(ValueError, match="duplicate phase name"):
        build_node_install_plan("1", [_phase("a", 1), _phase("a", 2)])


def test_build_rejects_invalid_order() -> None:
    with pytest.raises(ValueError, match="invalid order"):
        build_node_install_plan("1", [_phase("a", 0)])


def test_build_rejects_duplicate_order() -> None:
    with pytest.raises(ValueError, match="duplicate phase order"):
        build_node_install_plan("1", [_phase("a", 1), _phase("b", 1)])


def test_build_rejects_non_list_depends_on() -> None:
    with pytest.raises(ValueError, match="depends_on must be a list"):
        build_node_install_plan("1", [_phase("a", 1, depends_on="b")])


def test_build_rejects_unknown_dependency() -> None:
    with pytest.raises(ValueError, match="unknown phase"):
        build_node_install_plan("1", [_phase("a", 1, depends_on=["ghost"])])


def test_build_rejects_self_dependency() -> None:
    with pytest.raises(ValueError, match="cannot depend on itself"):
        build_node_install_plan("1", [_phase("a", 1, depends_on=["a"])])


def test_build_assigns_implicit_order_and_trims_version() -> None:
    plan = build_node_install_plan(" 2.1 ", [_phase("a"), _phase("b")])
    assert plan.version == "2.1"
    assert [(p.name, p.order) for p in plan.ordered_phases] == [("a", 1), ("b", 2)]


def test_build_preserves_command_and_description() -> None:
    plan = build_node_install_plan(
        "1",
        [_phase("a", 1, description="d", command="echo hi")],
    )
    phase = plan.phase_for_name("a")
    assert phase.description == "d"
    assert phase.command == "echo hi"


# ---------------------------------------------------------------------------
# execute_node_install
# ---------------------------------------------------------------------------


def test_execute_requires_run_fn_without_simulate() -> None:
    plan = build_node_install_plan("1", [_phase("a", 1)])
    with pytest.raises(ValueError, match="run_fn is required"):
        execute_node_install(plan)


def test_execute_simulate_marks_all_succeeded() -> None:
    plan = build_node_install_plan("1", [_phase("a", 1), _phase("b", 2)])
    result = execute_node_install(plan, simulate=True)
    assert result.ok is True
    assert result.succeeded == ["a", "b"]
    assert result.failed == []
    assert result.skipped == []
    assert all(p.status == "succeeded" for p in plan.phases)


def test_execute_full_success_with_run_fn() -> None:
    plan = build_node_install_plan("1", [_phase("a", 1), _phase("b", 2)])
    ran: list[str] = []

    def runner(phase: InstallPhase) -> bool:
        ran.append(phase.name)
        return True

    result = execute_node_install(plan, run_fn=runner)
    assert ran == ["a", "b"]
    assert result.succeeded == ["a", "b"]
    assert result.ok is True


def test_execute_failure_halts_and_skips_remaining() -> None:
    plan = build_node_install_plan(
        "1", [_phase("a", 1), _phase("b", 2), _phase("c", 3)]
    )

    def runner(phase: InstallPhase) -> bool:
        return phase.name != "b"

    result = execute_node_install(plan, run_fn=runner)
    assert result.succeeded == ["a"]
    assert result.failed == ["b"]
    assert result.skipped == ["c"]
    assert plan.phase_for_name("c").status == "skipped"


def test_execute_skips_phase_with_unmet_dependency() -> None:
    plan = build_node_install_plan(
        "1",
        [_phase("a", 1), _phase("b", 2, depends_on=["a"])],
    )

    def runner(phase: InstallPhase) -> bool:
        # Fail "a" so "b"'s dependency is unmet -> b skipped.
        return phase.name != "a"

    result = execute_node_install(plan, run_fn=runner)
    assert result.failed == ["a"]
    assert result.skipped == ["b"]
    assert "b" not in result.succeeded


def test_execute_runs_dependent_phase_when_dependency_succeeds() -> None:
    plan = build_node_install_plan(
        "1",
        [_phase("a", 1), _phase("b", 2, depends_on=["a"])],
    )
    result = execute_node_install(plan, run_fn=_always_ok)
    assert result.succeeded == ["a", "b"]
    assert result.ok is True
