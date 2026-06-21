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


def test_stale_agents_check():
    cp = ControlPlane.in_memory()
    m = cp.register_machine("h", resources={"cpu": 4, "memory_gb": 8})
    a = cp.register_agent(m.id, "lagging-agent", capabilities=[])

    # Fresh agent: nothing is stale yet.
    findings = diagnostics.run_diagnostics(cp, names=["stale-agents"])
    assert findings and len(findings) == 1
    assert findings[0].check == "stale-agents"
    assert findings[0].severity == "ok"

    # Force the agent well past the default threshold (use the year 2000).
    cp.store.execute(
        "UPDATE agents SET last_seen_at=? WHERE id=?",
        ("2000-01-01T00:00:00.000000+00:00", a.id),
    )

    findings = diagnostics.run_diagnostics(cp, names=["stale-agents"])
    assert findings and len(findings) == 1
    warn = findings[0]
    assert warn.check == "stale-agents"
    assert warn.severity == "warn"
    assert warn.detail["count"] == 1
    stale_ids = {entry["id"] for entry in warn.detail["agents"]}
    stale_names = {entry["name"] for entry in warn.detail["agents"]}
    assert a.id in stale_ids
    assert "lagging-agent" in stale_names
