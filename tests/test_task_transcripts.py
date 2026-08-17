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


# ---------------------------------------------------------------------------
# Compression. A storage detail that must be invisible above the store.
#
# Measured rather than assumed, because Postgres already TOAST-compresses large
# TEXT with pglz and gets 2.8x for free. The question was what application
# compression ADDS: zlib-6 reached a net 1.4x over TOAST for 2ms, lzma-6 only
# 1.6x for 42ms. zlib won on that trade, not on raw ratio.
# ---------------------------------------------------------------------------


def test_a_round_trip_returns_exactly_what_went_in(cp, task):
    """The one property that matters: compression must not alter the text."""
    prompt = "fix the bug\n\twith tabs, ünicode, and \"quotes\"\n" * 50
    response = "I changed evaluate_pair.\n" * 200

    cp.record_task_transcript(task.id, prompt=prompt, response=response, stderr="warn")

    stored = cp.task_transcript(task.id)[0]
    assert stored["prompt"] == prompt
    assert stored["response"] == response
    assert stored["stderr"] == "warn"


def test_what_lands_on_disk_is_smaller_than_the_text(cp, task):
    """Otherwise the compression is decorative."""
    import zlib

    text = (
        "def evaluate_pair(task, agent):\n    return PairEvaluation(task.id)\n" * 400
    )
    cp.record_task_transcript(task.id, prompt=text, response=text)

    row = cp.store.query_one(
        "SELECT payload, compression FROM task_agent_transcripts WHERE task_id = ?",
        (task.id,),
    )
    assert row["compression"] == "zlib"
    assert len(bytes(row["payload"])) < len(text.encode("utf-8"))
    # And it is genuinely a zlib stream, not merely marked as one.
    assert zlib.decompress(bytes(row["payload"]))


def test_an_empty_session_round_trips(cp, task):
    cp.record_task_transcript(task.id)

    stored = cp.task_transcript(task.id)[0]

    assert stored["prompt"] == ""
    assert stored["response"] == ""


def test_a_row_written_before_compression_still_reads(cp, task):
    """compression='none' is what every pre-existing row says. Reading one must
    not blow up, or adding compression would make old transcripts unreadable."""
    cp.record_task_transcript(task.id, prompt="ask")
    with cp.store.transaction() as conn:
        conn.execute(
            "UPDATE task_agent_transcripts SET payload = NULL, compression = 'none' "
            "WHERE task_id = ?",
            (task.id,),
        )

    stored = cp.task_transcript(task.id)[0]

    assert stored["prompt"] == ""
    assert stored["id"]


def test_a_corrupt_payload_does_not_break_the_task(cp, task):
    """A transcript is a record of the work, not the work. An undecodable one
    must not make the task it belongs to unreadable."""
    cp.record_task_transcript(task.id, prompt="ask")
    with cp.store.transaction() as conn:
        conn.execute(
            "UPDATE task_agent_transcripts SET payload = ? WHERE task_id = ?",
            (b"not a zlib stream at all", task.id),
        )

    stored = cp.task_transcript(task.id)[0]

    assert "unreadable" in stored["stderr"]
    assert cp.export_task(task.id)["task"]["id"] == task.id


