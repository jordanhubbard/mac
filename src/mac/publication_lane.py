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
    package_ready: bool = False,
) -> str:
    """Return the publication lane for a single atomic repository change.

    A change enters the managed lane only when it is linked to a work package
    *and* the acting worker satisfies package readiness.  Everything else --
    an unlinked task, or a package-linked task whose worker is not yet
    package-ready -- stays on the legacy compatibility lane.  This mirrors the
    fail-closed actor policy in :func:`mac.worker_credentials.evaluate_worker_actor`
    so a mixed-version hub/worker can never be treated as managed by accident.
    """

    if package_linked and package_ready:
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
    return {
        "schema": PUBLICATION_LANE_SCHEMA,
        "lane": normalized,
        "managed": managed,
        "guarantees": list(
            MANAGED_LANE_GUARANTEES if managed else LEGACY_LANE_GUARANTEES
        ),
        "external_certifier": managed,
        "landing_receipt": managed,
        "summary": (
            "Managed single-node fast lane: exact lease-specific attempt ref, "
            "controller verification, independently pinned exact-candidate "
            "certification, compare-and-swap landing, remote read-back receipt, "
            "and finalization proof."
            if managed
            else "Legacy compatibility lane: executor pre-push tests, CodeGraph "
            "audit, review, and publication rules. Its pre-push tests are not "
            "the external work-package certifier and it does not produce a "
            "work-package landing receipt."
        ),
    }


def annotate_inventory_entry(entry: Mapping[str, Any]) -> Dict[str, Any]:
    """Return ``entry`` enriched with its publication-lane classification.

    Readiness-inventory rows already carry ``package_linked_allowed`` and
    ``legacy_fast_lane_allowed`` booleans.  A ready worker is eligible for the
    managed lane; otherwise it can only publish through the legacy lane.  The
    added ``publication_lane`` field lets every projection show the distinction
    without re-deriving it.
    """

    result = dict(entry)
    ready = bool(entry.get("ready"))
    lane = PUBLICATION_LANE_MANAGED if ready else PUBLICATION_LANE_LEGACY
    result["publication_lane"] = lane
    result["external_certifier"] = lane_provides_external_certifier(lane)
    return result


def _validate_lane(lane: Optional[str]) -> str:
    if lane not in PUBLICATION_LANES:
        raise PublicationLaneError(
            "unknown publication lane: %r (expected one of %s)"
            % (lane, ", ".join(sorted(PUBLICATION_LANES)))
        )
    return lane
