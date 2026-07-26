"""Single source of truth for evidence-kind validation.

`mac.models.normalize_evidence_kind` / `EVIDENCE_KIND_CHOICES` are the one place
the CLI and the runtime service both consult, so the canonical set of accepted
kinds and the forgiving (case-insensitive, whitespace-trimmed) parsing behave
identically everywhere.
"""

from __future__ import annotations

import pytest

from mac.models import (
    EVIDENCE_KIND_CHOICES,
    EVIDENCE_KINDS,
    normalize_evidence_kind,
)


def test_choices_match_canonical_set_and_are_sorted():
    assert set(EVIDENCE_KIND_CHOICES) == EVIDENCE_KINDS
    assert list(EVIDENCE_KIND_CHOICES) == sorted(EVIDENCE_KINDS)


@pytest.mark.parametrize("kind", sorted(EVIDENCE_KINDS))
def test_every_canonical_kind_normalizes_to_itself(kind):
    assert normalize_evidence_kind(kind) == kind


def test_normalizes_case_and_whitespace():
    assert normalize_evidence_kind("  TeSt ") == "test"
    assert normalize_evidence_kind("REVIEW") == "review"


def test_blank_kind_is_rejected():
    with pytest.raises(ValueError, match="evidence kind is required"):
        normalize_evidence_kind("")
    with pytest.raises(ValueError, match="evidence kind is required"):
        normalize_evidence_kind("   ")
    with pytest.raises(ValueError, match="evidence kind is required"):
        normalize_evidence_kind(None)


def test_unsupported_kind_lists_choices():
    with pytest.raises(ValueError) as excinfo:
        normalize_evidence_kind("bogus")
    message = str(excinfo.value)
    assert "unsupported evidence kind: bogus" in message
    for choice in EVIDENCE_KIND_CHOICES:
        assert choice in message
