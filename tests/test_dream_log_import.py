"""The dream-log importer must merge substantive gateway dream reports into the
durable memory store, idempotently, skipping empty reports."""

from __future__ import annotations

import json

from mac import dream_log_import
from mac.services import ControlPlane

_SUBSTANTIVE = """# Dream Cycle Report — 2026-07-10 08:01
Analyzed 42 messages across 12 sessions (last 30 days)

## Error Patterns
Repeated rc124 executor timeouts on the contract gate (7 occurrences).

## jkh Corrections
Corrected the hub prune column from created_at to timestamp.

## Skills Near Failures
codegraph audit failed twice before the impacted-tests fix.

## High-Confidence Action Items
Adopt the impact-scoped gate for the contract suite.
"""

_EMPTY = """# Dream Cycle Report — 2026-07-05 07:21
Analyzed 11 messages across 8 sessions (last 30 days)

## Error Patterns
No error patterns detected.

## jkh Corrections
No corrections detected.

## Skills Near Failures
No skill-failure correlations detected.

## High-Confidence Action Items
No high-confidence actions this cycle.
"""


def _write_logs(tmp_path):
    d = tmp_path / "dream_logs"
    d.mkdir()
    (d / "dream_20260705_072144.md").write_text(_EMPTY, encoding="utf-8")
    (d / "dream_20260710_080115.md").write_text(_SUBSTANTIVE, encoding="utf-8")
    return d


def test_parse_and_empty_detection():
    parsed = dream_log_import.parse_dream_report(_SUBSTANTIVE)
    assert parsed["generated_at"] == "2026-07-10 08:01"
    assert "Error Patterns" in parsed["sections"]
    assert dream_log_import.report_is_empty(dream_log_import.parse_dream_report(_EMPTY))
    assert not dream_log_import.report_is_empty(parsed)


def test_import_merges_substantive_skips_empty(tmp_path):
    cp = ControlPlane.in_memory()
    d = _write_logs(tmp_path)
    report = dream_log_import.import_dream_logs(cp, dream_logs_dir=d, agent_id="rocky")
    assert report["scanned"] == 2
    assert report["imported"] == 1
    assert report["skipped_empty"] == 1
    assert report["errors"] == []

    rows = cp.store.query_all(
        "SELECT content, subject_id FROM memory_records WHERE record_type = ?",
        (dream_log_import.IMPORTED_RECORD_TYPE,),
    )
    assert len(rows) == 1
    payload = json.loads(rows[0]["content"])
    assert payload["source_file"] == "dream_20260710_080115.md"
    assert payload["source"] == "hermes_dream_logs"
    assert "content_hash" in payload
    assert rows[0]["subject_id"] == "rocky"


def test_import_is_idempotent(tmp_path):
    cp = ControlPlane.in_memory()
    d = _write_logs(tmp_path)
    first = dream_log_import.import_dream_logs(cp, dream_logs_dir=d)
    assert first["imported"] == 1
    second = dream_log_import.import_dream_logs(cp, dream_logs_dir=d)
    assert second["imported"] == 0
    assert second["skipped_duplicate"] == 1
    rows = cp.store.query_all(
        "SELECT id FROM memory_records WHERE record_type = ?",
        (dream_log_import.IMPORTED_RECORD_TYPE,),
    )
    assert len(rows) == 1


def test_same_findings_different_timestamp_dedupe_to_one(tmp_path):
    # Hourly reports repeat identical findings with only a changed header/time.
    d = tmp_path / "dream_logs"
    d.mkdir()
    hour1 = _SUBSTANTIVE
    hour2 = _SUBSTANTIVE.replace(
        "2026-07-10 08:01", "2026-07-10 09:01"
    ).replace("Analyzed 42 messages across 12 sessions", "Analyzed 44 messages across 13 sessions")
    (d / "dream_20260710_080115.md").write_text(hour1, encoding="utf-8")
    (d / "dream_20260710_090036.md").write_text(hour2, encoding="utf-8")
    cp = ControlPlane.in_memory()
    report = dream_log_import.import_dream_logs(cp, dream_logs_dir=d)
    assert report["imported"] == 1
    assert report["skipped_duplicate"] == 1


def test_dry_run_writes_nothing(tmp_path):
    cp = ControlPlane.in_memory()
    d = _write_logs(tmp_path)
    report = dream_log_import.import_dream_logs(cp, dream_logs_dir=d, dry_run=True)
    assert report["imported"] == 1  # would import
    rows = cp.store.query_all(
        "SELECT id FROM memory_records WHERE record_type = ?",
        (dream_log_import.IMPORTED_RECORD_TYPE,),
    )
    assert len(rows) == 0


class _FakeWriter:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def embed_memory(self, memory_id, *, tier, created_by):
        self.calls.append((memory_id, tier, created_by))
        if self.fail:
            raise RuntimeError("qdrant down")


def test_import_embeds_each_memory_when_writer_provided(tmp_path):
    cp = ControlPlane.in_memory()
    d = _write_logs(tmp_path)
    writer = _FakeWriter()
    report = dream_log_import.import_dream_logs(cp, dream_logs_dir=d, vector_writer=writer)
    assert report["imported"] == 1
    assert report["embedded"] == 1
    assert len(writer.calls) == 1
    assert writer.calls[0][1] == "medium"  # MEDIUM tier


def test_embed_failure_keeps_the_memory(tmp_path):
    cp = ControlPlane.in_memory()
    d = _write_logs(tmp_path)
    report = dream_log_import.import_dream_logs(
        cp, dream_logs_dir=d, vector_writer=_FakeWriter(fail=True)
    )
    assert report["imported"] == 1
    assert report["embedded"] == 0
    assert any(e.get("phase") == "embed" for e in report["errors"])
    rows = cp.store.query_all(
        "SELECT id FROM memory_records WHERE record_type = ?",
        (dream_log_import.IMPORTED_RECORD_TYPE,),
    )
    assert len(rows) == 1  # memory persists even though embedding failed


def test_missing_directory_is_reported_not_raised(tmp_path):
    cp = ControlPlane.in_memory()
    report = dream_log_import.import_dream_logs(cp, dream_logs_dir=tmp_path / "nope")
    assert report["scanned"] == 0
    assert report["errors"]
