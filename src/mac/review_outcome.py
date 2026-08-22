"""Canonical review outcomes: harness, reproducibility, and semantics.

A review answers three orthogonal questions. Collapsing them into
``approved | rejected`` is how harness faults get recorded as judgements
about the work. This module is the shared vocabulary for the finalizer,
the hub verifier, and submit_review.

Axes
----
H  harness health         clone / checkout / bootstrap / CodeGraph / sandbox
R  reproducibility        the exact reviewed commit vs the deterministic suite
S  semantic               independent reviewer judgement of the change
"""

from __future__ import annotations

import re
from typing import Optional

VERDICT_APPROVED = "approved"
VERDICT_REJECTED = "rejected"
VERDICT_TESTS_FAILED = "tests_failed"
VERDICT_INFRASTRUCTURE = "infrastructure"

CANONICAL_VERDICTS = frozenset(
    {
        VERDICT_APPROVED,
        VERDICT_REJECTED,
        VERDICT_TESTS_FAILED,
        VERDICT_INFRASTRUCTURE,
    }
)
WORK_QUALITY_VERDICTS = frozenset({VERDICT_REJECTED, VERDICT_TESTS_FAILED})

REVIEW_STATUS_FOR_VERDICT = {
    VERDICT_APPROVED: "approved",
    VERDICT_REJECTED: "rejected",
    VERDICT_TESTS_FAILED: "tests_failed",
    VERDICT_INFRASTRUCTURE: "infrastructure",
}

# pytest's session summary, e.g.
# ``36 failed, 84 passed, 4 skipped, 588 errors in 29.56s``
_PYTEST_FAILED = re.compile(r"(\d+)\s+failed\b", re.I)
_PYTEST_ERRORS = re.compile(r"(\d+)\s+errors?\b", re.I)

_HARNESS_OUTPUT = (
    re.compile(r"unicodeencodeerror", re.I),
    re.compile(r"failed to create sandbox", re.I),
    re.compile(r"error: could not create sandbox", re.I),
    re.compile(r"INTERNALERROR", re.I),
    re.compile(r"ERROR collecting", re.I),
)


def last_pytest_count(pattern: re.Pattern[str], text: str) -> int:
    """Return the last integer a pytest summary pattern matched, else 0."""
    matches = pattern.findall(text or "")
    if not matches:
        return 0
    try:
        return int(matches[-1])
    except (TypeError, ValueError):
        return 0


def classify_independent_test_outcome(
    output: str,
    returncode: int,
) -> str:
    """Classify a deterministic test run as pass, tests_failed, or infrastructure.

    Collection / sandbox / encoding collapse is harness health (H). A pytest
    session that finished with failed tests and zero errors is reproducibility
    (R). Unknown non-zero without a failing-test summary is treated as
    infrastructure so a broken runner cannot spend the work's retry budget.
    """
    if int(returncode) == 0:
        return "pass"
    text = output or ""
    for pattern in _HARNESS_OUTPUT:
        if pattern.search(text):
            return VERDICT_INFRASTRUCTURE
    errors = last_pytest_count(_PYTEST_ERRORS, text)
    failed = last_pytest_count(_PYTEST_FAILED, text)
    if errors > 0:
        return VERDICT_INFRASTRUCTURE
    if failed > 0:
        return VERDICT_TESTS_FAILED
    return VERDICT_INFRASTRUCTURE


def canonical_verdict(value: str) -> Optional[str]:
    """Return a canonical verdict token, or None if the value is unknown."""
    token = str(value or "").strip().lower()
    if token in CANONICAL_VERDICTS:
        return token
    return None


def compose_canonical_review_verdict(
    *,
    harness_ok: bool,
    semantic_verdict: str,
    semantic_valid: bool,
    reproducibility: str,
    independent_pass: bool,
) -> str:
    """Compose H, then S, then R into one canonical verdict token."""
    if not harness_ok:
        return VERDICT_INFRASTRUCTURE
    if not semantic_valid or semantic_verdict == VERDICT_REJECTED:
        return VERDICT_REJECTED
    if reproducibility == "fail":
        return VERDICT_TESTS_FAILED
    if semantic_verdict == VERDICT_APPROVED and independent_pass:
        return VERDICT_APPROVED
    return VERDICT_REJECTED
