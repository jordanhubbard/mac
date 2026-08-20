"""Periodic sweep that advances held/stalled tasks toward terminal states.

A held task has no owner and no clock.  Nothing revisits it, so a hold
outlives its reason indefinitely: measured on 2026-08-20, 75 of 81 open tasks
in project ``mac`` carried ``metadata.no_dispatch``, 17 of them parked pending
a decomposition fix that had already merged.  Four more were worse -- ``open``
with ``attempt_count >= max_attempts``.  Those are permanently undispatchable
AND invisible: they read as ordinary open work in every list view, and
``mac task reopen`` silently does nothing to them because reopen requires a
terminal state.

This module is the clock.  It runs on an hours-scale timer (not on the
dispatch tick), examines a bounded number of held/stalled tasks per run, and
records a verdict on each:

    released              the condition that justified the hold is gone
    budget_raised         attempts were exhausted and the work is still wanted
    cancelled             superseded, already landed, or no longer wanted
    reviewed_still_valid  re-examined and re-justified; the hold stays
    undecidable           the sweep could not decide cheaply, so it did not act

``reviewed_still_valid`` is not a no-op.  A hold that has been re-examined and
re-justified is a different object from one nobody has looked at since August,
and before this the ledger could not tell them apart.  The verdict is written
to ``metadata["hold_review"]`` together with a fingerprint of the hold reason,
which is also what makes the next run cheap: an unchanged task inside its
review TTL is skipped without re-deciding it.

Why the attribution rules below are so strict
---------------------------------------------
The obvious way to find "work that already landed" is to look for a merged PR
mentioning the task id.  Measured on the same day, that returned 24 hits out of
75 and EVERY ONE was a false positive -- PRs cite task ids as evidence, in
their bodies, for unrelated work.  Tightening to "the id is in the PR title or
branch" returned zero, correctly, because a held task is never claimed, so no
PR is ever opened for it.  Both signals are wrong in opposite directions.

So a close must cite the CHANGE that satisfies the task, and a citation whose
only link to the task is a mention in a body or trailer is rejected
(:func:`change_attribution` returns ``mention_only``, and the sweep reports the
task undecidable rather than guessing).  An unexplained automated close is
indistinguishable from work being lost.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from mac.config_coercion import bounded_env_number

HOLD_SWEEP_SCHEMA = "mac.task_hold_sweep.v1"
HOLD_REVIEW_SCHEMA = "mac.task_hold_review.v1"

#: Where a verdict is durably recorded on the task itself.
HOLD_REVIEW_KEY = "hold_review"

#: Archaeology over the whole backlog is expensive; hours, not minutes.
MIN_INTERVAL_SECONDS = 15 * 60.0
MAX_INTERVAL_SECONDS = 7 * 24 * 60 * 60.0
DEFAULT_INTERVAL_SECONDS = 6 * 60 * 60.0
DEFAULT_INITIAL_DELAY_SECONDS = 300.0
MAX_INITIAL_DELAY_SECONDS = 6 * 60 * 60.0

#: A cap on tasks EXAMINED per run, so one run cannot become an unbounded
#: sweep of thousands of rows.
DEFAULT_BUDGET = 50
MAX_BUDGET = 500

#: How long a recorded verdict suppresses re-examination of an unchanged task.
DEFAULT_REVIEW_TTL_SECONDS = 7 * 24 * 60 * 60.0
MIN_REVIEW_TTL_SECONDS = 60.0
MAX_REVIEW_TTL_SECONDS = 90 * 24 * 60 * 60.0

#: Extra attempts granted when the sweep releases an exhausted task, and how
#: many times it may do so before the task must go terminal instead.  Both
#: bounded: "raise the budget" cannot become an infinite retry loop.
DEFAULT_ATTEMPT_GRANT = 1
DEFAULT_MAX_ATTEMPT_GRANTS = 1

VERDICT_RELEASED = "released"
VERDICT_BUDGET_RAISED = "budget_raised"
VERDICT_CANCELLED = "cancelled"
VERDICT_REVIEWED_STILL_VALID = "reviewed_still_valid"
VERDICT_UNDECIDABLE = "undecidable"

VERDICTS = (
    VERDICT_RELEASED,
    VERDICT_BUDGET_RAISED,
    VERDICT_CANCELLED,
    VERDICT_REVIEWED_STILL_VALID,
    VERDICT_UNDECIDABLE,
)

#: Verdicts that changed the task's dispatchability.  Everything else is a
#: recorded observation: the task itself is left exactly as it was.
ACTING_VERDICTS = frozenset(
    {VERDICT_RELEASED, VERDICT_BUDGET_RAISED, VERDICT_CANCELLED}
)

HOLD_NO_DISPATCH = "no_dispatch"
HOLD_ATTEMPTS_EXHAUSTED = "attempts_exhausted"

_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
#: A task an agent is actively holding is not stalled; the lease is its clock.
_IN_FLIGHT_STATES = frozenset({"claimed", "running", "needs_review", "reviewing"})
_SWEEPABLE_STATES = frozenset(
    {"open", "waiting", "blocked", "needs_input", "stopped"}
)

_TASK_ID_RE = re.compile(r"^task_[0-9a-f]{32}$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

_log = logging.getLogger("mac.task_hold_sweep")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_time(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _truthy(value: Any) -> bool:
    return value is True or _text(value).lower() in {"1", "true", "yes", "on"}


def _task_metadata(task: Any) -> Dict[str, Any]:
    return _mapping(getattr(task, "metadata", None))


def _task_state(task: Any) -> str:
    return _text(getattr(task, "state", ""))


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TaskHoldSweepConfig:
    """Operator-settable knobs, all with an env override.

    Default-off, like every other autonomous controller in the hub: enabling
    the sweep is a deliberate act because it moves tasks to terminal states.
    """

    enabled: bool = False
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS
    budget: int = DEFAULT_BUDGET
    review_ttl_seconds: float = DEFAULT_REVIEW_TTL_SECONDS
    attempt_grant: int = DEFAULT_ATTEMPT_GRANT
    max_attempt_grants: int = DEFAULT_MAX_ATTEMPT_GRANTS
    project: str = ""
    configuration_error: str = ""

    @property
    def active(self) -> bool:
        return self.enabled and not self.configuration_error

    def to_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "active": self.active}

    @classmethod
    def from_env(
        cls, environ: Optional[Mapping[str, str]] = None
    ) -> "TaskHoldSweepConfig":
        env = os.environ if environ is None else environ
        errors: List[str] = []

        def _num(name: str, default: float, low: float, high: float) -> float:
            return bounded_env_number(env, name, default, low, high, errors=errors)

        return cls(
            enabled=_truthy(env.get("MAC_HOLD_SWEEP_ENABLED")),
            interval_seconds=_num(
                "MAC_HOLD_SWEEP_INTERVAL_SECONDS",
                DEFAULT_INTERVAL_SECONDS,
                MIN_INTERVAL_SECONDS,
                MAX_INTERVAL_SECONDS,
            ),
            initial_delay_seconds=_num(
                "MAC_HOLD_SWEEP_INITIAL_DELAY_SECONDS",
                DEFAULT_INITIAL_DELAY_SECONDS,
                0.0,
                MAX_INITIAL_DELAY_SECONDS,
            ),
            budget=int(_num("MAC_HOLD_SWEEP_BUDGET", DEFAULT_BUDGET, 1, MAX_BUDGET)),
            review_ttl_seconds=_num(
                "MAC_HOLD_SWEEP_REVIEW_TTL_SECONDS",
                DEFAULT_REVIEW_TTL_SECONDS,
                MIN_REVIEW_TTL_SECONDS,
                MAX_REVIEW_TTL_SECONDS,
            ),
            attempt_grant=int(
                _num("MAC_HOLD_SWEEP_ATTEMPT_GRANT", DEFAULT_ATTEMPT_GRANT, 1, 10)
            ),
            max_attempt_grants=int(
                _num(
                    "MAC_HOLD_SWEEP_MAX_ATTEMPT_GRANTS",
                    DEFAULT_MAX_ATTEMPT_GRANTS,
                    0,
                    10,
                )
            ),
            project=_text(env.get("MAC_HOLD_SWEEP_PROJECT")),
            configuration_error="; ".join(errors),
        )


# --------------------------------------------------------------------------- #
# Hold classification and fingerprinting
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HoldClassification:
    """Why a task counts as held or stalled, and what the hold claims."""

    kind: str
    reason: str
    declaration: Dict[str, Any] = field(default_factory=dict)
    attempts_exhausted: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "attempts_exhausted": self.attempts_exhausted,
        }


def hold_declaration(task: Any) -> Dict[str, Any]:
    """Normalize whatever the hold's author left behind into one shape.

    ``metadata["hold"]`` is the structured form this sweep can actually reason
    about.  Everything else here is a compatibility read of holds that predate
    it, so an old hold is not silently unreadable (which would make every old
    task undecidable forever -- exactly today's failure).
    """

    metadata = _task_metadata(task)
    declared = _mapping(metadata.get("hold"))
    reason = _text(declared.get("reason")) or _text(metadata.get("no_dispatch_reason"))

    until_tasks: List[str] = []
    for key in ("until_task", "until_tasks", "blocked_by_task", "blocked_by"):
        raw = declared.get(key, metadata.get(key))
        values = raw if isinstance(raw, (list, tuple)) else [raw]
        for value in values:
            candidate = _text(value)
            if _TASK_ID_RE.fullmatch(candidate) and candidate not in until_tasks:
                until_tasks.append(candidate)

    replacement = ""
    for key in ("replacement_task_id", "superseded_by"):
        candidate = _text(declared.get(key, metadata.get(key)))
        if _TASK_ID_RE.fullmatch(candidate):
            replacement = candidate
            break

    # Two different claims, deliberately kept apart. ``satisfied_by`` says
    # "this task's work already landed" -> close it. ``until_change`` says
    # "the thing this hold was waiting for" -> release it. Conflating them
    # would close a task the moment its blocker merged.
    changes: List[Dict[str, Any]] = []
    until_changes: List[Dict[str, Any]] = []
    for key, sink in (
        ("satisfied_by", changes),
        ("landed_change", changes),
        ("until_change", until_changes),
    ):
        raw = declared.get(key, metadata.get(key))
        values = raw if isinstance(raw, (list, tuple)) else [raw]
        for value in values:
            change = _mapping(value)
            if change:
                sink.append(change)

    quarantine = _mapping(metadata.get("dependency_quarantine"))
    if quarantine and not reason:
        reason = "dependency quarantine: %s" % ", ".join(
            sorted(
                {
                    _text(issue.get("reason"))
                    for issue in quarantine.get("issues") or []
                    if isinstance(issue, Mapping) and _text(issue.get("reason"))
                }
            )
            or ["unresolvable dependency"]
        )

    return {
        "reason": reason,
        "disposition": _text(declared.get("disposition")).lower(),
        "until_tasks": until_tasks,
        "replacement_task_id": replacement,
        "changes": changes,
        "until_changes": until_changes,
        "dependency_quarantine": quarantine,
    }


def classify_hold(task: Any) -> Optional[HoldClassification]:
    """Return why *task* is held or stalled, or ``None`` if it is neither.

    Two populations, deliberately.  ``no_dispatch`` is a hold somebody asked
    for.  ``open`` + attempts exhausted is not a hold at all -- it is a task
    that fell out of dispatch without anyone deciding it should, and it is the
    one this sweep is forbidden to leave in place.
    """

    state = _task_state(task)
    if state in _TERMINAL_STATES or state in _IN_FLIGHT_STATES:
        return None
    if state and state not in _SWEEPABLE_STATES:
        return None

    metadata = _task_metadata(task)
    max_attempts = _int(getattr(task, "max_attempts", 0))
    attempts = _int(getattr(task, "attempt_count", 0))
    exhausted = max_attempts > 0 and attempts >= max_attempts
    declaration = hold_declaration(task)

    if _truthy(metadata.get("no_dispatch")):
        reason = declaration["reason"] or "held with no recorded reason"
        return HoldClassification(
            kind=HOLD_NO_DISPATCH,
            reason=reason,
            declaration=declaration,
            attempts_exhausted=exhausted,
        )

    if exhausted and state == "open":
        return HoldClassification(
            kind=HOLD_ATTEMPTS_EXHAUSTED,
            reason=(
                "open with attempts exhausted (%d/%d): undispatchable and "
                "invisible in every list view" % (attempts, max_attempts)
            ),
            declaration=declaration,
            attempts_exhausted=True,
        )

    return None


def hold_fingerprint(task: Any) -> str:
    """Digest of everything a verdict depended on.

    The cheap skip is only sound if this changes whenever the answer could.
    It therefore covers the hold reason AND the surrounding facts the decision
    reads: state, attempt budget, dependencies, and the citations offered.  It
    deliberately excludes ``hold_review`` itself, so recording a verdict does
    not invalidate the verdict.
    """

    declaration = hold_declaration(task)
    payload = {
        "state": _task_state(task),
        "attempt_count": _int(getattr(task, "attempt_count", 0)),
        "max_attempts": _int(getattr(task, "max_attempts", 0)),
        "no_dispatch": _truthy(_task_metadata(task).get("no_dispatch")),
        "dependencies": sorted(
            _text(item) for item in (getattr(task, "dependencies", None) or [])
        ),
        "hold": {
            "reason": declaration["reason"],
            "disposition": declaration["disposition"],
            "until_tasks": declaration["until_tasks"],
            "replacement_task_id": declaration["replacement_task_id"],
            "changes": declaration["changes"],
            "until_changes": declaration["until_changes"],
            "dependency_quarantine": declaration["dependency_quarantine"],
        },
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return "sha256:%s" % hashlib.sha256(encoded).hexdigest()


# --------------------------------------------------------------------------- #
# Change attribution: the mention-vs-citation distinction
# --------------------------------------------------------------------------- #

ATTRIBUTION_NONE = "none"
ATTRIBUTION_MENTION_ONLY = "mention_only"
ATTRIBUTION_SUBJECT = "subject"
ATTRIBUTION_BRANCH = "branch"

#: The only attributions strong enough to close a task on.
SATISFYING_ATTRIBUTIONS = frozenset({ATTRIBUTION_SUBJECT, ATTRIBUTION_BRANCH})


def _task_id_forms(task_id: str) -> List[str]:
    """Full ledger id plus the CLI's eight-hex display form."""

    forms = [task_id]
    match = re.fullmatch(r"task_([0-9a-f]{32})", task_id)
    if match is not None:
        forms.append("task_" + match.group(1)[:8])
    return forms


def _names_task(haystack: str, task_id: str) -> bool:
    if not haystack or not task_id:
        return False
    for form in _task_id_forms(task_id):
        # Boundary-checked so one task's prefix cannot match a longer hex token
        # belonging to another task.
        if re.search(r"(?<![0-9A-Za-z_])%s(?![0-9A-Za-z])" % re.escape(form), haystack):
            return True
    return False


def change_attribution(change: Mapping[str, Any], task_id: str) -> str:
    """How strongly *change* is attributed to *task_id*.

    ``subject``/``branch`` mean the change identifies itself AS this task's
    work: the id is in the commit subject, the PR title, or the branch name --
    all written by whoever produced the change, about the change.

    ``mention_only`` means the id appears solely in a commit body, PR body, or
    trailer.  That is a citation of evidence, not a claim of authorship, and it
    is the exact signal that produced 24 false positives out of 24 on
    2026-08-20.  It never satisfies a close.
    """

    if not isinstance(change, Mapping) or not _TASK_ID_RE.fullmatch(_text(task_id)):
        return ATTRIBUTION_NONE

    subject = " ".join(
        _text(change.get(key)) for key in ("subject", "title", "pr_title")
    )
    branch = " ".join(
        _text(change.get(key)) for key in ("branch", "head_ref", "ref")
    )
    body = " ".join(
        _text(change.get(key))
        for key in ("body", "message", "pr_body", "commit_message", "notes")
    )

    if _names_task(subject, task_id):
        return ATTRIBUTION_SUBJECT
    if _names_task(branch, task_id):
        return ATTRIBUTION_BRANCH
    if _names_task(body, task_id):
        return ATTRIBUTION_MENTION_ONLY
    return ATTRIBUTION_NONE


def _change_is_landed(change: Mapping[str, Any]) -> bool:
    """A citation only counts once the change is actually on canonical."""

    if _truthy(change.get("merged")) or _truthy(change.get("landed")):
        return True
    if _truthy(change.get("on_canonical")) or _truthy(change.get("ancestor_of_canonical")):
        return True
    return _text(change.get("integration_status")) in {
        "ancestor",
        "patch_equivalent",
        "merged",
    }


def change_citation(change: Mapping[str, Any], task_id: str) -> Dict[str, Any]:
    """Render one candidate change into a verdict-ready citation."""

    attribution = change_attribution(change, task_id)
    sha = _text(change.get("commit") or change.get("sha") or change.get("head_sha"))
    return {
        "attribution": attribution,
        "landed": _change_is_landed(change),
        "commit": sha if _SHA_RE.fullmatch(sha) else "",
        "subject": _text(change.get("subject") or change.get("title")),
        "branch": _text(change.get("branch") or change.get("head_ref")),
        "repository": _text(change.get("repository") or change.get("repository_url")),
        "pull_request": _text(change.get("pull_request") or change.get("pr")),
    }


def satisfying_change(
    changes: Sequence[Mapping[str, Any]], task_id: str
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (the citation that satisfies the task, every rejected citation).

    A satisfying citation must be landed AND strongly attributed AND name
    something specific (a commit or a branch).  "A merged PR mentions this id"
    is rejected here, by construction.
    """

    rejected: List[Dict[str, Any]] = []
    for change in changes or []:
        citation = change_citation(change, task_id)
        if (
            citation["attribution"] in SATISFYING_ATTRIBUTIONS
            and citation["landed"]
            and (citation["commit"] or citation["branch"])
        ):
            return citation, rejected
        rejected.append(citation)
    return None, rejected


# --------------------------------------------------------------------------- #
# Verdicts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HoldVerdict:
    task_id: str
    verdict: str
    hold_kind: str
    reason: str
    fingerprint: str
    citation: Optional[Dict[str, Any]] = None
    action: Optional[Dict[str, Any]] = None

    @property
    def acts(self) -> bool:
        return self.verdict in ACTING_VERDICTS

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        return {key: value for key, value in data.items() if value is not None}


@dataclass(frozen=True)
class LedgerView:
    """The states of every task the sweep may need to reason about.

    Seeded once per run from the sweepable population, so the common case
    costs no extra queries and every task in a run is decided against the same
    snapshot.  A hold that names a task OUTSIDE that population -- which is the
    normal case, since the thing a hold waits on is usually finished -- is
    resolved on demand and cached.  Without that, "the task this hold waits on
    completed" would be indistinguishable from "that task does not exist", and
    the sweep would report every satisfied hold as undecidable.
    """

    states: Dict[str, str] = field(default_factory=dict)
    resolver: Optional[Any] = None

    def state_of(self, task_id: str) -> str:
        key = _text(task_id)
        if key in self.states:
            return self.states[key]
        state = ""
        if self.resolver is not None and key:
            try:
                state = _task_state(self.resolver(key))
            except Exception:  # noqa: BLE001 - a missing task is an answer.
                state = ""
        self.states[key] = state
        return state

    def exists(self, task_id: str) -> bool:
        return bool(self.state_of(task_id))


def decide_verdict(
    task: Any,
    classification: HoldClassification,
    *,
    ledger: LedgerView,
    config: TaskHoldSweepConfig,
) -> HoldVerdict:
    """Decide the next appropriate state for one held or stalled task.

    Pure: it reads the task and a ledger snapshot and returns a verdict.  All
    mutation lives in :meth:`TaskHoldSweeper._apply`, so this function is
    exhaustively testable and a dry run is the same code path as a real one.
    """

    task_id = _text(getattr(task, "id", ""))
    fingerprint = hold_fingerprint(task)
    declaration = classification.declaration
    metadata = _task_metadata(task)
    prior = _mapping(metadata.get(HOLD_REVIEW_KEY))

    attempts = _int(getattr(task, "attempt_count", 0))
    max_attempts = _int(getattr(task, "max_attempts", 0))
    raised_budget = max(max_attempts, attempts) + config.attempt_grant

    def verdict(name: str, reason: str, **extra: Any) -> HoldVerdict:
        return HoldVerdict(
            task_id=task_id,
            verdict=name,
            hold_kind=classification.kind,
            reason=reason,
            fingerprint=fingerprint,
            citation=extra.get("citation"),
            action=extra.get("action"),
        )

    def release_action() -> Dict[str, Any]:
        """Release, and hand back a budget the task can actually run on.

        Releasing an exhausted task without raising its budget would move it
        from "held" to "open and undispatchable" -- creating by hand the exact
        invisible state this sweep exists to remove.
        """

        action: Dict[str, Any] = {"op": "release"}
        if classification.attempts_exhausted:
            action["max_attempts"] = raised_budget
        return action

    def release_note() -> str:
        if not classification.attempts_exhausted:
            return ""
        return "; attempts were exhausted (%d/%d), so the budget is raised to %d" % (
            attempts,
            max_attempts,
            raised_budget,
        )

    # 1. Explicitly no longer wanted.  Somebody already decided; the sweep is
    #    only carrying the decision out.
    if declaration["disposition"] in {"not_wanted", "abandoned", "obsolete"}:
        return verdict(
            VERDICT_CANCELLED,
            "hold declares the work no longer wanted: %s"
            % (declaration["reason"] or declaration["disposition"]),
            action={
                "op": "cancel",
                "disposition": "not_applicable",
                "reason": "no longer wanted: %s"
                % (declaration["reason"] or declaration["disposition"]),
            },
        )

    # 2. Superseded -- close NAMING what replaced it.  A replacement id that is
    #    not in the ledger is not a name, it is a typo.
    replacement = declaration["replacement_task_id"]
    if replacement and replacement != task_id:
        if not ledger.exists(replacement):
            return verdict(
                VERDICT_UNDECIDABLE,
                "hold names replacement %s, which is not in the ledger"
                % replacement,
            )
        return verdict(
            VERDICT_CANCELLED,
            "superseded by %s" % replacement,
            citation={"kind": "replacement_task", "task_id": replacement},
            action={
                "op": "cancel",
                "disposition": "superseded",
                "reason": "superseded by %s" % replacement,
                "replacement_task_id": replacement,
            },
        )

    # 3. Work already landed -- close CITING THE CHANGE.  A citation whose only
    #    link is a mention is reported, never acted on.
    citation, rejected = satisfying_change(declaration["changes"], task_id)
    if citation is not None:
        named = citation["commit"] or citation["branch"]
        return verdict(
            VERDICT_CANCELLED,
            "work already landed in %s (%s)"
            % (named, citation["subject"] or "no subject recorded"),
            citation={"kind": "landed_change", **citation},
            action={
                "op": "cancel",
                "disposition": "not_applicable",
                "reason": "work already landed in %s (%s)"
                % (named, citation["subject"] or "no subject recorded"),
            },
        )
    mention_only = [
        item for item in rejected if item["attribution"] == ATTRIBUTION_MENTION_ONLY
    ]
    if mention_only:
        return verdict(
            VERDICT_UNDECIDABLE,
            "the only change offered merely MENTIONS this task id (%s); a "
            "mention is evidence someone cited the task, not proof the work "
            "landed" % (mention_only[0]["commit"] or mention_only[0]["subject"] or "?"),
            citation={"kind": "rejected_mention", **mention_only[0]},
        )

    # 4. The hold's stated condition is satisfied -> release, citing what
    #    satisfied it.
    until_tasks = declaration["until_tasks"]
    if until_tasks:
        unknown = [item for item in until_tasks if not ledger.exists(item)]
        if unknown:
            return verdict(
                VERDICT_UNDECIDABLE,
                "hold waits on %s, which is not in the ledger" % ", ".join(unknown),
            )
        states = {item: ledger.state_of(item) for item in until_tasks}
        dead = {
            item: state
            for item, state in states.items()
            if state in {"failed", "cancelled"}
        }
        if dead:
            return verdict(
                VERDICT_UNDECIDABLE,
                "hold waits on %s, which ended %s without completing; releasing "
                "or closing this is a human judgement"
                % (", ".join(sorted(dead)), "/".join(sorted(set(dead.values())))),
            )
        pending = sorted(item for item, state in states.items() if state != "completed")
        if not pending:
            return verdict(
                VERDICT_RELEASED,
                "hold reason satisfied: %s completed%s"
                % (", ".join(sorted(until_tasks)), release_note()),
                citation={
                    "kind": "completed_tasks",
                    "task_ids": sorted(until_tasks),
                },
                action=release_action(),
            )
        # Still waiting: a live, re-justified hold.  Fall through to step 6 so
        # the ledger records that it was looked at.
        still_valid_reason = "hold still waits on %s (%s)" % (
            ", ".join(pending),
            "/".join(sorted({states[item] or "unknown" for item in pending})),
        )
    elif declaration["until_changes"]:
        # "Release me when that change lands."  No task attribution is required
        # here -- the change belongs to whatever unblocked this, not to this
        # task -- but it must name something specific and be on canonical.
        landed = [
            change_citation(change, task_id)
            for change in declaration["until_changes"]
            if _change_is_landed(change)
        ]
        named = [item for item in landed if item["commit"] or item["branch"]]
        if len(named) == len(declaration["until_changes"]) and named:
            return verdict(
                VERDICT_RELEASED,
                "hold reason satisfied: %s landed%s"
                % (
                    ", ".join(item["commit"] or item["branch"] for item in named),
                    release_note(),
                ),
                citation={"kind": "landed_blocker", "changes": named},
                action=release_action(),
            )
        still_valid_reason = (
            "hold waits on a change that has not landed: %s"
            % (classification.reason or "unnamed change")
        )
    elif declaration["dependency_quarantine"]:
        still_valid_reason = (
            "dependency quarantine is unresolved: %s" % classification.reason
        )
    else:
        still_valid_reason = ""

    # 5. Attempts exhausted.  `open` + exhausted must NOT persist -- it is
    #    undispatchable and invisible -- so this branch always acts.
    if classification.attempts_exhausted:
        grants = _int(prior.get("attempt_grants"), 0)
        if grants < config.max_attempt_grants:
            # A hold whose reason is still live keeps its hold: raising the
            # budget fixes the undispatchable state, releasing it would
            # override a decision that is still standing.
            release = classification.kind == HOLD_NO_DISPATCH and not still_valid_reason
            return verdict(
                VERDICT_BUDGET_RAISED,
                "attempts exhausted (%d/%d) and the work is still wanted; "
                "raising the budget to %d (grant %d of %d)%s"
                % (
                    attempts,
                    max_attempts,
                    raised_budget,
                    grants + 1,
                    config.max_attempt_grants,
                    ("; the hold stays: %s" % still_valid_reason)
                    if still_valid_reason
                    else "",
                ),
                action={
                    "op": "raise_attempts",
                    "max_attempts": raised_budget,
                    "release": release,
                },
            )
        if still_valid_reason and classification.kind == HOLD_NO_DISPATCH:
            # Held, exhausted, and its reason is still live. It is not the
            # invisible case -- a held task is visibly held -- so it is not
            # closed on a budget technicality.
            return verdict(
                VERDICT_REVIEWED_STILL_VALID,
                "%s; attempts are exhausted but the hold, not the budget, is "
                "what is keeping it parked" % still_valid_reason,
            )
        return verdict(
            VERDICT_CANCELLED,
            "attempts exhausted (%d/%d) after %d sweep-granted retr%s; closed "
            "deliberately rather than left open and undispatchable"
            % (
                attempts,
                max_attempts,
                grants,
                "y" if grants == 1 else "ies",
            ),
            action={
                "op": "cancel",
                "disposition": "failed_attempt",
                "reason": "attempt budget exhausted (%d/%d) after %d "
                "sweep-granted retr%s"
                % (attempts, max_attempts, grants, "y" if grants == 1 else "ies"),
            },
        )

    # 6. Re-examined and still justified.  Recording this is the point: it
    #    makes a re-justified hold a different object from a forgotten one.
    if still_valid_reason:
        return verdict(VERDICT_REVIEWED_STILL_VALID, still_valid_reason)

    # 7. Nothing cheap to decide on.  Leave it alone and say so.
    return verdict(
        VERDICT_UNDECIDABLE,
        "held with no machine-checkable condition (%s); a human or a richer "
        "signal must decide" % (classification.reason or "no reason recorded"),
    )


# --------------------------------------------------------------------------- #
# The sweeper
# --------------------------------------------------------------------------- #


class TaskHoldSweeper:
    """Hours-scale controller that advances held/stalled tasks.

    Concurrency: two overlapping runs must not double-act.  Three things make
    that true.  ``_run_lock`` keeps one process to a single run.  Every action
    re-reads the task and re-checks its fingerprint immediately before writing,
    so a task another run already moved fails its precondition and is skipped.
    And every action is idempotent in its own right -- releasing an unheld
    task, raising a budget that is already raised, or cancelling a cancelled
    task all reduce to no-ops.
    """

    def __init__(self, control_plane: Any, config: TaskHoldSweepConfig) -> None:
        self.control_plane = control_plane
        self.config = config
        self._stop_event = threading.Event()
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._last_report: Optional[Dict[str, Any]] = None
        #: How much of the backlog the current run skipped as unchanged.
        self._skipped_unchanged = 0

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> bool:
        if not self.config.active:
            if self.config.configuration_error:
                self._observe(
                    "hold.sweep.configuration_invalid",
                    "warning",
                    {"error": self.config.configuration_error},
                )
            return False
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            thread = threading.Thread(
                target=self._loop, name="mac-hold-sweeper", daemon=True
            )
            self._thread = thread
            thread.start()
        self._observe("hold.sweep.started", "info", {"config": self.config.to_dict()})
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop_event.set()
        with self._state_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        stopped = thread is None or not thread.is_alive()
        if stopped:
            self._observe("hold.sweep.stopped", "info", {})
        return stopped

    def status(self) -> Dict[str, Any]:
        with self._state_lock:
            thread = self._thread
            last_report = copy.deepcopy(self._last_report)
        return {
            "schema": HOLD_SWEEP_SCHEMA,
            "config": self.config.to_dict(),
            "thread_alive": bool(thread is not None and thread.is_alive()),
            "run_active": self._run_lock.locked(),
            "last_report": last_report,
        }

    def _loop(self) -> None:
        if self._stop_event.wait(max(0.0, self.config.initial_delay_seconds)):
            return
        while not self._stop_event.is_set():
            try:
                self.run_once(trigger="scheduled")
            except Exception:  # noqa: BLE001 - a future tick must still run.
                _log.warning("hold-sweep tick failed", exc_info=True)
            if self._stop_event.wait(max(0.01, self.config.interval_seconds)):
                return

    # -- core ---------------------------------------------------------------

    def run_once(
        self,
        *,
        actor: str = "hold-sweeper",
        trigger: str = "operator",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Examine at most ``config.budget`` held/stalled tasks and act."""

        if not self._run_lock.acquire(blocking=False):
            # A second run in this process is not queued behind the first: by
            # the time it woke, the first has already read a fresher snapshot.
            return {
                "schema": HOLD_SWEEP_SCHEMA,
                "status": "busy",
                "trigger": trigger,
                "verdicts": [],
            }
        run_id = "sweep_%s" % uuid.uuid4().hex
        started = _utcnow()
        verdicts: List[Dict[str, Any]] = []
        examined = 0
        errors = 0
        try:
            tasks = self._sweepable_tasks()
            ledger = LedgerView(
                states={
                    _text(getattr(task, "id", "")): _task_state(task)
                    for task in tasks
                    if _text(getattr(task, "id", ""))
                },
                resolver=getattr(self.control_plane, "get_task", None),
            )
            candidates = self._candidates(tasks, now=started)
            deferred = max(0, len(candidates) - self.config.budget)
            for task, classification in candidates[: self.config.budget]:
                examined += 1
                try:
                    decided = decide_verdict(
                        task, classification, ledger=ledger, config=self.config
                    )
                    record = decided.to_dict()
                    record["applied"] = (
                        self._apply(decided, task, actor=actor, run_id=run_id)
                        if not dry_run
                        else {"status": "dry_run"}
                    )
                except Exception as exc:  # noqa: BLE001 - isolate one task.
                    errors += 1
                    record = {
                        "task_id": _text(getattr(task, "id", "")),
                        "verdict": "error",
                        "hold_kind": classification.kind,
                        "reason": str(exc)[:500],
                    }
                verdicts.append(record)
        finally:
            self._run_lock.release()

        counts: Dict[str, int] = {}
        for record in verdicts:
            key = _text(record.get("verdict")) or "unknown"
            counts[key] = counts.get(key, 0) + 1
        report = {
            "schema": HOLD_SWEEP_SCHEMA,
            "run_id": run_id,
            "status": "ok",
            "trigger": trigger,
            "dry_run": bool(dry_run),
            "started_at": _iso(started),
            "budget": self.config.budget,
            "examined": examined,
            # A bounded sweep that says nothing about what it did not reach
            # reads as "the backlog is clean" when it is merely truncated.
            "deferred_over_budget": deferred,
            "skipped_unchanged": self._skipped_unchanged,
            "verdict_counts": counts,
            "errors": errors,
            "verdicts": verdicts,
        }
        with self._state_lock:
            self._last_report = report
        self._observe(
            "hold.sweep.run",
            "warning" if errors else "info",
            {key: value for key, value in report.items() if key != "verdicts"},
        )
        return report

    # -- candidate selection ------------------------------------------------

    def _sweepable_tasks(self) -> List[Any]:
        """Every non-terminal task, filtered in SQL where the plane supports it."""

        states = sorted(_SWEEPABLE_STATES)
        try:
            return list(
                self.control_plane.list_tasks(
                    state=states, project=self.config.project or None
                )
            )
        except TypeError:
            pass
        except Exception as exc:  # noqa: BLE001
            _log.warning("hold-sweep could not list tasks: %s", exc)
            return []
        try:
            return list(self.control_plane.list_tasks())
        except Exception as exc:  # noqa: BLE001
            _log.warning("hold-sweep could not list tasks: %s", exc)
            return []

    def _candidates(
        self, tasks: Sequence[Any], *, now: datetime
    ) -> List[Tuple[Any, HoldClassification]]:
        """Held/stalled tasks that are due for review, oldest review first.

        Ordering matters as much as the budget does: a run that always starts
        from the same end of the list would re-decide the same head forever and
        never reach the tail.  Never-reviewed tasks sort first, then the least
        recently reviewed.
        """

        self._skipped_unchanged = 0
        due: List[Tuple[str, Any, HoldClassification]] = []
        for task in tasks:
            if self.config.project and _text(getattr(task, "project", "")) != self.config.project:
                continue
            classification = classify_hold(task)
            if classification is None:
                continue
            marker = _mapping(_task_metadata(task).get(HOLD_REVIEW_KEY))
            if self._skip_unchanged(task, marker, now=now):
                self._skipped_unchanged += 1
                continue
            due.append((_text(marker.get("reviewed_at")), task, classification))
        due.sort(key=lambda item: (item[0] != "", item[0]))
        return [(task, classification) for _, task, classification in due]

    def _skip_unchanged(
        self, task: Any, marker: Mapping[str, Any], *, now: datetime
    ) -> bool:
        """Cheap skip: same hold, already reviewed, still inside the TTL."""

        if not marker:
            return False
        if _text(marker.get("fingerprint")) != hold_fingerprint(task):
            return False
        reviewed_at = _parse_time(marker.get("reviewed_at"))
        if reviewed_at is None:
            return False
        return (now - reviewed_at).total_seconds() < self.config.review_ttl_seconds

    # -- application --------------------------------------------------------

    def _apply(
        self, verdict: HoldVerdict, task: Any, *, actor: str, run_id: str
    ) -> Dict[str, Any]:
        """Carry out one verdict, re-checking its preconditions first."""

        task_id = verdict.task_id
        try:
            current = self.control_plane.get_task(task_id)
        except Exception as exc:  # noqa: BLE001 - a vanished task is not an error.
            return {"status": "unreadable", "detail": str(exc)[:200]}

        state = _task_state(current)
        if state in _TERMINAL_STATES:
            # Another run (or a human) already moved it. Nothing to double-act.
            return {"status": "already_terminal", "state": state}
        if hold_fingerprint(current) != verdict.fingerprint:
            return {"status": "changed_under_run"}

        marker = _mapping(_task_metadata(current).get(HOLD_REVIEW_KEY))
        if _text(marker.get("run_id")) not in {"", run_id} and _text(
            marker.get("fingerprint")
        ) == verdict.fingerprint:
            reviewed_at = _parse_time(marker.get("reviewed_at"))
            if reviewed_at is not None and (
                _utcnow() - reviewed_at
            ).total_seconds() < self.config.review_ttl_seconds:
                return {"status": "claimed_by_concurrent_run", "run_id": marker.get("run_id")}

        action = verdict.action or {}
        op = _text(action.get("op"))
        applied: Dict[str, Any] = {"status": "recorded", "op": op or "none"}

        if op == "cancel":
            detail = {
                "reason": action["reason"],
                "disposition": action["disposition"],
                "closed_by": "hold-sweep",
                "hold_sweep": {
                    "schema": HOLD_SWEEP_SCHEMA,
                    "run_id": run_id,
                    "verdict": verdict.verdict,
                    "citation": verdict.citation,
                },
            }
            if action.get("replacement_task_id"):
                detail["replacement_task_id"] = action["replacement_task_id"]
            # Close FIRST, record after. A marker written before a close that
            # then fails would claim the task was cancelled while it is still
            # parked -- and, because the fingerprint matches, the cheap skip
            # would suppress re-examining it for a whole review TTL.
            self.control_plane.close_task(task_id, "cancelled", actor, detail)
            self._record_review(current, verdict, actor=actor, run_id=run_id)
            applied["status"] = "cancelled"
            return applied

        if op == "raise_attempts":
            self._record_review(
                current,
                verdict,
                actor=actor,
                run_id=run_id,
                grant=True,
                max_attempts=_int(action.get("max_attempts")),
            )
            if action.get("release"):
                self.control_plane.release_task(task_id, actor=actor)
            applied["status"] = "budget_raised"
            applied["max_attempts"] = _int(action.get("max_attempts"))
            return applied

        if op == "release":
            raised = _int(action.get("max_attempts"))
            if raised:
                # Budget before release, always. The reverse order leaves the
                # task open and exhausted in the window between the two calls,
                # and permanently so if the second one fails -- which is the
                # invisible state this sweep exists to remove.
                self._record_review(
                    current,
                    verdict,
                    actor=actor,
                    run_id=run_id,
                    grant=True,
                    max_attempts=raised,
                )
                self.control_plane.release_task(task_id, actor=actor)
                applied["max_attempts"] = raised
            else:
                self.control_plane.release_task(task_id, actor=actor)
                self._record_review(current, verdict, actor=actor, run_id=run_id)
            applied["status"] = "released"
            return applied

        # reviewed_still_valid / undecidable: the task's state, hold and budget
        # are left exactly as they were.  Only the review record is written --
        # and for an undecidable task that record is the report, nothing more.
        self._record_review(current, verdict, actor=actor, run_id=run_id)
        return applied

    def _record_review(
        self,
        task: Any,
        verdict: HoldVerdict,
        *,
        actor: str,
        run_id: str,
        grant: bool = False,
        max_attempts: int = 0,
    ) -> None:
        """Write the reviewed-at marker (and, for a grant, the new budget).

        Through the narrow control-plane write, not ``update_task``: the marker
        must not be able to re-normalize or reject the rest of a held task's
        metadata, some of which is controller-owned.
        """

        prior = _mapping(_task_metadata(task).get(HOLD_REVIEW_KEY))
        marker = {
            "schema": HOLD_REVIEW_SCHEMA,
            "reviewed_at": _iso(_utcnow()),
            "reviewed_by": actor,
            "run_id": run_id,
            "verdict": verdict.verdict,
            "hold_kind": verdict.hold_kind,
            "reason": verdict.reason,
            "fingerprint": verdict.fingerprint,
            "review_count": _int(prior.get("review_count"), 0) + 1,
            "attempt_grants": _int(prior.get("attempt_grants"), 0) + (1 if grant else 0),
        }
        if verdict.citation:
            marker["citation"] = verdict.citation
        self.control_plane.record_task_hold_review(
            _text(getattr(task, "id", "")),
            marker,
            actor=actor,
            max_attempts=max_attempts if (grant and max_attempts > 0) else None,
        )

    # -- telemetry ----------------------------------------------------------

    def _observe(self, event_type: str, level: str, detail: Dict[str, Any]) -> None:
        try:
            self.control_plane.record_log(
                event_type,
                layer="control_plane",
                source="hold-sweeper",
                level=level,
                subject_type="service",
                subject_id="hold-sweeper",
                detail=detail,
            )
        except Exception:  # noqa: BLE001 - telemetry must not stop the sweep.
            _log.warning("could not record hold-sweep telemetry", exc_info=True)


__all__ = [
    "ACTING_VERDICTS",
    "ATTRIBUTION_BRANCH",
    "ATTRIBUTION_MENTION_ONLY",
    "ATTRIBUTION_NONE",
    "ATTRIBUTION_SUBJECT",
    "HOLD_ATTEMPTS_EXHAUSTED",
    "HOLD_NO_DISPATCH",
    "HOLD_REVIEW_KEY",
    "HOLD_REVIEW_SCHEMA",
    "HOLD_SWEEP_SCHEMA",
    "SATISFYING_ATTRIBUTIONS",
    "VERDICTS",
    "VERDICT_BUDGET_RAISED",
    "VERDICT_CANCELLED",
    "VERDICT_RELEASED",
    "VERDICT_REVIEWED_STILL_VALID",
    "VERDICT_UNDECIDABLE",
    "HoldClassification",
    "HoldVerdict",
    "LedgerView",
    "TaskHoldSweepConfig",
    "TaskHoldSweeper",
    "change_attribution",
    "change_citation",
    "classify_hold",
    "decide_verdict",
    "hold_declaration",
    "hold_fingerprint",
    "satisfying_change",
]
