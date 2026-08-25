"""A queue entry must learn its pull request, and must not retry forever.

MEASURED on the live hub 2026-08-18:

    task_id       | pr | state  | attempts
    task_db792cc1 |  0 | tested |       70

Every entry carried pull_request_number = 0 -- the schema default -- and one
had been retried SEVENTY times in `tested` while its work had already merged
hours earlier as #404.

WHY IT COULD NEVER RESOLVE. `claim_slot` runs BEFORE the pull request is
opened, so the column starts at 0 and nothing ever wrote it. Everything the
queue does afterwards assumes a PR to look at: the already-merged observation
(#400's read-before-acting pattern), the land gate, the eviction path. An entry
that never learns its number can neither land nor be evicted; it accumulates
attempts and holds a slot forever, while `entries_testing` counts it as work in
flight -- the same lie `release()` was fixed to stop telling.
"""

from __future__ import annotations

import pytest

from mac.native_merge_queue import NativeMergeQueue
from mac.services import ControlPlane


@pytest.fixture()
def queue():
    cp = ControlPlane.in_memory()
    return NativeMergeQueue(cp.store)


def _admit(queue, task_id="task_a", pr=0):
    return queue.admit(
        repository="github.com/x/y",
        branch="main",
        task_id=task_id,
        head_sha="a" * 40,
        pull_request_number=pr,
    )


def test_an_entry_learns_its_pull_request_after_enrolment(queue):
    """The reported bug: claim_slot runs before the PR exists."""
    entry = _admit(queue)
    assert entry.pull_request_number == 0

    assert queue.record_pull_request(entry.id, 406) is True

    assert queue.entry(entry.id).pull_request_number == 406


def test_recording_the_same_number_twice_is_a_no_op(queue):
    """Idempotent: the landing path runs once per attempt."""
    entry = _admit(queue)
    queue.record_pull_request(entry.id, 406)

    queue.record_pull_request(entry.id, 406)

    assert queue.entry(entry.id).pull_request_number == 406


def test_an_entry_is_not_silently_repointed_at_another_pr(queue):
    """A second, DIFFERENT number means something is confused. Overwriting
    would aim the land gate at a PR this entry was never tested against."""
    entry = _admit(queue)
    queue.record_pull_request(entry.id, 406)

    assert queue.record_pull_request(entry.id, 999) is False
    assert queue.entry(entry.id).pull_request_number == 406


@pytest.mark.parametrize("bad", [0, -1])
def test_a_meaningless_number_is_refused(queue, bad):
    entry = _admit(queue)

    assert queue.record_pull_request(entry.id, bad) is False


# --------------------------------------------------------------------------
# the 70-attempt zombie
# --------------------------------------------------------------------------


def test_an_exhausted_entry_is_evicted_with_a_reason(queue):
    """70 attempts is not a bad day. Eviction rather than a silent skip, so
    `recent_evictions` says why a change stopped moving."""
    entry = _admit(queue)
    queue._store.execute(
        "UPDATE merge_queue_entries SET attempts = ? WHERE id = ?",
        (NativeMergeQueue.MAX_ATTEMPTS_BEFORE_EVICTION, entry.id),
    )

    evicted = queue.evict_exhausted("github.com/x/y", "main")

    assert entry.id in evicted
    after = queue.entry(entry.id)
    assert after.state == "evicted"
    assert "exhausted" in after.eviction_reason


def test_the_eviction_reason_names_a_missing_pull_request(queue):
    """The two failures compound: no PR number AND retried out. A reader needs
    to know which one to fix."""
    entry = _admit(queue)
    queue._store.execute(
        "UPDATE merge_queue_entries SET attempts = ? WHERE id = ?",
        (NativeMergeQueue.MAX_ATTEMPTS_BEFORE_EVICTION + 5, entry.id),
    )

    queue.evict_exhausted("github.com/x/y", "main")

    assert "no pull request recorded" in queue.entry(entry.id).eviction_reason


