"""Validation and ACC conversion coverage for migration helpers."""

from __future__ import annotations

import io
import json

import pytest

from mac import migration
from mac.models import ValidationError
from mac.services import ControlPlane


def test_record_required_field_validation() -> None:
    cp = ControlPlane.in_memory()
    migrator = migration.Migrator(cp)
    records = [
        {"record": "tenant"},
        {"record": "user"},
        {"record": "machine"},
        {"record": "agent"},
        {"record": "agent", "machine_id": "m"},
        {"record": "task"},
        {"record": "evidence"},
    ]
    report = migrator.import_stream(json.dumps(item) for item in records)
    assert len(report.errors) == len(records)
    assert "requires 'name'" in report.errors[0]["error"]
    assert "requires 'tenant'" in report.errors[1]["error"]
    assert "requires 'hostname'" in report.errors[2]["error"]
    assert "requires 'machine_id'" in report.errors[3]["error"]
    assert "requires 'name'" in report.errors[4]["error"]
    assert "requires 'title'" in report.errors[5]["error"]


def test_direct_task_ref_and_standalone_provenance() -> None:
    cp = ControlPlane.in_memory()
    report = migration.import_jsonl(
        cp,
        stream=io.StringIO("\n".join([
            json.dumps({"record": "task", "title": "work", "task_ref": "local:one"}),
            json.dumps({"record": "evidence", "task_ref": "local:one", "kind": "log", "uri": "x", "summary": "s"}),
            json.dumps({"record": "provenance", "standalone": True, "event_type": "migration.note"}),
        ])),
    )
    assert report.tasks_imported == 1
    assert report.evidence_imported == 1
    assert report.provenance_imported == 1


def test_task_ref_resolution_validation_and_store_lookup(monkeypatch) -> None:
    cp = ControlPlane.in_memory()
    migrator = migration.Migrator(cp)
    with pytest.raises(ValidationError, match="task_id.*task_ref"):
        migrator._resolve_task_ref({})
    with pytest.raises(ValidationError, match="must be"):
        migrator._resolve_task_ref({"task_ref": "bad"})
    monkeypatch.setattr(cp.store, "query_one", lambda *_a, **_k: {"task_id": "task"})
    assert migrator._resolve_task_ref({"task_ref": "source:id"}) == "task"
    assert migrator._resolve_task_ref({"task_ref": "source:id"}) == "task"
    assert migrator._resolve_task_ref({"task_id": "direct"}) == "direct"


def test_acc_parsing_redaction_priority_and_capabilities() -> None:
    assert migration._parse_json(None) is None
    assert migration._parse_json(3) == 3
    assert migration._parse_json(" ") is None
    assert migration._parse_json('{"x":1}') == {"x": 1}
    assert migration._parse_json("bad") == "bad"
    redacted = migration._redact_project_payload({
        "data": {"git_url": "secret", "repoUrl": "secret", "keep": True}
    })
    assert redacted["data"]["git_url"] == "[redacted]"
    assert redacted["data"]["keep"] is True
    assert migration._acc_priority(2) == 98
    assert migration._acc_priority("bad") == 0
    assert migration._acc_required_capabilities({}, {"required_capabilities": ["python", "python"]}) == ["python"]
    assert migration._acc_required_capabilities({}, {"required_executors": "codex"}) == ["codex"]
    assert migration._acc_required_capabilities({}, {"preferred_executor": "claude"}) == ["claude"]
    assert migration._acc_required_capabilities({"task_type": "review"}, {}) == ["review"]
    assert migration._acc_required_capabilities({}, {}) == []
    assert migration._string_list([" b ", "a", ""]) == ["a", "b"]
    assert migration._string_list(3) == []


def test_acc_missing_table_helpers_report_warnings() -> None:
    counts = {
        "agents": 0,
        "projects": 0,
        "tasks": 0,
        "tasks_by_status": {},
        "terminal_tasks_skipped": 0,
        "active_tasks_blocking": 0,
        "tasks_planned_for_import": 0,
        "attempts": 0,
        "audit_events": 0,
        "audit_events_planned": 0,
    }
    warnings = []
    assert migration._acc_agent_records(None, set(), counts, warnings) == []
    assert migration._acc_project_records(None, set(), counts, warnings) == []
    assert migration._acc_task_records(None, set(), counts, warnings) == ([], [])
    assert migration._acc_attempt_provenance_records(None, set(), counts, warnings, []) == []
    assert migration._acc_audit_provenance_records(None, set(), counts, warnings, [], 10) == []
    assert len(warnings) == 5


def test_acc_audit_zero_limit_and_task_ref_helpers(monkeypatch) -> None:
    counts = {"audit_events": 0, "audit_events_planned": 0}
    warnings = []
    monkeypatch.setattr(migration, "_acc_count", lambda *_a: 5)
    assert migration._acc_audit_provenance_records(
        None, {"work_audit_events"}, counts, warnings, [], 0
    ) == []
    assert counts["audit_events"] == 5
    assert "audit_limit" in warnings[0]
    records = [{"record": "task", "task_ref": "acc:one"}, {"record": "other", "task_ref": "x"}]
    assert migration._planned_task_refs(records) == {"acc:one"}
    active = migration._downgrade_active_acc_task({
        "record": "task", "metadata": {"acc_status": "in_progress"}
    })
    assert active["metadata"]["migration_requeued_from_active_acc_claim"] is True


def test_report_to_dict_variants() -> None:
    assert migration.MigrationReport().to_dict()["errors"] == []
    plan = migration.AccMigrationPlan("source", [], {}, [], [], [], [])
    assert plan.to_dict()["records_planned"] == 0
    report = migration.AccMigrationReport("dry-run", "source", {}, [], [], [], [], migration.MigrationReport())
    assert "import" in report.to_dict()
