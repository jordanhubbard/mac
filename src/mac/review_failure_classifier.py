"""Review failure taxonomy and classifier.

Distinguishes *semantic/code review* rejections from *infrastructure-only*
failures so that callers (review service, finalizer, workflow advance) can
make correct retry decisions:

* Infrastructure failures are retryable — they say nothing about the quality
  of the executor's work and should not block the task permanently.
* Semantic failures represent a genuine reviewer judgment (or missing
  judgment) and must be preserved as-is.

Taxonomy
--------

INFRASTRUCTURE classes (retryable):
  transport_error         – network / HTTP / clone / push transport error
  reviewer_unavailable    – reviewer agent offline, lease expired, or not seen
  reviewer_timeout        – reviewer failed to deliver a verdict within the cap
  hub_verification_error  – hub-side contract/verification infrastructure error
  credential_access       – authentication or authorization failure on clone/push

SEMANTIC classes (not retryable):
  semantic_rejection      – reviewer issued a REJECTED or CHANGES_REQUESTED verdict
  reviewer_findings       – approved with findings / code-quality notes
  protocol_noncompliant   – reviewer violated the declared review protocol
                            (e.g. blind-review noncompliance); distinct from a
                            transport failure and must not be silently retried

UNKNOWN class:
  unknown                 – cannot determine from available signals

API
---
The primary entry-point is :func:`classify_review_failure`.  It accepts the
string-valued *reason* that the workflow engine or worker attaches to review
retraction events, together with optional free-text *error* and *evidence_type*
signals, and returns a :class:`ReviewFailureCategory` tuple:

    category, is_infrastructure = classify_review_failure(reason, error=...)
    if is_infrastructure:
        # safe to retry / re-assign a different reviewer
    else:
        # semantic outcome — preserve and surface to operator

The module is intentionally dependency-free (no imports from mac.services or
mac.worker) so it can be loaded from the finalizer subprocess without
triggering the full control-plane import tree.
"""

from __future__ import annotations

import re
from typing import NamedTuple, Optional


# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------

#: Infrastructure failure reasons — retryable; do not reflect code quality.
INFRASTRUCTURE_REASONS = frozenset(
    {
        # reviewer agent disappeared before submitting a verdict
        "reviewer_not_available",
        "reviewer_unavailable",
        "reviewer_stale",
        # verdict-wait cap exceeded (reviewer produced nothing)
        "review_verdict_wait_cap_hit",
        # retraction cap reached after repeated infrastructure failures
        "review_retraction_cap_hit",
        # review clone or push transport failure
        "review_clone_failed",
        "clone_failed",
        "transport_error",
        # hub-side verification infrastructure errors
        "hub_verification_error",
        "hub_verification_failed",
        # repository credential / access failures on the reviewer's side
        "credential_error",
        "authentication",
        "authorization",
        "repository_missing",
        "network",
    }
)

#: Semantic failure reasons — not retryable; reflect reviewer judgment.
SEMANTIC_REASONS = frozenset(
    {
        "rejected",
        "changes_requested",
        "semantic_rejection",
        "reviewer_findings",
        # reviewer submitted a semantically invalid verdict (missing fields,
        # wrong schema) — counts as a protocol violation, not infrastructure
        "semantic_verdict_invalid",
        # blind-review protocol noncompliance is a semantic/protocol failure
        "blind_protocol_noncompliant",
    }
)

#: Protocol-failure reasons produced by _review_attempt_protocol_failure().
#: These represent *reviewer execution* failures (wrong exit code, bad
#: manifest) and are classified as infrastructure so the workflow retries
#: with a different reviewer rather than blocking the task.
PROTOCOL_FAILURE_PREFIX = "reviewer_protocol_failure:"

#: Reason string produced when the review executor exits nonzero.
REVIEW_EXECUTOR_NONZERO = "review_executor_nonzero"


class ReviewFailureClassification(NamedTuple):
    """Result of :func:`classify_review_failure`."""

    #: One of the taxonomy strings above, e.g. ``"reviewer_unavailable"``.
    failure_class: str
    #: ``True`` when the failure is infrastructure-only and retryable.
    is_infrastructure: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: Ordered list of ``(pattern, failure_class)`` pairs applied to the combined