def test_an_entry_under_the_cap_is_left_alone(queue):
    entry = _admit(queue)
    queue._store.execute(
        "UPDATE merge_queue_entries SET attempts = ? WHERE id = ?",
        (NativeMergeQueue.MAX_ATTEMPTS_BEFORE_EVICTION - 1, entry.id),
    )

    assert queue.evict_exhausted("github.com/x/y", "main") == []
    assert queue.entry(entry.id).state != "evicted"


def test_an_entry_with_a_pr_still_evicts_but_says_so_differently(queue):
    entry = _admit(queue)
    queue.record_pull_request(entry.id, 406)
    queue._store.execute(
        "UPDATE merge_queue_entries SET attempts = ? WHERE id = ?",
        (NativeMergeQueue.MAX_ATTEMPTS_BEFORE_EVICTION, entry.id),
    )

    queue.evict_exhausted("github.com/x/y", "main")

    reason = queue.entry(entry.id).eviction_reason
    assert "exhausted" in reason
    assert "no pull request recorded" not in reason


# ---------------------------------------------------------------------------
# Saying so: an entry that cannot progress must not read as work in flight
# ---------------------------------------------------------------------------


def _tested_entry(queue, *, task_id, attempts, pr=0):
    entry = queue.admit(
        repository="r",
        branch="main",
        task_id=task_id,
        head_sha="a" * 40,
        pull_request_number=pr,
    )
    queue._store.execute(
        "UPDATE merge_queue_entries SET state = 'tested', attempts = ? WHERE id = ?",
        (attempts, entry.id),
    )
    return queue.entry(entry.id)


def test_an_entry_that_finished_an_attempt_without_a_pr_is_stalled(queue):
    """The seventy-attempt case, detected on the FIRST attempt instead."""
    entry = _tested_entry(queue, task_id="t_stalled", attempts=1)
    stalled = queue.stalled_entries("r", "main")
    assert [e.id for e in stalled] == [entry.id]


def test_an_entry_awaiting_its_first_attempt_is_not_stalled(queue):
    """The false positive that would evict healthy work.

    claim_slot runs BEFORE the agent opens the pull request, so an entry with
    no number and no completed attempt is normal and momentary. Treating "no
    PR" alone as stalled would evict every entry in that window -- worse than
    the bug, because it breaks work that was about to succeed.
    """
    _tested_entry(queue, task_id="t_fresh", attempts=0)
    assert queue.stalled_entries("r", "main") == []


def test_an_entry_with_a_pr_is_never_stalled(queue):
    _tested_entry(queue, task_id="t_ok", attempts=9, pr=455)
    assert queue.stalled_entries("r", "main") == []


def test_a_queued_entry_is_never_stalled(queue):
    """`queued` precedes the PR by construction, so it cannot be stuck for it."""
    queue.admit(repository="r", branch="main", task_id="t_q", head_sha="b" * 40)
    assert queue.stalled_entries("r", "main") == []


def test_the_snapshot_reports_stalled_apart_from_the_state_counts(queue):
    """entries_testing/tested answer "what state"; entries_stalled answers
    "is it going anywhere", and only the second is actionable."""
    entry = _tested_entry(queue, task_id="t_snap", attempts=3)
    snap = queue.snapshot("r", "main")
    assert snap["entries_tested"] == 1
    assert snap["entries_stalled"] == 1
    assert snap["stalled_entry_ids"] == [entry.id]


def test_a_stalled_entry_is_evicted_without_burning_the_attempt_cap(queue):
    """It will never learn its number, so making it retry twelve times first
    buys nothing and holds a slot for the whole window."""
    entry = _tested_entry(queue, task_id="t_evict", attempts=1)
    assert entry.attempts < queue.MAX_ATTEMPTS_BEFORE_EVICTION

    assert queue.evict_exhausted("r", "main") == [entry.id]

    after = queue.entry(entry.id)
    assert after.state == "evicted"
    assert "no pull request recorded" in after.eviction_reason
    assert "neither be landed nor observed as merged" in after.eviction_reason
    assert queue.snapshot("r", "main")["entries_stalled"] == 0
