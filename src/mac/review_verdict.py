"""The three axes a review actually measures, and the verdict they resolve to.

A review answers three independent questions, and for most of this system's
life it recorded the answer to all three in one boolean:

H -- **harness**: did the review machinery run at all?  Did the sandbox come
     up, did bootstrap finish, did the repository's tests get as far as being
     *collected*?  A failure here says nothing whatsoever about the work.
R -- **reproducibility**: given a harness that ran, did the repository's own
     checks reproduce green on the reviewed commit?  A failure here is about
     the change, but it is not a human judgement -- it is a fact the executor
     can go and fix.
S -- **semantics**: did the reviewer judge the work correct, complete, and
     safe?  This is the only axis that carries an opinion.

Collapsing them cost real work.  On task_4ce995cb (2026-08-13) a worker
submitted a correct one-line regression test three times.  All three reviews
came back ``rejected`` -- not on the merits, but because the review harness
blew up: attempts 1 and 3 reported ``36 failed, 84 passed, 588 errors``, and
attempt 2 hit a sandbox ``UnicodeEncodeError`` that was fixed eleven hours
later.  ``attempt_count`` increments at claim time, so each harness failure had
already spent an attempt before any judgement about the work existed.  The task
reached 3/3, went terminal, and the post-mortem classifier -- reconstructing
"was this infrastructure?" from free text after the fact -- labelled it
``scope``, whose operator remediation is "decompose".  That advice was actively
wrong for a one-line change.  An equivalent task filed afterwards succeeded
unchanged.

The repair is to stop reconstructing the axes from prose.  Whoever *observed*
the failure knows which axis it belongs to, so they record it, and the verdict
follows deterministically:

    harness failed                     -> ``infrastructure``
    semantics rejected                 -> ``rejected``
    reproducibility failed             -> ``tests_failed``
    everything passed                  -> ``approved``

``infrastructure`` is not a judgement, so it never consumes an attempt and
never records an opinion about the work.  ``rejected`` and ``tests_failed``
both are judgements, so both do.  Which of the two it is stays visible, because
"a reviewer thinks this design is wrong" and "four tests are red" want
different responses from the next attempt.

This module is deliberately dependency-free (no ``mac.services``, no
``mac.models``) so the deterministic finalizer can import it from a subprocess
without dragging in the control-plane import tree -- the same constraint
``mac.review_failure_classifier`` operates under.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping, NamedTuple, Optional, Tuple


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Every check passed and the reviewer agreed.
VERDICT_APPROVED = "approved"
#: The reviewer judged the work deficient.  A statement about the work.
VERDICT_REJECTED = "rejected"
#: The harness ran and the repository's own checks did not reproduce green.
#: A statement about the work, but a mechanical one.
VERDICT_TESTS_FAILED = "tests_failed"
#: The review machinery failed.  NOT a statement about the work.
VERDICT_INFRASTRUCTURE = "infrastructure"

#: The complete canonical verdict vocabulary.  Nothing else may be signed.
CANONICAL_VERDICTS: frozenset[str] = frozenset(
    {
        VERDICT_APPROVED,
        VERDICT_REJECTED,
        VERDICT_TESTS_FAILED,
        VERDICT_INFRASTRUCTURE,
    }
)

#: Verdicts that assert something about the quality of the work, and therefore
#: spend one of the task's bounded attempts.  ``infrastructure`` is absent on
#: purpose: it is the whole point of the distinction.
ATTEMPT_CONSUMING_VERDICTS: frozenset[str] = frozenset(
    {VERDICT_REJECTED, VERDICT_TESTS_FAILED}
)

#: Axis outcomes.  ``skipped`` means the axis does not apply to this review
#: (non-repository work has no reproducibility axis), which is distinct from
#: "passed" and must not be read as evidence either way.
AXIS_PASS = "pass"
AXIS_FAIL = "fail"
AXIS_SKIPPED = "skipped"

#: Schema tag for the structured axis block carried on verdict evidence.
REVIEW_AXES_SCHEMA = "mac.review_axes.v1"


def canonical_verdict(value: Any) -> str:
    """Normalise *value* to a canonical verdict, failing closed.

    An unknown or malformed verdict must never read as an approval, and it must
    never read as ``infrastructure`` either -- that would let a broken change
    retry forever on the strength of a typo.  It degrades to ``rejected``,
    which is the conservative answer in both directions.
    """
    text = str(value or "").strip().lower()
    return text if text in CANONICAL_VERDICTS else VERDICT_REJECTED


def verdict_consumes_attempt(verdict: Any) -> bool:
    """Whether *verdict* spends one of the task's attempts."""
    return canonical_verdict(verdict) in ATTEMPT_CONSUMING_VERDICTS


