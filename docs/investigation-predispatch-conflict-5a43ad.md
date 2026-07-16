# Investigation: `predispatch_conflict.py` failure-pattern (dream finding)

**Task**: task_5a43ad5b512b4250ad494e33466d6ad2 —
"Investigate: predispatch_conflict.py failure pattern".
**Parent task**: task_2e90375e1ff2424fbc2907e4319f2f3d
(goal: "Investigate dream finding: predispatch_conflict.py failure pattern (mac)").
**Investigated by**: fleet worker (investigation only; no module implemented).
**Baseline**: `scripts/run-contract-tests.sh` on the task worktree (see evidence).
**Scope**: audit only. This note records ground truth and the intended contract
for a *future* implementation task; it deliberately does NOT add
`src/mac/predispatch_conflict.py`.

## Status: REAL GAP — a coherent, non-duplicative module contract exists

There is currently **no** `src/mac/predispatch_conflict.py` in the repository,
and **no** reference to the strings `predispatch`, `pre-dispatch`, or
`pre_dispatch` anywhere in the tracked tree or in git history (confirmed by
full-tree and `git log --all --grep` searches). So the finding names a module
that does not exist. But it is **not** mere label noise: the surrounding
merge-conflict surface has a real, describable hole that a `predispatch_conflict`
gate would fill, and that hole is distinct from what the existing merge-queue
gate covers.

The existing safety net is **land-time only**:

- `mac.merge_queue.validate_projected_merge(repo_dir, base_ref, topic_ref)`
  runs the OCC validation phase with `git merge-tree --write-tree --name-only`
  and returns a `MergeGateVerdict` (`clean`, `base_sha`, `topic_sha`,
  `conflicted_files`, `error`). See `src/mac/merge_queue.py:99`.
- It is consumed by `mac.auto_land.safe_do_land`
  (`src/mac/auto_land.py:457`) and by `mac.services` (`src/mac/services.py:12836`)
  as the *final land-time* check — "never land onto a tip the branch conflicts
  with".

That gate fires at the **end** of a task's life, when a completed branch is about
to merge. The failure pattern the dream finding points at is the **symmetric,
earlier** problem: work is **dispatched** to an agent/worktree that will
*predictably* conflict at land time — for example two open tasks whose declared
or inferred file sets overlap the same regions, or a task authored against a base
the trunk has already moved past on the very lines it will touch. Today nothing
inspects that at dispatch time; the conflict is only discovered after an agent
has spent a full task budget, at `auto_land`. That wasted-work / late-abort shape
is exactly a "predispatch conflict" failure pattern, and it is real.

## (a) Real or false positive

**Real gap, correctly named, worth one focused module.** The module does not
exist and is not referenced, but the *capability* it describes is genuinely
missing and is not covered by `merge_queue` (which is land-time) or by any
dispatch-time overlap check (there is none). It is a false positive only in the
narrow sense that the finding implies a *regression in an existing file* — there
is no such file to have regressed. Treat it as a **new-capability** finding, not
a repair of existing code.

## (b) Intended behavior / contract (the failure pattern it detects)

**Failure pattern**: a task is dispatched to an agent/worktree whose change will
**predictably** conflict with the current trunk tip (or with another
in-flight/queued task) at land time, so the work is doomed before it starts and
the conflict is only surfaced later by the land-time `merge_queue` gate — after a
full task budget has been spent.

**Behavior**: a *pre-dispatch*, read-only, fail-open-with-warning gate that,
given a candidate task's branch/base (and optionally a set of already-in-flight
branches), reports whether dispatching now would land on a tip it already
conflicts with, and against which files. It must:

- Be **side-effect free**: no working tree touched, nothing committed, mirroring
  `merge_queue`'s `git merge-tree --write-tree` approach.
- **Reuse** `mac.merge_queue.validate_projected_merge` rather than reimplement
  conflict detection — the merge-tree logic, the `MergeGateVerdict` shape, the
  `mac.merge_gate.v1` schema, and the fail-closed error handling already exist
  and are tested. `predispatch_conflict` is an *orchestration/decision* layer on
  top, analogous to how `auto_land.decide_land` wraps the gate.
- Distinguish two inputs it can gate on:
  1. **branch-vs-current-tip**: the candidate branch already conflicts with the
     *current* base tip (its base moved under it on lines it edits).
  2. **branch-vs-in-flight**: the candidate conflicts with another open task's
     branch that is expected to land first (pairwise projected merge).
- Return a structured, serializable verdict (schema-tagged, like
  `mac.merge_gate.v1`) carrying at minimum: whether a predispatch conflict is
  predicted, the conflicting ref(s), the `conflicted_files`, and an `error`
  string when refs cannot be resolved.
