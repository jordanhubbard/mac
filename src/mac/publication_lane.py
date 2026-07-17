"""Canonical classification of a repository change's publication lane.

The control plane runs two publication lanes for atomic, single-node
repository changes:

* the **managed** lane (``PUBLICATION_LANE_MANAGED``) is the migrated
  single-node fast lane.  A change reaches canonical ``main`` only after an
  exact lease-specific attempt ref, controller verification, an independently
  pinned exact-candidate certification produced without landing credentials, a
  compare-and-swap landing, a remote read-back receipt, and a finalization
  proof.  See :mod:`mac.landing_service`,
  :mod:`mac.work_package_certification_service`, and
  :mod:`mac.work_package_publication_finalizer`.

* the **legacy** lane (``PUBLICATION_LANE_LEGACY``) is the compatibility path
  for existing tasks that are not linked to a work package.  It retains its
  executor pre-push tests, CodeGraph audit, review, and publication rules, but
  it does **not** acquire the managed lane's external exact-candidate certifier
  or work-package landing receipt.

Lane identity belongs to the task route, not to a worker.  A package-linked
task remains managed when no compatible worker is currently ready; it is
blocked rather than silently downgraded to legacy publication.  Worker
readiness is reported separately as managed-lane eligibility.

This module is deliberately tiny and dependency-free so that every surface
(API, CLI, Fleet IDE projections, dispatcher, and documentation) classifies a
change the same way and describes the same guarantee set.  Centralising the
guarantee vocabulary keeps any caller from describing the legacy pre-push tests
as if they were the external work-package certifier -- they are not, and
:func:`describe_lane` records the difference explicitly.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional


PUBLICATION_LANE_SCHEMA = "mac.publication_lane.v1"

PUBLICATION_LANE_MANAGED = "managed"
PUBLICATION_LANE_LEGACY = "legacy"

PUBLICATION_LANES = frozenset({PUBLICATION_LANE_MANAGED, PUBLICATION_LANE_LEGACY})

# The exact-candidate guarantees required before a managed change lands on the
# canonical ref.  These names mirror the completion criteria in
# ``docs/work-graph-control-plane.md`` so documentation and code cannot drift.
MANAGED_LANE_GUARANTEES = (
    "exact_lease_attempt_ref",
    "controller_verification",
    "independent_pinned_certification",
    "compare_and_swap_landing",
    "remote_read_back_receipt",
    "finalization_proof",
)

# What the legacy compatibility path actually provides.  It is intentionally a
# strict subset that does NOT include the managed external certifier or the
# work-package landing receipt.
LEGACY_LANE_GUARANTEES = (
    "executor_pre_push_tests",
    "codegraph_audit",
    "review",
    "publication_rules",
)


class PublicationLaneError(ValueError):
    """Raised when a lane value is not one of the supported lanes."""


def classify_publication_lane(
    *,
    package_linked: bool,
    package_ready: Optional[bool] = None,
) -> str:
    """Return the publication lane for a single atomic repository change.

    A package link is durable route authority.  Readiness controls whether an
    actor may execute that route, not which publication mechanism the task is
    allowed to use.  ``package_ready`` is retained as a compatibility keyword
    for callers rolling forward from v1; it intentionally does not change the
    lane.  Mixed-version or unready workers are blocked by the package claim
    gate and can never make linked work fall back to legacy publication.
    """

    del package_ready
    if package_linked:
        return PUBLICATION_LANE_MANAGED
    return PUBLICATION_LANE_LEGACY


def is_managed_lane(lane: str) -> bool:
    """Return ``True`` iff ``lane`` is the managed exact-candidate lane."""

    return _validate_lane(lane) == PUBLICATION_LANE_MANAGED


def is_legacy_lane(lane: str) -> bool:
    """Return ``True`` iff ``lane`` is the legacy compatibility lane."""

    return _validate_lane(lane) == PUBLICATION_LANE_LEGACY


def lane_provides_external_certifier(lane: str) -> bool:
    """Whether ``lane`` is certified by the external work-package certifier.

    Only the managed lane runs the independently pinned, credential-free
    exact-candidate certifier.  The legacy lane's executor pre-push tests are
    NOT that certifier; callers must not conflate the two.
    """

    return is_managed_lane(lane)


def lane_provides_landing_receipt(lane: str) -> bool:
    """Whether ``lane`` produces a compare-and-swap remote read-back receipt."""

    return is_managed_lane(lane)


def describe_lane(lane: str) -> Dict[str, Any]:
    """Return a stable, secret-free description of a publication lane.

    The description is the single source of truth that API, CLI, and Fleet IDE
    projections render, so every surface states the same guarantee set and the
    same explicit note that the legacy pre-push tests are not the external
    work-package certifier.
    """

    normalized = _validate_lane(lane)
    managed = normalized == PUBLICATION_LANE_MANAGED
    guarantees = list(
        MANAGED_LANE_GUARANTEES if managed else LEGACY_LANE_GUARANTEES
    )
    return {
        "schema": PUBLICATION_LANE_SCHEMA,
        "lane": normalized,
        "managed": managed,
        # Kept for v1 clients; these are required route guarantees, not a claim
        # that a held or active task has already produced the corresponding
        # candidate, certificate, receipt, or finalization.
        "guarantees": guarantees,
        "required_guarantees": guarantees,
        "external_certifier": managed,
        "landing_receipt": managed,
        "summary": (
            "Managed single-node route requires an exact lease-specific attempt ref, "
            "controller verification, independently pinned exact-candidate "
            "certification, compare-and-swap landing, remote read-back receipt, "
            "and finalization proof."
            if managed
            else "Legacy compatibility route requires executor pre-push tests, CodeGraph "
            "audit, review, and publication rules. Its pre-push tests are not "
            "the external work-package certifier and it does not produce a "
            "work-package landing receipt."
        ),
    }


def annotate_inventory_entry(entry: Mapping[str, Any]) -> Dict[str, Any]:
    """Return ``entry`` enriched with managed-lane eligibility.

    Readiness-inventory rows already carry ``package_linked_allowed`` and
    ``legacy_fast_lane_allowed`` booleans.  A ready worker may execute a task
    already assigned to the managed route.  It does not itself have a
    publication lane: the same worker may execute an unlinked legacy task.
    """

    result = dict(entry)
    ready = bool(entry.get("ready"))
    result["managed_lane_eligible"] = ready
    result["external_certifier_capable"] = ready
    result.pop("publication_lane", None)
    result.pop("external_certifier", None)
    return result


def _validate_lane(lane: Optional[str]) -> str:
    if lane not in PUBLICATION_LANES:
        raise PublicationLaneError(
            "unknown publication lane: %r (expected one of %s)"
            % (lane, ", ".join(sorted(PUBLICATION_LANES)))
        )
    return lane