# ---------------------------------------------------------------------------
# The axes
# ---------------------------------------------------------------------------


class ReviewAxes(NamedTuple):
    """The three axis outcomes plus the reason each one landed where it did."""

    harness: str = AXIS_PASS
    reproducibility: str = AXIS_SKIPPED
    semantics: str = AXIS_SKIPPED
    harness_reason: str = ""
    reproducibility_reason: str = ""
    semantics_reason: str = ""

    def verdict(self) -> str:
        """Resolve the axes to a canonical verdict.

        Order matters and is not arbitrary.  H is evaluated first because a
        harness that did not run produced no information about R or S -- their
        values are not merely unknown, they are meaningless.  S precedes R
        because a reviewer who read the change and rejected it has said
        something a red test suite has not; both spend an attempt, so nothing
        is lost by reporting the more specific of the two.
        """
        if self.harness == AXIS_FAIL:
            return VERDICT_INFRASTRUCTURE
        if self.semantics == AXIS_FAIL:
            return VERDICT_REJECTED
        if self.reproducibility == AXIS_FAIL:
            return VERDICT_TESTS_FAILED
        return VERDICT_APPROVED

    def reason(self) -> str:
        """The reason belonging to the axis that decided the verdict."""
        if self.harness == AXIS_FAIL:
            return self.harness_reason
        if self.semantics == AXIS_FAIL:
            return self.semantics_reason
        if self.reproducibility == AXIS_FAIL:
            return self.reproducibility_reason
        return ""

    def evidence(self) -> Dict[str, Any]:
        """The structured block written onto verdict evidence.

        Recorded as three named fields rather than a boolean so that a reader
        -- an operator, the observe UI, or a later hub decision -- never has to
        re-derive "was this infrastructure?" from free text.
        """
        return {
            "schema": REVIEW_AXES_SCHEMA,
            "harness": self.harness,
            "harness_reason": self.harness_reason,
            "reproducibility": self.reproducibility,
            "reproducibility_reason": self.reproducibility_reason,
            "semantics": self.semantics,
            "semantics_reason": self.semantics_reason,
            "verdict": self.verdict(),
            "attempt_consumed": verdict_consumes_attempt(self.verdict()),
        }


def review_axes_from_evidence(raw: Any) -> Optional[ReviewAxes]:
    """Rebuild :class:`ReviewAxes` from an ``evidence()`` block, or ``None``."""
    if not isinstance(raw, Mapping):
        return None
    if str(raw.get("schema") or "") != REVIEW_AXES_SCHEMA:
        return None

    def axis(key: str) -> str:
        value = str(raw.get(key) or "").strip().lower()
        return value if value in {AXIS_PASS, AXIS_FAIL, AXIS_SKIPPED} else AXIS_SKIPPED

    return ReviewAxes(
        harness=axis("harness"),
        reproducibility=axis("reproducibility"),
        semantics=axis("semantics"),
        harness_reason=str(raw.get("harness_reason") or ""),
        reproducibility_reason=str(raw.get("reproducibility_reason") or ""),
        semantics_reason=str(raw.get("semantics_reason") or ""),
    )


# ---------------------------------------------------------------------------
# Reading a gate run: which axis failed?
# ---------------------------------------------------------------------------

#: pytest's own announcements that it could not even *collect* the suite.
#: A collection error means the tests never ran, so the run measured nothing --
#: which is a harness outcome, not a red suite.  task_4ce995cb's three
#: rejections all carried 588 of these.
_COLLECTION_ERROR_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"(\d+)\s+errors?\s+during\s+collection", re.I),
    re.compile(r"interrupted:\s*(\d+)\s+errors?", re.I),
)

#: The per-file form pytest prints above the summary.  Counted when no summary
#: number is available (a run killed before its summary still shows these).
_COLLECTING_ERROR_LINE = re.compile(r"^ERROR\s+\S+", re.M)

#: pytest's failure count from the terminal summary line.
_FAILED_COUNT = re.compile(r"\b(\d+)\s+failed\b", re.I)

