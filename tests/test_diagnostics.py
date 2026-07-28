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


def test_recent_rows_counts_all_but_caps_recent_dicts():
    cp = ControlPlane.in_memory()

    # Seed three failed tasks; force the state directly.
    ids = []
    for i in range(3):
        t = cp.create_task("task %d" % i, project="diag")
        cp.store.execute("UPDATE tasks SET state='failed' WHERE id=?", (t.id,))
        ids.append(t.id)

    sql = "SELECT id, title, project FROM tasks WHERE state = 'failed' ORDER BY created_at DESC"

    # Count reflects all matching rows; recent is capped and sqlite3.Row -> dict.
    count, recent = diagnostics._recent_rows(cp, sql, limit=2)
    assert count == 3
    assert len(recent) == 2
    assert all(isinstance(r, dict) for r in recent)
    assert set(recent[0].keys()) == {"id", "title", "project"}
    assert {r["id"] for r in recent} <= set(ids)

    # Default limit (10) returns every row when fewer than the cap.
    count_all, recent_all = diagnostics._recent_rows(cp, sql)
    assert count_all == 3
    assert len(recent_all) == 3


def test_threshold_finding_ok_and_warn_shape():
    recent = [{"id": "x", "title": "t", "project": "p"}]

    ok = diagnostics._threshold_finding("c", count=2, threshold=5, recent=recent, noun="widget(s)")
    assert ok.severity == "ok"
    assert ok.summary == "2 widget(s) (threshold 5)"
    assert ok.detail == {"count": 2, "threshold": 5}
    assert "recent" not in ok.detail

    warn = diagnostics._threshold_finding("c", count=7, threshold=5, recent=recent, noun="widget(s)")
    assert warn.severity == "warn"
    assert warn.summary == "7 widget(s) exceed threshold 5"
    assert warn.detail == {"count": 7, "threshold": 5, "recent": recent}


def test_stranded_replacements_check_ok_when_clean():
    cp = ControlPlane.in_memory()

    findings = diagnostics.run_diagnostics(cp, names=["stranded-replacements"])
    assert findings and len(findings) == 1
    assert findings[0].check == "stranded-replacements"
    assert findings[0].severity == "ok"
    assert findings[0].detail["count"] == 0


def test_stranded_replacements_check_warns_when_stranded():
    import json

    cp = ControlPlane.in_memory()

    # Create two tasks: A (cancelled) with replacement_task_id pointing to B (also cancelled, no
    # further replacement). The chain A -> B is stranded because B has no live successor.
    task_a = cp.create_task("task-a", project="diag")
    task_b = cp.create_task("task-b", project="diag")

    # Force both to cancelled state and wire replacement_task_id from A -> B.
    meta_a = {
        "repository_ref_lifecycle": {
            "replacement_task_id": task_b.id,
        }
    }
    cp.store.execute(
        "UPDATE tasks SET state='cancelled', metadata=? WHERE id=?",
        (json.dumps(meta_a), task_a.id),
    )
    cp.store.execute(
        "UPDATE tasks SET state='cancelled', metadata=? WHERE id=?",
        (json.dumps({}), task_b.id),
    )

    findings = diagnostics.run_diagnostics(cp, names=["stranded-replacements"])
    assert findings and len(findings) == 1
    finding = findings[0]
    assert finding.check == "stranded-replacements"
    assert finding.severity == "warn"
    assert finding.detail["count"] == 1
    recent_ids = [r["id"] for r in finding.detail["recent"]]
    assert task_a.id in recent_ids


def test_stranded_replacements_check_ok_when_chain_has_live_successor():
    import json

    cp = ControlPlane.in_memory()

    # A (cancelled) -> B (open). Chain is live, not stranded.
    task_a = cp.create_task("task-a", project="diag")
    task_b = cp.create_task("task-b", project="diag")

    meta_a = {
        "repository_ref_lifecycle": {
            "replacement_task_id": task_b.id,
        }
    }
    cp.store.execute(
        "UPDATE tasks SET state='cancelled', metadata=? WHERE id=?",
        (json.dumps(meta_a), task_a.id),
    )
    # task_b stays in 'open' state (the default after create_task).

    findings = diagnostics.run_diagnostics(cp, names=["stranded-replacements"])
    assert findings and len(findings) == 1
    assert findings[0].severity == "ok"
    assert findings[0].detail["count"] == 0


