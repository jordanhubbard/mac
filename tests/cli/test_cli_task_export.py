"""Exporting a task, and reading its coding-CLI session.

The session is hidden from ordinary reads and available on demand: `mac task
show` and every dispatch read stay small, while the export carries everything
needed to hand a task to a summarising model or an embedding store.
"""

from __future__ import annotations

import io
import json
import sys

from mac.cli import main
from mac.test_support import control_plane_on, dsn_for


def _run(tmp_path, *args):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", dsn_for(tmp_path), "--json", *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, (json.loads(raw) if raw else None)


def _task_with_session(tmp_path):
    cp = control_plane_on(dsn_for(tmp_path))
    cp.create_project("mac", dispatch_paused=False)
    task = cp.create_task("do the thing", project="mac")
    cp.record_task_transcript(
        task.id,
        prompt="fix the allocator",
        response="I changed evaluate_pair",
        coding_agent="claude",
    )
    return cp, task


def test_export_carries_the_session(tmp_path):
    _cp, task = _task_with_session(tmp_path)

    rc, out = _run(tmp_path, "task", "export", task.id)

    assert rc in (None, 0)
    assert out["schema"] == "mac.task_export.v1"
    assert out["transcript"][0]["response"] == "I changed evaluate_pair"


def test_export_can_omit_the_session(tmp_path):
    _cp, task = _task_with_session(tmp_path)

    rc, out = _run(tmp_path, "task", "export", task.id, "--no-transcript")

    assert rc in (None, 0)
    assert "transcript" not in out


def test_export_writes_a_file_for_feeding_another_system(tmp_path):
    """The serialised form is meant to be handed onward -- to a summarising
    model, a commit, a vector store -- so writing it out is the common case."""
    _cp, task = _task_with_session(tmp_path)
    target = tmp_path / "task.json"

    rc, out = _run(tmp_path, "task", "export", task.id, "--output", str(target))

    assert rc in (None, 0)
    assert out["written"] == str(target)
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["transcript"][0]["prompt"] == "fix the allocator"


def test_transcript_reads_the_session_alone(tmp_path):
    _cp, task = _task_with_session(tmp_path)

    rc, out = _run(tmp_path, "task", "transcript", task.id)

    assert rc in (None, 0)
    assert out[0]["coding_agent"] == "claude"


def test_a_task_with_no_session_exports_cleanly(tmp_path):
    """Every task predating this feature has no transcript. Exporting one must
    produce an empty session, not an error."""
    cp = control_plane_on(dsn_for(tmp_path))
    cp.create_project("mac", dispatch_paused=False)
    task = cp.create_task("older work", project="mac")

    rc, out = _run(tmp_path, "task", "export", task.id)

    assert rc in (None, 0)
    assert out["transcript"] == []
