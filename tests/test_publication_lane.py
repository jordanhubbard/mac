"""Unit tests for the canonical publication-lane classifier.

These lock in the single-source-of-truth distinction between the migrated
managed exact-candidate fast lane and the legacy compatibility lane, and the
explicit rule that the legacy pre-push tests are NOT the external
work-package certifier.
"""

from __future__ import annotations

import pytest

from mac.publication_lane import (
    LEGACY_LANE_GUARANTEES,
    MANAGED_LANE_GUARANTEES,
    PUBLICATION_LANE_LEGACY,
    PUBLICATION_LANE_MANAGED,
    PublicationLaneError,
    annotate_inventory_entry,
    classify_publication_lane,
    describe_lane,
    is_legacy_lane,
    is_managed_lane,
    lane_provides_external_certifier,
    lane_provides_landing_receipt,
)


def test_only_package_linked_and_ready_reaches_managed_lane() -> None:
    assert (
        classify_publication_lane(package_linked=True, package_ready=True)
        == PUBLICATION_LANE_MANAGED
    )
    # Every fail-closed combination stays on the legacy compatibility lane.
    assert (
        classify_publication_lane(package_linked=True, package_ready=False)
        == PUBLICATION_LANE_LEGACY
    )
    assert (
        classify_publication_lane(package_linked=False, package_ready=True)
        == PUBLICATION_LANE_LEGACY
    )
    assert (
        classify_publication_lane(package_linked=False, package_ready=False)
        == PUBLICATION_LANE_LEGACY
    )


def test_managed_lane_predicates() -> None:
    assert is_managed_lane(PUBLICATION_LANE_MANAGED) is True
    assert is_legacy_lane(PUBLICATION_LANE_MANAGED) is False
    assert lane_provides_external_certifier(PUBLICATION_LANE_MANAGED) is True
    assert lane_provides_landing_receipt(PUBLICATION_LANE_MANAGED) is True


def test_legacy_lane_is_not_the_external_certifier() -> None:
    assert is_legacy_lane(PUBLICATION_LANE_LEGACY) is True
    assert is_managed_lane(PUBLICATION_LANE_LEGACY) is False
    # The core invariant this task protects: the legacy pre-push tests are not
    # the external work-package certifier and there is no landing receipt.
    assert lane_provides_external_certifier(PUBLICATION_LANE_LEGACY) is False
    assert lane_provides_landing_receipt(PUBLICATION_LANE_LEGACY) is False


def test_describe_managed_lane_lists_exact_candidate_guarantees() -> None:
    described = describe_lane(PUBLICATION_LANE_MANAGED)
    assert described["managed"] is True
    assert described["external_certifier"] is True
    assert described["landing_receipt"] is True
    assert tuple(described["guarantees"]) == MANAGED_LANE_GUARANTEES
    for required in (
        "exact_lease_attempt_ref",
        "controller_verification",
        "independent_pinned_certification",
        "compare_and_swap_landing",
        "remote_read_back_receipt",
        "finalization_proof",
    ):
        assert required in described["guarantees"]


def test_describe_legacy_lane_states_it_is_not_the_certifier() -> None:
    described = describe_lane(PUBLICATION_LANE_LEGACY)
    assert described["managed"] is False
    assert described["external_certifier"] is False
    assert described["landing_receipt"] is False
    assert tuple(described["guarantees"]) == LEGACY_LANE_GUARANTEES
    assert "executor_pre_push_tests" in described["guarantees"]
    # The summary must not describe the pre-push tests as the certifier.
    summary = described["summary"].lower()
    assert "not the external work-package certifier" in summary


def test_unknown_lane_fails_closed() -> None:
    with pytest.raises(PublicationLaneError):
        describe_lane("turbo")
    with pytest.raises(PublicationLaneError):
        is_managed_lane("")
    with pytest.raises(PublicationLaneError):
        lane_provides_external_certifier(None)  # type: ignore[arg-type]


def test_annotate_inventory_entry_maps_readiness_to_lane() -> None:
    ready = annotate_inventory_entry({"agent_id": "a", "ready": True})
    assert ready["publication_lane"] == PUBLICATION_LANE_MANAGED
    assert ready["external_certifier"] is True
    # Original keys are preserved and the input is not mutated in place.
    original = {"agent_id": "b", "ready": False}
    legacy = annotate_inventory_entry(original)
    assert legacy["publication_lane"] == PUBLICATION_LANE_LEGACY
    assert legacy["external_certifier"] is False
    assert "publication_lane" not in original
