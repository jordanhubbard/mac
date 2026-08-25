"""Tests for the .tickets/<id>.md auto-emit mirror (parity-tickets-autoemit-01)."""

from __future__ import annotations

import pytest

from mac import tickets_mirror as tm

TASK = {
    "id": "task_abc123",
    "state": "open",
    "title": "Fix the thing",
    "description": "Do the fix.\nMore detail.",
    "dependencies": ["task_dep1"],
    "priority": 1,
    "created_at": "2026-06-10T00:00:00Z",
    "metadata": {"type": "bug"},
}


def test_render_ticket_frontmatter_and_body():
    out = tm.render_ticket(TASK)
    assert "id: task_abc123" in out
    assert "status: open" in out
    assert "deps: [task_dep1]" in out
    assert "type: bug" in out
    assert "priority: 1" in out
    assert "mac-task-id: task_abc123" in out
    assert "# Fix the thing" in out
    assert "Do the fix." in out


def test_render_ticket_close_reason():
    out = tm.render_ticket({**TASK, "state": "completed"}, close_reason="done well")
    assert "status: completed" in out
    assert "## Close Reason" in out
    assert "done well" in out


def test_emit_writes_when_tickets_dir_exists(tmp_path, monkeypatch):
    d = tmp_path / ".tickets"
    d.mkdir()
    monkeypatch.setattr(tm, "tickets_dir", lambda: d)
    monkeypatch.delenv("MAC_NO_TICKET_MIRROR", raising=False)
    path = tm.emit(TASK)
    assert path == d / "task_abc123.md"
    assert "id: task_abc123" in path.read_text(encoding="utf-8")
    # Idempotent: re-emitting identical content is a no-op.
    assert tm.emit(TASK) is None


def test_emit_noop_without_tickets_dir(monkeypatch):
    monkeypatch.setattr(tm, "tickets_dir", lambda: None)
    assert tm.emit(TASK) is None


def test_emit_respects_optout_env(tmp_path, monkeypatch):
    d = tmp_path / ".tickets"
    d.mkdir()
    monkeypatch.setattr(tm, "tickets_dir", lambda: d)
    monkeypatch.setenv("MAC_NO_TICKET_MIRROR", "1")
    assert tm.emit(TASK) is None
    assert not (d / "task_abc123.md").exists()


def test_emit_updates_on_status_change(tmp_path, monkeypatch):
    d = tmp_path / ".tickets"
    d.mkdir()
    monkeypatch.setattr(tm, "tickets_dir", lambda: d)
    monkeypatch.delenv("MAC_NO_TICKET_MIRROR", raising=False)
    tm.emit(TASK)
    updated = tm.emit({**TASK, "state": "completed"}, close_reason="shipped")
    assert updated is not None
    assert "status: completed" in (d / "task_abc123.md").read_text(encoding="utf-8")
