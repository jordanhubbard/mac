"""Tests for the beads-to-MAC migrator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mac.beads_migrator import detect, migrate
from mac.models import TaskState
from mac.services import ControlPlane

# imports relocated from test_beads_migrator_edges.py
import subprocess
from types import SimpleNamespace
from mac import beads_migrator as beads


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


def test_tickets_only_writes_files_without_db(tmp_path):
    _write_beads_repo(
        tmp_path,
        [
            {"id": "x-9", "status": "open", "title": "no db needed"},
            {"id": "x-10", "status": "closed", "title": "also no db"},
        ],
    )
    report = migrate(tmp_path, None, project="standalone", tickets_only=True)
    assert report.issues_migrated == 2
    assert report.tickets_written == 2
    assert report.issues_skipped_existing == 0
    files = sorted(p.name for p in (tmp_path / ".tickets").iterdir())
    assert files == ["x-10.md", "x-9.md"]
    body = (tmp_path / ".tickets" / "x-9.md").read_text(encoding="utf-8")
    assert "mac-task-id: pending:x-9" in body


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


# --- relocated from test_beads_migrator_edges.py (coverage companion folded in) ---

def _repo(tmp_path, records):
    directory = tmp_path / '.beads'
    directory.mkdir()
    (directory / 'issues.jsonl').write_text('\n'.join(records) + '\n')


def test_migrate_contract_validation_and_missing_file(tmp_path) -> None:
    with pytest.raises(ValueError, match='requires emit_tickets'):
        beads.migrate(tmp_path, None, project='p', tickets_only=True, emit_tickets=False)
    with pytest.raises(ValueError, match='cp is required'):
        beads.migrate(tmp_path, None, project='p')
    report = beads.migrate(tmp_path, ControlPlane.in_memory(), project='p')
    assert report.errors and 'no .beads/issues.jsonl' in report.errors[0]
    assert report.to_dict()['detected']['issue_count'] == 0


def test_read_issues_skips_blank_invalid_nonobject_and_nonissue(tmp_path) -> None:
    path = tmp_path / 'issues.jsonl'
    path.write_text('\nnot-json\n[]\n{"_type":"other"}\n{"_type":"issue","id":"one"}\n')
    assert beads._read_issues_jsonl(path) == [{'_type': 'issue', 'id': 'one'}]


def test_migrate_records_lookup_create_and_memory_failures(monkeypatch, tmp_path) -> None:
    _repo(tmp_path, [json.dumps({'_type': 'issue', 'title': 'missing'}), json.dumps({'_type': 'issue', 'id': 'lookup', 'title': 'lookup'}), json.dumps({'_type': 'issue', 'id': 'create', 'title': 'create'})])
    cp = ControlPlane.in_memory()

    def find(_cp, bead_id):
        if bead_id == 'lookup':
            raise RuntimeError('lookup failed')
        return None
    monkeypatch.setattr(beads, '_find_task_by_beads_id', find)
    monkeypatch.setattr(beads, '_create_task_from_bead', lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError('create failed')))
    monkeypatch.setattr(beads, '_import_memory', lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError('memory failed')))
    report = beads.migrate(tmp_path, cp, project='p', emit_tickets=False, memories={'schema_version': '1', 'rule': 'value'})
    assert report.issues_failed == 3
    assert report.memories_failed == 1
    assert len(report.errors) == 4


def test_existing_issue_writes_ticket_and_tickets_only_dry_run(monkeypatch, tmp_path) -> None:
    issue = {'_type': 'issue', 'id': 'one', 'title': 'One', 'status': 'open'}
    _repo(tmp_path, [json.dumps(issue)])
    cp = ControlPlane.in_memory()
    monkeypatch.setattr(beads, '_find_task_by_beads_id', lambda *_a: SimpleNamespace(id='task'))
    report = beads.migrate(tmp_path, cp, project='p')
    assert report.issues_skipped_existing == 1 and report.tickets_written == 1
    report = beads.migrate(tmp_path, None, project='p', tickets_only=True, dry_run=True)
    assert report.issues_migrated == 1 and report.tickets_written == 0


def test_read_beads_memories_cli_failure_and_success(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(beads.shutil, 'which', lambda *_a: None)
    assert beads.read_beads_memories_via_cli(tmp_path) == {}
    monkeypatch.setattr(beads.shutil, 'which', lambda *_a: '/bd')
    monkeypatch.setattr(beads.subprocess, 'run', lambda *_a, **_k: (_ for _ in ()).throw(OSError('missing')))
    assert beads.read_beads_memories_via_cli(tmp_path) == {}
    monkeypatch.setattr(beads.subprocess, 'run', lambda *_a, **_k: subprocess.CompletedProcess([], 2, '', 'bad'))
    assert beads.read_beads_memories_via_cli(tmp_path) == {}
    monkeypatch.setattr(beads.subprocess, 'run', lambda *_a, **_k: subprocess.CompletedProcess([], 0, 'bad', ''))
    assert beads.read_beads_memories_via_cli(tmp_path) == {}
    monkeypatch.setattr(beads.subprocess, 'run', lambda *_a, **_k: subprocess.CompletedProcess([], 0, '[]', ''))
    assert beads.read_beads_memories_via_cli(tmp_path) == {}
    monkeypatch.setattr(beads.subprocess, 'run', lambda *_a, **_k: subprocess.CompletedProcess([], 0, '{"schema_version":1,"rule":2,"text":"ok"}', ''))
    assert beads.read_beads_memories_via_cli(tmp_path) == {'rule': '2', 'text': 'ok'}


def test_create_task_dry_run_priority_and_historical_state(tmp_path) -> None:
    cp = ControlPlane.in_memory()
    issue = {'id': 'one', 'status': 'closed', 'priority': 'bad', 'closed_at': '2026-01-01', 'title': ''}
    assert beads._create_task_from_bead(cp, issue, project='p', actor='a', dry_run=True) is None
    task_id = beads._create_task_from_bead(cp, issue, project='p', actor='a', dry_run=False)
    task = cp.get_task(task_id)
    assert task.title == 'Imported one'
    assert task.priority == 0
    assert task.state == 'completed'
    assert beads._coerce_priority(None) == 0


def test_render_ticket_optional_sections_dependencies_and_yaml() -> None:
    issue = {'id': 'one', 'status': 'closed', 'title': 'Title', 'assignee': 'alice', 'external_ref': 'github#1', 'description': 'description', 'design': 'design', 'acceptance_criteria': 'criteria', 'notes': 'notes', 'close_reason': 'done', 'dependencies': ['bad', {'type': 'blocks', 'depends_on_id': 'dep'}, {'type': 'related', 'target': 'link'}, {'type': 'parent-child', 'target': 'parent'}, {'type': 'parent-child', 'target': 'ignored'}, {'type': 'blocks'}]}
    rendered = beads._render_ticket(issue, 'task')
    assert 'deps: [dep]' in rendered
    assert 'links: [link]' in rendered
    assert 'parent: parent' in rendered
    assert 'assignee: alice' in rendered and 'external-ref: github#1' in rendered
    assert '## Close Reason' in rendered
    assert beads._yaml_list([]) == '[]'
