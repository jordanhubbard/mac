"""Three-axis review verdicts: harness, reproducibility, semantics.

Why this module exists
----------------------
Review outcomes used to collapse into a single boolean: anything that was not
an approval became ``rejected``.  A bootstrap failure, a sandbox transport
fault, 588 pytest *collection* errors, a red test, and a reviewer who read the
diff and disagreed with it all produced the same signed word.  Downstream then
tried to reconstruct which one had happened by pattern-matching free text
(``classify_review_failure``) and refunding attempts when the guess said
"infrastructure".

That is backwards.  The producer knows which axis failed; only the producer
knows.  So the producer records all three axes and derives the verdict from
them, and nothing downstream has to guess.

The three axes
--------------
``harness`` (H)
    Could the review even run?  Checkout present, executor commit reachable,
    bootstrap succeeded, CodeGraph tooling available, test collection intact.
    H is evaluated FIRST.  A harness failure says nothing about the work, so
    it never records a judgement about the work and never consumes an attempt.

``reproducibility`` (R)
    Given a working harness, did the repository's own contract test agree?
    A red suite with an intact collection is a fact about the change.

``semantics`` (S)
    What did the reviewing agent conclude after reading the change?  This axis
    stays binary (approved / rejected) because that is what a reviewer is asked
    for; ``invalid`` records a reviewer that produced no usable verdict, and
    ``not_evaluated`` records an axis that was never reached.

Canonical verdicts
------------------
``approved``        H pass, R pass/not-run, S approved.
``rejected``        A reviewer judged the work and said no.
``tests_failed``    The work is judged by the repository's own suite, not by a
                    reviewer's opinion.  A first-class disposition, distinct
                    from a semantic rejection.
``infrastructure``  The review harness failed.  Not a judgement.

Attempt accounting
------------------
``attempt_count`` increments at CLAIM time, so a run that dies on harness
grounds has already spent one before any judgement about the work exists.  The
old fix decremented it back out ("refund"), driven by a free-text classifier.
This module replaces that with a durable count of harness-only review outcomes
carried on task metadata: ``attempt_count`` is left exactly as the claim path
wrote it (it is the honest number of runs started), and *consumed* attempts --
the number that count against ``max_attempts`` -- subtract the runs whose
review ended in ``infrastructure``.  Consumption is therefore a work-quality
verdict, never a classifier guess after the fact.

The module is dependency-free by design (no imports from ``mac.services`` or
``mac.worker``) so the finalizer subprocess can load it without pulling in the
control-plane import tree.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Dict, Mapping, Optional, Tuple


__all__ = [
    "ATTEMPT_ACCOUNTING_KEY",
    "ATTEMPT_ACCOUNTING_SCHEMA",
    "REVIEW_AXES_SCHEMA",
    "HarnessOutcome",
    "ReproducibilityOutcome",
    "ReviewVerdict",
    "SemanticOutcome",
    "WORK_QUALITY_VERDICTS",
    "classify_contract_run",
    "consumed_attempt_count",
    "infrastructure_attempt_count",
    "normalize_verdict",
    "pytest_collection_error_count",
    "resolve_review_verdict",
    "review_axes_block",
    "verdict_consumes_attempt",
    "verdict_is_harness_failure",
    "with_infrastructure_attempt",
]


REVIEW_AXES_SCHEMA = "mac.review_verdict_axes.v1"
ATTEMPT_ACCOUNTING_SCHEMA = "mac.review_attempt_accounting.v1"
#: Task-metadata key holding the durable count of harness-only review outcomes.
ATTEMPT_ACCOUNTING_KEY = "review_attempt_accounting"


class ReviewVerdict(StrEnum):
    """The complete set of verdicts a review may produce."""

    APPROVED = "approved"
    REJECTED = "rejected"
    TESTS_FAILED = "tests_failed"
    INFRASTRUCTURE = "infrastructure"


class HarnessOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class ReproducibilityOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NOT_RUN = "not_run"


class SemanticOutcome(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    #: The reviewer ran but produced no usable verdict (missing/malformed
    #: manifest, unrecognised verdict word).  Fails closed as a rejection.
    INVALID = "invalid"
    #: The axis was never reached, because an earlier axis already decided.
    NOT_EVALUATED = "not_evaluated"


#: Verdicts that are a judgement about the WORK and therefore spend the retry
#: budget.  ``infrastructure`` is deliberately absent; ``approved`` ends the
#: task rather than retrying it.
WORK_QUALITY_VERDICTS = frozenset(
    {ReviewVerdict.REJECTED.value, ReviewVerdict.TESTS_FAILED.value}
)


def normalize_verdict(value: Any) -> str:
    """Lowercase a verdict word, returning "" when it is not canonical."""
    text = str(value or "").strip().lower()
    return text if text in {item.value for item in ReviewVerdict} else ""


def verdict_consumes_attempt(verdict: Any) -> bool:
    """True when this verdict is a judgement about the work."""
    return normalize_verdict(verdict) in WORK_QUALITY_VERDICTS


def verdict_is_harness_failure(verdict: Any) -> bool:
    return normalize_verdict(verdict) == ReviewVerdict.INFRASTRUCTURE.value


def resolve_review_verdict(
    harness: HarnessOutcome | str,
    reproducibility: ReproducibilityOutcome | str,
    semantics: SemanticOutcome | str,
) -> ReviewVerdict:
    """Derive the canonical verdict from the three axes.

    Precedence is H, then R, then S.  Harness first because a broken harness
    makes the other two axes meaningless -- reading them would be exactly the
    mistake this design removes.  Reproducibility before semantics because the
    repository's own suite is a stronger, cheaper-to-act-on signal than an
    opinion, and because the semantic axis is still recorded either way: no
    information is lost by ordering them, only the headline word changes.
    """
    if str(harness) != HarnessOutcome.PASS.value:
        return ReviewVerdict.INFRASTRUCTURE
    if str(reproducibility) == ReproducibilityOutcome.FAIL.value:
        return ReviewVerdict.TESTS_FAILED
    if str(semantics) == SemanticOutcome.APPROVED.value:
        return ReviewVerdict.APPROVED
    return ReviewVerdict.REJECTED


def review_axes_block(
    harness: HarnessOutcome | str,
    reproducibility: ReproducibilityOutcome | str,
    semantics: SemanticOutcome | str,
    *,
    harness_problem: str = "",
    reproducibility_problem: str = "",
    semantic_problem: str = "",
) -> Dict[str, Any]:
    """The structured axes block carried on a review_verdict manifest.

    Every axis is recorded on every verdict, including approvals.  An operator
    reading one manifest can tell "the harness worked, the suite was green, the
    reviewer disagreed" from "nothing ran" without re-parsing prose.
    """
    return {
        "schema": REVIEW_AXES_SCHEMA,
        "harness": {
            "status": str(harness),
            "problem": harness_problem.strip(),
        },
        "reproducibility": {
            "status": str(reproducibility),
            "problem": reproducibility_problem.strip(),
        },
        "semantics": {
            "status": str(semantics),
            "problem": semantic_problem.strip(),
        },
    }


# ---------------------------------------------------------------------------
# Contract-run classification (harness fault vs. red suite)
# ---------------------------------------------------------------------------

#: pytest's summary line counts collection failures separately from test
#: failures: "36 failed, 84 passed, 4 skipped, 588 errors in 29.56s".  An
#: "error" there means the test never ran, which is a fact about the harness.
_PYTEST_ERROR_SUMMARY_RE = re.compile(r"\b(\d+)\s+errors?\b")
_PYTEST_INTERRUPTED_RE = re.compile(r"!+\s*Interrupted:\s*(\d+)\s+errors?", re.IGNORECASE)

#: Text that only appears when the machinery around the tests broke.  Kept
#: narrow on purpose -- see ``classify_contract_run`` for why the unknown case
#: deliberately does NOT land here.
_HARNESS_SIGNATURES: Tuple[str, ...] = (
    "errors during collection",
    "error collecting ",
    "importerror while loading conftest",
    "internalerror",
    "unicodeencodeerror",
    "modulenotfounderror",
    "mac_test_pg_url",
    "out of shared memory",
    "max_locks_per_transaction",
    "ssh exited with status",
    "connection reset by peer",
    "connection refused",
    "no space left on device",
    "failed to create sandbox",
    "could not create sandbox",
    "bootstrap failed",
)


def pytest_collection_error_count(output: str) -> int:
    """How many collection errors the run reported, 0 when none/unknown."""
    text = output or ""
    total = 0
    for match in _PYTEST_INTERRUPTED_RE.finditer(text):
        total = max(total, int(match.group(1)))
    for line in text.splitlines():
        stripped = line.strip()
        # Only the pytest summary rule counts.  "ERROR tests/foo.py::bar" lines
        # are per-item echoes of the same errors and would double-count.
        if not (stripped.startswith("=") and stripped.endswith("=")):
            continue
        match = _PYTEST_ERROR_SUMMARY_RE.search(stripped)
        if match:
            total = max(total, int(match.group(1)))
    return total


def classify_contract_run(
    returncode: int, output: str
) -> Tuple[HarnessOutcome, ReproducibilityOutcome, str]:
    """Split one contract-test run into a harness axis and a tests axis.

    Returns ``(harness, reproducibility, problem)``.

    The unknown case fails CLOSED, as a red suite rather than a harness fault.
    Treating unrecognised failures as infrastructure would let a genuinely
    broken change retry forever behind "we could not verify it" -- failing open
    on the one gate this exists to enforce.  A wrongly-classified red suite
    costs one attempt and produces reviewer feedback; a wrongly-classified
    harness fault costs the gate.
    """
    if int(returncode) == 0:
        return HarnessOutcome.PASS, ReproducibilityOutcome.PASS, ""
    text = (output or "").lower()
    errors = pytest_collection_error_count(output or "")
    if errors > 0:
        return (
            HarnessOutcome.FAIL,
            ReproducibilityOutcome.NOT_RUN,
            "contract run reported %d collection error%s; the suite never ran"
            % (errors, "" if errors == 1 else "s"),
        )
    for signature in _HARNESS_SIGNATURES:
        if signature in text:
            return (
                HarnessOutcome.FAIL,
                ReproducibilityOutcome.NOT_RUN,
                "review harness failed: %s" % signature,
            )
    return (
        HarnessOutcome.PASS,
        ReproducibilityOutcome.FAIL,
        "contract tests failed (rc=%d)" % int(returncode),
    )


# ---------------------------------------------------------------------------
# Attempt accounting
# ---------------------------------------------------------------------------


def infrastructure_attempt_count(metadata: Optional[Mapping[str, Any]]) -> int:
    """Runs whose review ended in ``infrastructure``, from task metadata."""
    if not isinstance(metadata, Mapping):
        return 0
    block = metadata.get(ATTEMPT_ACCOUNTING_KEY)
    if not isinstance(block, Mapping):
        return 0
    try:
        value = int(block.get("infrastructure_attempts") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def with_infrastructure_attempt(
    metadata: Optional[Mapping[str, Any]]
) -> Dict[str, Any]:
    """Return a copy of ``metadata`` with one more harness-only outcome."""
    updated: Dict[str, Any] = dict(metadata or {})
    updated[ATTEMPT_ACCOUNTING_KEY] = {
        "schema": ATTEMPT_ACCOUNTING_SCHEMA,
        "infrastructure_attempts": infrastructure_attempt_count(metadata) + 1,
    }
    return updated


def consumed_attempt_count(
    attempt_count: Any, metadata: Optional[Mapping[str, Any]] = None
) -> int:
    """Attempts that count against ``max_attempts``.

    ``attempt_count`` stays the honest number of runs STARTED.  This is the
    number of them that produced a judgement about the work.
    """
    try:
        started = int(attempt_count or 0)
    except (TypeError, ValueError):
        started = 0
    return max(0, started - infrastructure_attempt_count(metadata))


def task_consumed_attempts(task: Any) -> int:
    """``consumed_attempt_count`` for a Task-like object."""
    return consumed_attempt_count(
        getattr(task, "attempt_count", 0), getattr(task, "metadata", None)
    )


def task_attempts_exhausted(task: Any) -> bool:
    """True when the work has spent its whole retry budget on real verdicts."""
    try:
        max_attempts = int(getattr(task, "max_attempts", 0) or 0)
    except (TypeError, ValueError):
        return False
    return task_consumed_attempts(task) >= max_attempts