- **Fail policy**: unlike the land-time gate (which is *fail-closed* / block),
  the pre-dispatch gate should default to **advisory / fail-open**: an
  unresolved ref or a `git merge-tree` error must NOT silently block dispatch;
  it should surface a warning and let dispatch proceed, because a false positive
  at dispatch time is more costly than at land time (the land-time gate is the
  authoritative backstop). This policy choice should be an explicit parameter,
  not hard-coded.

**Outputs (semantics)**: a verdict object plus a boolean "predicted-conflict"
signal that a dispatcher (`mac.dispatch` / ready-task selection / the executor
scheduler) can log, attach to task evidence, or use to *re-order* (prefer
dispatching non-conflicting tasks first) rather than hard-block.

## (c) Exact public API signature the implementation should expose

Mirror the shape and naming conventions of `mac.merge_queue` (frozen dataclass
verdict with `to_dict()` + a schema tag; a top-level pure function taking a
`repo_dir` and refs plus an injectable `git_runner`; an `__all__`). Proposed:

```python
# src/mac/predispatch_conflict.py
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence
import subprocess

GitRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess"]


@dataclass(frozen=True)
class PredispatchVerdict:
    """Advisory pre-dispatch conflict prediction for a candidate task branch."""

    would_conflict: bool
    base_ref: str
    topic_ref: str
    conflicting_ref: str = ""          # "" == vs base tip; else the in-flight ref
    conflicted_files: List[str] = field(default_factory=list)
    advisory: bool = True              # True == fail-open (warn, do not block)
    error: str = ""

    def to_dict(self) -> dict: ...     # schema: "mac.predispatch_conflict.v1"


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
    ``in_flight_refs``). Delegates conflict detection to
    ``mac.merge_queue.validate_projected_merge``. Fail-open by default
    (``advisory=True``): ref/merge errors are reported, not raised."""


__all__ = ["PredispatchVerdict", "check_predispatch_conflict"]
```

Conventions this follows (verified against the checked-out code):
- Frozen dataclass verdict + `to_dict()` with a `schema` key, exactly like
  `MergeGateVerdict.to_dict()` returning `mac.merge_gate.v1`
  (`src/mac/merge_queue.py:65`).
- Injectable `git_runner` for hermetic tests, exactly like
  `validate_projected_merge`'s `git_runner` parameter (`src/mac/merge_queue.py:99`).
- Pure decision core separated from side effects, exactly like
  `auto_land.decide_land` vs `run_auto_land` (`src/mac/auto_land.py`).

## (d) Modules it should integrate with

- **`mac.merge_queue`** (`src/mac/merge_queue.py`): reuse
  `validate_projected_merge` / `MergeGateVerdict` as the underlying conflict
  primitive. Do NOT duplicate the `git merge-tree` logic. This is the single
  most important integration point.
- **`mac.dispatch`** (`src/mac/dispatch.py`): the dispatch/transport layer and
  ready-task selection (`ready_tasks`, `resolve_dispatch`,
  `explain_task_dispatch`) is where a pre-dispatch advisory would be consulted
  or surfaced — the natural caller. The check itself must stay transport-neutral
  (a pure git function), with dispatch code calling it.
- **`mac.auto_land`** (`src/mac/auto_land.py`): the *symmetric* land-time gate;
  `predispatch_conflict` is its early-warning counterpart and should share the
  verdict/`to_dict` conventions so evidence records read consistently across the
  dispatch→land lifecycle.
- **Tests**: follow `tests/test_merge_queue.py` (real temporary git repos, no
  mocks, hermetic via a per-test `_git` helper) and `tests/test_dispatch.py`
  conventions; the future `tests/test_predispatch_conflict.py` should exercise:
  clean-dispatch, base-moved-underneath conflict, pairwise in-flight conflict,
  bad-ref fail-open (advisory) vs fail-closed, and verdict serialization
  (`mac.predispatch_conflict.v1`).

## Note on the task description's `MergeGateResult`

The task brief refers to `MergeGateResult.conflicted_files`; the checked-out code
names that type **`MergeGateVerdict`** (`src/mac/merge_queue.py:65`). The
`.conflicted_files` field exists as described; only the class name differs. The
implementer should target `MergeGateVerdict`.

## Explicit assumptions (recorded per policy)

- The dream finding is treated as a **new-capability** signal, not a regression,
  because no `predispatch_conflict.py` has ever existed in tracked history.
- The proposed default is **advisory / fail-open** at dispatch time, with the
  authoritative fail-closed backstop remaining `merge_queue` at land time. If the
  implement-child prefers a fail-closed dispatch gate, that is a policy decision
  for that task; this note flags the parameter (`advisory`) rather than forcing it.
- The exact wiring point inside `mac.dispatch` (which selection/explain path
  consults the gate) is left to the implement-child; this note fixes the module
  contract and the conflict primitive to reuse, not the dispatcher edit.
