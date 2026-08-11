"""Keep what the coding CLI was asked and what it answered.

The executor invokes claude/codex/cursor headless with the whole prompt as one
argument, and kept only sha256(stdout), sha256(stderr) and two byte counts.
That proves an output existed and supports nothing else: no summary, no
knowledge base, no answering "why did the agent do that". Every task in the
ledger reads "llm: no attributed model calls recorded" for the same reason.

The prompt was doubly lost -- audit_safe_argv truncates any argument over 512
characters, a rule meant to keep secrets out of audit records that also removes
every real prompt.
"""

from __future__ import annotations

import pytest

from mac.services import ControlPlane


@pytest.fixture()
def cp():
    plane = ControlPlane.in_memory()
    plane.create_project("mac", dispatch_paused=False)
    return plane


@pytest.fixture()
def task(cp):
    return cp.create_task("do the thing", project="mac")


def test_a_turn_is_stored_and_read_back(cp, task):
    cp.record_task_transcript(
        task.id, prompt="fix the bug", response="I fixed it", coding_agent="claude"
    )

    turns = cp.task_transcript(task.id)

    assert len(turns) == 1
    assert turns[0]["prompt"] == "fix the bug"
    assert turns[0]["response"] == "I fixed it"
    assert turns[0]["coding_agent"] == "claude"


def test_turns_come_back_in_the_order_they_happened(cp, task):
    """A session read out of order is not a session."""
    cp.record_task_transcript(task.id, prompt="first")
    cp.record_task_transcript(task.id, prompt="second")
    cp.record_task_transcript(task.id, prompt="third")

    turns = cp.task_transcript(task.id)

    assert [t["prompt"] for t in turns] == ["first", "second", "third"]
    assert [t["sequence"] for t in turns] == [0, 1, 2]


def test_an_enormous_turn_is_capped_and_says_so(cp, task):
    """A whole repository pasted into a prompt would otherwise let a handful of
    tasks dominate the database -- which is how action_events reached 16GB and
    wedged the hub."""
    huge = "x" * (cp.TRANSCRIPT_FIELD_LIMIT + 5000)

    result = cp.record_task_transcript(task.id, prompt=huge, response="ok")

    assert result["truncated"] is True
    stored = cp.task_transcript(task.id)[0]
    assert len(stored["prompt"]) < len(huge)
    assert "truncated" in stored["prompt"]
    assert stored["truncated"] is True


def test_the_hash_describes_what_the_cli_produced_not_what_was_stored(cp, task):
    """Hashed before capping. A digest of the truncated text would certify the
    wrong artefact and quietly defeat the tamper-evidence."""
    import hashlib

    huge = "y" * (cp.TRANSCRIPT_FIELD_LIMIT + 5000)
    cp.record_task_transcript(task.id, prompt=huge)

    stored = cp.task_transcript(task.id)[0]

    assert stored["prompt_sha256"] == (
        "sha256:" + hashlib.sha256(huge.encode("utf-8")).hexdigest()
    )


def test_the_export_carries_the_session(cp, task):
    """The point of keeping transcripts is doing something with them: handing a
    task to a summarising model, committing the summary, embedding it."""
    cp.record_task_transcript(task.id, prompt="ask", response="answer")

    document = cp.export_task(task.id)

    assert document["task"]["id"] == task.id
    assert document["transcript"][0]["response"] == "answer"
    assert document["history"]


def test_the_export_can_leave_it_out(cp, task):
    """For the caller who wants the record without megabytes of session."""
    cp.record_task_transcript(task.id, prompt="ask", response="answer")

    document = cp.export_task(task.id, include_transcript=False)

    assert "transcript" not in document


def test_the_task_record_itself_stays_small(cp, task):
    """A child table, not columns on tasks: the tasks row is read constantly by
    dispatch, and inlining transcripts would put megabytes behind every
    allocator scan."""
    cp.record_task_transcript(task.id, prompt="x" * 10000, response="y" * 10000)

    record = cp.get_task(task.id).to_dict()

    assert "prompt" not in record
    assert "transcript" not in record


def test_transcripts_die_with_their_task(cp, task):
    """ON DELETE CASCADE: an orphaned transcript is unreachable bulk that no
    retention sweep keyed on tasks would ever find."""
    cp.record_task_transcript(task.id, prompt="ask")
    with cp.store.transaction() as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task.id,))

    rows = cp.store.query_all(
        "SELECT id FROM task_agent_transcripts WHERE task_id = ?", (task.id,)
    )

    assert rows == []


def test_recording_against_an_unknown_task_is_refused(cp):
    from mac.models import NotFoundError

    with pytest.raises(NotFoundError):
        cp.record_task_transcript("task_nope", prompt="ask")
