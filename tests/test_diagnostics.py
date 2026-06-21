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


def test_expired_active_leases_check():
    cp = ControlPlane.in_memory()

    # Clean fleet: no leases at all -> the check reports ok.
    clean = diagnostics.run_diagnostics(cp, names=["expired-active-leases"])
    assert clean and all(f.check == "expired-active-leases" for f in clean)
    assert clean[0].severity == "ok"

    # Seed a real lease by claiming a task, then force it expired-but-active.
    m = cp.register_machine("h", resources={"cpu": 4, "memory_gb": 8})
    a = cp.register_agent(m.id, "n", capabilities=[])
    t = cp.create_task("title", project="p")
    _task, lease = cp.claim_task(t.id, a.id)

    past_iso = "2000-01-01T00:00:00.000000+00:00"
    cp.store.execute(
        "UPDATE leases SET status='active', expires_at=? WHERE id=?",
        (past_iso, lease.id),
    )

    findings = diagnostics.run_diagnostics(cp, names=["expired-active-leases"])
    assert findings and all(f.check == "expired-active-leases" for f in findings)
    finding = findings[0]
    assert finding.severity == "warn"
    assert lease.id in finding.detail["lease_ids"]
    assert any(entry["id"] == lease.id and entry["task_id"] == t.id for entry in finding.detail["leases"])