#: reason + error text when no exact reason match is found.
_ERROR_TEXT_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"could not clone|clone.*fail|refusing review clone|git clone",
            re.I,
        ),
        "transport_error",
    ),
    (
        re.compile(
            r"could not read (username|password)"
            r"|authentication failed"
            r"|bad credentials"
            r"|invalid username"
            r"|terminal prompts disabled",
            re.I,
        ),
        "credential_access",
    ),
    (
        re.compile(
            r"permission denied"
            r"|not authorized"
            r"|authorization failed"
            r"|write access.*not granted"
            r"|returned error: 403"
            r"|saml sso",
            re.I,
        ),
        "credential_access",
    ),
    (
        re.compile(
            r"repository not found|does not appear to be a git repository",
            re.I,
        ),
        "credential_access",
    ),
    (
        re.compile(
            r"could not resolve host"
            r"|connection refused"
            r"|connection timed out"
            r"|network is unreachable"
            r"|temporary failure in name resolution",
            re.I,
        ),
        "transport_error",
    ),
    (
        re.compile(
            r"reviewer.*not available"
            r"|reviewer.*offline"
            r"|reviewer.*timed? ?out"
            r"|lease expired"
            r"|heartbeat.*offline"
            r"|agent.*unavailable",
            re.I,
        ),
        "reviewer_unavailable",
    ),
    (
        re.compile(
            r"verdict.*wait.*cap"
            r"|review.*verdict.*cap"
            r"|reviewer.*stale",
            re.I,
        ),
        "reviewer_timeout",
    ),
    (
        re.compile(
            r"hub.*verif"
            r"|verif.*infra"
            r"|hub.*sandbox"
            r"|hub.*contract",
            re.I,
        ),
        "hub_verification_error",
    ),
    (
        re.compile(
            r"rejected|changes.requested",
            re.I,
        ),
        "semantic_rejection",
    ),
]

#: Map each infrastructure ``failure_class`` to a canonical label.
_INFRASTRUCTURE_CLASSES: frozenset[str] = frozenset(
    {
        "transport_error",
        "reviewer_unavailable",
        "reviewer_timeout",
        "hub_verification_error",
        "credential_access",
        "review_executor_nonzero",  # harness crashed / bad exit code → retry
    }
)

#: Map each semantic ``failure_class`` to a canonical label.
_SEMANTIC_CLASSES: frozenset[str] = frozenset(
    {
        "semantic_rejection",
        "reviewer_findings",
        "semantic_verdict_invalid",
        "blind_protocol_noncompliant",
    }
)


def _normalize_reason(reason: str) -> str:
    """Return a compact, case-folded reason token for set lookups."""
    return re.sub(r"[\s_-]+", "_", str(reason or "").strip().lower())


def _is_infrastructure_class(failure_class: str) -> bool:
    return failure_class in _INFRASTRUCTURE_CLASSES


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_review_failure(
    reason: str,
    *,
    error: Optional[str] = None,
    evidence_type: Optional[str] = None,
) -> ReviewFailureClassification:
    """Classify a review failure as infrastructure or semantic.

    Parameters
    ----------
    reason:
        The ``reason`` string attached to the review retraction or failure
        event.  May be a bare token such as ``"reviewer_not_available"`` or a
        prefixed compound string such as
        ``"reviewer_protocol_failure:review_executor_nonzero"``.
    error:
        Optional free-text error message that accompanies the reason (e.g.
        from ``mac-evidence.json`` or the worker log).  Used as a secondary
        signal when the reason alone is insufficient.
    evidence_type:
        Optional ``evidence_type`` field from the manifest (e.g.
        ``"review_verdict"``).  When the evidence type is ``"review_verdict"``
        and the manifest carries a clear semantic verdict the result is always
        semantic.

    Returns
    -------
    ReviewFailureClassification
        A named-tuple ``(failure_class, is_infrastructure)`` where
        ``is_infrastructure`` is ``True`` for retryable infrastructure
        failures and ``False`` for semantic outcomes.
    """
    reason_str = str(reason or "").strip()

    # ------------------------------------------------------------------
    # 1. Compound "reviewer_protocol_failure:<sub_reason>" strings
    # ------------------------------------------------------------------
    if reason_str.lower().startswith(PROTOCOL_FAILURE_PREFIX):
        sub = reason_str[len(PROTOCOL_FAILURE_PREFIX):].strip().lower()
        sub_norm = _normalize_reason(sub)
        # semantic_verdict_invalid and blind_protocol_noncompliant are
        # semantic (the reviewer understood the task but delivered a
        # malformed or noncompliant response intentionally).
        if sub_norm in {"semantic_verdict_invalid", "blind_protocol_noncompliant"}:
            fc = sub_norm
            return ReviewFailureClassification(fc, False)
        # review_executor_nonzero and similar harness failures are infra.
        return ReviewFailureClassification(
            sub_norm or "review_executor_nonzero", True
        )

    # ------------------------------------------------------------------
    # 2. Exact reason-set lookup
    # ------------------------------------------------------------------
    norm = _normalize_reason(reason_str)

    if norm in {_normalize_reason(r) for r in INFRASTRUCTURE_REASONS}:
        # Map to canonical class names
        canonical = _reason_to_canonical_class(norm)
        return ReviewFailureClassification(canonical, True)

    if norm in {_normalize_reason(r) for r in SEMANTIC_REASONS}:
        canonical = _reason_to_canonical_class(norm)
        return ReviewFailureClassification(canonical, False)

    # ------------------------------------------------------------------
    # 3. Evidence-type hint: explicit review_verdict with no semantic
    #    markers → treat as semantic (reviewer finished, outcome unclear)
    # ------------------------------------------------------------------
    etype = str(evidence_type or "").strip().lower()
    if etype == "review_verdict":
        return ReviewFailureClassification("semantic_rejection", False)

    # ------------------------------------------------------------------
    # 4. Free-text pattern matching on combined reason + error blob
    # ------------------------------------------------------------------
    blob = " ".join(
        filter(None, [reason_str, str(error or "").strip()])
    ).lower()

    for pattern, failure_class in _ERROR_TEXT_RULES:
        if pattern.search(blob):
            return ReviewFailureClassification(
                failure_class, _is_infrastructure_class(failure_class)
            )

    # ------------------------------------------------------------------
    # 5. Final fallback
    # ------------------------------------------------------------------
    return ReviewFailureClassification("unknown", False)


