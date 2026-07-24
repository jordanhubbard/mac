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
    PHASE_FAILURE_EVIDENCE_SCHEMA,
    PHASE_STATUSES,
    REDACTED_PLACEHOLDER,
    InstallPhase,
    NodeInstallPlan,
    NodeInstallResult,
    PhaseFailureEvidence,
    build_node_install_plan,
    capture_phase_failure_evidence,
    execute_node_install,
    redact_secret_text,
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


# ---------------------------------------------------------------------------
# redact_secret_text()
# ---------------------------------------------------------------------------


def test_redact_secret_text_empty_is_unchanged() -> None:
    assert redact_secret_text("") == ""


def test_redact_secret_text_leaves_plain_text_alone() -> None:
    text = "installing package foo bar baz version 1.2.3"
    assert redact_secret_text(text) == text


def test_redact_secret_text_redacts_secret_assignments() -> None:
    out = redact_secret_text("GITHUB_TOKEN=ghp_supersecret123 remaining")
    assert "ghp_supersecret123" not in out
    assert out == "GITHUB_TOKEN=<redacted> remaining"


def test_redact_secret_text_redacts_various_secret_keys() -> None:
    for key in ("API_KEY", "AUTH_TOKEN", "MAC_SECRET_KEY", "DB_PASSWORD"):
        out = redact_secret_text(f"{key}=leakvalue999")
        assert "leakvalue999" not in out
        assert out == f"{key}=<redacted>"


def test_redact_secret_text_ignores_non_secret_assignment() -> None:
    text = "HOSTNAME=node-01"
    assert redact_secret_text(text) == text


def test_redact_secret_text_redacts_colon_assignment() -> None:
    out = redact_secret_text("password: hunter2secret")
    assert "hunter2secret" not in out
    assert out == "password: <redacted>"


def test_redact_secret_text_redacts_bearer_header() -> None:
    out = redact_secret_text("Authorization: Bearer abcDEF123456ghi")
    assert "abcDEF123456ghi" not in out
    assert out.endswith("Bearer <redacted>")


def test_redact_secret_text_redacts_url_userinfo_but_keeps_host() -> None:
    out = redact_secret_text(
        "cloning https://x-access-token:tok_secret999@github.com/org/repo.git"
    )
    assert "tok_secret999" not in out
    assert out == "cloning https://<redacted>@github.com/org/repo.git"


def test_redact_secret_text_redacts_basic_url_credentials() -> None:
    out = redact_secret_text("https://user:passw0rd@example.com/path")
    assert "passw0rd" not in out
    assert out == "https://<redacted>@example.com/path"


# ---------------------------------------------------------------------------
# PhaseFailureEvidence / capture_phase_failure_evidence()
# ---------------------------------------------------------------------------


def test_phase_failure_evidence_schema_constant() -> None:
    assert PHASE_FAILURE_EVIDENCE_SCHEMA == "mac.fleet_node_install.phase_failure.v1"
    assert REDACTED_PLACEHOLDER == "<redacted>"


def test_phase_failure_evidence_direct_construction_defaults() -> None:
    ev = PhaseFailureEvidence(phase="p", order=1)
    assert ev.command is None
    assert ev.detail == []
    assert ev.schema == PHASE_FAILURE_EVIDENCE_SCHEMA
    assert ev.to_dict()["phase"] == "p"


def test_capture_phase_failure_evidence_records_phase_and_order() -> None:
    phase = InstallPhase(name="bootstrap", order=3)
    ev = capture_phase_failure_evidence(phase)
    assert ev.phase == "bootstrap"
    assert ev.order == 3
    assert ev.command is None
    assert ev.detail == []
    assert ev.schema == PHASE_FAILURE_EVIDENCE_SCHEMA


def test_capture_phase_failure_evidence_redacts_command() -> None:
    phase = InstallPhase(
        name="auth", order=1, command="deploy --token=abc_secret_token123"
    )
    ev = capture_phase_failure_evidence(phase)
    assert ev.command is not None
    assert "abc_secret_token123" not in ev.command
    assert "<redacted>" in ev.command


def test_capture_phase_failure_evidence_redacts_and_filters_output() -> None:
    output = "MAC_API_TOKEN=leak_me_now999\n\n   \nplain diagnostic line\n"
    phase = InstallPhase(name="net", order=2)
    ev = capture_phase_failure_evidence(phase, output)
    # Blank / whitespace-only lines dropped; secret redacted; plain line kept.
    assert ev.detail == ["MAC_API_TOKEN=<redacted>", "plain diagnostic line"]
    assert "leak_me_now999" not in "".join(ev.detail)


def test_phase_failure_evidence_to_dict_is_serialisable() -> None:
    import json

    phase = InstallPhase(name="net", order=2, command="run")
    ev = capture_phase_failure_evidence(phase, "line one")
    doc = ev.to_dict()
    assert doc["schema"] == PHASE_FAILURE_EVIDENCE_SCHEMA
    assert doc["phase"] == "net"
    assert doc["order"] == 2
    assert doc["detail"] == ["line one"]
    # Round-trips cleanly as JSON (no non-serialisable objects leaked in).
    assert json.loads(json.dumps(doc)) == doc


def test_phase_failure_evidence_is_frozen() -> None:
    ev = capture_phase_failure_evidence(InstallPhase(name="x", order=1))
    with pytest.raises(Exception):
        ev.phase = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# execute_node_install() failure-evidence wiring
# ---------------------------------------------------------------------------


def test_execute_records_failure_evidence_with_redacted_output() -> None:
    plan = build_node_install_plan("1", [_phase("a", 1), _phase("b", 2)])
    outputs = {"a": "GITHUB_TOKEN=ghp_leak12345\nboom failed"}

    def runner(phase: InstallPhase) -> bool:
        return phase.name != "a"

    result = execute_node_install(
        plan, run_fn=runner, output_fn=lambda p: outputs.get(p.name)
    )
    assert result.failed == ["a"]
    assert result.skipped == ["b"]
    assert len(result.failure_evidence) == 1
    ev = result.failure_evidence[0]
    assert ev.phase == "a"
    assert ev.order == 1
    assert "ghp_leak12345" not in "".join(ev.detail)
    assert ev.detail == ["GITHUB_TOKEN=<redacted>", "boom failed"]


def test_execute_records_evidence_without_output_fn() -> None:
    plan = build_node_install_plan("1", [_phase("a", 1)])
    result = execute_node_install(plan, run_fn=_always_fail)
    assert len(result.failure_evidence) == 1
    assert result.failure_evidence[0].phase == "a"
    assert result.failure_evidence[0].detail == []


def test_execute_success_has_no_failure_evidence() -> None:
    plan = build_node_install_plan("1", [_phase("a", 1), _phase("b", 2)])
    result = execute_node_install(plan, run_fn=_always_ok)
    assert result.failure_evidence == []


def test_execute_evidence_covers_only_the_failed_phase() -> None:
    plan = build_node_install_plan(
        "1", [_phase("a", 1), _phase("b", 2), _phase("c", 3)]
    )

    def runner(phase: InstallPhase) -> bool:
        return phase.name != "b"

    result = execute_node_install(plan, run_fn=runner)
    assert [e.phase for e in result.failure_evidence] == ["b"]
