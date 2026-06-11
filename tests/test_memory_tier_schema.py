"""mem-06: typed payload + tier enum + collection registry tests."""

from __future__ import annotations

import pytest

from mac.models import (
    MAC_MEMORY_COLLECTIONS,
    MAC_MEMORY_DEFAULT_EMBEDDING_DIM,
    MAC_MEMORY_DEFAULT_EMBEDDING_MODEL,
    MAC_MEMORY_PAYLOAD_SCHEMA,
    MacMemoryTier,
    MacVectorPayload,
    ValidationError,
)


def _payload(**overrides) -> MacVectorPayload:
    base = dict(
        tier=MacMemoryTier.MEDIUM.value,
        subject_type="task_summary",
        subject_id="task_42",
        memory_id="mem_42",
        summary="closed task that did X",
        created_at="2026-05-29T00:00:00+00:00",
        embedded_at="2026-05-29T00:01:00+00:00",
        embedding_model="text-embedding-3-small",
    )
    base.update(overrides)
    return MacVectorPayload(**base)


def test_collection_registry_has_two_tiers():
    """Single point of truth: medium + long, no per-concept fanout."""
    assert MAC_MEMORY_COLLECTIONS == {
        "medium": "mac_memory_medium",
        "long": "mac_memory_long",
    }
    # tier enum agrees with registry keys
    assert {t.value for t in MacMemoryTier} == set(MAC_MEMORY_COLLECTIONS)


def test_default_embedding_dim_matches_default_model():
    """If anyone bumps the dim without changing the model name, the
    install script's collection size will mismatch the writer's
    vectors. Pin the pair here."""
    assert MAC_MEMORY_DEFAULT_EMBEDDING_MODEL == "text-embedding-3-small"
    assert MAC_MEMORY_DEFAULT_EMBEDDING_DIM == 1536


def test_payload_to_dict_round_trips():
    p = _payload(
        task_id="task_42",
        project="mac",
        agent_id="agent_hosta",
        tenant_id="tenant_acme",
        evidence_type="repo_change",
        tags=["incident", "review-pipeline"],
    )
    serialized = p.to_dict()
    parsed = MacVectorPayload.from_dict(serialized)
    assert parsed == p


def test_payload_to_dict_drops_none_fields():
    """Optional fields don't bloat the Qdrant payload."""
    p = _payload()  # no task_id / project / etc.
    out = p.to_dict()
    assert "task_id" not in out
    assert "project" not in out
    assert "tenant_id" not in out
    assert "evidence_type" not in out
    # required fields always present
    assert out["schema"] == MAC_MEMORY_PAYLOAD_SCHEMA
    assert out["tier"] == "medium"
    assert out["memory_id"] == "mem_42"


def test_payload_from_dict_requires_schema_match():
    raw = _payload().to_dict()
    raw["schema"] = "mac.memory.v999"
    with pytest.raises(ValidationError, match="schema"):
        MacVectorPayload.from_dict(raw)


def test_payload_from_dict_validates_tier():
    raw = _payload().to_dict()
    raw["tier"] = "cold"
    with pytest.raises(ValidationError, match="tier"):
        MacVectorPayload.from_dict(raw)


def test_payload_from_dict_requires_summary_and_memory_id():
    raw = _payload().to_dict()
    del raw["summary"]
    with pytest.raises(ValidationError, match="summary"):
        MacVectorPayload.from_dict(raw)
    raw = _payload().to_dict()
    del raw["memory_id"]
    with pytest.raises(ValidationError, match="memory_id"):
        MacVectorPayload.from_dict(raw)
