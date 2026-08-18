"""Unit tests for the canonical publication-lane classifier.

The control plane once documented two lanes: a ``managed`` work-package fast
lane and a ``legacy`` compatibility lane.  The work-package pipeline has been
removed, so there is exactly one lane.  These tests lock in that collapse: the
``mac.publication_lane.v1`` wire shape survives, the field has a single
reachable value, and a worker readiness entry still carries no lane claim.
"""

from __future__ import annotations

import pytest

from mac.publication_lane import (
    LEGACY_LANE_GUARANTEES,
    PUBLICATION_LANE_LEGACY,
    PUBLICATION_LANE_SCHEMA,
    PUBLICATION_LANES,
    PublicationLaneError,
    annotate_inventory_entry,
    classify_publication_lane,
    describe_lane,
    is_legacy_lane,
)


def test_there_is_exactly_one_publication_lane() -> None:
    assert PUBLICATION_LANES == frozenset({PUBLICATION_LANE_LEGACY})


def test_every_classification_resolves_to_the_single_lane() -> None:
    # The retained v1 keyword arguments no longer select between routes.
    for package_linked in (True, False):
        for package_ready in (True, False, None):
            assert (
                classify_publication_lane(
                    package_linked=package_linked, package_ready=package_ready
                )
                == PUBLICATION_LANE_LEGACY
            )
    assert classify_publication_lane() == PUBLICATION_LANE_LEGACY


def test_legacy_lane_predicate() -> None:
    assert is_legacy_lane(PUBLICATION_LANE_LEGACY) is True


def test_describe_lane_states_the_publication_guarantees() -> None:
    described = describe_lane(PUBLICATION_LANE_LEGACY)
    assert described["schema"] == PUBLICATION_LANE_SCHEMA
    assert described["lane"] == PUBLICATION_LANE_LEGACY
    assert described["managed"] is False
    assert described["external_certifier"] is False
    assert described["landing_receipt"] is False
    assert tuple(described["guarantees"]) == LEGACY_LANE_GUARANTEES
    assert described["required_guarantees"] == described["guarantees"]
    for required in (
        "executor_pre_push_tests",
        "codegraph_audit",
        "review",
        "publication_rules",
    ):
        assert required in described["guarantees"]
    assert described["summary"]


def test_unknown_lane_fails_closed() -> None:
    with pytest.raises(PublicationLaneError):
        describe_lane("turbo")
    with pytest.raises(PublicationLaneError):
        describe_lane("managed")
    with pytest.raises(PublicationLaneError):
        is_legacy_lane("")
    with pytest.raises(PublicationLaneError):
        is_legacy_lane(None)  # type: ignore[arg-type]


def test_annotate_inventory_entry_makes_no_lane_claim_for_a_worker() -> None:
    original = {
        "agent_id": "a",
        "ready": True,
        "publication_lane": "legacy",
        "external_certifier": True,
    }
    annotated = annotate_inventory_entry(original)

    assert annotated["agent_id"] == "a"
    assert annotated["ready"] is True
    assert "publication_lane" not in annotated
    assert "external_certifier" not in annotated
    # A worker entry never gains a lane-eligibility claim.
    assert "managed_lane_eligible" not in annotated
    assert "external_certifier_capable" not in annotated
    # The input is not mutated in place.
    assert original["publication_lane"] == "legacy"
