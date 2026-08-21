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

This module also owns the step BEFORE classification: deciding which bytes of
a long failing run survive to be classified at all. The two belong together,
because a classifier is only as good as the text it is handed and every
classifier here keys on strings that a blind tail throws away. Keeping the
capture next to the signatures it is derived from is what makes "the excerpt
still contains something a classifier can act on" checkable rather than hoped
for -- and it gives every stage that bounds the same output one function to
call instead of a tail of its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

__all__ = [
    "ContractFailureCause",
    "ContractFailure",
    "classify_contract_failure",
    "VERDICT_SIGNATURES",
    "FAILURE_ANCHORS",
    "capture_failure_window",
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
    if "plan_decomposed" in text or "no code was changed" in text or (
        "decompos" in text and "children" in text
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
    if "push_rejected" in text or "rejected" in text and "push" in text or "non-fast-forward" in text:
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


#: Output signatures proving a contract run RAN AND JUDGED THE CHANGE WANTING,
#: as opposed to a harness that died before reaching a judgement.
#:
#: These lived in `mac.services`, next to the transport-fault signatures they
#: are checked against. They moved here so the capture below can be derived
#: from them in the same file: the anchors and the signatures are one
#: invariant -- "the excerpt keeps what the classifier reads" -- and two
#: modules cannot hold one invariant between them. `mac.services` re-exports
#: this tuple under its original name.
#:
#: Every entry must appear ONLY on failure. That is the whole discipline here,
#: and it is easy to get wrong in the direction that reintroduces PR #478:
#: `coverage safety:` was an obvious-looking candidate and is emitted whether
#: the floors pass or fail, so it would have marked a passed-then-the-stream-
#: died run as a rejection -- exactly the bug #478 existed to fix. Likewise
#: `repository contract` appears in "running fail-fast repository contract
#: preflight", which is a start message, not a verdict.
#:
#: When in doubt leave a signature OUT. A missing signature means a real
#: rejection is retried as "unavailable", which wastes a run. A wrong one
#: means a transport fault is signed as a rejection, which discards correct
#: work.
VERDICT_SIGNATURES: Tuple[str, ...] = (
    "documentation contract failed",
    "is stale:",
    "stale generated",
    "regenerate with",
    "contract test failed",
    " failed, ",         # pytest summary: "3 failed, 40 passed"
    "assertionerror",
    "error: process completed with exit code",
)


#: Where the reason for a failure is announced, in the order a run prints it.
#: "short test summary info" is pytest's own answer to "what failed"; the rest
#: are the verdict signatures, so anything a classifier can act on is kept.
FAILURE_ANCHORS: Tuple[str, ...] = ("short test summary info",) + VERDICT_SIGNATURES


def capture_failure_window(
    output: str,
    *,
    anchors: Sequence[str] = FAILURE_ANCHORS,
    head: int = 1500,
    window: int = 2000,
    tail: int = 1000,
) -> str:
    """Keep the head, the TAIL, and the part that says why the run failed.

    Head-and-tail is not enough here, which is the trap that replaced a blind
    tail. A failing contract run prints, in order: a session header, several
    hundred lines of pytest progress, the failure and its summary, a whole-repo
    coverage report (one row per source file, ~14KB), a coverage line whose
    floors both PASSED, and -- last -- OpenShell's generic "ssh exited with
    status 1". The verdict sits in the middle, out of reach of both ends, so a
    fixed head and tail preserve the two regions that say nothing and drop the
    only one that does.

    Position is the wrong selector. Anchor on the text that announces the
    failure and keep a window around its LAST occurrence, so the excerpt still
    carries a verdict signature after truncation and a rejection stays
    classifiable as a rejection.

    Safe to apply more than once, which is what lets every stage that bounds
    the same output call THIS instead of slicing a tail of its own: re-running
    it re-finds the same anchor and keeps the reason on every pass. A blind
    `[-2000:]` applied downstream instead cuts the anchored middle back out and
    restores the original bug one layer further down -- which is exactly what
    the publication gate was doing to the hub's already-anchored capture.
    """

    text = (output or "").strip()
    if len(text) <= head + window + tail:
        return text

    spans = [(0, head), (len(text) - tail, len(text))]
    lowered = text.lower()
    found = [lowered.rfind(anchor.lower()) for anchor in anchors]
    anchor_at = max(found) if found else -1
    if anchor_at >= 0:
        start = max(0, anchor_at - window // 4)
        spans.append((start, min(len(text), start + window)))

    merged: List[Tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    parts: List[str] = []
    previous = 0
    for start, end in merged:
        if start > previous:
            parts.append("... [%d chars omitted] ..." % (start - previous))
        parts.append(text[start:end])
        previous = end
    return "\n".join(parts)