def test_stranded_replacements_check_ok_when_chain_ends_completed():
    import json

    cp = ControlPlane.in_memory()

    # A (failed) -> B (cancelled) -> C (completed). Chain is satisfied.
    task_a = cp.create_task("task-a", project="diag")
    task_b = cp.create_task("task-b", project="diag")
    task_c = cp.create_task("task-c", project="diag")

    meta_a = {"repository_ref_lifecycle": {"replacement_task_id": task_b.id}}
    meta_b = {"repository_ref_lifecycle": {"replacement_task_id": task_c.id}}

    cp.store.execute(
        "UPDATE tasks SET state='failed', metadata=? WHERE id=?",
        (json.dumps(meta_a), task_a.id),
    )
    cp.store.execute(
        "UPDATE tasks SET state='cancelled', metadata=? WHERE id=?",
        (json.dumps(meta_b), task_b.id),
    )
    cp.store.execute(
        "UPDATE tasks SET state='completed' WHERE id=?",
        (task_c.id,),
    )

    findings = diagnostics.run_diagnostics(cp, names=["stranded-replacements"])
    assert findings and len(findings) == 1
    assert findings[0].severity == "ok"
    assert findings[0].detail["count"] == 0


def test_data_source_identity_reports_sqlite_backend():
    cp = ControlPlane.in_memory()
    findings = diagnostics.run_diagnostics(cp, names=["data-source-identity"])
    assert findings and len(findings) == 1
    finding = findings[0]
    assert finding.check == "data-source-identity"
    # An in-memory authority is ephemeral, so the check warns rather than ok.
    assert finding.severity == "warn"
    assert finding.detail["backend"] == "sqlite"
    assert finding.detail["in_memory"] is True
    assert finding.detail["authoritative"] is True


def test_data_source_identity_ok_for_durable_sqlite_file(tmp_path):
    from mac.store import SQLiteStore

    store = SQLiteStore(str(tmp_path / "authority.db"))
    try:
        cp = ControlPlane(store=store, secret_key="diagnostics-test-secret-key-32-characters")
        findings = diagnostics.run_diagnostics(cp, names=["data-source-identity"])
        assert findings and findings[0].severity == "ok"
        assert findings[0].detail["backend"] == "sqlite"
        assert findings[0].detail["in_memory"] is False
        assert findings[0].detail["location"].endswith("authority.db")
    finally:
        store.close()


def test_backend_identity_helper_falls_back_for_bare_store():
    class _BareStore:
        path = "/tmp/whatever.db"

    class _Plane:
        store = _BareStore()

    identity = diagnostics.backend_identity(_Plane())
    assert identity["backend"] == "_BareStore"
    assert identity["location"] == "/tmp/whatever.db"
    assert identity["authoritative"] is True


def test_diagnostics_report_always_includes_data_source():
    cp = ControlPlane.in_memory()

    full = cp.diagnostics_report()
    assert full["schema"] == "mac.diagnostics.report.v1"
    assert full["data_source"]["backend"] == "sqlite"

    # Even a narrowed selection that omits the identity check still carries the
    # top-level machine-readable data_source block.
    subset = cp.diagnostics_report(names=["failed-tasks"])
    assert subset["data_source"]["backend"] == "sqlite"
    assert {f["check"] for f in subset["findings"]} == {"failed-tasks"}


def test_lifecycle_stage_dwell_ok_when_fresh_then_warns_when_stuck():
    cp = ControlPlane.in_memory()

    # A brand-new open task is fresh in its stage -> ok.
    task = cp.create_task("fresh", project="diag")
    ok = diagnostics.run_diagnostics(cp, names=["lifecycle-stage-dwell"])
    assert ok and len(ok) == 1
    assert ok[0].check == "lifecycle-stage-dwell"
    assert ok[0].severity == "ok"

    # Force the task to look like it entered its current stage long ago.
    cp.store.execute(
        "UPDATE tasks SET updated_at=? WHERE id=?",
        ("2000-01-01T00:00:00.000000+00:00", task.id),
    )
    warn = diagnostics.run_diagnostics(cp, names=["lifecycle-stage-dwell"])
    assert warn and warn[0].severity == "warn"
    assert warn[0].detail["count"] == 1
    stuck = warn[0].detail["tasks"][0]
    assert stuck["id"] == task.id
    assert stuck["stage"] == task.state
    assert stuck["dwell_seconds"] is not None and stuck["dwell_seconds"] > 0


def test_lifecycle_stage_dwell_ignores_terminal_tasks():
    cp = ControlPlane.in_memory()

    task = cp.create_task("done long ago", project="diag")
    # Completed tasks may sit "old" forever; dwelling in a terminal stage is
    # expected and must not warn.
    cp.store.execute(
        "UPDATE tasks SET state='completed', updated_at=? WHERE id=?",
        ("2000-01-01T00:00:00.000000+00:00", task.id),
    )
    findings = diagnostics.run_diagnostics(cp, names=["lifecycle-stage-dwell"])
    assert findings and findings[0].severity == "ok"
    assert findings[0].detail["count"] == 0 if "count" in findings[0].detail else True