def _reason_to_canonical_class(norm: str) -> str:
    """Map a normalised reason token to its canonical failure-class string."""
    _INFRA_NORM_MAP = {
        _normalize_reason(r): _canonical_infra(r)
        for r in INFRASTRUCTURE_REASONS
    }
    _SEMANTIC_NORM_MAP = {
        _normalize_reason(r): _canonical_semantic(r)
        for r in SEMANTIC_REASONS
    }
    return (
        _INFRA_NORM_MAP.get(norm)
        or _SEMANTIC_NORM_MAP.get(norm)
        or norm
    )


def _canonical_infra(reason: str) -> str:
    """Map an infrastructure reason to its canonical class name."""
    r = _normalize_reason(reason)
    if r in {"reviewer_not_available", "reviewer_unavailable", "reviewer_stale"}:
        return "reviewer_unavailable"
    if r in {
        "review_verdict_wait_cap_hit",
        "review_retraction_cap_hit",
    }:
        return "reviewer_timeout"
    if r in {
        "review_clone_failed",
        "clone_failed",
        "transport_error",
    }:
        return "transport_error"
    if r in {
        "hub_verification_error",
        "hub_verification_failed",
    }:
        return "hub_verification_error"
    if r in {
        "credential_error",
        "authentication",
        "authorization",
        "repository_missing",
        "network",
    }:
        return "credential_access"
    return r


def _canonical_semantic(reason: str) -> str:
    """Map a semantic reason to its canonical class name."""
    r = _normalize_reason(reason)
    if r in {"rejected", "changes_requested", "semantic_rejection"}:
        return "semantic_rejection"
    if r == "reviewer_findings":
        return "reviewer_findings"
    if r == "semantic_verdict_invalid":
        return "semantic_verdict_invalid"
    if r == "blind_protocol_noncompliant":
        return "blind_protocol_noncompliant"
    return r


# ---------------------------------------------------------------------------
# Convenience helpers for review service / finalizer paths
# ---------------------------------------------------------------------------


def is_infrastructure_failure(reason: str, *, error: Optional[str] = None) -> bool:
    """Return ``True`` when the failure is retryable infrastructure-only.

    Convenience wrapper around :func:`classify_review_failure` for callers
    that only need the boolean predicate.
    """
    result = classify_review_failure(reason, error=error)
    return result.is_infrastructure


def is_semantic_failure(reason: str, *, error: Optional[str] = None) -> bool:
    """Return ``True`` when the failure reflects a reviewer semantic judgment.

    Semantic failures must not be silently retried; they should be surfaced
    to the task owner or operator.
    """
    result = classify_review_failure(reason, error=error)
    return not result.is_infrastructure and result.failure_class != "unknown"
