"""Tests for src/mac/changeset_adoption.py.

Covers the two cohesive halves of the controller changeset-adoption core:

- BINDING: build_adoption_binding() / bind_changeset() validation of the
  changeset id, target, and generation (empty, whitespace-only, null-byte,
  non-integer, negative), label validation (mapping type, duplicate keys,
  empty key/value), deterministic/canonical content_hash output, and the
  ChangesetProposal wrapper (type + schema guards).
- ATTESTATION: attest_adoption() producing a stable digest for identical
  bindings, verify_adoption_attestation() accepting a genuine attestation and
  detecting tampering when any bound field, attestation field, or the digest
  is mutated, plus wrong-key rejection and the exported schema constant.

All tests are hermetic: pure dataclasses and pure functions, no network, no
live services, no clock/env dependence beyond deterministic helpers.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from mac import changeset_adoption
from mac.changeset_adoption import (
    ADOPTION_ATTESTATION_SCHEMA,
    ADOPTION_BINDING_SCHEMA,
    ADOPTION_PROPOSAL_SCHEMA,
    BINDING_STAGE_BIND,
    BINDING_STATUS_PROPOSED,
    AdoptionAttestation,
    AdoptionBinding,
    ChangesetAdoptionError,
    ChangesetProposal,
    attest_adoption,
    bind_changeset,
    build_adoption_binding,
    verify_adoption_attestation,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KEY = "controller-secret-key"


def _binding(**overrides):
    """Build a valid binding, overriding named constructor arguments."""

    kwargs = {
        "changeset_id": "cs-123",
        "target": "cluster-a",
        "generation": 7,
        "labels": {"env": "prod", "team": "core"},
    }
    kwargs.update(overrides)
    return build_adoption_binding(
        kwargs["changeset_id"],
        kwargs["target"],
        kwargs["generation"],
        labels=kwargs["labels"],
    )


# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------


def test_schema_constants_pin_wire_version():
    assert ADOPTION_PROPOSAL_SCHEMA == "mac.changeset_adoption.v1"
    assert ADOPTION_BINDING_SCHEMA == "mac.changeset_adoption.v1"
    assert ADOPTION_ATTESTATION_SCHEMA == "mac.changeset_adoption.v1"


def test_error_class_is_value_error_subclass():
    assert issubclass(ChangesetAdoptionError, ValueError)


# ---------------------------------------------------------------------------
# BINDING: happy path + determinism
# ---------------------------------------------------------------------------


def test_build_adoption_binding_valid_single_binding():
    binding = build_adoption_binding("cs-1", "cluster-a", 0)
    assert isinstance(binding, AdoptionBinding)
    assert binding.changeset_id == "cs-1"
    assert binding.target == "cluster-a"
    assert binding.generation == 0
    assert binding.labels == {}
    assert binding.status == BINDING_STATUS_PROPOSED
    assert binding.stage == BINDING_STAGE_BIND
    assert binding.schema == ADOPTION_BINDING_SCHEMA
    assert binding.content_hash.startswith("sha256:")
    assert binding.binding_id.startswith("adopt_")
    assert binding.created_at


def test_build_adoption_binding_trims_whitespace_inputs():
    binding = build_adoption_binding("  cs-1  ", "\tcluster-a\n", 3)
    assert binding.changeset_id == "cs-1"
    assert binding.target == "cluster-a"


def test_content_hash_is_deterministic_for_identical_content():
    first = _binding()
    second = _binding()
    # Distinct binding_ids / timestamps, but stable content hash.
    assert first.binding_id != second.binding_id
    assert first.content_hash == second.content_hash


def test_content_hash_ignores_label_ordering():
    ordered = build_adoption_binding("cs", "t", 1, labels={"a": "1", "b": "2"})
    reordered = build_adoption_binding("cs", "t", 1, labels={"b": "2", "a": "1"})
    assert ordered.content_hash == reordered.content_hash
    assert ordered.labels == {"a": "1", "b": "2"}


@pytest.mark.parametrize(
    "field_overrides",
    [
        {"changeset_id": "cs-other"},
        {"target": "cluster-b"},
        {"generation": 8},
        {"labels": {"env": "staging", "team": "core"}},
    ],
)
def test_content_hash_changes_when_bound_content_changes(field_overrides):
    base = _binding()
    changed = _binding(**field_overrides)
    assert base.content_hash != changed.content_hash


def test_canonical_excludes_volatile_fields():
    binding = _binding()
    canonical = binding.canonical()
    assert set(canonical) == {
        "changeset_id",
        "generation",
        "labels",
        "schema",
        "target",
    }
    assert "binding_id" not in canonical
    assert "created_at" not in canonical
    assert "status" not in canonical
    assert "stage" not in canonical


# ---------------------------------------------------------------------------
# BINDING: rejection of invalid changeset id / target / generation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "   ", "\t\n", None])
def test_reject_empty_or_whitespace_changeset_id(bad):
    with pytest.raises(ChangesetAdoptionError):
        build_adoption_binding(bad, "cluster-a", 1)


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_reject_empty_or_whitespace_target(bad):
    with pytest.raises(ChangesetAdoptionError):
        build_adoption_binding("cs-1", bad, 1)


def test_reject_null_byte_changeset_id():
    with pytest.raises(ChangesetAdoptionError):
        build_adoption_binding("cs\x00id", "cluster-a", 1)


def test_reject_null_byte_target():
    with pytest.raises(ChangesetAdoptionError):
        build_adoption_binding("cs-1", "clus\x00ter", 1)


@pytest.mark.parametrize("bad", ["1", 1.0, None, True, False])
def test_reject_non_integer_generation(bad):
    with pytest.raises(ChangesetAdoptionError):
        build_adoption_binding("cs-1", "cluster-a", bad)


def test_reject_negative_generation():
    with pytest.raises(ChangesetAdoptionError):
        build_adoption_binding("cs-1", "cluster-a", -1)


# ---------------------------------------------------------------------------
# BINDING: label validation
# ---------------------------------------------------------------------------


def test_labels_none_yields_empty_mapping():
    assert build_adoption_binding("cs-1", "t", 1, labels=None).labels == {}


def test_reject_non_mapping_labels():
    with pytest.raises(ChangesetAdoptionError):
        build_adoption_binding("cs-1", "t", 1, labels=[("a", "b")])


def test_reject_empty_label_key():
    with pytest.raises(ChangesetAdoptionError):
        build_adoption_binding("cs-1", "t", 1, labels={"   ": "v"})


def test_reject_empty_label_value():
    with pytest.raises(ChangesetAdoptionError):
        build_adoption_binding("cs-1", "t", 1, labels={"k": "  "})


def test_reject_null_byte_label_value():
    with pytest.raises(ChangesetAdoptionError):
        build_adoption_binding("cs-1", "t", 1, labels={"k": "v\x00"})


def test_reject_duplicate_label_keys_after_trimming():
    # Two keys that collapse to the same trimmed key are a duplicate conflict.
    with pytest.raises(ChangesetAdoptionError):
        build_adoption_binding("cs-1", "t", 1, labels={"env": "a", " env ": "b"})


def test_labels_are_sorted_in_binding():
    binding = build_adoption_binding("cs", "t", 1, labels={"z": "1", "a": "2"})
    assert list(binding.labels) == ["a", "z"]


# ---------------------------------------------------------------------------
# BINDING: ChangesetProposal wrapper (bind_changeset)
# ---------------------------------------------------------------------------


def test_bind_changeset_from_valid_proposal():
    proposal = ChangesetProposal(
        changeset_id="cs-9",
        target="cluster-x",
        generation=2,
        labels={"env": "prod"},
    )
    assert proposal.schema == ADOPTION_PROPOSAL_SCHEMA
    binding = bind_changeset(proposal)
    direct = build_adoption_binding("cs-9", "cluster-x", 2, labels={"env": "prod"})
    assert binding.content_hash == direct.content_hash


def test_proposal_canonical_is_sorted():
    proposal = ChangesetProposal(
        changeset_id="cs",
        target="t",
        generation=1,
        labels={"z": "1", "a": "2"},
    )
    canonical = proposal.canonical()
    assert list(canonical["labels"]) == ["a", "z"]
    assert set(canonical) == {
        "changeset_id",
        "generation",
        "labels",
        "schema",
        "target",
    }


def test_bind_changeset_rejects_non_proposal():
    with pytest.raises(ChangesetAdoptionError):
        bind_changeset({"changeset_id": "cs"})


def test_bind_changeset_rejects_unsupported_proposal_schema():
    proposal = ChangesetProposal(
        changeset_id="cs",
        target="t",
        generation=1,
        schema="mac.changeset_adoption.v2",
    )
    with pytest.raises(ChangesetAdoptionError):
        bind_changeset(proposal)


def test_bind_changeset_propagates_validation_errors():
    proposal = ChangesetProposal(changeset_id="   ", target="t", generation=1)
    with pytest.raises(ChangesetAdoptionError):
        bind_changeset(proposal)


# ---------------------------------------------------------------------------
# ATTESTATION: happy path + determinism
# ---------------------------------------------------------------------------


def test_attest_adoption_produces_verifiable_attestation():
    binding = _binding()
    attestation = attest_adoption(binding, _KEY)
    assert isinstance(attestation, AdoptionAttestation)
    assert attestation.binding_id == binding.binding_id
    assert attestation.content_hash == binding.content_hash
    assert attestation.algorithm == "hmac-sha256"
    assert attestation.schema == ADOPTION_ATTESTATION_SCHEMA
    assert attestation.key_fingerprint.startswith("sha256:")
    assert attestation.digest
    assert verify_adoption_attestation(binding, attestation, _KEY) is True


def test_attest_adoption_digest_stable_for_identical_bindings():
    first = _binding()
    second = _binding()
    assert first.content_hash == second.content_hash
    # Digest is over canonical content (not binding_id), so it is stable.
    assert attest_adoption(first, _KEY).digest == attest_adoption(second, _KEY).digest


def test_attest_adoption_accepts_bytes_key():
    binding = _binding()
    attestation = attest_adoption(binding, b"raw-bytes-key")
    assert verify_adoption_attestation(binding, attestation, b"raw-bytes-key") is True


def test_attest_adoption_string_and_bytes_keys_agree():
    binding = _binding()
    str_att = attest_adoption(binding, "abc")
    bytes_att = attest_adoption(binding, b"abc")
    assert str_att.digest == bytes_att.digest


# ---------------------------------------------------------------------------
# ATTESTATION: input guards
# ---------------------------------------------------------------------------


def test_attest_rejects_non_binding():
    with pytest.raises(ChangesetAdoptionError):
        attest_adoption(object(), _KEY)


def test_attest_rejects_binding_without_content_hash():
    binding = replace(_binding(), content_hash="")
    with pytest.raises(ChangesetAdoptionError):
        attest_adoption(binding, _KEY)


@pytest.mark.parametrize("bad_key", ["", "   ", None])
def test_attest_rejects_empty_string_key(bad_key):
    with pytest.raises(ChangesetAdoptionError):
        attest_adoption(_binding(), bad_key)


def test_attest_rejects_empty_bytes_key():
    with pytest.raises(ChangesetAdoptionError):
        attest_adoption(_binding(), b"")


# ---------------------------------------------------------------------------
# ATTESTATION: tamper detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field_overrides",
    [
        {"changeset_id": "tampered"},
        {"target": "other-cluster"},
        {"generation": 99},
        {"labels": {"env": "prod", "team": "evil"}},
        {"schema": "mac.changeset_adoption.v2"},
    ],
)
def test_verify_detects_bound_field_tampering(field_overrides):
    binding = _binding()
    attestation = attest_adoption(binding, _KEY)
    tampered = replace(binding, **field_overrides)
    assert verify_adoption_attestation(tampered, attestation, _KEY) is False


def test_verify_detects_content_hash_tampering():
    binding = _binding()
    attestation = attest_adoption(binding, _KEY)
    tampered = replace(binding, content_hash="sha256:deadbeef")
    assert verify_adoption_attestation(tampered, attestation, _KEY) is False


def test_verify_detects_binding_id_mismatch():
    binding = _binding()
    attestation = attest_adoption(binding, _KEY)
    tampered = replace(binding, binding_id="adopt_other")
    assert verify_adoption_attestation(tampered, attestation, _KEY) is False


def test_verify_detects_digest_tampering():
    binding = _binding()
    attestation = attest_adoption(binding, _KEY)
    tampered = replace(attestation, digest="0" * 64)
    assert verify_adoption_attestation(binding, tampered, _KEY) is False


def test_verify_detects_algorithm_tampering():
    binding = _binding()
    attestation = attest_adoption(binding, _KEY)
    tampered = replace(attestation, algorithm="hmac-sha512")
    assert verify_adoption_attestation(binding, tampered, _KEY) is False


def test_verify_detects_schema_tampering():
    binding = _binding()
    attestation = attest_adoption(binding, _KEY)
    tampered = replace(attestation, schema="mac.changeset_adoption.v2")
    assert verify_adoption_attestation(binding, tampered, _KEY) is False


def test_verify_detects_attestation_content_hash_tampering():
    binding = _binding()
    attestation = attest_adoption(binding, _KEY)
    tampered = replace(attestation, content_hash="sha256:deadbeef")
    assert verify_adoption_attestation(binding, tampered, _KEY) is False


def test_verify_rejects_wrong_key():
    binding = _binding()
    attestation = attest_adoption(binding, _KEY)
    assert verify_adoption_attestation(binding, attestation, "wrong-key") is False


def test_verify_rejects_non_binding():
    binding = _binding()
    attestation = attest_adoption(binding, _KEY)
    with pytest.raises(ChangesetAdoptionError):
        verify_adoption_attestation(object(), attestation, _KEY)


def test_verify_rejects_non_attestation():
    with pytest.raises(ChangesetAdoptionError):
        verify_adoption_attestation(_binding(), object(), _KEY)


@pytest.mark.parametrize("bad_key", ["", None])
def test_verify_rejects_empty_key(bad_key):
    binding = _binding()
    attestation = attest_adoption(binding, _KEY)
    with pytest.raises(ChangesetAdoptionError):
        verify_adoption_attestation(binding, attestation, bad_key)


def test_module_exposes_expected_public_surface():
    for name in (
        "ChangesetProposal",
        "AdoptionBinding",
        "AdoptionAttestation",
        "ChangesetAdoptionError",
        "build_adoption_binding",
        "bind_changeset",
        "attest_adoption",
        "verify_adoption_attestation",
    ):
        assert hasattr(changeset_adoption, name)
