"""Head-of-line recovery for mac's own merge queue.

MEASURED on the live hub, 2026-08-19 to 2026-08-22:

    depth=12 (11 queued + 1 tested at the front)
    last_event: "landed task_3141dce7..." at 2026-08-19T19:27:46
    landed_count=1, failure_count=211

For three days nothing landed. main advanced through GitHub pull requests, so
the front entry's ``tested_base_tree`` stopped matching the canonical tip and
``landing_is_safe`` refused it -- correctly; that gate is the one invariant this
module will not trade. What was missing is what happens NEXT. Nothing re-tested
the front, because re-testing is driven by the front's own publication loop and
that task was no longer retrying. Nothing evicted it either, because eviction is
also driven by a publication attempt reaching the land gate, and the eleven
entries behind it never got that far: each was deferred by the window every ~6
minutes, forever. An approved task sat reviewed for four hours and had to be
merged by hand.

The shape of the bug is worth naming, because it is not "a gate was wrong": the
queue had recovery paths for every entry EXCEPT the one whose failure blocks
everyone else. These tests pin the recovery for that entry, and pin that the
recovery cannot become a new way to throw away work that was about to land.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mac.native_merge_queue import (
    STATE_EVICTED,
    STATE_QUEUED,
    STATE_TESTED,
    NativeMergeQueue,
    WindowBounds,
)
from mac.services import ControlPlane

REPO = "github.invalid/acme/widgets"
BRANCH = "main"
OLD_TREE = "a" * 40
NEW_TREE = "b" * 40
IDLE_SECONDS = 5400


class Clock:
    """A hand-wound clock, in the exact format ``mac.models.utcnow`` emits."""

    def __init__(self) -> None:
        self._at = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> str:
        return self._at.isoformat(timespec="microseconds")

    def advance(self, seconds: float) -> None:
        self._at += timedelta(seconds=seconds)


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def queue(clock):
    cp = ControlPlane.in_memory()
    return NativeMergeQueue(
        cp.store,
        # ceiling=1 is the serial queue: exactly the live configuration, and
        # the one where a stuck front blocks absolutely everything.
        bounds=WindowBounds(floor=1, ceiling=1),
        lease_seconds=5400,
        front_idle_seconds=IDLE_SECONDS,
        now=clock,
    )


def _admit(queue, task_id: str, head: str = ""):
    return queue.admit(
        repository=REPO,
        branch=BRANCH,
        task_id=task_id,
        head_sha=head or (task_id + "0" * 40)[:40],
    )


def _claim(queue, task_id: str, head: str = "", owner: str = "hub-a"):
    return queue.claim_slot(
        repository=REPO,
        branch=BRANCH,
        task_id=task_id,
        head_sha=head or (task_id + "0" * 40)[:40],
        owner=owner,
    )


def _make_stale_front(queue, entry_id: str, *, tested_base_tree: str = OLD_TREE):
    """The observed state: `tested` against a tree main has moved off, with no
    lease left because the holder stopped renewing it.

    The pull request number matters -- an entry that never learned one is a
    DIFFERENT defect with its own recovery (it is evicted as unable to
    progress), and using it here would prove that rule rather than this one.
    """
    queue._store.execute(
        """
        UPDATE merge_queue_entries
           SET state = ?, tested_base_sha = ?, tested_base_tree = ?,
               tested_merge_tree = ?, lease_owner = NULL,
               lease_expires_at = NULL, attempts = 1, pull_request_number = 608
         WHERE id = ?
        """,
        (STATE_TESTED, "c" * 40, tested_base_tree, "d" * 40, entry_id),
    )


# ---------------------------------------------------------------------------
# 1. a stale result is discarded, not left to refuse the land forever
# ---------------------------------------------------------------------------


def test_a_front_tested_against_a_tree_main_moved_off_is_requeued(queue):
    """The three-day block, in four lines.

    Discarding the RESULT and not the entry is the whole point: the change was
    never the problem, only the tree it was measured against.
    """
    front = _admit(queue, "task_front")
    _make_stale_front(queue, front.id)

    report = queue.reconcile(REPO, BRANCH, canonical_tip_tree=NEW_TREE)

    assert report["invalidated"] == [front.id]
    after = queue.entry(front.id)
    assert after.state == STATE_QUEUED
    assert after.tested_base_tree == ""
    assert after.tested_merge_tree == ""
    # It kept its turn. Recovery must not reorder the queue.
    assert queue.front(REPO, BRANCH).id == front.id


def test_a_front_still_tested_against_the_current_tip_is_untouched(queue):
    """The gate would let this land. Recovery must not race it."""
    front = _admit(queue, "task_front")
    _make_stale_front(queue, front.id, tested_base_tree=NEW_TREE)

    report = queue.reconcile(REPO, BRANCH, canonical_tip_tree=NEW_TREE)

    assert report["invalidated"] == []
    assert queue.entry(front.id).state == STATE_TESTED


def test_an_unreadable_tip_discards_nothing(queue):
    """An empty tip tree is a refusal everywhere else in this module; it is a
    refusal here too. Guessing would throw away a good result."""
    front = _admit(queue, "task_front")
    _make_stale_front(queue, front.id)

    report = queue.reconcile(REPO, BRANCH, canonical_tip_tree="")

    assert report["invalidated"] == []
    assert queue.entry(front.id).state == STATE_TESTED


def test_a_front_being_tested_right_now_is_not_disturbed(queue):
    """A live lease is a worker inside a contract run.

    Its ``tested_base_tree`` legitimately differs from the tip while the run is
    in flight. Discarding it here would throw away up to 45 minutes of testing
    and hand the slot back for no reason.
    """
    claimed = _claim(queue, "task_front")
    assert claimed.admitted
    queue.record_tested(
        claimed.entry.id,
        owner="hub-a",
        base_sha="c" * 40,
        base_tree=OLD_TREE,
        merge_tree="d" * 40,
    )

    report = queue.reconcile(REPO, BRANCH, canonical_tip_tree=NEW_TREE)

    assert report["invalidated"] == []
    assert queue.entry(claimed.entry.id).state == STATE_TESTED


# ---------------------------------------------------------------------------
# 2. a front nothing is driving is evicted, so the queue drains
# ---------------------------------------------------------------------------


def test_an_abandoned_front_is_evicted_with_a_reason(queue, clock):
    front = _admit(queue, "task_dead")
    _make_stale_front(queue, front.id, tested_base_tree=NEW_TREE)
    clock.advance(IDLE_SECONDS + 1)

    report = queue.reconcile(REPO, BRANCH, canonical_tip_tree=NEW_TREE)

    assert report["idle_evicted"] == [front.id]
    after = queue.entry(front.id)
    assert after.state == STATE_EVICTED
    assert "not progressed" in after.eviction_reason
    assert "blocked" in after.eviction_reason


def test_a_front_that_only_needed_its_result_discarded_gets_a_full_window(
    queue, clock
):
    """Invalidation restarts the idle clock, on purpose.

    A front whose task IS still publishing needs one more attempt, not an
    eviction. Discarding its stale result and evicting it in the same sweep
    would make recovery indistinguishable from giving up.
    """
    front = _admit(queue, "task_front")
    _make_stale_front(queue, front.id)
    clock.advance(IDLE_SECONDS + 1)

    report = queue.reconcile(REPO, BRANCH, canonical_tip_tree=NEW_TREE)

    assert report["invalidated"] == [front.id]
    assert report["idle_evicted"] == []
    assert queue.entry(front.id).state == STATE_QUEUED


def test_a_fresh_front_is_never_evicted_for_idling(queue):
    front = _admit(queue, "task_front")

    report = queue.reconcile(REPO, BRANCH, canonical_tip_tree=NEW_TREE)

    assert report["idle_evicted"] == []
    assert queue.entry(front.id).state == STATE_QUEUED


def test_a_leased_front_is_never_evicted_for_idling(queue, clock):
    """The lease outranks the idle clock while it is live; once it expires the
    ordinary reclaim path, not this one, takes the slot back."""
    claimed = _claim(queue, "task_front")
    clock.advance(IDLE_SECONDS + 1)  # idle, but the lease runs to 5400s

    report = queue.reconcile(REPO, BRANCH, canonical_tip_tree=NEW_TREE)

    assert report["idle_evicted"] == []
    assert queue.entry(claimed.entry.id).state != STATE_EVICTED


# ---------------------------------------------------------------------------
# 3. the acceptance criterion: the queue drains and the change behind publishes
# ---------------------------------------------------------------------------


def test_the_queue_drains_and_the_entry_behind_can_publish(queue, clock):
    """The whole failure, start to finish, with no operator in it.

    Three entries, a serial window, a dead front. Before: the front refuses to
    land and the two behind defer forever -- which is exactly what four hours of
    an approved task's life looked like. After: the front is gone, the next
    change is at the head, and it gets a slot.
    """
    front = _admit(queue, "task_dead")
    second = _admit(queue, "task_next")
    _admit(queue, "task_third")
    _make_stale_front(queue, front.id)

    # Before: the change behind the block cannot get a slot, however often it
    # asks. This is the ~6-minute defer loop, reproduced.
    for _ in range(3):
        clock.advance(360)
        blocked = _claim(queue, "task_next", owner="hub-b")
        assert not blocked.admitted
        assert "this entry is #2 in line" in blocked.reason

    # One sweep discards the stale result; the next gives up on the front.
    queue.reconcile(REPO, BRANCH, canonical_tip_tree=NEW_TREE)
    clock.advance(IDLE_SECONDS + 1)
    report = queue.reconcile(REPO, BRANCH, canonical_tip_tree=NEW_TREE)
    assert report["idle_evicted"] == [front.id]

    # After: the queue moved, and the next change publishes on its own.
    assert queue.front(REPO, BRANCH).id == second.id
    admitted = _claim(queue, "task_next", owner="hub-b")
    assert admitted.admitted is True
    assert admitted.depth == 0  # tested against the real tip, not speculation
    assert queue.snapshot(REPO, BRANCH)["queue_depth"] == 2


# ---------------------------------------------------------------------------
# 4. entries that cannot progress, without evicting ones that can
# ---------------------------------------------------------------------------


def test_reconcile_evicts_an_entry_that_retried_past_the_cap(queue):
    entry = _admit(queue, "task_burned")
    queue._store.execute(
        "UPDATE merge_queue_entries SET attempts = ? WHERE id = ?",
        (NativeMergeQueue.MAX_ATTEMPTS_BEFORE_EVICTION, entry.id),
    )

    report = queue.reconcile(REPO, BRANCH)

    assert report["evicted"] == [entry.id]
    assert "exhausted after 12 attempts" in queue.entry(entry.id).eviction_reason


def test_reconcile_evicts_an_entry_that_never_learned_its_pull_request(queue):
    """The seventy-attempt zombie, caught by the sweep instead of by nothing."""
    entry = _admit(queue, "task_nopr")
    queue._store.execute(
        "UPDATE merge_queue_entries SET state = ?, attempts = 1 WHERE id = ?",
        (STATE_TESTED, entry.id),
    )

    report = queue.reconcile(REPO, BRANCH)

    assert report["evicted"] == [entry.id]
    assert "no pull request recorded" in queue.entry(entry.id).eviction_reason


def test_reconcile_does_not_evict_a_worker_mid_contract_run(queue):
    """The false positive that would be worse than the bug.

    ``claim_slot`` runs before the pull request is opened and the contract suite
    runs for the ~45 minutes in between. During that window the entry is in
    `testing`, has one attempt, and has no PR number -- indistinguishable from
    the zombie above by state alone. The lease is what tells them apart, and
    honouring it is why this sweep is safe to run on every publication attempt.
    """
    claimed = _claim(queue, "task_working")
    assert claimed.admitted
    assert queue.entry(claimed.entry.id).pull_request_number == 0

    report = queue.reconcile(REPO, BRANCH)

    assert report["evicted"] == []
    assert queue.entry(claimed.entry.id).state != STATE_EVICTED


def test_reconcile_reclaims_a_dead_workers_slot(queue, clock):
    claimed = _claim(queue, "task_crashed")
    clock.advance(5401)  # the lease expired; the hub holding it never came back

    report = queue.reconcile(REPO, BRANCH)

    assert report["reclaimed"] == 1
    assert queue.entry(claimed.entry.id).state == STATE_QUEUED


def test_a_quiet_queue_reports_no_change(queue):
    _admit(queue, "task_fine")

    report = queue.reconcile(REPO, BRANCH, canonical_tip_tree=NEW_TREE)

    assert report["changed"] is False
    assert report["queue_depth"] == 1
    assert report["front"]["task_id"] == "task_fine"


def test_reconciling_an_empty_queue_is_harmless(queue):
    report = queue.reconcile("github.invalid/never/seen", "release")

    assert report["changed"] is False
    assert report["queue_depth"] == 0
    assert report["front"] is None


# ---------------------------------------------------------------------------
# 5. eviction churn: the same commit, refused instead of retried forever
# ---------------------------------------------------------------------------


def test_a_re_admitted_commit_backs_off_after_an_eviction(queue, clock):
    """Observed: one task evicted dozens of times with the identical reason.

    The conflict was a property of the commit, so every re-admission reproduced
    it exactly -- halving the window again and spending an attempt that a
    landable change could have used.
    """
    first = _claim(queue, "task_churn")
    queue.evict(first.entry.id, reason="projected merge conflicts with the queue base")

    clock.advance(30)
    again = _claim(queue, "task_churn")

    assert again.admitted is False
    assert again.terminal is False
    assert again.defer_seconds > 0
    assert "evicted from this queue 1 time(s)" in again.reason
    assert "projected merge conflicts" in again.reason


def test_the_backoff_expires_and_the_commit_is_tried_again(queue, clock):
    """A first eviction may have been the queue's fault -- a predecessor that
    has since been evicted itself. Backing off is not giving up."""
    first = _claim(queue, "task_churn")
    queue.evict(first.entry.id, reason="projected merge conflicts with the queue base")

    clock.advance(NativeMergeQueue.READMISSION_BACKOFF_SECONDS + 1)

    assert _claim(queue, "task_churn").admitted is True


def test_a_commit_evicted_repeatedly_is_refused_terminally(queue, clock):
    for _ in range(NativeMergeQueue.MAX_EVICTIONS_PER_HEAD):
        clock.advance(NativeMergeQueue.READMISSION_BACKOFF_CEILING_SECONDS + 1)
        claimed = _claim(queue, "task_churn")
        assert claimed.admitted, "setup: the commit should still be re-admitted"
        queue.evict(
            claimed.entry.id, reason="projected merge conflicts with the queue base"
        )

    clock.advance(NativeMergeQueue.READMISSION_BACKOFF_CEILING_SECONDS + 1)
    refused = _claim(queue, "task_churn")

    assert refused.admitted is False
    assert refused.terminal is True
    assert refused.defer_seconds == 0
    assert "Rebase or re-review" in refused.reason
    assert refused.to_dict()["terminal"] is True


def test_a_new_commit_for_the_same_task_is_admitted_immediately(queue, clock):
    """The history is held against the COMMIT, not the task.

    A re-reviewed change is a different change with a different chance of
    landing; refusing it would block the fix for the very conflicts that got it
    evicted.
    """
    for _ in range(NativeMergeQueue.MAX_EVICTIONS_PER_HEAD):
        clock.advance(NativeMergeQueue.READMISSION_BACKOFF_CEILING_SECONDS + 1)
        claimed = _claim(queue, "task_churn")
        queue.evict(claimed.entry.id, reason="projected merge conflicts")

    assert _claim(queue, "task_churn", head="f" * 40).admitted is True


def test_a_live_entry_is_never_held_back_by_its_own_history(queue, clock):
    """Backoff applies to REJOINING the queue. An entry already in line keeps
    its slot -- otherwise a change that was evicted once could never be
    re-tested even while it sat at the front."""
    first = _claim(queue, "task_churn")
    queue.evict(first.entry.id, reason="projected merge conflicts")
    clock.advance(NativeMergeQueue.READMISSION_BACKOFF_SECONDS + 1)
    second = _claim(queue, "task_churn")
    assert second.admitted
    queue.release(second.entry.id, owner="hub-a")

    assert _claim(queue, "task_churn").admitted is True


# ---------------------------------------------------------------------------
# 6. the operator verbs
# ---------------------------------------------------------------------------


def test_requeue_discards_the_result_and_keeps_the_place_in_line(queue):
    front = _admit(queue, "task_front")
    _make_stale_front(queue, front.id)
    behind = _admit(queue, "task_behind")

    outcome = queue.requeue(front.id, reason="tip moved under it")

    assert outcome["changed"] is True
    assert outcome["from_state"] == STATE_TESTED
    after = queue.entry(front.id)
    assert after.state == STATE_QUEUED
    assert after.tested_base_tree == ""
    assert [entry.id for entry in queue.live_entries(REPO, BRANCH)] == [
        front.id,
        behind.id,
    ]


def test_requeueing_a_terminal_entry_changes_nothing(queue):
    entry = _admit(queue, "task_gone")
    queue.evict(entry.id, reason="conflicts")

    outcome = queue.requeue(entry.id, reason="operator")

    assert outcome["changed"] is False
    assert queue.entry(entry.id).state == STATE_EVICTED


def test_requeueing_an_unknown_entry_is_reported_not_raised(queue):
    assert queue.requeue("mergeq_nope", reason="operator") == {
        "changed": False,
        "reason": "entry not found",
    }


def test_queues_lists_what_an_operator_can_name(queue):
    """There is no registry of queues -- a queue exists because something was
    admitted to it -- so an operator cannot inspect one without this."""
    _admit(queue, "task_a")
    queue.admit(
        repository=REPO, branch="release", task_id="task_b", head_sha="e" * 40
    )
    evicted = queue.admit(
        repository="github.invalid/other/repo",
        branch=BRANCH,
        task_id="task_c",
        head_sha="f" * 40,
    )
    queue.evict(evicted.id, reason="conflicts")

    listed = queue.queues()

    assert {(item["repository"], item["branch"]) for item in listed} == {
        (REPO, BRANCH),
        (REPO, "release"),
        ("github.invalid/other/repo", BRANCH),
    }
    by_key = {(item["repository"], item["branch"]): item for item in listed}
    assert by_key[(REPO, BRANCH)]["queue_depth"] == 1
    # A queue whose only entry is terminal still exists, at depth zero.
    assert by_key[("github.invalid/other/repo", BRANCH)]["queue_depth"] == 0
    assert by_key[("github.invalid/other/repo", BRANCH)]["entries_total"] == 1


# ---------------------------------------------------------------------------
# 7. the same verbs, reachable from the control plane
# ---------------------------------------------------------------------------


@pytest.fixture()
def plane():
    return ControlPlane.in_memory()


def _plane_entry(plane, task_id: str = "task_a"):
    queue = plane._native_merge_queue()
    return queue, queue.admit(
        repository=REPO, branch=BRANCH, task_id=task_id, head_sha="a" * 40
    )


def test_the_control_plane_can_list_and_read_a_queue(plane):
    _plane_entry(plane)

    assert plane.list_merge_queues() == [
        {
            "repository": REPO,
            "branch": BRANCH,
            "queue_depth": 1,
            "entries_total": 1,
        }
    ]
    assert plane.merge_queue_snapshot(REPO, BRANCH)["queue_depth"] == 1


def test_the_control_plane_can_evict_and_names_who_did_it(plane):
    """`recent_evictions` is where an operator reads why a change stopped
    moving. An eviction with no hand on it is the one entry that cannot be
    explained later."""
    _, entry = _plane_entry(plane)

    outcome = plane.evict_merge_queue_entry(
        entry.id, reason="stale front, unblocking main", actor="operator"
    )

    assert outcome["changed"] is True
    reason = plane._native_merge_queue().entry(entry.id).eviction_reason
    assert "stale front, unblocking main" in reason
    assert "by operator" in reason


def test_the_control_plane_can_requeue(plane):
    queue, entry = _plane_entry(plane)
    _make_stale_front(queue, entry.id)

    outcome = plane.requeue_merge_queue_entry(entry.id, actor="operator")

    assert outcome["changed"] is True
    assert queue.entry(entry.id).state == STATE_QUEUED


def test_reading_the_queue_is_ordinary_visibility_and_changing_it_is_not():
    """`_required_scope` is the authorization gate (docs/authority-boundary.md).

    Depth and evictions are already on the observability console, so a read
    token can see them. Evicting and requeueing decide which changes reach the
    trunk and in what order -- that is a control-plane operation, and a `write`
    token that may file tasks has no business making it.
    """
    from mac.api import _required_scope

    assert _required_scope("GET", "/merge-queue") == "read"
    assert _required_scope("GET", "/merge-queue/entries") == "read"
    assert _required_scope("POST", "/merge-queue/reconcile") == "admin"
    assert _required_scope("POST", "/merge-queue/entries/mergeq_1/evict") == "admin"
    assert _required_scope("POST", "/merge-queue/entries/mergeq_1/requeue") == "admin"


def test_the_control_plane_can_run_the_sweep_on_demand(plane):
    """The verb exists for the state that made it necessary: a queue with no
    publication attempts left to drive its own recovery."""
    queue, entry = _plane_entry(plane)
    _make_stale_front(queue, entry.id)

    report = plane.reconcile_merge_queue(
        REPO, BRANCH, canonical_tip_tree=NEW_TREE, actor="operator"
    )

    assert report["invalidated"] == [entry.id]
    assert report["actor"] == "operator"
    assert queue.entry(entry.id).state == STATE_QUEUED
