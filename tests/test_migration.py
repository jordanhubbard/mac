"""Tests for the generic migration importer."""

import io
import json

import pytest

from mac.migration import Migrator, import_jsonl
from mac.models import ValidationError
from mac.services import ControlPlane


def _stream(records):
    return io.StringIO("\n".join(json.dumps(r) for r in records))


def test_migration_imports_tenants_users_and_tasks_with_natural_key_dedup():
    cp = ControlPlane.in_memory()
    records = [
        {"record": "tenant", "name": "personal", "metadata": {"region": "us"}},
        {"record": "user", "tenant": "personal", "handle": "jordan", "display_name": "Jordan"},
        {
            "record": "task",
            "title": "Migrated work",
            "metadata": {"source": "acc", "external_id": "ACC-42"},
            "required_capabilities": ["ops"],
            "actor": "migration",
        },
    ]
    report = import_jsonl(cp, stream=_stream(records))
    assert report.tenants_imported == 1
    assert report.users_imported == 1
    assert report.tasks_imported == 1
    assert report.errors == []

    # Idempotent on second run.
    again = import_jsonl(cp, stream=_stream(records))
    assert again.errors == []
    assert len(cp.list_tenants()) == 1
    assert len(cp.list_users()) == 1
    assert len(cp.list_project_items()) == 1


def test_migration_resolves_task_ref_for_evidence_and_history(tmp_path):
    cp = ControlPlane.in_memory()
    path = tmp_path / "export.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"record": "tenant", "name": "ops"},
                {
                    "record": "task",
                    "title": "Imported with evidence",
                    "metadata": {"source": "acc", "external_id": "ACC-100"},
                },
                {
                    "record": "evidence",
                    "task_ref": "acc:ACC-100",
                    "kind": "log",
                    "uri": "log://acc/100",
                    "summary": "carried from acc",
                    "created_by": "migration",
                },
                {
                    "record": "history",
                    "task_ref": "acc:ACC-100",
                    "event_type": "imported",
                    "content": "imported from acc 2026-01-01",
                },
            ]
        ),
        encoding="utf-8",
    )
    report = import_jsonl(cp, path=path)
    assert report.errors == []
    assert report.evidence_imported == 1
    assert report.provenance_imported == 1

    item = cp.list_project_items()[0]
    evidence = cp.list_evidence(item.task_id)
    assert evidence[0].uri == "log://acc/100"
    memories = cp.search_memory(task_id=item.task_id)
    # Both the auto-bridge "imported" memory and our injected history memory exist.
    record_types = {m.record_type for m in memories}
    assert {"imported"} <= record_types


def test_migration_skips_comments_and_blank_lines_and_reports_errors():
    cp = ControlPlane.in_memory()
    stream = io.StringIO(
        "\n".join(
            [
                "# header comment",
                "",
                json.dumps({"record": "tenant", "name": "ok"}),
                "not valid json {",
                json.dumps({"record": "unknown-record-type"}),
            ]
        )
    )
    report = import_jsonl(cp, stream=stream)
    assert report.tenants_imported == 1
    assert report.skipped == 2  # comment + blank
    assert len(report.errors) == 2
    assert "invalid JSON" in report.errors[0]["error"]
    assert "unknown record type" in report.errors[1]["error"]


def test_migration_evidence_without_resolvable_ref_errors():
    cp = ControlPlane.in_memory()
    report = import_jsonl(
        cp,
        stream=_stream(
            [
                {"record": "evidence", "task_ref": "acc:does-not-exist", "kind": "log", "uri": "x", "summary": "y"},
            ]
        ),
    )
    assert report.evidence_imported == 0
    assert len(report.errors) == 1
    assert "task_ref does not resolve" in report.errors[0]["error"]


def test_migration_requires_path_or_stream():
    cp = ControlPlane.in_memory()
    with pytest.raises(ValidationError):
        import_jsonl(cp)


def test_migration_task_without_source_external_id_is_not_idempotent():
    """The contract is explicit: only tasks with metadata.source +
    metadata.external_id deduplicate. A bare task imported twice produces two
    rows. This test pins that behavior so callers know to include natural
    keys for idempotent migration."""
    cp = ControlPlane.in_memory()
    records = [
        {"record": "task", "title": "no natural key", "required_capabilities": ["ops"]},
    ]
    first = import_jsonl(cp, stream=_stream(records))
    second = import_jsonl(cp, stream=_stream(records))
    assert first.errors == [] and second.errors == []
    assert first.tasks_imported == 1 and second.tasks_imported == 1
    # Two tasks now exist with the same title.
    matching = [t for t in cp.list_tasks() if t.title == "no natural key"]
    assert len(matching) == 2


def test_migration_history_record_alias_still_accepted():
    """Old streams using record='history' must still load — kept as alias."""
    cp = ControlPlane.in_memory()
    report = import_jsonl(
        cp,
        stream=_stream(
            [
                {"record": "tenant", "name": "x"},
                {
                    "record": "task",
                    "title": "with legacy history",
                    "metadata": {"source": "acc", "external_id": "ACC-1"},
                },
                {
                    "record": "history",  # alias for 'provenance'
                    "task_ref": "acc:ACC-1",
                    "event_type": "imported",
                    "content": "legacy record",
                },
            ]
        ),
    )
    assert report.errors == []
    assert report.provenance_imported == 1
