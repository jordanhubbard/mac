"""Deterministic conflict analysis for two declared effect sets.

This is the one piece of the retired work-package plan compiler that has a live
consumer: :mod:`mac.directive_service` uses it to decide whether two directive
macros may run together (``macro_effect_conflict`` /
``macro_effect_overlap_unproven`` blockers).  It was extracted from
``mac.work_package_models`` when the work-package pipeline was removed, so the
directive feature no longer drags a 2,150-line plan compiler behind it.

Resource tokens are intentionally opaque: callers can use paths, subsystems,
database tables, or service names without teaching this module domain-specific
alias rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Tuple

from mac.models import JsonDict


@dataclass(frozen=True)
class DeclaredEffects:
    """Resources a unit of work observes or changes."""

    reads: Tuple[str, ...] = ()
    writes: Tuple[str, ...] = ()
    exclusive: Tuple[str, ...] = ()
    external: Tuple[str, ...] = ()
    external_contract: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return {
            "reads": list(self.reads),
            "writes": list(self.writes),
            "exclusive": list(self.exclusive),
            "external": list(self.external),
            "external_contract": _copy_json_object(self.external_contract or {}),
        }


def effect_conflicts(left: DeclaredEffects, right: DeclaredEffects) -> List[str]:
    """Return deterministic conflict reasons for two declared effect sets.

    Read/read overlap is safe.  Writes conflict with reads or writes, exclusive
    resources conflict with any local access, and external effects serialize
    against the same external resource.
    """

    reasons = set()
    for resource in _overlapping_resources(
        left.writes, right.reads + right.writes + right.exclusive
    ):
        reasons.add("write:%s" % resource)
    for resource in _overlapping_resources(right.writes, left.reads + left.writes + left.exclusive):
        reasons.add("write:%s" % resource)
    for resource in _overlapping_resources(
        left.exclusive, right.reads + right.writes + right.exclusive
    ):
        reasons.add("exclusive:%s" % resource)
    for resource in _overlapping_resources(
        right.exclusive, left.reads + left.writes + left.exclusive
    ):
        reasons.add("exclusive:%s" % resource)
    for resource in _overlapping_resources(left.external, right.external):
        reasons.add("external:%s" % resource)
    for reason in tuple(reasons):
        if reason.startswith("exclusive:"):
            reasons.discard("write:%s" % reason.split(":", 1)[1])
    return sorted(reasons)


def _resources_overlap(left: str, right: str) -> bool:
    if left == right or left == "*" or right == "*":
        return True
    if left.startswith("repo:") or right.startswith("repo:"):
        return True
    return left.startswith(right.rstrip("/") + "/") or right.startswith(left.rstrip("/") + "/")


def _overlapping_resources(left: Tuple[str, ...], right: Tuple[str, ...]) -> Tuple[str, ...]:
    overlaps = set()
    for left_resource in left:
        for right_resource in right:
            if _resources_overlap(left_resource, right_resource):
                if left_resource == right_resource:
                    overlaps.add(left_resource)
                else:
                    overlaps.add("%s~%s" % tuple(sorted((left_resource, right_resource))))
    return tuple(sorted(overlaps))


def _copy_json_object(value: Mapping[str, Any]) -> Dict[str, Any]:
    return {str(key): value[key] for key in value}
