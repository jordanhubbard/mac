"""Who decides that a task should become several tasks.

Until now: the framework did, on every task. The executor prompt carried five
numbered steps explaining how to fan out, unconditionally, followed by one
hedged sentence permitting the agent not to:

    1. Do NOT attempt to implement all steps in one run.
    2. Break the work into 2-10 focused child tasks.
    ...
    If the task IS a single atomic work item ... execute it directly and skip
    step 1-5.

Five imperatives for splitting, one conditional for not splitting. The sizing
heuristic that could have counterweighted it only ever spoke when it detected a
plan; when it decided a task was atomic it said nothing at all, so the agent
read a fan-out recipe with no opposing evidence.

The measured result: a task whose description read "run `command -v` on these
sixteen commands and report what you find" -- and which the detector correctly
scored as NOT a plan, zero signals -- was split into five child tasks. Machine
-originated work in this ledger completes at 0-9.6% against 20% for
human-filed, and over-decomposition is one of the things manufacturing it.

So the default inverts. The framework no longer proposes decomposition; the
SUBMITTER declares it, with a budget:

    metadata.decomposition = {"max_children": 6, "kind": "one per subsystem"}

Absent that, the task is atomic and the agent is told so plainly. The heuristic
survives as an OBSERVATION -- when it thinks a task is really a plan and nobody
authorised splitting, it says so and asks the agent to report it, which is a
question for the submitter rather than a licence to fan out.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

#: Where a submitter declares intent.
DECOMPOSITION_KEY = "decomposition"

#: The pre-existing hard "never split this" flag. It still wins over
#: everything, including an explicit budget: a task carrying both is
#: contradictory, and refusing is the safe reading.
NO_DECOMPOSE_KEY = "no_decompose"

#: A ceiling on what a submitter may authorise. Not a policy about good task
#: design -- a bound on blast radius, so a typo in a budget cannot enqueue a
#: thousand tasks.
MAX_AUTHORISED_CHILDREN = 20


class DecompositionBudget:
    """What the submitter authorised, if anything."""

    def __init__(
        self,
        *,
        authorised: bool,
        max_children: int = 0,
        kind: str = "",
        reason: str = "",
    ) -> None:
        self.authorised = bool(authorised)
        self.max_children = int(max_children)
        self.kind = str(kind or "")
        self.reason = str(reason or "")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "authorised": self.authorised,
            "max_children": self.max_children,
            "kind": self.kind,
            "reason": self.reason,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "DecompositionBudget(%r)" % self.to_dict()


def _metadata_of(task: Any) -> Mapping[str, Any]:
    record = task.to_dict() if hasattr(task, "to_dict") else task
    if not isinstance(record, Mapping):
        return {}
    metadata = record.get("metadata")
    return metadata if isinstance(metadata, Mapping) else {}


def decomposition_budget(task: Any) -> DecompositionBudget:
    """What this task's submitter authorised.

    Default is NOT authorised. That is the whole change: a task is one task
    unless somebody said otherwise.
    """
    metadata = _metadata_of(task)

    if metadata.get(NO_DECOMPOSE_KEY):
        return DecompositionBudget(authorised=False, reason="the task carries no_decompose")

    raw = metadata.get(DECOMPOSITION_KEY)
    if raw is None:
        return DecompositionBudget(
            authorised=False,
            reason="the submitter did not authorise decomposition",
        )

    # A bare integer is the obvious shorthand for "at most this many".
    if isinstance(raw, bool):
        # `decomposition: true` says split but not how much. Refuse rather than
        # invent a budget -- inventing one is how the framework got here.
        return DecompositionBudget(
            authorised=False,
            reason="decomposition must state max_children, not just true",
        )
    if isinstance(raw, int):
        return _budget_from(raw, "")
    if isinstance(raw, Mapping):
        return _budget_from(raw.get("max_children"), str(raw.get("kind") or ""))

    return DecompositionBudget(authorised=False, reason="decomposition metadata is not readable")


def _budget_from(max_children: Any, kind: str) -> DecompositionBudget:
    try:
        limit = int(max_children)
    except (TypeError, ValueError):
        return DecompositionBudget(authorised=False, reason="max_children is not a number")
    if limit <= 0:
        return DecompositionBudget(authorised=False, reason="max_children is not positive")
    if limit > MAX_AUTHORISED_CHILDREN:
        limit = MAX_AUTHORISED_CHILDREN
    return DecompositionBudget(authorised=True, max_children=limit, kind=kind)


def check_children_allowed(task: Any, count: int) -> Tuple[bool, str]:
    """Whether *count* children may be created for *task*, and why not.

    Enforced at the control plane, not only described in the prompt. Prompt
    text is advice to a model; this is the part that holds when the model
    decides otherwise.
    """
    budget = decomposition_budget(task)
    if not budget.authorised:
        return False, (
            "this task did not authorise decomposition (%s). The submitter "
            'declares it: metadata.decomposition = {"max_children": N, '
            '"kind": "..."}. Splitting work nobody asked to split is how a '
            "one-command task becomes five tasks." % budget.reason
        )
    if count > budget.max_children:
        return False, (
            "this task authorised at most %d child task(s); %d were requested"
            % (budget.max_children, count)
        )
    return True, ""


def prompt_section(task: Any, *, is_plan: bool, signals: Any = ()) -> str:
    """The decomposition text for the executor prompt.

    Unauthorised is the common case and gets the SHORT, unambiguous answer.
    The five-step recipe appears only when somebody asked for it.
    """
    budget = decomposition_budget(task)
    signal_text = ", ".join(str(s) for s in (signals or ()))

    if not budget.authorised:
        lines = [
            "Task Sizing:",
            "This task is ATOMIC. Do NOT create child tasks; do NOT post to the "
            "children endpoint. Execute it and write your evidence to "
            "mac-evidence.json.",
        ]
        if is_plan:
            # The heuristic disagrees. That is a question for the submitter,
            # not a licence to fan out.
            lines.append(
                "NOTE: automated sizing thinks this may really be a plan (%s). "
                "Do not act on that by splitting it. Do the part that is "
                "unambiguous, and say so in your evidence summary so the "
                "submitter can decide whether to re-file it with a "
                "decomposition budget." % (signal_text or "no signals recorded")
            )
        return "\n".join(lines)

    lines = [
        "Task Sizing and Plan Decomposition:",
        "The submitter AUTHORISED decomposition: at most %d child task(s)." % budget.max_children,
    ]
    if budget.kind:
        lines.append("They asked for them to be split by: %s" % budget.kind)
    if is_plan and signal_text:
        lines.append("Automated sizing agrees this is a plan (%s)." % signal_text)
    lines.extend(
        [
            "If -- and only if -- the work genuinely has independent deliverables:",
            "  1. Do NOT attempt to implement all of them in one run.",
            "  2. Create at most %d focused child tasks. Each must be independently"
            " completable and verifiable by a different agent." % budget.max_children,
            "  3. Post them to the MAC API children endpoint for this task.",
            "  4. Write mac-evidence.json with evidence_type=operator_result, a summary,"
            " and a result listing the child task titles you created.",
            "  5. Exit -- the parent will block on its children.",
            "If it is really one item, execute it directly. An authorised budget is a"
            " ceiling, not a quota: creating fewer children, or none, is a valid outcome.",
        ]
    )
    return "\n".join(lines)
