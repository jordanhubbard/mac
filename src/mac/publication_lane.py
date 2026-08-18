"""Canonical description of a repository change's publication lane.

There is exactly one lane.  The control plane used to document two: a
``managed`` work-package fast lane requiring six exact-candidate guarantees,
and a ``legacy`` compatibility path "for existing tasks that are not linked to
a work package".  The managed lane never ran -- every work-package table on the
live hub was empty, and the single-task call site passed
``package_linked=False`` unconditionally -- so ``legacy`` was the only
reachable value of a two-valued field printed on every task.  The work-package
pipeline has been removed, and with it the managed lane.

The field itself is kept.  ``publication_lane`` and ``publication_route`` are
stamped into task metadata and read by the CLI, the API, and the Fleet IDE, so
the ``mac.publication_lane.v1`` / ``mac.task_publication_route.v1`` wire shape
is unchanged; it just no longer has a second value that cannot occur.

This module is deliberately tiny and dependency-free so that every surface
classifies a change the same way and describes the same guarantee set.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional


PUBLICATION_LANE_SCHEMA = "mac.publication_lane.v1"

PUBLICATION_LANE_LEGACY = "legacy"

PUBLICATION_LANES = frozenset({PUBLICATION_LANE_LEGACY})

# What the publication path actually provides.
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
    package_linked: bool = False,
    package_ready: Optional[bool] = None,
) -> str:
    """Return the publication lane for a single atomic repository change.

    Both keyword arguments are retained for v1 callers and neither changes the
    result: the work-package route they selected between no longer exists.
    """

    del package_linked, package_ready
    return PUBLICATION_LANE_LEGACY


def is_legacy_lane(lane: str) -> bool:
    """Return ``True`` iff ``lane`` is the publication lane."""

    return _validate_lane(lane) == PUBLICATION_LANE_LEGACY


def describe_lane(lane: str) -> Dict[str, Any]:
    """Return a stable, secret-free description of a publication lane.

    The description is the single source of truth that API, CLI, and Fleet IDE
    projections render, so every surface states the same guarantee set.
    """

    normalized = _validate_lane(lane)
    guarantees = list(LEGACY_LANE_GUARANTEES)
    return {
        "schema": PUBLICATION_LANE_SCHEMA,
        "lane": normalized,
        "managed": False,
        # Kept for v1 clients; these are required route guarantees, not a claim
        # that a held or active task has already satisfied them.
        "guarantees": guarantees,
        "required_guarantees": guarantees,
        "external_certifier": False,
        "landing_receipt": False,
        "summary": (
            "Publication requires executor pre-push tests, CodeGraph audit, "
            "review, and publication rules."
        ),
    }


def annotate_inventory_entry(entry: Mapping[str, Any]) -> Dict[str, Any]:
    """Return ``entry`` without any per-worker publication-lane claim.

    A worker does not have a publication lane: readiness says whether it may
    execute work, not which publication mechanism the work uses.
    """

    result = dict(entry)
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
