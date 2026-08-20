"""Is this task bounded enough to hand to a worker?

WHY THIS EXISTS

MAC files a title and a description and lets the agent work out the scope.
That is the input to the failure that dominated 2026-08-19/20: an agent reads
an unbounded task, concludes it must decompose, cannot create children, emits
``plan_decomposed`` with zero children, and dies non-retryable at attempt 1 of
3. Every one of the 24 failures in that window was the contract gate rejecting
work that was never bounded enough to do.

Two earlier fixes went at the executor: stop it entering a phase it cannot
complete, and move the sizing decision to the agent (ADR 0016). Neither
addresses the input. An agent deciding well still needs something decidable.
A two-line rename -- ``_assert_task_actor`` dropping its ``task_id`` parameter
-- was split into "a rename child plus two test children" and died. It was
already atomic. What was missing was any statement that it was.

WHAT A SCOPE PACKET IS

Four statements, in ``metadata.scope_packet``, that between them close every
question a worker would otherwise have to answer by searching:

    outcome        the ONE thing that must be true when this is done
    current_state  what is true at the current head that contradicts it
    surface        the paths this worker owns -- its write authority and its
                   search bound
    validation     the check that proves the outcome

The prior art is horde-claw-fleet's ``fleet-plan-scope`` skill and its
ADR-0121, which requires a scope packet before write authority is granted.
Its vocabulary is not copied: it is shaped around a task-graph model with
ownership slices, first inspection points and node dependencies, and MAC has
no task graph. Dependencies here are ledger edges (``dependencies[]``), not a
packet field; search bounds and the ownership slice are the same thing in a
worktree, so they are one field called ``surface``.

WHAT THIS MODULE IS NOT

It does not decide whether the gate is ENFORCED. Per ADR 0022 a gate takes an
evidence value and returns a named decision; fetching and policy belong to the
caller. :func:`evaluate` is pure -- no hub, no database, no task model -- which
is why the whole vocabulary is testable without a fleet.

FILING AN UNBOUNDED TASK STAYS LEGAL

The ledger is also an inbox and a place to think. What changes is that an
unbounded task is not DISPATCHABLE until it is bounded, and that is said out
loud at filing time and named by ``why-unclaimed`` rather than being silent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

SCHEMA = "mac.task_scope_packet.v1"

#: Where a submitter puts it.
SCOPE_PACKET_KEY = "scope_packet"

#: Project metadata key that turns the allocator gate on for a project.
#:
#: Off by default, and deliberately so: every task already in the ledger
#: predates the packet, so a global default-on would stop dispatch for the
#: entire backlog in one commit. The DECISION is computed and reported
#: everywhere regardless -- ``why-unclaimed`` and ``task preflight`` name an
#: unbounded scope whether or not it is being enforced -- so a project can see
#: what enforcing would cost before it opts in.
REQUIRE_SCOPE_PACKET_KEY = "require_scope_packet"

#: Bounded means all four are present and say something.
REQUIRED_FIELDS: Tuple[str, ...] = (
    "outcome",
    "current_state",
    "surface",
    "validation",
)

#: Recorded when present, never required. ``exclusions`` narrows the surface;
#: ``start_at`` is where to look first; ``notes`` is everything else.
OPTIONAL_FIELDS: Tuple[str, ...] = ("exclusions", "start_at", "notes")

#: ``surface`` and ``exclusions`` are path lists; the rest are prose.
_LIST_FIELDS: Tuple[str, ...] = ("surface", "exclusions")

#: What each required field has to answer, in the words the operator sees. One
#: vocabulary, per ADR 0022 -- these strings go into the CLI, the filing
#: advisory and ``why-unclaimed`` unchanged rather than being paraphrased.
FIELD_PROMPTS: Dict[str, str] = {
    "outcome": "the ONE thing that must be true when this task is done",
    "current_state": (
        "what is true at the current head that the outcome contradicts"
    ),
    "surface": "the repository paths this worker owns (a list)",
    "validation": "the check that proves the outcome",
}

#: A field filled in to get past the gate rather than to answer it. Compared
#: case-folded against the whole stripped value, so a sentence CONTAINING
#: "unknown" is fine -- only a value that is nothing but a placeholder is not.
_PLACEHOLDERS = frozenset(
    {
        "-", "--", "?", "??", "n/a", "na", "none", "nil", "null", "tbd",
        "todo", "to do", "unknown", "unspecified", "as needed", "see above",
        "see description", "same", "x", "xxx",
    }
)

#: Shorter than this is not a statement. Set low on purpose: the job here is to
#: catch empties and placeholders, not to grade prose. ``pytest -q`` is eight
#: characters and is a perfectly good ``validation``.
MIN_FIELD_CHARS = 6

# -- outcomes ---------------------------------------------------------------

BOUNDED = "bounded"
UNBOUNDED = "unbounded"

# -- reason codes (closed set) ----------------------------------------------
#
# ADR 0022: there must be no way to answer "no" without saying which "no", and
# a new rejection path cannot be added anonymously. Renaming one of these is a
# breaking change once it reaches an operator or a dashboard.

#: Every required field is present and says something.
SCOPE_BOUNDED = "scope_bounded"
#: No ``metadata.scope_packet`` at all. The common case, and the one the
#: 2026-08-19/20 failures were made of.
SCOPE_PACKET_MISSING = "scope_packet_missing"
#: A packet is there but is not an object, so nothing can be read from it.
SCOPE_PACKET_MALFORMED = "scope_packet_malformed"
#: A packet is there and some required field is absent, empty, or a
#: placeholder. ``missing_fields`` says which.
SCOPE_PACKET_INCOMPLETE = "scope_packet_incomplete"

SCOPE_REASON_CODES: Tuple[str, ...] = (
    SCOPE_BOUNDED,
    SCOPE_PACKET_MISSING,
    SCOPE_PACKET_MALFORMED,
    SCOPE_PACKET_INCOMPLETE,
)

#: The allocator rejection this maps to. Defined here rather than imported from
#: mac.allocator so the vocabulary has one home; the allocator re-exports it.
TASK_SCOPE_UNBOUNDED = "task_scope_unbounded"


@dataclass(frozen=True)
class ScopeDecision:
    """A named decision about one task's scope.

    Not a bool. The reason a gate refused is the return value, not a second
    system that has to agree with it -- which is exactly how ``why-unclaimed``
    came to print a title and two attempt counters for a task nothing could
    take.
    """

    outcome: str
    code: str
    message: str
    missing_fields: Tuple[str, ...] = ()
    present_fields: Tuple[str, ...] = ()
    packet: Mapping[str, Any] = field(default_factory=dict)

    @property
    def bounded(self) -> bool:
        return self.outcome == BOUNDED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": SCHEMA,
            "outcome": self.outcome,
            "code": self.code,
            "message": self.message,
            "bounded": self.bounded,
            "missing_fields": list(self.missing_fields),
            "present_fields": list(self.present_fields),
        }


def _clean_text(value: Any) -> str:
    """The value as a statement, or "" if it is not one."""
    if isinstance(value, (list, tuple)):
        # A prose field given as a list of lines still says something.
        value = " ".join(str(item) for item in value)
    if value is None or isinstance(value, (Mapping, bool)):
        return ""
    text = str(value).strip()
    if len(text) < MIN_FIELD_CHARS:
        return ""
    if text.casefold() in _PLACEHOLDERS:
        return ""
    return text


def _clean_paths(value: Any) -> List[str]:
    """A path list, or [] if nothing usable is there.

    A single string is accepted as a one-entry list: ``surface:
    "src/mac/cli.py"`` is what a submitter writes for a one-file change, and
    refusing it would teach people that the gate is pedantic rather than
    useful.
    """
    if isinstance(value, str):
        candidates: Iterable[Any] = [value]
    elif isinstance(value, (list, tuple, set)):
        candidates = value
    else:
        return []
    paths: List[str] = []
    for item in candidates:
        if isinstance(item, (Mapping, bool)) or item is None:
            continue
        text = str(item).strip()
        # No MIN_FIELD_CHARS here: "Makefile" is eight characters but "cli.py"
        # is six and "setup.py" a legitimate whole surface. A path is a path.
        if not text or text.casefold() in _PLACEHOLDERS:
            continue
        paths.append(text)
    return paths


def packet_of(metadata: Any) -> Any:
    """Pull the raw packet out of task metadata without interpreting it."""
    if not isinstance(metadata, Mapping):
        return None
    return metadata.get(SCOPE_PACKET_KEY)


def evaluate(packet: Any) -> ScopeDecision:
    """Decide whether *packet* bounds a task. Pure: no I/O, no task model.

    Takes the packet itself rather than a task so the same function serves
    filing, the allocator snapshot, ``task preflight`` and the executor's
    sizing -- four callers that fetch four different things and must not be
    allowed to disagree about what "bounded" means.
    """
    if packet is None:
        return ScopeDecision(
            outcome=UNBOUNDED,
            code=SCOPE_PACKET_MISSING,
            message=(
                "no metadata.scope_packet: the worker would have to resolve "
                "the outcome, the current-head defect, the paths it owns and "
                "the validating check by searching. State them: %s."
                % _field_list(REQUIRED_FIELDS)
            ),
            missing_fields=tuple(REQUIRED_FIELDS),
        )
    if not isinstance(packet, Mapping):
        return ScopeDecision(
            outcome=UNBOUNDED,
            code=SCOPE_PACKET_MALFORMED,
            message=(
                "metadata.scope_packet is %s, not an object with %s"
                % (type(packet).__name__, _field_list(REQUIRED_FIELDS))
            ),
            missing_fields=tuple(REQUIRED_FIELDS),
        )

    cleaned: Dict[str, Any] = {}
    missing: List[str] = []
    for name in REQUIRED_FIELDS:
        raw = packet.get(name)
        value: Any = _clean_paths(raw) if name in _LIST_FIELDS else _clean_text(raw)
        if not value:
            missing.append(name)
        else:
            cleaned[name] = value
    for name in OPTIONAL_FIELDS:
        raw = packet.get(name)
        value = _clean_paths(raw) if name in _LIST_FIELDS else _clean_text(raw)
        if value:
            cleaned[name] = value

    present = tuple(name for name in REQUIRED_FIELDS if name in cleaned)
    if missing:
        return ScopeDecision(
            outcome=UNBOUNDED,
            code=SCOPE_PACKET_INCOMPLETE,
            message=(
                "metadata.scope_packet does not state %s"
                % _field_list(tuple(missing))
            ),
            missing_fields=tuple(missing),
            present_fields=present,
            packet=cleaned,
        )
    return ScopeDecision(
        outcome=BOUNDED,
        code=SCOPE_BOUNDED,
        message="scope is bounded: %s" % cleaned["outcome"],
        present_fields=present,
        packet=cleaned,
    )


def evaluate_metadata(metadata: Any) -> ScopeDecision:
    """:func:`evaluate` for callers holding whole task metadata."""
    return evaluate(packet_of(metadata))


def evaluate_task(task: Any) -> ScopeDecision:
    """:func:`evaluate` for callers holding a Task model or its dict."""
    record = task.to_dict() if hasattr(task, "to_dict") else task
    metadata = record.get("metadata") if isinstance(record, Mapping) else None
    return evaluate_metadata(metadata)


def gate_enforced(project_metadata: Any) -> bool:
    """Does this project refuse to dispatch unbounded tasks?

    Read from the PROJECT rather than a global switch so a project can adopt
    the gate once its own backlog carries packets, without stranding every
    other project's open tasks the moment this ships.
    """
    if not isinstance(project_metadata, Mapping):
        return False
    value = project_metadata.get(REQUIRE_SCOPE_PACKET_KEY)
    if isinstance(value, str):
        return value.strip().casefold() in {"true", "1", "yes", "on"}
    return bool(value)


def _field_list(names: Iterable[str]) -> str:
    return ", ".join(
        "%s (%s)" % (name, FIELD_PROMPTS[name])
        if name in FIELD_PROMPTS
        else name
        for name in names
    )


def explain(decision: ScopeDecision, *, enforced: Optional[bool] = None) -> str:
    """One line a filer, an operator, or a blocking caller can use verbatim."""
    if decision.bounded:
        return "scope bounded: " + str(decision.packet.get("outcome") or "")
    prefix = "scope unbounded"
    if enforced is True:
        prefix = "scope unbounded (not dispatchable)"
    elif enforced is False:
        prefix = "scope unbounded (advisory: this project does not enforce it yet)"
    return "%s: %s" % (prefix, decision.message)


def filing_advisory(decision: ScopeDecision, *, enforced: bool = False) -> str:
    """What to tell somebody who just filed an unbounded task.

    Filing succeeds -- the ledger is an inbox. This is the part that stops the
    task from being unbounded SILENTLY, which is the whole difference between
    a task waiting to be scoped and a task waiting for nothing.
    """
    if decision.bounded:
        return ""
    lines = [
        "mac: this task is filed but %s."
        % (
            "is NOT dispatchable yet"
            if enforced
            else "has no bounded scope"
        ),
        "  %s" % decision.message,
    ]
    if decision.missing_fields:
        lines.append(
            "  Add metadata.scope_packet with: %s"
            % ", ".join(decision.missing_fields)
        )
    lines.append(
        "  Filing it was legal; a worker cannot bound it for you. See "
        "`mac task preflight --scope-packet-file <file>`."
    )
    return "\n".join(lines)
