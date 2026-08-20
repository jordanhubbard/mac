"""The five retry kinds, and the one field that separates them.

``redispatch_same_scope`` is the safety-relevant output: exactly one kind sets
it. A scope failure that re-dispatches its own proven-insufficient scope is
ADR-0121 finding 2, and it is what made a MAC retry re-emit a divergent pull
request.
"""

from __future__ import annotations

from mac.attempt_failure_classifier import AttemptFailureClass
from mac.retry_kinds import (
    NON_RETRY_KINDS,
    REDISPATCHES_SAME_SCOPE,
    RetryKind,
    decide_retry_kind,
    decide_retry_success_supersession,
)


TASK_ID = "task_" + "a" * 32
REPLACEMENT_ID = "task_" + "b" * 32
MERGED_PR = "https://github.com/example/mac/pull/498"
TERMINAL = {"present": True, "kind": "merged_pull_request"}


def test_only_task_retry_redispatches_the_same_scope():
    assert REDISPATCHES_SAME_SCOPE == {RetryKind.TASK_RETRY.value}
    assert RetryKind.TASK_RETRY.value not in NON_RETRY_KINDS


def test_transient_work_failure_with_attempts_left_is_an_ordinary_retry():
    decision = decide_retry_kind(
        task_id=TASK_ID,
        failure_class=AttemptFailureClass.WORK.value,
        attempts_remaining=2,
    )
    assert decision.kind == RetryKind.TASK_RETRY.value
    assert decision.redispatch_same_scope is True
    assert decision.lineage == {"retry_of_task_id": TASK_ID}


def test_scope_failure_amends_the_graph_instead_of_rerunning_the_same_scope():
    decision = decide_retry_kind(
        task_id=TASK_ID,
        failure_class=AttemptFailureClass.SCOPE.value,
        attempts_remaining=2,
    )
    assert decision.kind == RetryKind.GRAPH_AMENDMENT.value
    # ADR-0121 finding 2: prior failure evidence already proved the scope
    # insufficient, so attempts remaining is not a licence to run it again.
    assert decision.redispatch_same_scope is False
    assert decision.lineage == {"amends_task_id": TASK_ID}
    assert decision.creates_successor


def test_failure_inside_a_live_declared_scope_is_self_repair_not_a_retry():
    decision = decide_retry_kind(
        task_id=TASK_ID,
        failure_class=AttemptFailureClass.WORK.value,
        attempts_remaining=1,
        within_declared_scope=True,
    )
    assert decision.kind == RetryKind.VALIDATION_SELF_REPAIR.value
    assert decision.redispatch_same_scope is False
    # The attempt never ended, so there is no successor row to create.
    assert not decision.creates_successor


def test_superseded_classification_is_a_supersession():
    decision = decide_retry_kind(
        task_id=TASK_ID,
        failure_class=AttemptFailureClass.SUPERSEDED.value,
        attempts_remaining=3,
    )
    assert decision.kind == RetryKind.SUPERSESSION.value
    assert decision.redispatch_same_scope is False


def test_terminal_evidence_alone_reconciles_and_never_reruns():
    decision = decide_retry_kind(
        task_id=TASK_ID,
        failure_class=AttemptFailureClass.WORK.value,
        terminal_evidence=TERMINAL,
        attempts_remaining=3,
    )
    assert decision.kind == RetryKind.TERMINAL_RECONCILIATION.value
    assert decision.redispatch_same_scope is False
    assert not decision.creates_successor


def test_terminal_evidence_outranks_a_scope_failure():
    # Both signals present: the work exists, so amending the graph would still
    # produce a second implementation of something already merged.
    decision = decide_retry_kind(
        task_id=TASK_ID,
        failure_class=AttemptFailureClass.SCOPE.value,
        terminal_evidence=TERMINAL,
        attempts_remaining=3,
    )
    assert decision.kind == RetryKind.TERMINAL_RECONCILIATION.value


def test_terminal_evidence_plus_a_landed_replacement_is_a_supersession():
    decision = decide_retry_kind(
        task_id=TASK_ID,
        terminal_evidence=TERMINAL,
        replacement_succeeded=True,
        attempts_remaining=3,
    )
    assert decision.kind == RetryKind.SUPERSESSION.value
    assert decision.lineage == {"superseded_task_id": TASK_ID}


def test_exhausted_attempts_reconcile_rather_than_looping():
    decision = decide_retry_kind(
        task_id=TASK_ID,
        failure_class=AttemptFailureClass.ENVIRONMENT.value,
        attempts_remaining=0,
    )
    assert decision.kind == RetryKind.TERMINAL_RECONCILIATION.value
    assert decision.redispatch_same_scope is False


def test_every_kind_but_task_retry_refuses_to_redispatch():
    seen = set()
    for kwargs in (
        {"failure_class": AttemptFailureClass.WORK.value, "attempts_remaining": 1},
        {"failure_class": AttemptFailureClass.SCOPE.value, "attempts_remaining": 1},
        {
            "failure_class": AttemptFailureClass.WORK.value,
            "attempts_remaining": 1,
            "within_declared_scope": True,
        },
        {"failure_class": AttemptFailureClass.SUPERSEDED.value, "attempts_remaining": 1},
        {"terminal_evidence": TERMINAL, "attempts_remaining": 1},
    ):
        decision = decide_retry_kind(task_id=TASK_ID, **kwargs)
        seen.add(decision.kind)
        assert decision.redispatch_same_scope == (
            decision.kind in REDISPATCHES_SAME_SCOPE
        )
    assert seen == {kind.value for kind in RetryKind}


def test_supersession_decision_fires_only_when_a_replacement_actually_landed():
    assert (
        decide_retry_success_supersession(
            prior_task_id=TASK_ID,
            prior_terminal_evidence=TERMINAL,
            replacement_task_id=REPLACEMENT_ID,
            replacement_succeeded=False,
        )
        is None
    )
    assert (
        decide_retry_success_supersession(
            prior_task_id=TASK_ID,
            prior_terminal_evidence={"present": False},
            replacement_task_id=REPLACEMENT_ID,
            replacement_succeeded=True,
        )
        is None
    )
    assert (
        decide_retry_success_supersession(
            prior_task_id=TASK_ID,
            prior_terminal_evidence=TERMINAL,
            replacement_succeeded=True,
        )
        is None
    )


def test_supersession_decision_accepts_a_merged_pull_request_as_the_replacement():
    record = decide_retry_success_supersession(
        prior_task_id=TASK_ID,
        prior_terminal_evidence=TERMINAL,
        replacement_pull_request=MERGED_PR,
        replacement_succeeded=True,
    )
    assert record == {
        "action": "SupersedePriorFailure",
        "superseded_task_id": TASK_ID,
        "terminal_evidence_kind": "merged_pull_request",
        "replacement_pull_request": MERGED_PR,
    }
