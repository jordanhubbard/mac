"""Keep the part of a contract run's output that says WHY it failed.

Every gate in this repo that runs ``scripts/run-contract-tests.sh`` gets back
one large blob and has to store a small one. The reduction is where the reason
was being lost, and it was being lost by POSITION: a blind ``out[-2000:]``.

``run-contract-tests.sh`` prints, in this order:

    1. a session header and several hundred lines of pytest progress
    2. the failure and pytest's "short test summary info"
    3. an unconditional whole-repo ``coverage report`` -- one row per source
       file, ~14KB in this repo
    4. a coverage summary whose floors both PASSED
    5. OpenShell's generic "ssh exited with status 1"

and only then exits with the saved pytest status. So the last 2000 bytes of a
FAILING run are the coverage table's tail, a passing coverage line, and a
generic transport message. Every string in :data:`CONTRACT_VERDICT_SIGNATURES`
lives in the discarded prefix, which made a genuine rejection unclassifiable by
construction: read as a dead harness, no verdict signed, review retried,
identical output produced again. Observed 2026-08-20 -- six tasks retried 3-4
times each over ~6 hours while `completed` stayed frozen and all three agents
sat idle.

Head-and-tail is not sufficient either. The reason sits in region 2, between
several hundred lines of progress and a 14KB table, out of reach of both ends.
Position is simply the wrong selector. :func:`capture_failure_window` anchors
on the text that ANNOUNCES the failure and keeps a window around its last
occurrence, so a rejection still reads as a rejection after truncation.

Deliberately pure -- no I/O, no hub, no subprocess -- so both the hub verifier
and the publication merge gate can share one implementation, and so it can be
tested directly against real failure transcripts instead of only through a live
task.
"""
from __future__ import annotations

from typing import List, Sequence, Tuple

__all__ = [
    "CONTRACT_VERDICT_SIGNATURES",
    "CONTRACT_FAILURE_ANCHORS",
    "capture_failure_window",
    "failure_reason_line",
]


#: Output signatures proving the gate RAN AND JUDGED THE CHANGE WANTING.
#:
#: Checked BEFORE the "harness could not run" signatures, and they win, because
#: the two overlap in exactly the case that matters.
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
#: work.
CONTRACT_VERDICT_SIGNATURES: Tuple[str, ...] = (
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
CONTRACT_FAILURE_ANCHORS: Tuple[str, ...] = (
    "short test summary info",
) + CONTRACT_VERDICT_SIGNATURES


def capture_failure_window(
    output: str,
    *,
    head: int = 1500,
    window: int = 2000,
    tail: int = 1000,
    anchors: Sequence[str] = CONTRACT_FAILURE_ANCHORS,
) -> str:
    """Keep the head, the TAIL, and the part that says why the run failed.

    The head carries the session header (which sandbox, which command); the
    tail carries the exit; the anchored window carries the reason. Omitted
    regions are replaced by an explicit ``... [N chars omitted] ...`` marker so
    a reader can tell a short run from a reduced one.

    Idempotent in the way that matters: applying it to its own result keeps the
    anchored window, because the window still contains the anchor. That is what
    makes it safe at a second gate downstream of the first, which is where the
    blind tail kept coming back.
    """

    text = (output or "").strip()
    if len(text) <= head + window + tail:
        return text

    spans = [(0, head), (len(text) - tail, len(text))]
    lowered = text.lower()
    anchor_at = max([lowered.rfind(a) for a in anchors] or [-1])
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


def failure_reason_line(
    output: str,
    *,
    limit: int = 300,
    anchors: Sequence[str] = CONTRACT_FAILURE_ANCHORS,
) -> str:
    """One line naming why the run failed, for callers with no room for more.

    An eviction reason is truncated to 200 characters and a publication error
    is read by a human in a ledger; neither can hold an excerpt. They were
    given a constant instead ("full repository contract test failed"), which is
    true of every failing gate and diagnostic of none.

    Returns the line holding the LAST anchor -- pytest's count line, the stale
    generated artifact, the documentation contract -- skipping pytest's ``===``
    banners, which announce a section rather than state a result. Falls back to
    the last non-empty line when nothing is recognised, because an unrecognised
    reason is still a better answer than a fixed sentence. Returns ``""`` for
    empty output, so callers can supply their own wording for "no output".
    """

    text = (output or "").strip()
    if not text:
        return ""

    lines = text.splitlines()
    lowered = text.lower()
    anchor_at = max([lowered.rfind(a) for a in anchors] or [-1])
    index = len(lines) - 1
    if anchor_at >= 0:
        consumed = 0
        for position, line in enumerate(lines):
            consumed += len(line) + 1
            if consumed > anchor_at:
                index = position
                break

    for line in lines[index:]:
        candidate = line.strip().strip("=").strip()
        if candidate:
            return candidate[:limit]
    for line in reversed(lines):
        candidate = line.strip()
        if candidate:
            return candidate[:limit]
    return ""
