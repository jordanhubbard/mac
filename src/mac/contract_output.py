"""What a contract run's output says, and how much of it is worth keeping.

Two questions are asked of every hub contract verification, and until now the
answers lived inside ``services`` where only the review path could reach them:

  * **Did the gate judge the change, or did the harness die?**
    ``unavailable_reason`` -- #478 then #522.
  * **Which bytes of a multi-megabyte run do we keep?**
    ``failure_window_excerpt`` -- the anchored capture.

They belong together and they belong outside ``services``, because the review
path is not the only caller. ``merge_queue.validate_projected_merge_contract``
runs the SAME verification (``_hub_verify_run_contract_test`` is its default
``test_runner``) at publication time, and re-truncated the answer with a blind
``output_tail[-2000:]`` -- the exact tail this repository already established
cannot reach the reason a run failed. The anchored capture was applied and then
undone one layer downstream, so the publication gate reported "full repository
contract test failed" and nothing else.

A pure module with no I/O, like :mod:`mac.contract_failure`: the capture and
classification rules can then be tested directly against real failure text
rather than only through a live verification.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

__all__ = [
    "VERDICT_SIGNATURES",
    "UNAVAILABLE_SIGNATURES",
    "FAILURE_ANCHORS",
    "unavailable_reason",
    "head_and_tail_excerpt",
    "failure_window_excerpt",
]

#: Output signatures that mean the verification COULD NOT RUN, as opposed to
#: ran and found the change wanting.
#:
#: These are transport and environment faults of the harness itself. On
#: 2026-08-19 every review in a 90-minute window was rejected, and the signed
#: verdict for one of them ended:
#:
#:     coverage safety: statements 69300/76238 (90.90%, floor 90.00%);
#:                      branches   20216/24618 (82.12%, floor 80.00%)
#:       - Uploading files to /sandbox...
#:       + Files uploaded
#:     Error:   x ssh exited with status exit status: 1
#:
#: BOTH COVERAGE FLOORS PASSED. The gate the run exists to enforce was
#: satisfied, and the coding-agent's ssh stream then died. That exit status
#: became `rejected`, signed, and indistinguishable downstream from a reviewer
#: judging the work deficient. One task was rejected, redone more thoroughly
#: (58 tests -> 60, 2 files -> 11, ruff and a CodeGraph audit added), and
#: rejected identically, because the verdict never depended on the diff.
#: Output signatures proving the gate RAN AND JUDGED THE CHANGE WANTING.
#:
#: Checked BEFORE the unavailable signatures, and they win, because the two
#: overlap in exactly the case that matters.
#:
#: `ssh exited with status` is the generic wrapper exit printed whenever a
#: remote command returns non-zero -- it accompanies every failure through the
#: ssh transport, not only a transport fault. Listing it as "unavailable"
#: (2026-08-19, PR #478) therefore inverted the original bug instead of fixing
#: it. Before, a transport death was signed as a rejection. After, a genuine
#: rejection was swallowed as "could not verify", so NO verdict was signed and
#: the task sat in REVIEWING forever.
#:
#: Observed live on 2026-08-20: twelve `hub_verify_unavailable` events in
#: ninety minutes whose real failures were
#:     "documentation contract failed: published shell fences outside the
#:      executable book are forbidden"
#: and
#:     "documentation-inventory.md is stale: regenerate with
#:      scripts/generate-docs-reference.py --write"
#: -- both real, actionable, and both discarded. Twenty tasks accumulated in
#: REVIEWING, five of them for over a hundred hours.
#:
#: The discriminator is whether the gate reached a judgement. It is not
#: "did the gate produce output": in the #478 case the coverage gate ran and
#: PASSED before the stream died, so output alone would have called that a
#: rejection too. Only an explicit FAILING verdict counts.
#: Every entry must appear ONLY on failure. That is the whole discipline here,
#: and it is easy to get wrong in the direction that reintroduces #478:
#: `coverage safety:` was an obvious-looking candidate and is emitted whether
#: the floors pass or fail, so it would have marked the original
#: passed-then-the-stream-died run as a rejection -- exactly the bug #478
#: existed to fix. Likewise `repository contract` appears in
#: "running fail-fast repository contract preflight", which is a start
#: message, not a verdict.
#:
#: When in doubt leave a signature OUT. A missing signature means a real
#: rejection is retried as "unavailable", which wastes a run. A wrong one
#: means a transport fault is signed as a rejection, which discards correct
#: work and is what this pair of fixes is for.
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


UNAVAILABLE_SIGNATURES: Tuple[str, ...] = (
    # cursor-agent's stream transport, the observed cause
    "ssh exited with status",
    "connection reset by peer",
    "connection refused",
    "retriableerror",
    "resource_exhausted",
    # no route to run anything at all
    "no acceptable coding agent",
    "agent_binary_missing",
    "sandbox_policy_denied",
    # the harness never got far enough to test the change
    "failed to create sandbox",
    "error: could not create sandbox",
)


#: Where the reason for a failure is announced, in the order a run prints it.
#: "short test summary info" is pytest's own answer to "what failed"; the rest
#: are the verdict signatures, so anything the classifier can act on is kept.
FAILURE_ANCHORS: Tuple[str, ...] = ("short test summary info",) + VERDICT_SIGNATURES


def unavailable_reason(output: str) -> Optional[str]:
    """The signature saying this run could not verify anything, if present.

    Returning a reason means "we do not know whether the change is good" --
    which must NOT be recorded as a rejection. A signature over "rejected" is
    a claim the evidence does not support, and downstream nothing can tell it
    apart from a real verdict.

    Deliberately narrow. An unrecognised failure stays a rejection, because
    treating unknown failures as infrastructure would let a genuinely broken
    change pass through as "could not verify" and retry forever -- failing
    open on the gate this exists to enforce.
    """
    text = (output or "").lower()
    # A gate that judged the change wanting is a REJECTION, whatever the
    # transport did afterwards. Checked first because the two sets overlap:
    # a real contract failure still exits through ssh and still prints
    # "ssh exited with status".
    for verdict in VERDICT_SIGNATURES:
        if verdict in text:
            return None
    for signature in UNAVAILABLE_SIGNATURES:
        if signature in text:
            return signature
    return None


def head_and_tail_excerpt(output: str, *, head: int = 2000, tail: int = 1500) -> str:
    """Keep the head AND the tail of a rejected verification's output.

    The tail alone was kept, and the tail of a pytest run is its summary line:
    "36 failed, 84 passed, 588 errors". That says the gate failed, which the
    verdict already said. WHY it failed is announced once, at the top -- an
    unprovisionable database, a bootstrap that never finished, a collection
    error -- and 500 characters of tail cannot reach it.

    Diagnosing one such rejection took a hub-side archaeology session that
    ended in the evidence being gone: the sandbox that produced it had been
    cleaned up, and nothing else recorded the run.

    Still position-based, so still blind to a reason that sits in the middle:
    use :func:`failure_window_excerpt` for anything a classifier reads.
    """

    text = (output or "").strip()
    if len(text) <= head + tail:
        return text
    omitted = len(text) - head - tail
    return "%s\n... [%d chars omitted] ...\n%s" % (
        text[:head],
        omitted,
        text[-tail:],
    )


def failure_window_excerpt(
    output: str,
    *,
    head: int = 1500,
    window: int = 2000,
    tail: int = 1000,
    anchors: Sequence[str] = FAILURE_ANCHORS,
) -> str:
    """Keep the head, the TAIL, and the part that says why the run failed.

    Head-and-tail is not enough here, which is the trap this replaced a blind
    tail with. A failing contract run prints, in order: a session header,
    several hundred lines of pytest progress, the failure and its summary, a
    whole-repo coverage report (one row per source file, ~14KB), a coverage
    line whose floors both PASSED, and -- last -- OpenShell's generic
    "ssh exited with status 1". The verdict sits in the middle, out of reach
    of both ends, so a fixed head and tail preserve the two regions that say
    nothing and drop the only one that does.

    Position is the wrong selector. Anchor on the text that announces the
    failure and keep a window around its LAST occurrence, so the excerpt still
    carries a verdict signature after truncation and a rejection stays
    classifiable as a rejection.

    Idempotent on its own output by construction: an excerpt is shorter than
    the budget unless the anchored window was itself bigger, and it still
    contains the anchor. Callers handed an already-excerpted string should
    nonetheless pass it through unchanged rather than re-truncating it --
    re-truncation is what put the blind tail back at the publication gate.
    """

    text = (output or "").strip()
    if len(text) <= head + window + tail:
        return text

    spans = [(0, head), (len(text) - tail, len(text))]
    lowered = text.lower()
    found = [lowered.rfind(a) for a in anchors]
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
