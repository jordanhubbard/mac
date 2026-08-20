"""Decide whether a task is still the right work, against its branch's head.

WHY THIS EXISTS

A task is written at one moment and executed at another. In between the work
may have landed by another route, been superseded, become unnecessary, or had
its scope invalidated. An agent that starts without checking redoes the
already-done thing: one task produced five divergent implementations and
eleven duplicate pull requests on 2026-08-19, and a merged module was
re-implemented on 2026-08-20 after its task was reopened.

THE MISTAKE THIS IS SHAPED TO PREVENT

The obvious triage signal is "does a merged pull request reference this task
id". It does not work, and it does not fail quietly. Triaging 75 held tasks
that way returned 24 hits and EVERY ONE was a false positive: the id appears in
changes that CITE the task as evidence, not that implement it. PR #489 -- an
ADR about the task-graph UI -- names task_9f3b80b8 because the ADR quotes it as
motivating evidence, while the task is entirely outstanding. Closing on that
signal destroys live work.

Tightening to "task id in the title or branch" returns zero across the same 75,
correctly: a held task is never claimed, so no agent ever opens a change for
it. Both signals are wrong in opposite directions, which is why triage is a
judgement made by reading the repository rather than a query, and why the
central rule here is structural rather than advisory:

    A MENTION IS NOT A CITATION. ``ChangeRelation.MENTIONS_TASK_ID`` can never
    reach a closing verdict; ``verdict_closes_task`` refuses to build one, and
    ``validate_close`` refuses to let one out of the module.

SHAPE (ADR 0022)

The decision is a named value, not a boolean and not an ``Optional`` whose
``None`` means "work it out yourself". ``decide_triage`` is pure: it takes a
``TriageEvidence`` and returns a ``TriageDecision`` carrying an outcome, a
reason code from a closed set, the citation that justifies it, and the measured
cost of collecting the evidence. Fetching lives in ``collect_triage_evidence``,
which is bounded by a ``TriageBudget`` -- triage is a read, and must not become
a second execution. A budget that runs out yields ``CANNOT_TELL``, which
proceeds. A confident wrong verdict is worse than an honest "unclear"; see the
24 false positives above.

Related: ADR 0020 (a running task is not editable -- the atomic update that
makes a SCOPE_STALE correction safe), ADR 0022 (a gate returns a named
decision).
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

SCHEMA = "mac.task_triage.v1"

__all__ = [
    "SCHEMA",
    "TriageError",
    "TriageOutcome",
    "TriageReason",
    "TriageAction",
    "ChangeRelation",
    "ChangeRef",
    "TriageBudget",
    "TriageCost",
    "TriageEvidence",
    "TriageDecision",
    "OUTCOMES",
    "REASONS",
    "CLOSING_OUTCOMES",
    "verdict_closes_task",
    "decide_triage",
    "validate_close",
    "scope_paths_from_task",
    "acceptance_markers_from_task",
    "collect_triage_evidence",
    "plan_scope_update",
    "ledger_payload",
]


class TriageError(ValueError):
    """A triage value was built or used in a way the vocabulary forbids."""


class TriageOutcome:
    """The five verdicts. Closed set; a sixth needs a name, not a bare False."""

    #: The described change is present at head. Closes the task, citing the
    #: change that carries it -- never merely one that mentions the task.
    ALREADY_LANDED = "already_landed"
    #: A different change made this unnecessary. Closes, naming the replacement.
    SUPERSEDED = "superseded"
    #: Still wanted, but the described defect, paths, or acceptance criteria no
    #: longer match the tree. The task is updated, then worked against the
    #: corrected scope.
    SCOPE_STALE = "scope_stale"
    #: Proceed unchanged.
    STILL_NEEDED = "still_needed"
    #: Undecidable within budget. Proceeds, and the uncertainty is recorded.
    CANNOT_TELL = "cannot_tell"


OUTCOMES: Tuple[str, ...] = (
    TriageOutcome.ALREADY_LANDED,
    TriageOutcome.SUPERSEDED,
    TriageOutcome.SCOPE_STALE,
    TriageOutcome.STILL_NEEDED,
    TriageOutcome.CANNOT_TELL,
)

#: The two outcomes that end a task. Kept as data so the close guard and the
#: routing table cannot drift apart.
CLOSING_OUTCOMES: Tuple[str, ...] = (
    TriageOutcome.ALREADY_LANDED,
    TriageOutcome.SUPERSEDED,
)


class TriageReason:
    """Why the verdict is what it is. One vocabulary, operator-visible.

    These codes appear in the ledger and in observability unchanged; there is
    no rendering layer that paraphrases them, because a paraphrase drifts.
    """

    #: A change at head implements the declared scope and every declared
    #: acceptance marker is present.
    LANDED_CITED_CHANGE = "landed_cited_change"
    #: A change at head declares that it replaces this task's approach.
    SUPERSEDED_BY_CITED_CHANGE = "superseded_by_cited_change"
    #: Declared scope paths no longer exist at head, so the task describes a
    #: tree that is not there any more.
    SCOPE_PATHS_MISSING = "scope_paths_missing"
    #: Nothing at head carries the described change.
    NO_LANDED_CHANGE_FOUND = "no_landed_change_found"
    #: The ONLY evidence found was a change naming the task id. That is the
    #: 24/24 false-positive signal; it never closes anything.
    MENTION_IS_NOT_A_CITATION = "mention_is_not_a_citation"
    #: The read budget ran out before the question could be answered.
    BUDGET_EXHAUSTED = "budget_exhausted"
    #: There was no head to read -- no repository, or no resolved sha.
    NO_HEAD_EVIDENCE = "no_head_evidence"
    #: A change touches the scope but the acceptance markers are only
    #: partially present, so "landed" and "in progress" are indistinguishable.
    PARTIAL_ACCEPTANCE_EVIDENCE = "partial_acceptance_evidence"
    #: The task declared nothing checkable -- no scope paths, no markers -- so
    #: head cannot confirm or deny it.
    NO_CHECKABLE_SCOPE = "no_checkable_scope"


REASONS: Tuple[str, ...] = (
    TriageReason.LANDED_CITED_CHANGE,
    TriageReason.SUPERSEDED_BY_CITED_CHANGE,
    TriageReason.SCOPE_PATHS_MISSING,
    TriageReason.NO_LANDED_CHANGE_FOUND,
    TriageReason.MENTION_IS_NOT_A_CITATION,
    TriageReason.BUDGET_EXHAUSTED,
    TriageReason.NO_HEAD_EVIDENCE,
    TriageReason.PARTIAL_ACCEPTANCE_EVIDENCE,
    TriageReason.NO_CHECKABLE_SCOPE,
)


class TriageAction:
    """What the claiming agent does with the verdict."""

    CLOSE = "close"
    UPDATE_SCOPE = "update_scope"
    PROCEED = "proceed"


#: Verdict -> action. CANNOT_TELL proceeds; that is the whole point of having
#: the verdict rather than blocking on it.
_ACTIONS: Dict[str, str] = {
    TriageOutcome.ALREADY_LANDED: TriageAction.CLOSE,
    TriageOutcome.SUPERSEDED: TriageAction.CLOSE,
    TriageOutcome.SCOPE_STALE: TriageAction.UPDATE_SCOPE,
    TriageOutcome.STILL_NEEDED: TriageAction.PROCEED,
    TriageOutcome.CANNOT_TELL: TriageAction.PROCEED,
}


class ChangeRelation:
    """How a change at head relates to the task. Closed set.

    The distinction between ``MENTIONS_TASK_ID`` and ``IMPLEMENTS_SCOPE`` is
    the whole defence against the 24 false positives: the first is what a
    citing ADR looks like, the second is what the work looks like.
    """

    #: The change names the task id and nothing more. NEVER closes a task.
    MENTIONS_TASK_ID = "mentions_task_id"
    #: The change touches the task's declared scope paths.
    IMPLEMENTS_SCOPE = "implements_scope"
    #: The change says, in as many words, that it replaces this task.
    REPLACES_SCOPE = "replaces_scope"
    #: Looked at, found irrelevant. Recorded so the read is auditable.
    UNRELATED = "unrelated"


#: Relations that may appear as the citation on a closing verdict. Deliberately
#: excludes MENTIONS_TASK_ID.
_CITABLE_RELATIONS: Tuple[str, ...] = (
    ChangeRelation.IMPLEMENTS_SCOPE,
    ChangeRelation.REPLACES_SCOPE,
)


@dataclass(frozen=True)
class ChangeRef:
    """One candidate change read at the branch head.

    ``relation`` is the judgement; ``ref``/``subject``/``matched_paths`` are
    what a human needs to audit it. A close that cannot name these is not a
    close, it is work being lost.
    """

    kind: str  # "commit" | "pull_request"
    ref: str
    subject: str = ""
    relation: str = ChangeRelation.UNRELATED
    matched_paths: Tuple[str, ...] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        if not str(self.ref or "").strip():
            raise TriageError("change reference needs a ref")
        if self.relation not in (
            ChangeRelation.MENTIONS_TASK_ID,
            ChangeRelation.IMPLEMENTS_SCOPE,
            ChangeRelation.REPLACES_SCOPE,
            ChangeRelation.UNRELATED,
        ):
            raise TriageError("unknown change relation: %s" % self.relation)

    @property
    def citable(self) -> bool:
        """True when this change may justify closing a task."""
        return self.relation in _CITABLE_RELATIONS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "subject": self.subject,
            "relation": self.relation,
            "matched_paths": list(self.matched_paths),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class TriageBudget:
    """Hard caps on the read. Triage is bounded by construction, not by hope.

    Every limit is small enough that exceeding it means the question needed a
    real investigation -- at which point the honest answer is CANNOT_TELL and
    the work proceeds.
    """

    max_git_calls: int = 12
    max_commits: int = 200
    max_path_probes: int = 40
    max_seconds: float = 15.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_git_calls": self.max_git_calls,
            "max_commits": self.max_commits,
            "max_path_probes": self.max_path_probes,
            "max_seconds": self.max_seconds,
        }


@dataclass(frozen=True)
class TriageCost:
    """What the read actually cost. Measured, because "bounded" is a claim."""

    git_calls: int = 0
    commits_scanned: int = 0
    path_probes: int = 0
    elapsed_ms: float = 0.0
    budget_exhausted: bool = False
    #: Which limit stopped it: "", "git_calls", "commits", "path_probes",
    #: or "seconds".
    exhausted_by: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "git_calls": self.git_calls,
            "commits_scanned": self.commits_scanned,
            "path_probes": self.path_probes,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "budget_exhausted": self.budget_exhausted,
            "exhausted_by": self.exhausted_by,
        }


@dataclass(frozen=True)
class TriageEvidence:
    """Everything the decision is allowed to look at.

    The gate does not fetch (ADR 0022 §2). This value is what a collector
    produced, and it is equally constructible by hand, which is why the
    decision can be tested adversarially without a repository.
    """

    task_id: str
    repository: str = ""
    branch: str = ""
    head_sha: str = ""
    #: Repo-relative paths the task says its work touches.
    scope_paths: Tuple[str, ...] = ()
    #: Of ``scope_paths``, those that exist at head.
    present_paths: Tuple[str, ...] = ()
    #: Of ``scope_paths``, those that do not.
    missing_paths: Tuple[str, ...] = ()
    #: Strings whose presence at head means the described work is there.
    acceptance_markers: Tuple[str, ...] = ()
    markers_present: Tuple[str, ...] = ()
    markers_absent: Tuple[str, ...] = ()
    candidates: Tuple[ChangeRef, ...] = ()
    cost: TriageCost = field(default_factory=TriageCost)
    notes: Tuple[str, ...] = ()

    def citations(self) -> Tuple[ChangeRef, ...]:
        return tuple(c for c in self.candidates if c.citable)

    def mentions(self) -> Tuple[ChangeRef, ...]:
        return tuple(
            c for c in self.candidates
            if c.relation == ChangeRelation.MENTIONS_TASK_ID
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "repository": self.repository,
            "branch": self.branch,
            "head_sha": self.head_sha,
            "scope_paths": list(self.scope_paths),
            "present_paths": list(self.present_paths),
            "missing_paths": list(self.missing_paths),
            "acceptance_markers": list(self.acceptance_markers),
            "markers_present": list(self.markers_present),
            "markers_absent": list(self.markers_absent),
            "candidates": [c.to_dict() for c in self.candidates],
            "cost": self.cost.to_dict(),
            "notes": list(self.notes),
        }


def verdict_closes_task(outcome: str) -> bool:
    """Does this verdict end the task? One place, so nothing drifts."""
    return outcome in CLOSING_OUTCOMES


@dataclass(frozen=True)
class TriageDecision:
    """The verdict, its reason, and the evidence that justifies it."""

    outcome: str
    reason: str
    evidence: TriageEvidence
    citation: Optional[ChangeRef] = None
    #: Human-auditable one-liner. Never the sole record: the codes above are.
    summary: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise TriageError("unknown triage outcome: %s" % self.outcome)
        if self.reason not in REASONS:
            raise TriageError("unknown triage reason: %s" % self.reason)
        # The structural guard. A closing verdict without a citable change --
        # or with only a task-id mention behind it -- is the failure this
        # module exists to make impossible, so it cannot be constructed.
        if verdict_closes_task(self.outcome):
            if self.citation is None:
                raise TriageError(
                    "%s must cite the change that carries it" % self.outcome
                )
            if not self.citation.citable:
                raise TriageError(
                    "%s cannot be justified by a %s: a mention is not a citation"
                    % (self.outcome, self.citation.relation)
                )

    @property
    def action(self) -> str:
        return _ACTIONS[self.outcome]

    @property
    def closes(self) -> bool:
        return verdict_closes_task(self.outcome)

    @property
    def proceeds(self) -> bool:
        return self.action == TriageAction.PROCEED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": SCHEMA,
            "outcome": self.outcome,
            "reason": self.reason,
            "action": self.action,
            "summary": self.summary,
            "citation": self.citation.to_dict() if self.citation else None,
            "evidence": self.evidence.to_dict(),
        }


def validate_close(decision: TriageDecision) -> ChangeRef:
    """Gate the close path. Returns the citation, or refuses.

    Called at the point of closing as well as at construction: the caller that
    ends a task should not have to trust that the value it was handed was built
    here.
    """
    if not decision.closes:
        raise TriageError(
            "verdict %s does not close a task" % decision.outcome
        )
    citation = decision.citation
    if citation is None or not citation.citable:
        raise TriageError("a close needs a citable change, not a mention")
    return citation


# --- the decision ----------------------------------------------------------


def decide_triage(evidence: TriageEvidence) -> TriageDecision:
    """Judge a task against its branch head. Pure; no I/O.

    Ordered so that every "no" is a named "no". Note that the mention-only
    branch is checked for its REASON rather than its verdict: mentions never
    change the outcome, they only explain why the outcome is not a close.
    """
    def _decide(
        outcome: str,
        reason: str,
        summary: str,
        citation: Optional[ChangeRef] = None,
    ) -> TriageDecision:
        return TriageDecision(
            outcome=outcome,
            reason=reason,
            evidence=evidence,
            citation=citation,
            summary=summary,
        )

    # 1. Nothing to read. Say so; do not guess from the task text alone.
    if not str(evidence.head_sha or "").strip():
        return _decide(
            TriageOutcome.CANNOT_TELL,
            TriageReason.NO_HEAD_EVIDENCE,
            "no branch head was resolved, so head cannot confirm or deny this task",
        )

    # 2. The read ran out of budget. An unfinished read is not a verdict.
    if evidence.cost.budget_exhausted:
        return _decide(
            TriageOutcome.CANNOT_TELL,
            TriageReason.BUDGET_EXHAUSTED,
            "triage budget exhausted (%s); proceeding rather than guessing"
            % (evidence.cost.exhausted_by or "limit"),
        )

    # 3. Supersession is checked before landing: a change that replaces the
    #    task's approach makes "is the described change present" the wrong
    #    question.
    for candidate in evidence.candidates:
        if candidate.relation == ChangeRelation.REPLACES_SCOPE:
            return _decide(
                TriageOutcome.SUPERSEDED,
                TriageReason.SUPERSEDED_BY_CITED_CHANGE,
                "%s %s replaces this work: %s"
                % (candidate.kind, candidate.ref, candidate.subject or candidate.detail),
                citation=candidate,
            )

    implementing = tuple(
        c for c in evidence.candidates
        if c.relation == ChangeRelation.IMPLEMENTS_SCOPE
    )
    mentions = evidence.mentions()
    checkable = bool(evidence.scope_paths) or bool(evidence.acceptance_markers)

    # 4. Nothing checkable was declared. This is the honest answer for a task
    #    written as prose with no paths and no markers -- and it is the answer
    #    that would have been right 24 times out of 24, because the only thing
    #    left to match on is the task id.
    if not checkable:
        note = (
            "; %d change(s) mention the task id, which is not evidence that it "
            "was done" % len(mentions)
            if mentions else ""
        )
        return _decide(
            TriageOutcome.CANNOT_TELL,
            TriageReason.NO_CHECKABLE_SCOPE if not mentions
            else TriageReason.MENTION_IS_NOT_A_CITATION,
            "task declares no checkable paths or acceptance markers%s" % note,
        )

    markers_declared = bool(evidence.acceptance_markers)
    all_markers_present = markers_declared and not evidence.markers_absent
    some_markers_present = bool(evidence.markers_present)

    # 5. Landed. Requires BOTH a change that touched the declared scope AND
    #    every declared acceptance marker present. Either alone is the kind of
    #    partial signal that produced the false positives.
    if implementing and all_markers_present:
        citation = implementing[0]
        return _decide(
            TriageOutcome.ALREADY_LANDED,
            TriageReason.LANDED_CITED_CHANGE,
            "%s %s carries this change and all %d acceptance marker(s) are "
            "present at %s"
            % (
                citation.kind,
                citation.ref,
                len(evidence.acceptance_markers),
                evidence.head_sha[:12],
            ),
            citation=citation,
        )

    # 6. Half-there. "Landed" and "someone is mid-way through it" look the same
    #    from here, and picking one is exactly the confident wrong verdict.
    if implementing and markers_declared and some_markers_present:
        return _decide(
            TriageOutcome.CANNOT_TELL,
            TriageReason.PARTIAL_ACCEPTANCE_EVIDENCE,
            "%d of %d acceptance marker(s) present at head; cannot distinguish "
            "landed from in-progress"
            % (len(evidence.markers_present), len(evidence.acceptance_markers)),
        )

    # 7. The tree moved out from under the task's scope.
    if evidence.missing_paths:
        return _decide(
            TriageOutcome.SCOPE_STALE,
            TriageReason.SCOPE_PATHS_MISSING,
            "declared path(s) absent at %s: %s"
            % (evidence.head_sha[:12], ", ".join(evidence.missing_paths[:5])),
        )

    # 8. Still wanted. If the only thing found was a mention, name that as the
    #    reason -- it is the signal a naive triage would have closed on.
    if mentions and not implementing:
        return _decide(
            TriageOutcome.STILL_NEEDED,
            TriageReason.MENTION_IS_NOT_A_CITATION,
            "%d change(s) mention %s but none carry the described work"
            % (len(mentions), evidence.task_id),
        )
    return _decide(
        TriageOutcome.STILL_NEEDED,
        TriageReason.NO_LANDED_CHANGE_FOUND,
        "nothing at %s carries the described change" % evidence.head_sha[:12],
    )


# --- reading the task ------------------------------------------------------

#: A repo-relative path in prose: at least one slash, a file-ish tail. Loose
#: enough to catch `src/mac/foo.py`, tight enough not to catch `and/or`.
_PATH_RE = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./+-]*/[A-Za-z0-9_./+-]+)`")
_TASK_ID_RE = re.compile(r"\btask_[0-9a-f]{8,32}\b")
_SUPERSEDE_RE = re.compile(
    r"\b(supersedes?|superseded|replaces|obsoletes)\b[^\n]{0,80}?(task_[0-9a-f]{8,32})",
    re.IGNORECASE,
)


def _task_triage_hints(task: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = task.get("metadata")
    if not isinstance(metadata, Mapping):
        return {}
    hints = metadata.get("triage")
    return hints if isinstance(hints, Mapping) else {}


def _string_tuple(values: Any, limit: int) -> Tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        return ()
    out: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return tuple(out)


def scope_paths_from_task(task: Mapping[str, Any], *, limit: int = 24) -> Tuple[str, ...]:
    """Repo-relative paths the task claims its work touches.

    Explicit ``metadata.triage.scope_paths`` wins. Otherwise the backticked
    path-shaped tokens in the description are used -- a task that says it
    changes `src/mac/foo.py` has told us where to look. Prose with no paths
    yields nothing, and the decision then declines to close, which is correct.
    """
    hints = _task_triage_hints(task)
    declared = _string_tuple(hints.get("scope_paths"), limit)
    if declared:
        return declared
    description = str(task.get("description") or "")
    found: List[str] = []
    for match in _PATH_RE.finditer(description):
        candidate = match.group(1).strip().rstrip("/")
        if candidate and candidate not in found:
            found.append(candidate)
        if len(found) >= limit:
            break
    return tuple(found)


def acceptance_markers_from_task(
    task: Mapping[str, Any], *, limit: int = 12
) -> Tuple[str, ...]:
    """Strings whose presence at head means the described work is there.

    Only taken from ``metadata.triage.acceptance_markers``. Deriving markers
    from prose was considered and rejected: a guessed marker that happens to be
    present is precisely how a task gets closed on a coincidence, and this
    module's whole job is to not do that. With no markers declared, triage can
    still report SCOPE_STALE or STILL_NEEDED -- it just cannot close.
    """
    return _string_tuple(_task_triage_hints(task).get("acceptance_markers"), limit)


# --- the bounded read ------------------------------------------------------


GitRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class _BudgetMeter:
    """Counts the read against the budget and remembers what stopped it."""

    def __init__(self, budget: TriageBudget) -> None:
        self.budget = budget
        self.started = time.monotonic()
        self.git_calls = 0
        self.commits_scanned = 0
        self.path_probes = 0
        self.exhausted_by = ""

    @property
    def exhausted(self) -> bool:
        return bool(self.exhausted_by)

    def _elapsed(self) -> float:
        return time.monotonic() - self.started

    def spend_git_call(self) -> bool:
        if self.exhausted:
            return False
        if self._elapsed() > self.budget.max_seconds:
            self.exhausted_by = "seconds"
            return False
        if self.git_calls >= self.budget.max_git_calls:
            self.exhausted_by = "git_calls"
            return False
        self.git_calls += 1
        return True

    def spend_path_probes(self, count: int) -> bool:
        if self.exhausted:
            return False
        if self.path_probes + count > self.budget.max_path_probes:
            self.exhausted_by = "path_probes"
            return False
        self.path_probes += count
        return True

    def note_commits(self, count: int) -> None:
        self.commits_scanned += count
        if self.commits_scanned > self.budget.max_commits:
            self.exhausted_by = "commits"

    def cost(self) -> TriageCost:
        return TriageCost(
            git_calls=self.git_calls,
            commits_scanned=self.commits_scanned,
            path_probes=self.path_probes,
            elapsed_ms=self._elapsed() * 1000.0,
            budget_exhausted=self.exhausted,
            exhausted_by=self.exhausted_by,
        )


def _git_runner(worktree: Path) -> GitRunner:
    def run(args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            ["git", "-C", str(worktree), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    return run


# Field separator inside a `git log --format` record. NUL, and every split
# below is an explicit `split("\n")` rather than `splitlines()`: Python's
# splitlines ALSO breaks on \x1c/\x1d/\x1e/\x85, so the obvious record-separator
# choice (RS, \x1e) silently vanished into a line break and every subject came
# back empty while the shas still looked right -- a parse that fails by
# producing plausible values rather than by raising.
_LOG_SEP = "\x00"
_LOG_SEP_FORMAT = "%x00"


def collect_triage_evidence(
    task: Mapping[str, Any],
    *,
    worktree: Optional[Path] = None,
    head_sha: str = "",
    branch: str = "",
    repository: str = "",
    budget: Optional[TriageBudget] = None,
    runner: Optional[GitRunner] = None,
) -> TriageEvidence:
    """Read the branch head, under a hard budget, and report what is there.

    Read-only by construction: every git invocation below is a query. The
    caller supplies the checkout and the head it wants judged, because "which
    head does this task target" is the caller's question (see the repository
    contract), not this module's.
    """
    budget = budget or TriageBudget()
    meter = _BudgetMeter(budget)
    task_id = str(task.get("id") or "").strip()
    scope_paths = scope_paths_from_task(task)
    markers = acceptance_markers_from_task(task)
    notes: List[str] = []

    if runner is None:
        if worktree is None or not Path(worktree).is_dir():
            return TriageEvidence(
                task_id=task_id,
                repository=repository,
                branch=branch,
                head_sha="",
                scope_paths=scope_paths,
                acceptance_markers=markers,
                cost=meter.cost(),
                notes=("no readable checkout was supplied",),
            )
        runner = _git_runner(Path(worktree))

    head = str(head_sha or "").strip()
    if not head:
        if meter.spend_git_call():
            resolved = runner(["rev-parse", "HEAD"])
            if resolved.returncode == 0:
                head = resolved.stdout.strip()
    if not head:
        return TriageEvidence(
            task_id=task_id,
            repository=repository,
            branch=branch,
            head_sha="",
            scope_paths=scope_paths,
            acceptance_markers=markers,
            cost=meter.cost(),
            notes=("branch head could not be resolved",),
        )

    # Which declared paths exist at head. One git call for all of them, so the
    # probe count is bounded by the path count rather than by round trips.
    present: List[str] = []
    missing: List[str] = []
    if scope_paths:
        if meter.spend_path_probes(len(scope_paths)) and meter.spend_git_call():
            listed = runner(["ls-tree", "-r", "--name-only", head])
            if listed.returncode == 0:
                tree = {
                    entry for entry in listed.stdout.split("\n") if entry.strip()
                }
                prefixes = tuple(sorted(tree))
                for path in scope_paths:
                    if path in tree or any(
                        entry.startswith(path.rstrip("/") + "/") for entry in prefixes
                    ):
                        present.append(path)
                    else:
                        missing.append(path)
            else:
                notes.append("could not list the tree at head")
        else:
            notes.append("path probes exceeded the triage budget")

    # Which acceptance markers are present in the tree at head. `git grep` is
    # one call per marker and is capped by the git-call budget, so a task that
    # declares a dozen markers degrades to CANNOT_TELL rather than to a long
    # scan.
    markers_present: List[str] = []
    markers_absent: List[str] = []
    for marker in markers:
        if not meter.spend_git_call():
            notes.append("marker probes exceeded the triage budget")
            break
        found = runner(["grep", "--fixed-strings", "-l", "-e", marker, head])
        if found.returncode == 0 and found.stdout.strip():
            markers_present.append(marker)
        else:
            markers_absent.append(marker)

    candidates: List[ChangeRef] = []
    scanned = 0

    # Changes that touched the declared scope. THIS is the citable signal: it
    # is what the work looks like in the log, as opposed to what a citing ADR
    # looks like.
    if scope_paths and present and meter.spend_git_call():
        log = runner(
            [
                "log",
                "--max-count=%d" % min(budget.max_commits, 25),
                "--format=%%H%s%%s" % _LOG_SEP_FORMAT,
                head,
                "--",
                *present,
            ]
        )
        if log.returncode == 0:
            for line in log.stdout.split("\n"):
                sha, _, subject = line.partition(_LOG_SEP)
                sha = sha.strip()
                if not sha:
                    continue
                scanned += 1
                candidates.append(
                    ChangeRef(
                        kind="commit",
                        ref=sha,
                        subject=subject.strip()[:200],
                        relation=ChangeRelation.IMPLEMENTS_SCOPE,
                        matched_paths=tuple(present),
                        detail="touched declared scope paths",
                    )
                )

    # Changes that merely name the task id, and changes that say they replace
    # it. Both come from the same message scan; they are told apart by what the
    # message actually says, which is the entire mention/citation distinction.
    if task_id and meter.spend_git_call():
        log = runner(
            [
                "log",
                "--max-count=%d" % min(budget.max_commits, 200),
                "--format=%%H%s%%s%s%%b%s"
                % (_LOG_SEP_FORMAT, _LOG_SEP_FORMAT, _LOG_SEP_FORMAT),
                "--grep=%s" % task_id,
                "--fixed-strings",
                head,
            ]
        )
        if log.returncode == 0:
            known = {c.ref for c in candidates}
            for record in log.stdout.split(_LOG_SEP + "\n"):
                parts = record.split(_LOG_SEP)
                if len(parts) < 2:
                    continue
                sha = parts[0].strip()
                subject = parts[1].strip()[:200]
                body = parts[2] if len(parts) > 2 else ""
                if not sha:
                    continue
                scanned += 1
                supersedes = any(
                    match.group(2) == task_id
                    for match in _SUPERSEDE_RE.finditer(subject + "\n" + body)
                )
                if supersedes:
                    candidates.append(
                        ChangeRef(
                            kind="commit",
                            ref=sha,
                            subject=subject,
                            relation=ChangeRelation.REPLACES_SCOPE,
                            detail="message declares it replaces this task",
                        )
                    )
                    continue
                if sha in known:
                    # Already recorded as touching the scope; the stronger
                    # relation stands. Re-adding it as a mention would let the
                    # weaker signal dilute the audit trail.
                    continue
                candidates.append(
                    ChangeRef(
                        kind="commit",
                        ref=sha,
                        subject=subject,
                        relation=ChangeRelation.MENTIONS_TASK_ID,
                        detail="names the task id without touching its scope",
                    )
                )

    meter.note_commits(scanned)
    if not scope_paths and not markers:
        notes.append(
            "task declared no scope paths or acceptance markers; head cannot "
            "confirm or deny it"
        )
    return TriageEvidence(
        task_id=task_id,
        repository=repository,
        branch=branch,
        head_sha=head,
        scope_paths=scope_paths,
        present_paths=tuple(present),
        missing_paths=tuple(missing),
        acceptance_markers=markers,
        markers_present=tuple(markers_present),
        markers_absent=tuple(markers_absent),
        candidates=tuple(candidates),
        cost=meter.cost(),
        notes=tuple(notes),
    )


# --- what the caller does with it ------------------------------------------


def plan_scope_update(
    decision: TriageDecision, task: Mapping[str, Any]
) -> Dict[str, Any]:
    """The metadata edit that corrects a SCOPE_STALE task.

    Returned rather than applied: the update goes through ADR 0020's atomic
    path (abort, revoke, apply, restart, re-enter from the top), which is the
    caller's privilege to exercise, not this module's. The corrected scope
    keeps only the paths that exist at head, and the reason is written where
    the next attempt will read it -- so the re-entry is visibly derived from
    the new scope rather than from prose someone has to re-interpret.
    """
    if decision.outcome != TriageOutcome.SCOPE_STALE:
        raise TriageError(
            "only a scope_stale verdict plans an update, not %s" % decision.outcome
        )
    metadata_raw = task.get("metadata")
    metadata: Dict[str, Any] = (
        dict(metadata_raw) if isinstance(metadata_raw, Mapping) else {}
    )
    hints_raw = metadata.get("triage")
    hints: Dict[str, Any] = dict(hints_raw) if isinstance(hints_raw, Mapping) else {}
    hints.update(
        {
            "schema": SCHEMA,
            "scope_paths": list(decision.evidence.present_paths),
            "removed_scope_paths": list(decision.evidence.missing_paths),
            "corrected_against_head": decision.evidence.head_sha,
            "corrected_against_branch": decision.evidence.branch,
            "corrected_reason": decision.reason,
        }
    )
    metadata["triage"] = hints
    metadata["triage_verdict"] = ledger_payload(decision)
    return {"metadata": metadata}


def ledger_payload(decision: TriageDecision) -> Dict[str, Any]:
    """The durable record: verdict, reason, citation, and what it cost.

    An unexplained close is indistinguishable from work being lost, so the
    citation and the cost travel with the verdict rather than being
    reconstructible from logs by someone who already suspects a problem.
    """
    citation = decision.citation
    return {
        "schema": SCHEMA,
        "outcome": decision.outcome,
        "reason": decision.reason,
        "action": decision.action,
        "summary": decision.summary,
        "branch": decision.evidence.branch,
        "head_sha": decision.evidence.head_sha,
        "repository": decision.evidence.repository,
        "citation": citation.to_dict() if citation else None,
        "cost": decision.evidence.cost.to_dict(),
        "scope_paths": list(decision.evidence.scope_paths),
        "missing_paths": list(decision.evidence.missing_paths),
        "acceptance_markers_absent": list(decision.evidence.markers_absent),
        "mention_only_changes": [c.ref for c in decision.evidence.mentions()],
        "notes": list(decision.evidence.notes),
    }
