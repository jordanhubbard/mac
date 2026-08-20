"""The five distinct things MAC used to call "retry".

MAC had exactly one retry concept -- run the same task again -- so a task that
failed *because its scope was wrong* was re-dispatched against the scope its
own failure evidence had already proven insufficient, and emitted another
divergent pull request. horde-claw-fleet ADR-0121 findings 1 and 2 name the
distinction:

    A stale retry still owned only runner/src/cost.rs and
    runner/tests/test_telemetry_emission.rs even though the prior failure
    evidence already proved that scope was insufficient.

    For scoped work, the next action should have been a revised task or graph
    node, not an in-place auto-retry.

The five kinds, and the only thing that actually distinguishes them -- whether
the same scope gets dispatched again:

===========================  ==================  ==========================
kind                         re-dispatch scope?  when
===========================  ==================  ==========================
task_retry                   yes, unchanged      transient failure
validation_self_repair       no (never left)     failure inside declared
                                                 scope, attempt still live
graph_amendment              no, revised scope   the scope was wrong
supersession                 no, someone else's  a replacement landed
terminal_reconciliation      no, never re-run    the work already exists
===========================  ==================  ==========================

This module is pure decision logic over an already-classified failure. It maps
:class:`~mac.attempt_failure_classifier.AttemptFailureClass` onto a kind and
emits the lineage the next row must carry, so a caller cannot record a scope
amendment without recording what it amends.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any, Dict, NamedTuple, Optional

from mac.attempt_failure_classifier import AttemptFailureClass


JsonDict = Dict[str, Any]


class RetryKind(str, Enum):
    """The five outcomes ADR-0121 separates."""

    TASK_RETRY = "task_retry"
    VALIDATION_SELF_REPAIR = "validation_self_repair"
    GRAPH_AMENDMENT = "graph_amendment"
    SUPERSESSION = "supersession"
    TERMINAL_RECONCILIATION = "terminal_reconciliation"


#: Kinds for which dispatching the identical scope again is the correct action.
#: Every other kind must produce a revised scope, a pointer to someone else's
#: work, or a record and no run at all.
REDISPATCHES_SAME_SCOPE = frozenset({RetryKind.TASK_RETRY.value})

#: Kinds that are not retries at all -- no new attempt on this row is created.
NON_RETRY_KINDS = frozenset(
    {
        RetryKind.VALIDATION_SELF_REPAIR.value,
        RetryKind.SUPERSESSION.value,
        RetryKind.TERMINAL_RECONCILIATION.value,
    }
)


class RetryDecision(NamedTuple):
    """What to do next after an attempt failed.

    ``redispatch_same_scope`` is the safety-relevant field: only
    :attr:`RetryKind.TASK_RETRY` may set it, and every consumer that re-queues
    work must branch on it rather than on "did the task fail".
    ``lineage`` is the record the successor row must carry; it is empty exactly
    when no successor row is created.
    """

    kind: str
    redispatch_same_scope: bool
    reason: str
    lineage: JsonDict

    @property
    def creates_successor(self) -> bool:
        return bool(self.lineage)

    def to_dict(self) -> JsonDict:
        return {
            "kind": self.kind,
            "redispatch_same_scope": self.redispatch_same_scope,
            "reason": self.reason,
            "lineage": dict(self.lineage),
        }


def _text(value: Any) -> str:
    return str(value or "").strip()


def decide_retry_kind(
    *,
    task_id: str,
    failure_class: str = AttemptFailureClass.WORK.value,
    terminal_evidence: Optional[Mapping[str, Any]] = None,
    attempts_remaining: int = 0,
    replacement_succeeded: bool = False,
    within_declared_scope: bool = False,
) -> RetryDecision:
    """Choose which of the five kinds applies.

    The order of the branches is the precedence order and is not arbitrary:

    1. terminal evidence plus a landed replacement is a *supersession* -- this
       is ADR-0121's ``decide_retry_success_supersession``;
    2. terminal evidence alone is *terminal reconciliation*: record what
       happened, do not re-run, because the work exists;
    3. an explicitly superseded failure class is a supersession;
    4. a live attempt failing inside its own declared scope is *self-repair*,
       which is not a retry -- the attempt never ended;
    5. a scope failure is a *graph amendment*: the successor must carry a
       revised scope, so the same scope is never re-dispatched;
    6. anything else with attempts left is an ordinary *task retry*;
    7. with no attempts left and no other signal, reconcile.

    ``terminal_evidence`` is the mapping produced by
    :meth:`mac.terminal_evidence.TerminalEvidence.to_dict` (or any mapping with
    a truthy ``present``).
    """

    task_id = _text(task_id)
    terminal = dict(terminal_evidence or {})
    terminal_present = bool(terminal.get("present"))
    failure_class = _text(failure_class).lower() or AttemptFailureClass.WORK.value

    if terminal_present and replacement_succeeded:
        return RetryDecision(
            RetryKind.SUPERSESSION.value,
            False,
            "a replacement landed and %s carries terminal evidence (%s); "
            "supersede the prior failure rather than re-running it"
            % (task_id or "the prior task", _text(terminal.get("kind")) or "unknown"),
            {"superseded_task_id": task_id} if task_id else {},
        )
    if terminal_present:
        return RetryDecision(
            RetryKind.TERMINAL_RECONCILIATION.value,
            False,
            "terminal evidence exists (%s); record what happened and do not "
            "re-run" % (_text(terminal.get("kind")) or "unknown"),
            {},
        )
    if failure_class == AttemptFailureClass.SUPERSEDED.value:
        return RetryDecision(
            RetryKind.SUPERSESSION.value,
            False,
            "the failure was classified superseded; the work belongs to "
            "another row",
            {"superseded_task_id": task_id} if task_id else {},
        )
    if within_declared_scope and attempts_remaining > 0:
        return RetryDecision(
            RetryKind.VALIDATION_SELF_REPAIR.value,
            False,
            "the failure is inside the declared scope of a live attempt; "
            "repair in place -- this is not a retry",
            {},
        )
    if failure_class == AttemptFailureClass.SCOPE.value:
        return RetryDecision(
            RetryKind.GRAPH_AMENDMENT.value,
            False,
            "prior failure evidence proved the declared scope insufficient; "
            "amend the graph with a revised scope instead of re-dispatching "
            "the same one",
            {"amends_task_id": task_id} if task_id else {},
        )
    if attempts_remaining > 0:
        return RetryDecision(
            RetryKind.TASK_RETRY.value,
            True,
            "%s failure with %d attempt(s) remaining; the declared scope is "
            "still believed correct" % (failure_class, attempts_remaining),
            {"retry_of_task_id": task_id} if task_id else {},
        )
    return RetryDecision(
        RetryKind.TERMINAL_RECONCILIATION.value,
        False,
        "attempts are exhausted and no replacement exists; record the "
        "terminal outcome",
        {},
    )


def decide_retry_success_supersession(
    *,
    prior_task_id: str,
    prior_terminal_evidence: Optional[Mapping[str, Any]] = None,
    replacement_task_id: str = "",
    replacement_pull_request: str = "",
    replacement_succeeded: bool = False,
) -> Optional[JsonDict]:
    """Fire ``SupersedePriorFailure`` when a replacement landed.

    ADR-0121 pairs lineage with this decision: a replacement that succeeded,
    against a prior that carries terminal evidence, means the prior is
    superseded -- not retried, not failed again. Returns the supersession
    record to apply, or ``None`` when the preconditions do not hold.

    The replacement may be named by task id *or* by merged pull request. The
    pull-request form is the common case for operator work and used to be
    inexpressible, so it ended up in free text.
    """

    prior_task_id = _text(prior_task_id)
    replacement_task_id = _text(replacement_task_id)
    replacement_pull_request = _text(replacement_pull_request)
    if not prior_task_id:
        return None
    if not replacement_succeeded:
        return None
    if not (replacement_task_id or replacement_pull_request):
        return None
    if not dict(prior_terminal_evidence or {}).get("present"):
        return None
    record: JsonDict = {
        "action": "SupersedePriorFailure",
        "superseded_task_id": prior_task_id,
        "terminal_evidence_kind": _text(
            dict(prior_terminal_evidence or {}).get("kind")
        ),
    }
    if replacement_task_id:
        record["replacement_task_id"] = replacement_task_id
    if replacement_pull_request:
        record["replacement_pull_request"] = replacement_pull_request
    return record
