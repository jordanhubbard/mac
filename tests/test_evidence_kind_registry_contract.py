"""Contract: one canonical evidence-kind registry drives every validation path.

Live operator evidence found the CLI/runtime validator registry accepting kinds
(e.g. ``deployment``) that ``mac.models.EVIDENCE_KINDS`` rejected before storage.
These tests lock in the fix: ``mac.models.EVIDENCE_KINDS`` is the single source of
truth, every ``evidence_type`` the validator registry advertises is an addable
kind through the public control-plane API, and the validator registry can never
advertise a kind outside the canonical set.
"""

from __future__ import annotations

import pytest

import mac.evidence_validators as evidence_validators
from mac.models import (
    EVIDENCE_KINDS,
    EVIDENCE_KIND_CHOICES,
    ValidationError,
    normalize_evidence_kind,
)
from mac.services import ControlPlane


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def test_validator_registry_is_subset_of_canonical_kinds():
    """Every advertised validator evidence_type is an addable canonical kind."""
    advertised = set(evidence_validators.registered_evidence_types())
    assert advertised <= EVIDENCE_KINDS, (
        "validator registry advertises kinds the canonical registry rejects: %s"
        % ", ".join(sorted(advertised - EVIDENCE_KINDS))
    )
    # deployment was the concrete live-operator divergence — assert it explicitly.
    assert "deployment" in EVIDENCE_KINDS
    for kind in advertised:
        assert normalize_evidence_kind(kind) == kind


def test_choices_stay_the_sorted_canonical_view():
    assert set(EVIDENCE_KIND_CHOICES) == EVIDENCE_KINDS
    assert list(EVIDENCE_KIND_CHOICES) == sorted(EVIDENCE_KINDS)


@pytest.mark.parametrize("kind", sorted(EVIDENCE_KINDS))
def test_every_advertised_kind_is_addable_through_the_public_api(cp, kind):
    """The public ``ControlPlane.add_evidence`` accepts and stores every kind in
    the canonical registry — no kind the fleet advertises is rejected pre-storage.
    """
    task = cp.create_task("add every canonical evidence kind")
    checksum = "sha256:%s" % ("a" * 64) if kind == "publication" else None
    evidence = cp.add_evidence(
        task.id,
        kind,
        "file:///tmp/%s.json" % kind,
        "canonical %s evidence" % kind,
        "agent",
        checksum=checksum,
        _trusted_internal=True,
    )
    assert evidence.kind == kind


def test_unsupported_kind_still_rejected(cp):
    task = cp.create_task("reject unknown kind")
    with pytest.raises(ValidationError, match="unsupported evidence kind: bogus"):
        cp.add_evidence(
            task.id,
            "bogus",
            "file:///tmp/bogus.json",
            "unknown kind",
            "agent",
            _trusted_internal=True,
        )
