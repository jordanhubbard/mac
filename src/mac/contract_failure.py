"""Why a contract verification failed — as a named cause, not one message.

ADR 0022. In the 24 hours to 2026-08-20 the fleet failed 24 tasks and every
one carried the same diagnosis:

    Contract verification failed — work was not pushed/accepted (see evidence).

That sentence is true of every contract failure and diagnostic of none. Its
remediation told the agent to commit and push everything -- correct advice for
one of the causes below and actively misleading for the rest. An agent that
never wrote code cannot fix its problem by committing harder, and an operator
reading the ledger could not tell which of these had happened without opening
the output tail by hand.

The causes are genuinely different and want different responses:

  planned_instead_of_implementing  the agent decomposed and wrote no code
  hub_write_unavailable            it needed the hub and could not reach it
  nothing_committed                code was written, nothing was committed
  untracked_files_left             committed, but new files were left behind
  push_rejected                    pushed, and the remote refused it
  unclassified                     none of the above matched

Deliberately a pure function over evidence, with no I/O: it is the shape ADR
0022 asks for, and it means the classifier can be tested directly against real
failure text rather than only through a live task.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

__all__ = [
    "ContractFailureCause",
    "ContractFailure",
    "classify_contract_failure",
]


class ContractFailureCause:
    PLANNED_INSTEAD_OF_IMPLEMENTING = "planned_instead_of_implementing"
    HUB_WRITE_UNAVAILABLE = "hub_write_unavailable"
    NOTHING_COMMITTED = "nothing_committed"
    UNTRACKED_FILES_LEFT = "untracked_files_left"
    PUSH_REJECTED = "push_rejected"
    UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class ContractFailure:
    """A named cause plus the advice that actually follows from it."""

    cause: str
    problem: str
    remediation: str


def classify_contract_failure(
    blob: str,
    *,
    problems_text: str = "",
    error: str = "",
) -> ContractFailure:
    """Classify a contract failure from the executor's own output.

    ``blob`` is the lowercased haystack the caller already assembles from the
    failure detail, output tail and evidence. Matching on text is not ideal --
    the executor should emit a typed cause and this should read it -- but the
    text is what exists today, and a wrong-but-named cause is still far more
    useful than one message for every failure. When the signals disagree the
    EARLIER checks win, because they describe a task that never got as far as
    the later ones: an agent that wrote no code cannot also have left untracked
    files.
    """

    text = (blob or "").lower()
    detail = (problems_text or error or "see evidence")[:280]

    # Ordered most-upstream first. Each cause describes a task that stopped at
    # a different point, so the first match is the earliest stopping point.
    if (
        "plan_decomposed" in text
        or "no code was changed" in text
        or ("decompos" in text and "children" in text)
    ):
        return ContractFailure(
            cause=ContractFailureCause.PLANNED_INSTEAD_OF_IMPLEMENTING,
            problem=(
                "The agent planned the work instead of doing it: it emitted a "
                "decomposition and changed no code (%s)." % detail
            ),
            remediation=(
                "This is not an agent mistake to retry -- the task was dispatched "
                "without a bounded scope, so the agent could not tell it was "
                "atomic. Bound the scope (ownership slice, paths, exclusions, "
                "expected result, validation) and re-dispatch, or split it "
                "deliberately. See ADR 0016 and ADR 0020."
            ),
        )
    if (
        "did not authorise decomposition" in text
        or "mac_worker_token" in text
        or "mac_token" in text
        or ("localhost" in text and "8789" in text)
    ):
        return ContractFailure(
            cause=ContractFailureCause.HUB_WRITE_UNAVAILABLE,
            problem=(
                "The agent needed to write to the hub and could not: the sandbox "
                "holds no hub credential and cannot reach the hub (%s)." % detail
            ),
            remediation=(
                "An environment fault, not a task fault. The sandbox is denied "
                "hub authority on purpose (_HOST_ONLY_HUB_CREDENTIALS). The "
                "executor should not enter a phase requiring hub writes; if the "
                "phase is needed, the host performs the write on the agent's "
                "behalf. Do not widen the sandbox's credentials."
            ),
        )
    if (
        "push_rejected" in text
        or "rejected" in text
        and "push" in text
        or "non-fast-forward" in text
    ):
        return ContractFailure(
            cause=ContractFailureCause.PUSH_REJECTED,
            problem="The branch was pushed and the remote refused it (%s)." % detail,
            remediation=(
                "Usually a stale base or a protected-branch rule. Rebase onto the "
                "current head and push again; if a branch rule refused it, the "
                "task needs a different target branch, not another attempt."
            ),
        )
    if "untracked" in text or "staged-new" in text or "leave no untracked" in text:
        return ContractFailure(
            cause=ContractFailureCause.UNTRACKED_FILES_LEFT,
            problem=(
                "Work was committed but new files were left untracked or staged "
                "rather than committed (%s)." % detail
            ),
            remediation=(
                "Run `git add -A` and commit EVERY new file before declaring "
                "done, then push. A partially committed tree is why the "
                "verifier could not reproduce the work."
            ),
        )
    if "refusing to push" in text or "pushed=false" in text or "nothing to commit" in text:
        return ContractFailure(
            cause=ContractFailureCause.NOTHING_COMMITTED,
            problem="Code may have been written but nothing was committed (%s)." % detail,
            remediation=(
                "Commit the work and push the branch before declaring done. If "
                "there was genuinely nothing to change, the task should record a "
                "no-change result explicitly rather than finishing silently."
            ),
        )
    return ContractFailure(
        cause=ContractFailureCause.UNCLASSIFIED,
        problem="Contract verification failed — work was not pushed/accepted (%s)." % detail,
        remediation=(
            "The cause did not match a known signature, so read the output tail "
            "directly. If this recurs, add a signature: an unclassified contract "
            "failure is the one case this classifier cannot help with, and it "
            "should be rare enough to notice."
        ),
    )
