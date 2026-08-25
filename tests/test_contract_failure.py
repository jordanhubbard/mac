"""ADR 0022 applied to the contract gate.

In the 24 hours to 2026-08-20 the fleet failed 24 tasks and every one carried
the same diagnosis — a sentence true of every contract failure and diagnostic
of none, with a remediation ("commit and push everything") that was correct for
one cause and misleading for the rest.
"""

from __future__ import annotations

import pytest

from mac.contract_failure import (
    ContractFailureCause as Cause,
    classify_contract_failure,
)

# Verbatim from the failure record of task_45927341 on 2026-08-20.
REAL_PLANNED = (
    "planned adr 0017 router token accounting into six child tasks. no code was "
    "changed. evidence is in mac-evidence.json as plan_decomposed so the parent "
    "can block on these children."
)
REAL_HUB_DENIED = (
    "direct post to /tasks/.../children did not authenticate in this sandbox "
    "(mac_token / mac_worker_token unset; localhost 8789 refused)."
)


def test_the_dominant_real_failure_is_named(tmp_path=None):
    """The 24-of-24 case: an agent that planned instead of implementing."""
    assert classify_contract_failure(REAL_PLANNED).cause == (Cause.PLANNED_INSTEAD_OF_IMPLEMENTING)


def test_planning_failure_does_not_advise_committing_harder():
    """The old remediation told this agent to `git add -A` and push. It wrote
    no code; there was nothing to commit. Wrong advice is worse than none."""
    failure = classify_contract_failure(REAL_PLANNED)

    assert "git add -A" not in failure.remediation
    assert "scope" in failure.remediation.lower()


def test_a_hub_write_failure_is_an_environment_fault_not_a_task_fault():
    failure = classify_contract_failure(REAL_HUB_DENIED)

    assert failure.cause == Cause.HUB_WRITE_UNAVAILABLE
    assert "environment fault" in failure.remediation.lower()
    # And must not suggest handing the sandbox credentials.
    assert "do not widen" in failure.remediation.lower()


def test_untracked_files_still_gets_the_commit_everything_advice():
    """The one cause the old message WAS right about must keep its advice."""
    failure = classify_contract_failure("left untracked files in the worktree")

    assert failure.cause == Cause.UNTRACKED_FILES_LEFT
    assert "git add -A" in failure.remediation


def test_nothing_committed_is_distinct_from_untracked():
    """Different states: one wrote nothing, one wrote and half-committed."""
    assert classify_contract_failure("refusing to push").cause == Cause.NOTHING_COMMITTED


def test_a_rejected_push_is_not_a_missing_push():
    failure = classify_contract_failure("remote rejected the push: non-fast-forward")

    assert failure.cause == Cause.PUSH_REJECTED
    assert "rebase" in failure.remediation.lower()


def test_an_unmatched_failure_says_so_rather_than_guessing():
    """Silence and a confident wrong guess are both worse than admitting it."""
    failure = classify_contract_failure("something nobody has seen before")

    assert failure.cause == Cause.UNCLASSIFIED
    assert "output tail" in failure.remediation.lower()


def test_earlier_causes_win_when_signals_overlap():
    """An agent that wrote no code cannot also have left untracked files. The
    earliest stopping point is the real one."""
    both = REAL_PLANNED + " also there were untracked files and refusing to push"

    assert classify_contract_failure(both).cause == (Cause.PLANNED_INSTEAD_OF_IMPLEMENTING)


@pytest.mark.parametrize(
    "text",
    [
        REAL_PLANNED,
        REAL_HUB_DENIED,
        "untracked",
        "refusing to push",
        "rejected push non-fast-forward",
        "unknown",
    ],
)
def test_every_cause_carries_a_distinct_problem_and_remediation(text):
    """The point of naming a cause is that the advice differs. A classifier
    whose branches all said the same thing would pass every test above and
    still be useless."""
    failure = classify_contract_failure(text)

    assert failure.problem.strip()
    assert failure.remediation.strip()
    assert failure.problem != failure.remediation


def test_the_causes_are_not_all_the_same_advice():
    texts = [
        REAL_PLANNED,
        REAL_HUB_DENIED,
        "untracked",
        "refusing to push",
        "rejected push non-fast-forward",
        "unknown",
    ]
    remediations = {classify_contract_failure(t).remediation for t in texts}

    assert len(remediations) == len(texts), "causes must give distinct advice"
