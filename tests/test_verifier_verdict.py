"""The repository gate must judge the task's work, not the sandbox's git history.

WHAT WENT WRONG

A read-only canary that changed nothing failed with "repository verifier exited
with status 1". The chain:

  1. the sandbox replaces the uploaded `.git` with a fresh single-commit repo
     (the host worktree's `.git` is a pointer into a host-only directory, and
     its credentials must not be copied in)
  2. the verifier rewrites its command to `run-sanity-tests.sh --base <SHA>`
     using the HOST's base SHA
  3. that SHA does not exist in the sandbox, so the selector answers
     mode=full / reason=selection_error
  4. run-sanity-tests.sh answers mode=full by exec'ing the whole contract gate
  5. the full suite needs Postgres, the sandbox has none, exit 1

So an unresolvable base quietly escalated "run the tests this task touched"
into "run everything", and a task that changed nothing was failed for it.

Three workers reported the underlying fact unprompted across four canaries:
the worktree HEAD is a squashed sandbox baseline commit, not the declared base.

WHAT CHANGED SINCE

Clearing the base prevented the bogus SHA from reaching the selector, but an
empty base IS mode=full, so the escalation this module describes kept
happening on every task. The base is now resolved to the sandbox's own
baseline commit -- the same pre-task state, under a name this repository can
resolve. tests/test_sandbox_baseline.py pins that behaviourally, against a
real git repository, so the assertion that used to live here (that the script
contains `_repo_base_sha = ""`) has been retired rather than rewritten: it
pinned the implementation of a fix that has been superseded.
"""

from __future__ import annotations

from mac.executor_sandbox import _sandbox_repository_verification_shell


def _script() -> str:
    return _sandbox_repository_verification_shell(
        {
            "MAC_TASK_WORKSPACE": "/workspace/repo",
            "MAC_REPO_TEST_COMMAND": "scripts/run-contract-tests.sh",
        }
    )


def test_a_clean_tree_is_not_reported_as_a_pass():
    """A clean ``git status`` means the agent committed, not that nothing changed.

    The shortcut that answered a clean tree with ``status: pass, skipped: true``
    recorded 265 test passes over 90 days where no test ran -- the largest
    single source of worker "passes" the hub's second run then rejected. Only a
    HEAD that never left the uploaded baseline proves the task changed nothing.
    """
    script = _script()

    assert "no repository changes to verify" not in script
    assert "_no_changes" not in script
    # The one remaining skip -- HEAD never moved off the uploaded baseline --
    # is recorded as skipped, never as a pass.
    _head, _sep, tail = script.partition("elif _unchanged_baseline:")
    assert tail, "the unchanged-baseline branch is missing"
    branch = tail.split("else:", 1)[0]
    assert '"status": "skipped"' in branch
    assert '"status": "pass"' not in branch
    assert "rev-parse" in script and "HEAD" in script


def test_git_probes_do_not_inherit_stdin():
    """The lesson from the verifier hang: a subprocess given a stdin nobody
    will write to is a subprocess that can block forever."""
    script = _script()

    assert "cat-file" in script
    assert script.count("stdin=subprocess.DEVNULL") >= 2
