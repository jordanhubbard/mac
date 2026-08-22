"""`mac admin merge-queue` end to end, against a real database.

The queue's recovery verbs existed in code with no way to reach them. When
mac#main went head-of-line blocked for three days the recovery available to an
operator was to merge the pull request by hand on GitHub -- which does not touch
the queue's state, so the queue stayed blocked afterwards.

These tests drive the real command line, because that is the surface the claim
is about. A ControlPlane method nobody can call from a terminal is the state
this change exists to leave behind.
"""

from __future__ import annotations

import io
import json
import sys

from mac.cli import main
from mac.services import ControlPlane
from mac.test_support import dsn_for, store_on

REPO = "github.invalid/acme/widgets"
BRANCH = "main"


def _run(tmp_path, *args):
    """Run `mac --db <tmp> <args>` and return (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = main(["--json", "--db", dsn_for(tmp_path), *args])
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return rc, out.getvalue().strip(), err.getvalue().strip()


def _ok(result):
    """Assert the command succeeded and return its JSON body.

    Takes the RESULT of ``_run`` rather than its arguments so every call site
    below spells ``_run(tmp_path, "admin", "merge-queue", <verb>)`` literally --
    which is what tests/cli/test_cli_coverage_gate.py scans for when it checks
    that no CLI subcommand ships without a test.
    """
    rc, raw, err = result
    assert rc == 0, err or raw
    return json.loads(raw) if raw else None


def _queue(tmp_path):
    cp = ControlPlane(store_on(dsn_for(tmp_path)), secret_key="k" * 32)
    return cp._native_merge_queue()


def _admit(tmp_path, task_id, head):
    return _queue(tmp_path).admit(
        repository=REPO, branch=BRANCH, task_id=task_id, head_sha=head
    )


def test_merge_queue_list_names_the_queues_that_exist(tmp_path):
    """There is no registry of queues -- a queue exists because something was
    admitted to it -- so without this an operator cannot name one to inspect."""
    _admit(tmp_path, "task_a", "a" * 40)

    listed = _ok(_run(tmp_path, "admin", "merge-queue", "list"))

    assert listed == [
        {
            "repository": REPO,
            "branch": BRANCH,
            "queue_depth": 1,
            "entries_total": 1,
        }
    ]


def test_merge_queue_show_answers_why_nothing_is_landing(tmp_path):
    entry = _admit(tmp_path, "task_a", "a" * 40)

    snapshot = _ok(_run(tmp_path, "admin", "merge-queue", "show", REPO))

    assert snapshot["queue_depth"] == 1
    assert snapshot["front"]["id"] == entry.id
    assert snapshot["front"]["task_id"] == "task_a"


def test_merge_queue_requeue_discards_the_result_and_keeps_the_place(tmp_path):
    """The usual verb for a stuck front: the change keeps its turn and is
    re-tested against the tree that exists now."""
    queue = _queue(tmp_path)
    front = _admit(tmp_path, "task_front", "a" * 40)
    behind = _admit(tmp_path, "task_behind", "b" * 40)
    queue._store.execute(
        """
        UPDATE merge_queue_entries
           SET state = 'tested', tested_base_tree = ?, tested_merge_tree = ?
         WHERE id = ?
        """,
        ("c" * 40, "d" * 40, front.id),
    )

    outcome = _ok(
        _run(
            tmp_path,
            "admin",
            "merge-queue",
            "requeue",
            front.id,
            "--reason",
            "tip moved under it",
            "--actor",
            "operator",
        )
    )

    assert outcome["changed"] is True
    after = _queue(tmp_path).entry(front.id)
    assert after.state == "queued"
    assert after.tested_base_tree == ""
    assert [e.id for e in _queue(tmp_path).live_entries(REPO, BRANCH)] == [
        front.id,
        behind.id,
    ]


def test_merge_queue_evict_records_who_did_it_and_why(tmp_path):
    """`recent_evictions` is where an operator reads why a change stopped
    moving; an eviction with no hand on it cannot be explained later."""
    entry = _admit(tmp_path, "task_stuck", "a" * 40)

    outcome = _ok(
        _run(
            tmp_path,
            "admin",
            "merge-queue",
            "evict",
            entry.id,
            "--reason",
            "abandoned, unblocking main",
            "--actor",
            "operator",
        )
    )

    assert outcome["changed"] is True
    after = _queue(tmp_path).entry(entry.id)
    assert after.state == "evicted"
    assert "abandoned, unblocking main" in after.eviction_reason
    assert "by operator" in after.eviction_reason


def test_merge_queue_reconcile_discards_a_stale_front_result(tmp_path):
    """The three-day block, recovered from a terminal.

    ``--canonical-tip-tree`` comes from a checkout because the hub has none;
    without it the sweep leaves results alone rather than guessing at the tip.
    """
    queue = _queue(tmp_path)
    front = _admit(tmp_path, "task_front", "a" * 40)
    queue._store.execute(
        """
        UPDATE merge_queue_entries
           SET state = 'tested', tested_base_tree = ?, tested_merge_tree = ?,
               pull_request_number = 608, attempts = 1
         WHERE id = ?
        """,
        ("c" * 40, "d" * 40, front.id),
    )

    report = _ok(
        _run(
            tmp_path,
            "admin",
            "merge-queue",
            "reconcile",
            REPO,
            "--canonical-tip-tree",
            "e" * 40,
            "--actor",
            "operator",
        )
    )

    assert report["invalidated"] == [front.id]
    assert report["actor"] == "operator"
    assert _queue(tmp_path).entry(front.id).state == "queued"


def test_merge_queue_reconcile_without_a_tip_tree_discards_nothing(tmp_path):
    """An unreadable tip is a refusal everywhere else in the queue. It is a
    refusal here too: the sweep still reclaims and evicts, but it will not
    throw away a test result on suspicion."""
    queue = _queue(tmp_path)
    front = _admit(tmp_path, "task_front", "a" * 40)
    queue._store.execute(
        """
        UPDATE merge_queue_entries
           SET state = 'tested', tested_base_tree = ?, tested_merge_tree = ?,
               pull_request_number = 608, attempts = 1
         WHERE id = ?
        """,
        ("c" * 40, "d" * 40, front.id),
    )

    report = _ok(_run(tmp_path, "admin", "merge-queue", "reconcile", REPO))

    assert report["invalidated"] == []
    assert _queue(tmp_path).entry(front.id).state == "tested"
