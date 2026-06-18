"""Tests for the ticketing connector abstraction (meta-tickets).

The connector is how beads stops being a read/write source (it's an
import-only connector) and how any future ticketing system plugs in.
"""

from __future__ import annotations

import json
from pathlib import Path

from mac import ticketing


def _beads_repo(tmp_path: Path, issues):
    (tmp_path / ".beads").mkdir()
    # Real beads jsonl tags each record with _type: "issue".
    rows = [{"_type": "issue", **i} for i in issues]
    (tmp_path / ".beads" / "issues.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    return tmp_path


def test_detect_empty_repo_no_conversion(tmp_path):
    d = ticketing.detect_ticketing(tmp_path)
    assert d.needs_conversion is False
    assert d.conversion_from is None


def test_detect_beads_without_tickets_flags_conversion(tmp_path):
    _beads_repo(tmp_path, [{"id": "b-1", "title": "Fix X", "status": "open"}])
    d = ticketing.detect_ticketing(tmp_path)
    assert d.needs_conversion is True
    assert d.conversion_from == "beads"
    assert "one-way import" in d.message


def test_detect_tickets_present_suppresses_conversion(tmp_path):
    # Foreign source present, but a local compatibility mirror already exists -> no conversion.
    _beads_repo(tmp_path, [{"id": "b-1", "title": "Fix X", "status": "open"}])
    (tmp_path / ".tickets").mkdir()
    (tmp_path / ".tickets" / "mac-1.md").write_text(
        "---\nid: mac-1\ntitle: Native\nstatus: open\n---\nbody\n", encoding="utf-8"
    )
    d = ticketing.detect_ticketing(tmp_path)
    assert d.needs_conversion is False


def test_native_connector_imports_frontmatter(tmp_path):
    (tmp_path / ".tickets").mkdir()
    (tmp_path / ".tickets" / "mac-7.md").write_text(
        "---\nid: mac-7\ntitle: Hello\nstatus: closed\npriority: 2\n---\nbody\n", encoding="utf-8"
    )
    tickets = ticketing.NativeTicketingConnector().import_tickets(tmp_path)
    assert len(tickets) == 1
    t = tickets[0]
    assert (t.id, t.title, t.state, t.priority, t.source) == ("mac-7", "Hello", "closed", 2, "native")


def test_beads_connector_is_import_only_not_writeback():
    beads = ticketing.BeadsImportConnector()
    assert beads.is_writeback is False
    assert beads.is_canonical is False
    # Lifecycle hooks are no-ops (beads is never a read/write source).
    mt = ticketing.MetaTicket(id="x", title="x")
    assert beads.on_task_claimed(mt, "agent") is None
    assert beads.on_task_closed(mt, "done") is None


def test_beads_connector_imports_meta_tickets(tmp_path):
    _beads_repo(tmp_path, [
        {"id": "b-1", "title": "Fix X", "status": "open", "description": "d"},
        {"id": "b-2", "summary": "Ship Y", "status": "closed"},
    ])
    tickets = ticketing.BeadsImportConnector().import_tickets(tmp_path)
    assert {t.id for t in tickets} == {"b-1", "b-2"}
    assert all(t.source == "beads" and t.external_id for t in tickets)


def test_beads_convert_one_way_writes_tickets(tmp_path):
    _beads_repo(tmp_path, [{"id": "b-1", "title": "Fix X", "status": "open"}])
    # tickets-only conversion (cp=None) — one-way, writes .tickets, no ledger.
    report = ticketing.BeadsImportConnector().convert(tmp_path, project="demo", cp=None)
    assert report["tickets_written"] >= 1
    assert (tmp_path / ".tickets").is_dir()
    # After conversion the native source exists → no further conversion offered.
    assert ticketing.detect_ticketing(tmp_path).needs_conversion is False


def test_available_connectors_native_first():
    names = [c.name for c in ticketing.available_connectors()]
    assert names[0] == "native"
    assert "beads" in names
    assert ticketing.available_connectors()[0].is_canonical is True
