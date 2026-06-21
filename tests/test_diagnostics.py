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