#: The environment never got far enough to test anything.  Kept separate from
#: mac.services._HUB_VERIFY_UNAVAILABLE_SIGNATURES because this module must not
#: import the control plane; the two lists overlap on purpose and the services
#: one stays authoritative for the transport faults it names.
#: Every entry must be a phrase that can ONLY appear when the environment
#: broke.  Anything that also shows up in an ordinary red suite (``failed``,
#: ``assertionerror``, ``conftest.py``, a bare ``error:``) would silently
#: convert real rejections into infinite retries, which is the failure mode
#: this list is one step away from at all times.
_HARNESS_SIGNATURES: Tuple[str, ...] = (
    "errors during collection",
    "error collecting",
    "internalerror",
    "unicodeencodeerror",
    "out of shared memory",
    "no space left on device",
    "could not connect to server",
    "failed to create sandbox",
    "could not create sandbox",
    "no acceptable coding agent",
    "agent_binary_missing",
    "sandbox_policy_denied",
    "connection reset by peer",
    "connection refused",
    "retriableerror",
    "resource_exhausted",
)


def collection_error_count(output: Any) -> int:
    """How many tests pytest failed to collect, as best the output says.

    Zero means the suite was collected; whatever failed afterwards failed as a
    test.  Nonzero means some of the suite never ran, so a red result is not
    attributable to the change.
    """
    text = str(output or "")
    if not text:
        return 0
    for pattern in _COLLECTION_ERROR_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    # A pytest summary line reports collection failures as "N errors" alongside
    # "N failed"; "failed" is a test outcome and "errors" is not.  When pytest
    # got as far as printing a summary that line is the whole answer -- an
    # earlier "ERROR" in the log is already accounted for in it.
    summary = _last_summary_line(text)
    if summary:
        match = re.search(r"\b(\d+)\s+errors?\b", summary, re.I)
        return int(match.group(1)) if match else 0
    return len(_COLLECTING_ERROR_LINE.findall(text))


def failed_test_count(output: Any) -> int:
    """How many tests pytest reported as FAILED, or 0 when it never said."""
    summary = _last_summary_line(str(output or ""))
    match = _FAILED_COUNT.search(summary or str(output or ""))
    return int(match.group(1)) if match else 0


def _last_summary_line(text: str) -> str:
    """pytest's final ``==== 3 failed, 40 passed in 1.2s ====`` line."""
    best = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("=") and re.search(
            r"\b(passed|failed|error|no tests ran)\b", stripped, re.I
        ):
            best = stripped
    return best


def harness_failure_reason(output: Any) -> Optional[str]:
    """The signature saying this run could not measure the change, if any.

    Deliberately narrow, for the reason the hub verifier's sibling list gives:
    treating unknown failures as infrastructure would let a genuinely broken
    change read as "could not verify" and retry forever, failing open on the
    gate the whole path exists to enforce.  An unfamiliar failure is a
    reproducibility failure until someone adds its signature on purpose.
    """
    text = str(output or "").lower()
    if not text:
        return None
    if collection_error_count(text) > 0:
        return "errors during collection"
    for signature in _HARNESS_SIGNATURES:
        if signature in text:
            return signature
    return None


def classify_gate_run(
    returncode: int,
    output: Any = "",
    *,
    harness_problem: str = "",
) -> Tuple[str, str, str, str]:
    """Read one contract-gate run as harness and reproducibility outcomes.

    Returns ``(harness, harness_reason, reproducibility, reproducibility_reason)``.

    *harness_problem* is for failures the caller observed directly -- a missing
    checkout, a bootstrap that exited nonzero, a CodeGraph audit that could not
    run.  Those need no text mining: the caller watched them happen.
    """
    if harness_problem:
        return AXIS_FAIL, harness_problem, AXIS_SKIPPED, ""
    if int(returncode) == 0:
        return AXIS_PASS, "", AXIS_PASS, ""
    reason = harness_failure_reason(output)
    if reason is not None:
        return (
            AXIS_FAIL,
            "review harness did not measure the change (%s)" % reason,
            AXIS_SKIPPED,
            "",
        )
    failures = failed_test_count(output)
    detail = (
        "%d failing test(s) with no collection errors" % failures
        if failures
        else "contract gate exited %d" % int(returncode)
    )
    return AXIS_PASS, "", AXIS_FAIL, detail


def semantic_axis(
    verdict: Any,
    *,
    valid: bool = True,
    invalid_reason: str = "review agent did not produce a valid semantic verdict",
) -> Tuple[str, str]:
    """Read the review agent's own verdict as the semantics axis.

    An invalid or missing semantic verdict is a *reviewer* failure, not a
    harness one: the machinery worked and the agent did not answer.  It fails S
    so the work is looked at again, rather than silently approving.
    """
    if not valid:
        return AXIS_FAIL, invalid_reason
    value = str(verdict or "").strip().lower()
    if value == VERDICT_APPROVED:
        return AXIS_PASS, ""
    if value in {VERDICT_REJECTED, "changes_requested"}:
        return AXIS_FAIL, "semantic reviewer rejected the executor result"
    return AXIS_FAIL, invalid_reason


