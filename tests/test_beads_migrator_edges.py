"""Malformed-input and best-effort coverage for beads migration."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from mac import beads_migrator as beads
from mac.services import ControlPlane


def _repo(tmp_path, records):
    directory = tmp_path / ".beads"
    directory.mkdir()
    (directory / "issues.jsonl").write_text("\n".join(records) + "\n")


def test_migrate_contract_validation_and_missing_file(tmp_path) -> None:
    with pytest.raises(ValueError, match="requires emit_tickets"):
        beads.migrate(tmp_path, None, project="p", tickets_only=True, emit_tickets=False)
    with pytest.raises(ValueError, match="cp is required"):
        beads.migrate(tmp_path, None, project="p")
    report = beads.migrate(tmp_path, ControlPlane.in_memory(), project="p")
    assert report.errors and "no .beads/issues.jsonl" in report.errors[0]
    assert report.to_dict()["detected"]["issue_count"] == 0


def test_read_issues_skips_blank_invalid_nonobject_and_nonissue(tmp_path) -> None:
    path = tmp_path / "issues.jsonl"
    path.write_text('\nnot-json\n[]\n{"_type":"other"}\n{"_type":"issue","id":"one"}\n')
    assert beads._read_issues_jsonl(path) == [{"_type": "issue", "id": "one"}]


def test_migrate_records_lookup_create_and_memory_failures(monkeypatch, tmp_path) -> None:
    _repo(tmp_path, [
        json.dumps({"_type": "issue", "title": "missing"}),
        json.dumps({"_type": "issue", "id": "lookup", "title": "lookup"}),
        json.dumps({"_type": "issue", "id": "create", "title": "create"}),
    ])
    cp = ControlPlane.in_memory()

    def find(_cp, bead_id):
        if bead_id == "lookup":
            raise RuntimeError("lookup failed")
        return None

    monkeypatch.setattr(beads, "_find_task_by_beads_id", find)
    monkeypatch.setattr(beads, "_create_task_from_bead", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("create failed")))
    monkeypatch.setattr(beads, "_import_memory", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("memory failed")))
    report = beads.migrate(
        tmp_path, cp, project="p", emit_tickets=False, memories={"schema_version": "1", "rule": "value"}
    )
    assert report.issues_failed == 3
    assert report.memories_failed == 1
    assert len(report.errors) == 4


def test_existing_issue_writes_ticket_and_tickets_only_dry_run(monkeypatch, tmp_path) -> None:
    issue = {"_type": "issue", "id": "one", "title": "One", "status": "open"}
    _repo(tmp_path, [json.dumps(issue)])
    cp = ControlPlane.in_memory()
    monkeypatch.setattr(beads, "_find_task_by_beads_id", lambda *_a: SimpleNamespace(id="task"))
    report = beads.migrate(tmp_path, cp, project="p")
    assert report.issues_skipped_existing == 1 and report.tickets_written == 1
    report = beads.migrate(tmp_path, None, project="p", tickets_only=True, dry_run=True)
    assert report.issues_migrated == 1 and report.tickets_written == 0


def test_read_beads_memories_cli_failure_and_success(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(beads.shutil, "which", lambda *_a: None)
    assert beads.read_beads_memories_via_cli(tmp_path) == {}
    monkeypatch.setattr(beads.shutil, "which", lambda *_a: "/bd")
    monkeypatch.setattr(beads.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(OSError("missing")))
    assert beads.read_beads_memories_via_cli(tmp_path) == {}
    monkeypatch.setattr(beads.subprocess, "run", lambda *_a, **_k: subprocess.CompletedProcess([], 2, "", "bad"))
    assert beads.read_beads_memories_via_cli(tmp_path) == {}
    monkeypatch.setattr(beads.subprocess, "run", lambda *_a, **_k: subprocess.CompletedProcess([], 0, "bad", ""))
    assert beads.read_beads_memories_via_cli(tmp_path) == {}
    monkeypatch.setattr(beads.subprocess, "run", lambda *_a, **_k: subprocess.CompletedProcess([], 0, "[]", ""))
    assert beads.read_beads_memories_via_cli(tmp_path) == {}
    monkeypatch.setattr(
        beads.subprocess, "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, '{"schema_version":1,"rule":2,"text":"ok"}', ""),
    )
    assert beads.read_beads_memories_via_cli(tmp_path) == {"rule": "2", "text": "ok"}


def test_create_task_dry_run_priority_and_historical_state(tmp_path) -> None:
    cp = ControlPlane.in_memory()
    issue = {"id": "one", "status": "closed", "priority": "bad", "closed_at": "2026-01-01", "title": ""}
    assert beads._create_task_from_bead(cp, issue, project="p", actor="a", dry_run=True) is None
    task_id = beads._create_task_from_bead(cp, issue, project="p", actor="a", dry_run=False)
    task = cp.get_task(task_id)
    assert task.title == "Imported one"
    assert task.priority == 0
    assert task.state == "completed"
    assert beads._coerce_priority(None) == 0


def test_render_ticket_optional_sections_dependencies_and_yaml() -> None:
    issue = {
        "id": "one",
        "status": "closed",
        "title": "Title",
        "assignee": "alice",
        "external_ref": "github#1",
        "description": "description",
        "design": "design",
        "acceptance_criteria": "criteria",
        "notes": "notes",
        "close_reason": "done",
        "dependencies": [
            "bad",
            {"type": "blocks", "depends_on_id": "dep"},
            {"type": "related", "target": "link"},
            {"type": "parent-child", "target": "parent"},
            {"type": "parent-child", "target": "ignored"},
            {"type": "blocks"},
        ],
    }
    rendered = beads._render_ticket(issue, "task")
    assert "deps: [dep]" in rendered
    assert "links: [link]" in rendered
    assert "parent: parent" in rendered
    assert "assignee: alice" in rendered and "external-ref: github#1" in rendered
    assert "## Close Reason" in rendered
    assert beads._yaml_list([]) == "[]"
