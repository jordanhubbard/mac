"""Pre-dispatch conflict prediction: don't dispatch work into a state that will
predictably conflict at land time.

## The problem this solves

``mac.merge_queue`` implements the optimistic-concurrency **validation phase**
at *land* time — the last line of defence that a completed branch must clear
before it fast-forwards onto the trunk (see ``src/mac/merge_queue.py``). That
gate is *fail-closed*: a conflict routes the task to integration instead of a
dirty merge.

But land time is the *end* of a task's life. By the time
``validate_projected_merge`` fires, an agent has already spent a full task
budget on a branch that was doomed before it started — because the base it was
authored on moved under it on the very lines it edits, or because another
in-flight task queued to land first touches the same regions. Nothing inspects
that at *dispatch* time, so the wasted work is only discovered late.

This module is the **symmetric, earlier** counterpart to the merge-queue gate: a
side-effect-free, *advisory* (fail-open) prediction, consulted when a candidate
task is about to be dispatched, that reports whether dispatching now would land
on a tip it already conflicts with — and against which files. It is a decision /
orchestration layer on top of the merge-queue conflict primitive, analogous to
how ``auto_land.decide_land`` wraps the land-time gate: it **reuses**
``mac.merge_queue.validate_projected_merge`` for the actual ``git merge-tree``
computation rather than reimplementing conflict detection.

## Fail policy

Unlike the land-time gate (fail-closed / block), the pre-dispatch gate defaults
to **advisory / fail-open**: an unresolvable ref or a merge-tree error is
*reported*, not raised, and does not by itself hard-block dispatch. A dispatcher
(``mac.dispatch`` ready-task selection / scheduler) can log the verdict, attach
it to task evidence, or *re-order* to prefer non-conflicting tasks — without the
authoritative fail-closed backstop (``merge_queue`` at land time) being bypassed.
The ``advisory`` flag is an explicit parameter, not hard-coded, so a caller may
opt into a fail-closed dispatch gate as a policy decision.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

from mac.merge_queue import validate_projected_merge

GitRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess"]


@dataclass(frozen=True)
class PredispatchVerdict:
    """Advisory pre-dispatch conflict prediction for a candidate task branch.

    ``would_conflict`` is the boolean signal a dispatcher acts on. ``base_ref``
    and ``topic_ref`` echo the candidate landing being predicted.
    ``conflicting_ref`` names *what* the topic conflicts with: ``""`` means the
    current ``base_ref`` tip (its base moved underneath it), otherwise the
    in-flight ref expected to land first. ``conflicted_files`` are the paths the
    underlying ``git merge-tree`` reported. ``advisory`` records the fail policy
    in force (``True`` == fail-open / warn, do not block). ``error`` carries the
    reason when a ref cannot be resolved or the merge computation failed.
    """

    would_conflict: bool
    base_ref: str
    topic_ref: str
    conflicting_ref: str = ""
    conflicted_files: List[str] = field(default_factory=list)
    advisory: bool = True
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "schema": "mac.predispatch_conflict.v1",
            "would_conflict": self.would_conflict,
            "base_ref": self.base_ref,
            "topic_ref": self.topic_ref,
            "conflicting_ref": self.conflicting_ref,
            "conflicted_files": list(self.conflicted_files),
            "advisory": self.advisory,
            "error": self.error,
        }


def check_predispatch_conflict(
    repo_dir: str,
    base_ref: str,
    topic_ref: str,
    *,
    in_flight_refs: Optional[Sequence[str]] = None,
    advisory: bool = True,
    git_runner: Optional[GitRunner] = None,
) -> PredispatchVerdict:
    """Predict, side-effect-free, whether dispatching ``topic_ref`` now would
    conflict with the current ``base_ref`` tip (and optionally with each of
    ``in_flight_refs``).

    Delegates the actual conflict computation to
    ``mac.merge_queue.validate_projected_merge`` — the ``git merge-tree
    --write-tree`` logic, the verdict shape, and the fail-closed error handling
    already exist and are tested there. No working tree is touched and nothing is
    committed.

    The current-tip check runs first: if the candidate already conflicts with
    ``base_ref``, that is reported (``conflicting_ref == ""``). Otherwise each of
    ``in_flight_refs`` is checked pairwise, in order, and the first predicted
    conflict is reported (``conflicting_ref`` set to that in-flight ref). When
    no conflict is predicted, a clean verdict is returned.

    Fail policy is governed by ``advisory``. The default (``advisory=True``) is
    fail-open: a ref/merge error from the underlying gate is surfaced in
    ``error`` but does *not* set ``would_conflict``, so a spurious/unresolvable
    ref does not block dispatch. With ``advisory=False`` the gate is fail-closed:
    an error is treated as a predicted conflict (``would_conflict=True``),
    mirroring the land-time gate.
    """
    base_verdict = validate_projected_merge(
        repo_dir, base_ref, topic_ref, git_runner=git_runner
    )
    if base_verdict.error:
        return PredispatchVerdict(
            would_conflict=not advisory,
            base_ref=base_ref,
            topic_ref=topic_ref,
            conflicting_ref="",
            advisory=advisory,
            error=base_verdict.error,
        )
    if not base_verdict.clean:
        return PredispatchVerdict(
            would_conflict=True,
            base_ref=base_ref,
            topic_ref=topic_ref,
            conflicting_ref="",
            conflicted_files=list(base_verdict.conflicted_files),
            advisory=advisory,
        )

    for other_ref in in_flight_refs or ():
        other_verdict = validate_projected_merge(
            repo_dir, other_ref, topic_ref, git_runner=git_runner
        )
        if other_verdict.error:
            return PredispatchVerdict(
                would_conflict=not advisory,
                base_ref=base_ref,
                topic_ref=topic_ref,
                conflicting_ref=other_ref,
                advisory=advisory,
                error=other_verdict.error,
            )
        if not other_verdict.clean:
            return PredispatchVerdict(
                would_conflict=True,
                base_ref=base_ref,
                topic_ref=topic_ref,
                conflicting_ref=other_ref,
                conflicted_files=list(other_verdict.conflicted_files),
                advisory=advisory,
            )

    return PredispatchVerdict(
        would_conflict=False,
        base_ref=base_ref,
        topic_ref=topic_ref,
        advisory=advisory,
    )


__all__ = ["PredispatchVerdict", "check_predispatch_conflict"]
