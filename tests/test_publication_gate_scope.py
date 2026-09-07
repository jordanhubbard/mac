"""An approved task has to be able to publish.

Publication re-runs a repository contract gate on the projected current-main
merge, inside a sandbox, under MAC_HUB_VERIFY_TIMEOUT. That gate ran the WHOLE
suite -- about 45 minutes on this repository -- against a 1200-second cap. It
could not finish, so no approved task could publish at all: the gate was
killed, publication failed, it retried ~1200s later and failed again, and the
task sat in REVIEWING, approved and unpublished.

Measured on task_de42aa6c: review approved at 19:36:51, publication failed at
20:02:44, and again at 20:23:43.

The failure surfaced as a truncated CalledProcessError naming the openshell
argv, which reads like a sandbox fault. It is not one: `openshell sandbox
create ... -- <cmd>` works on that host. A day went into the wrong suspect.

The projected tree differs from the tree review already gated only by however
far main moved, so the changed-file selection is the honest question to ask of
it -- and it is the same question the review verifier asks, through the same
helper.
"""

from __future__ import annotations

import inspect

from mac import services


def test_publication_scopes_its_gate_the_way_review_does():
    """Hardcoding the full command is what made the cap unmeetable."""
    source = inspect.getsource(services.ControlPlane._publish_git_target_attempt)

    assert "_hub_review_test_command" in source, (
        "the projected-merge gate must scope its command like the review "
        "verifier; running the whole suite cannot finish inside "
        "MAC_HUB_VERIFY_TIMEOUT"
    )


def test_an_unreadable_diff_still_runs_everything():
    """Fail closed. If the projected diff cannot be read, the scoped question
    is unanswerable and the whole suite is the only honest gate."""
    source = inspect.getsource(services.ControlPlane._publish_git_target_attempt)

    assert "projected_changed = []" in source
    assert "except Exception" in source


def test_the_timeout_can_cover_the_work_it_gates():
    """A cap the work cannot meet is not a gate, it is an outage that reports
    itself as a gate failure. The scoped run alone takes ~15 minutes before
    clone, upload and dependency bootstrap."""
    source = inspect.getsource(services.ControlPlane._hub_verify_run_contract_test)

    assert '"2400"' in source, (
        "MAC_HUB_VERIFY_TIMEOUT's default must cover a scoped gate plus its setup; 1200s did not"
    )


def test_the_gate_failure_keeps_the_part_that_says_why():
    """Taking the first 500 characters spent all of them on the argv.

    `openshell sandbox create --no-auto-providers --policy ... --label ...` is
    itself about that long, so the part that says what happened -- "timed out
    after 2400 seconds", "returned non-zero exit status 3" -- was cut off every
    single time. Two debugging sessions ended on the same unfinished sentence,
    and one of them chased an OpenShell bug that did not exist.
    """
    from mac.merge_queue import _failure_excerpt

    argv = "Command '%s'" % (["/Users/jkh/.mac/bin/openshell", "sandbox", "create"] * 30)
    exc = RuntimeError("%s timed out after 2400 seconds" % argv)

    excerpt = _failure_excerpt(exc)

    assert "timed out after 2400 seconds" in excerpt
    assert "openshell" in excerpt


def test_a_short_failure_is_not_mangled():
    """Most failures are short enough to read whole; eliding them would add
    noise for nothing."""
    from mac.merge_queue import _failure_excerpt

    assert _failure_excerpt(RuntimeError("boom")) == "boom"


def test_the_publication_gate_also_runs_bootstrap_before_its_test_command():
    """The projected-merge gate reuses _hub_verify_run_contract_test, which
    needs bootstrap.command run before test.command in a fresh sandbox (see
    test_hub_verify_evidence_window.py). Confirmed live: mac-fleet-canary's
    review approved via hub_verify (which got the bootstrap fix), then
    publication immediately failed with the identical
    "full repository contract test failed" -- because this second call site
    curried the runner without threading bootstrap_command through, so the
    sandbox still had no venv for `.venv/bin/pytest`."""
    source = inspect.getsource(services.ControlPlane._publish_git_target_attempt)

    assert "_repository_contract_bootstrap_command_for_task" in source, (
        "the projected-merge gate's runner must supply bootstrap_command to "
        "_hub_verify_run_contract_test, or every repository whose "
        "test.command assumes a pre-built toolchain fails publication even "
        "after review approves it"
    )


def test_the_publish_attempt_reaps_stalled_merge_queue_entries():
    """evict_exhausted() must run on the same cadence as reconcile_front().

    An entry that wins a slot, tests clean, and then can never open a pull
    request (its branch has no commits against main because another entry
    already landed the same change) is invisible to claim_slot() -- that
    method only increments attempts, it never judges them. evict_exhausted()
    is the reaper built for exactly this, but it was defined in
    native_merge_queue.py and never called from anywhere in services.py, so
    it never ran and a stalled entry blocked the front of the queue forever.
    """
    source = inspect.getsource(services.ControlPlane._publish_git_target_attempt)

    assert "evict_exhausted" in source, (
        "the publish-attempt loop must call queue.evict_exhausted() before "
        "claim_slot(), or an entry that wins a slot and then can never open "
        "a pull request wedges the front of the queue forever"
    )


def test_the_chosen_gate_command_is_recorded():
    """The scoped and full commands take ~15 and ~45 minutes, and only one fits
    the timeout. A silent fallback to full is indistinguishable from a hang."""
    import inspect

    from mac import services

    source = inspect.getsource(services.ControlPlane._publish_git_target_attempt)

    assert "publication_gate_scope" in source