def test_the_digest_still_describes_the_original_text(cp, task):
    """Hashed before capping AND before compression, so it certifies what the
    CLI produced rather than what storage happened to do with it."""
    import hashlib

    text = "the model said this\n" * 100
    cp.record_task_transcript(task.id, prompt=text)

    stored = cp.task_transcript(task.id)[0]

    assert stored["prompt_sha256"] == (
        "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    )


# ---------------------------------------------------------------------------
# Vector indexing, wired BEFORE compression.
#
# The plaintext exists exactly once: at write time. Indexing later would mean
# reading every row back and inflating it again purely to feed the index --
# work that is free here because the text is already in hand.
# ---------------------------------------------------------------------------


class _RecordingWriter:
    def __init__(self, explode: bool = False):
        self.calls = []
        self.explode = explode

    def embed_transcript_turn(self, **kwargs):
        if self.explode:
            raise RuntimeError("qdrant is down")
        self.calls.append(kwargs)
        return 2


def test_a_turn_is_indexed_with_the_plaintext(cp, task):
    writer = _RecordingWriter()
    cp.vector_writer = writer

    cp.record_task_transcript(
        task.id, prompt="why is dispatch stuck", response="the gate refused"
    )

    assert len(writer.calls) == 1
    call = writer.calls[0]
    # Plain text, not bytes: indexing happens before the payload is compressed.
    assert call["prompt"] == "why is dispatch stuck"
    assert call["response"] == "the gate refused"
    assert isinstance(call["prompt"], str)


def test_the_indexed_turn_can_be_traced_back_to_its_row(cp, task):
    """A hit is only useful if it leads back to the task and the exact turn."""
    writer = _RecordingWriter()
    cp.vector_writer = writer

    cp.record_task_transcript(task.id, prompt="first")
    cp.record_task_transcript(task.id, prompt="second")

    stored = cp.task_transcript(task.id)
    assert [c["sequence"] for c in writer.calls] == [0, 1]
    assert [c["transcript_id"] for c in writer.calls] == [t["id"] for t in stored]
    assert all(c["task_id"] == task.id for c in writer.calls)


def test_a_broken_vector_store_does_not_lose_the_transcript(cp, task):
    """Postgres is the system of record and the index is a derived view that
    can be rebuilt from it. Losing the record to protect the index would be
    exactly backwards."""
    cp.vector_writer = _RecordingWriter(explode=True)

    result = cp.record_task_transcript(task.id, prompt="ask", response="answer")

    assert result["id"]
    stored = cp.task_transcript(task.id)[0]
    assert stored["prompt"] == "ask"
    assert stored["response"] == "answer"


def test_no_vector_store_configured_is_not_an_error(cp, task):
    """Most environments -- every test, every standalone hub -- have no qdrant."""
    assert getattr(cp, "vector_writer", None) is None

    cp.record_task_transcript(task.id, prompt="ask")

    assert cp.task_transcript(task.id)[0]["prompt"] == "ask"


# ---------------------------------------------------------------------------
# The EXECUTOR path.
#
# Everything above proves the column round-trips: the test hands
# `record_task_transcript` a `coding_agent=` and reads the same string back.
# That is a self-supplied assertion -- it never touches the code that writes
# real transcript rows, which is `executor_sandbox.run_audited_command`.
#
# The gap is not hypothetical. On the production hub all 197
# `task_agent_transcripts` rows have `coding_agent` and `model` empty, because
# executor_sandbox.py:351-352 reads them off the `opts` dict
# (`{"execution_kind", "timeout", "task"}`) instead of `opts["task"]["metadata"]`
# where the pinned agent/model actually live. Follow-up: task_8d701ea3.
# ---------------------------------------------------------------------------

import subprocess  # noqa: E402
import types  # noqa: E402
from pathlib import Path  # noqa: E402


def _drive_executor_transcript(monkeypatch, opts: dict) -> dict:
    """Run the REAL `executor_sandbox.run_audited_command` and return the
    payload it posts to the transcript endpoint.

    Only the two network seams and the subprocess are stubbed; the payload
    construction under test is untouched.
    """
    from mac import executor_sandbox

    posted: dict = {}

    def _capture(task_id, payload):
        posted.update(payload)

    monkeypatch.setattr(executor_sandbox, "post_task_transcript", _capture)
    monkeypatch.setattr(executor_sandbox, "post_command_audit", lambda *a, **k: None)
    monkeypatch.setattr(
        executor_sandbox,
        "_run_captured",
        lambda argv, cwd, timeout: types.SimpleNamespace(
            returncode=0, stdout="I fixed it", stderr=""
        ),
    )

    executor_sandbox.run_audited_command(
        ["claude", "-p", "fix the bug"], Path("."), "task_1", opts
    )
    assert posted, "run_audited_command posted no transcript at all"
    return posted


def test_executor_records_the_prompt_and_response_it_actually_ran(monkeypatch):
    """The executor seam is exercised, not just the storage layer."""
    posted = _drive_executor_transcript(
        monkeypatch,
        {"execution_kind": "task", "timeout": 60, "task": {"id": "task_1"}},
    )

    assert posted["prompt"] == "fix the bug"
    assert posted["response"] == "I fixed it"
    assert posted["returncode"] == 0


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN DEFECT, follow-up task_8d701ea3: executor_sandbox.py:351-352 reads "
        "coding_agent/model off the `opts` dict, whose keys are only "
        "{execution_kind, timeout, task}. The pinned values live at "
        "opts['task']['metadata'] (see services.py::_task_pinned_coding_model), so "
        "both resolve to None on every real run -- which is why all 197 rows on the "
        "production hub have both columns empty. STRICT: when the defect is fixed "
        "this test starts passing and pytest fails it, forcing this marker off."
    ),
)
def test_executor_populates_coding_agent_and_model_on_the_transcript(monkeypatch):
    """Attribution must survive the executor, not just the storage round-trip.

    `opts` here is byte-for-byte the shape the executor really passes
    (executor_sandbox.py:6343 -> `_invoke_agent` -> `runner(...)`), with a task
    whose metadata pins an agent and a model. Nothing in this test supplies
    `coding_agent` or `model` at the level the code under test reads them.
    """
    posted = _drive_executor_transcript(
        monkeypatch,
        {
            "execution_kind": "task",
            "timeout": 60,
            "task": {
                "id": "task_1",
                "metadata": {"coding_agent": "claude", "model": "claude-opus-4"},
            },
        },
    )

    assert posted["coding_agent"] == "claude"
    assert posted["model"] == "claude-opus-4"
