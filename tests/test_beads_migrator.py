"""Tests for the beads-to-MAC migrator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mac.beads_migrator import detect, migrate
from mac.models import TaskState
from mac.services import ControlPlane


def _write_beads_repo(repo: Path, issues: list[dict]) -> None:
    beads = repo / ".beads"
    beads.mkdir()
    lines = [json.dumps({"_type": "issue", **issue}) for issue in issues]
    (beads / "issues.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_detect_reports_zero_when_no_beads(tmp_path):
    report = detect(tmp_path)
    assert report.has_beads_dir is False
    assert report.has_issues_jsonl is False
    assert report.issue_count == 0


def test_detect_counts_open_and_closed(tmp_path):
    _write_beads_repo(
        tmp_path,
        [
            {"id": "x-1", "status": "open", "title": "first"},
            {"id": "x-2", "status": "closed", "title": "second"},
            {"id": "x-3", "status": "open", "title": "third"},
        ],
    )
    report = detect(tmp_path)
    assert report.has_beads_dir is True
    assert report.has_issues_jsonl is True
    assert report.issue_count == 3
    assert report.open_count == 2
    assert report.closed_count == 1


def test_migrate_creates_mac_tasks_with_origin_metadata(tmp_path):
    _write_beads_repo(
        tmp_path,
        [
            {
                "id": "x-1",
                "status": "open",
                "title": "open issue",
                "description": "needs doing",
                "priority": 1,
                "issue_type": "bug",
                "assignee": "alice",
                "created_at": "2026-05-01T00:00:00Z",
            },
            {
                "id": "x-2",
                "status": "closed",
                "title": "done issue",
                "priority": 0,
                "issue_type": "task",
                "closed_at": "2026-05-10T00:00:00Z",
                "close_reason": "shipped",
            },
        ],
    )
    cp = ControlPlane.in_memory()
    report = migrate(tmp_path, cp, project="testproj", emit_tickets=False)
    assert report.issues_migrated == 2
    assert report.issues_failed == 0

    tasks = cp.list_tasks(None)
    by_bead = {t.metadata.get("original_beads_id"): t for t in tasks}
    assert by_bead["x-1"].state == TaskState.OPEN.value
    assert by_bead["x-1"].title == "open issue"
    assert by_bead["x-1"].metadata["beads_type"] == "bug"
    assert by_bead["x-1"].priority == 1
    assert by_bead["x-2"].state == TaskState.COMPLETED.value
    assert by_bead["x-2"].metadata["beads_close_reason"] == "shipped"
    assert by_bead["x-2"].completed_at is not None


def test_migrate_is_idempotent(tmp_path):
    _write_beads_repo(
        tmp_path,
        [{"id": "x-1", "status": "open", "title": "single"}],
    )
    cp = ControlPlane.in_memory()
    first = migrate(tmp_path, cp, project="p", emit_tickets=False)
    second = migrate(tmp_path, cp, project="p", emit_tickets=False)
    assert first.issues_migrated == 1
    assert second.issues_migrated == 0
    assert second.issues_skipped_existing == 1
    assert len(cp.list_tasks(None)) == 1


def test_migrate_writes_ticket_files_with_wedow_frontmatter(tmp_path):
    _write_beads_repo(
        tmp_path,
        [
            {
                "id": "x-3",
                "status": "open",
                "title": "Make a thing",
                "description": "body text",
                "issue_type": "feature",
                "priority": 2,
                "created_at": "2026-05-01T00:00:00Z",
                "design": "design notes",
                "acceptance_criteria": "ac",
                "notes": "extra",
            }
        ],
    )
    cp = ControlPlane.in_memory()
    report = migrate(tmp_path, cp, project="p", emit_tickets=True)
    assert report.tickets_written == 1
    ticket = (tmp_path / ".tickets" / "x-3.md").read_text(encoding="utf-8")
    assert ticket.startswith("---\n")
    assert "id: x-3" in ticket
    assert "status: open" in ticket
    assert "type: feature" in ticket
    assert "priority: 2" in ticket
    assert "mac-task-id: task_" in ticket
    assert "# Make a thing" in ticket
    assert "## Design" in ticket
    assert "## Acceptance Criteria" in ticket
    assert "## Notes" in ticket


def test_migrate_dry_run_creates_nothing(tmp_path):
    _write_beads_repo(
        tmp_path,
        [{"id": "x-4", "status": "open", "title": "dry"}],
    )
    cp = ControlPlane.in_memory()
    report = migrate(tmp_path, cp, project="p", dry_run=True, emit_tickets=True)
    assert report.issues_migrated == 1
    assert report.tickets_written == 0
    assert len(cp.list_tasks(None)) == 0
    assert not (tmp_path / ".tickets").exists()


def test_migrate_memories_imports_under_project_subject(tmp_path):
    _write_beads_repo(tmp_path, [{"id": "x-1", "status": "open", "title": "t"}])
    cp = ControlPlane.in_memory()
    report = migrate(
        tmp_path,
        cp,
        project="p",
        emit_tickets=False,
        memories={
            "schema_version": 1,
            "first-rule": "always be closing",
            "second-rule": "never split the party",
        },
    )
    assert report.memories_migrated == 2
    records = cp.memory.search_memory(subject_type="project", subject_id="p")
    record_types = sorted(r.record_type for r in records)
    assert record_types == ["beads_memory:first-rule", "beads_memory:second-rule"]
    contents = {r.record_type: r.content for r in records}
    assert "always be closing" in contents["beads_memory:first-rule"]
