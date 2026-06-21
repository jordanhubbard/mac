"""Tests for the read-only control-plane diagnostics framework (`mac diagnostics`).

These cover the framework itself — registration, isolation of a broken check,
subset selection, and the report shape. Individual checks add their own tests
below (one block each), converging on this file the same way they converge on
the diagnostics.CHECKS registry.
"""

from mac import diagnostics
from mac.services import ControlPlane


def test_framework_runs_registered_checks():
    cp = ControlPlane.in_memory()
    report = diagnostics.summarize(diagnostics.run_diagnostics(cp))
    assert report["schema"] == "mac.diagnostics.report.v1"
    assert "database-reachable" in report["checks"]
    assert report["counts"]["error"] == 0
    assert report["ok"] is True


def test_broken_check_is_isolated_not_fatal():
    bad = diagnostics.Diagnostic(
        "boom", "always raises", lambda cp: (_ for _ in ()).throw(RuntimeError("kaboom"))
    )
    diagnostics.CHECKS.append(bad)
    try:
        cp = ControlPlane.in_memory()
        findings = diagnostics.run_diagnostics(cp)
        boom = [f for f in findings if f.check == "boom"]
        assert boom and boom[0].severity == "error"
        # The healthy baseline check still ran.
        assert any(f.check == "database-reachable" and f.severity == "ok" for f in findings)
    finally:
        diagnostics.CHECKS.remove(bad)


def test_subset_selection_runs_only_named_check():
    cp = ControlPlane.in_memory()
    findings = diagnostics.run_diagnostics(cp, names=["database-reachable"])
    assert findings and all(f.check == "database-reachable" for f in findings)


def test_register_rejects_duplicate_names():
    import pytest

    @diagnostics.register("dup-test-check", "first")
    def _first(cp):
        return []

    try:
        with pytest.raises(ValueError):

            @diagnostics.register("dup-test-check", "second")
            def _second(cp):
                return []
    finally:
        diagnostics.CHECKS[:] = [d for d in diagnostics.CHECKS if d.name != "dup-test-check"]


def test_finding_rejects_invalid_severity():
    import pytest

    with pytest.raises(ValueError):
        diagnostics.Finding("x", "catastrophic", "nope")


def test_failed_tasks_check_warns_when_failed_present_else_ok():
    cp = ControlPlane.in_memory()

    # No failed tasks yet -> ok.
    findings = diagnostics.run_diagnostics(cp, names=["failed-tasks"])
    assert findings and len(findings) == 1
    assert findings[0].check == "failed-tasks"
    assert findings[0].severity == "ok"
    assert findings[0].detail["count"] == 0

    # Create two tasks; force one into the 'failed' state directly (the real
    # transition API requires claim/run/lease bookkeeping not needed here).
    healthy = cp.create_task("healthy task", project="diag")
    broken = cp.create_task("broken task", project="diag")
    cp.store.execute("UPDATE tasks SET state='failed' WHERE id=?", (broken.id,))

    findings = diagnostics.run_diagnostics(cp, names=["failed-tasks"])
    assert findings and len(findings) == 1
    finding = findings[0]
    assert finding.check == "failed-tasks"
    assert finding.severity == "warn"
    assert "1" in finding.summary
    assert finding.detail["count"] == 1
    recent_ids = [r["id"] for r in finding.detail["recent"]]
    assert broken.id in recent_ids
    assert healthy.id not in recent_ids
