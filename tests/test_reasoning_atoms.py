"""Tests for reasoning_atoms.py — schema v2 + publication criterion.

TDD: these were written before the implementation.
All Qdrant and embedding calls are mocked.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from mac.reasoning_atoms import (
    ReasoningAtom,
    PublicationDecision,
    make_atom,
    validate_atom,
    publication_criterion,
    _cosine_similarity,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _atom(**kwargs) -> ReasoningAtom:
    defaults = dict(
        author="agent_bullwinkle",
        thread="shared-persistent-space",
        content="Hypothesis: splay-decay rate accelerates under high-write load",
        next_focus="Measure write contention effect on splay scores",
        provenance=["abc123"],
        empirical=False,
    )
    defaults.update(kwargs)
    return make_atom(**defaults)


# ── schema v2 validation ─────────────────────────────────────────────────────


def test_valid_atom_passes_validation() -> None:
    atom = _atom()
    # Must not raise
    validate_atom(atom)


def test_missing_next_focus_raises() -> None:
    atom = _atom()
    atom.next_focus = ""
    with pytest.raises(ValueError, match="next_focus"):
        validate_atom(atom)


def test_missing_author_raises() -> None:
    atom = _atom()
    atom.author = ""
    with pytest.raises(ValueError, match="author"):
        validate_atom(atom)


def test_missing_content_raises() -> None:
    atom = _atom()
    atom.content = ""
    with pytest.raises(ValueError, match="content"):
        validate_atom(atom)


def test_missing_thread_raises() -> None:
    atom = _atom()
    atom.thread = ""
    with pytest.raises(ValueError, match="thread"):
        validate_atom(atom)


def test_schema_version_is_2_0() -> None:
    atom = _atom()
    assert atom.schema_version == "2.0"


def test_ts_is_iso_format() -> None:
    atom = _atom()
    # Must parse as ISO-8601 without raising
    from datetime import datetime

    datetime.fromisoformat(atom.ts)


def test_atom_serialises_to_dict() -> None:
    atom = _atom()
    d = asdict(atom)
    assert d["author"] == "agent_bullwinkle"
    assert d["next_focus"] == "Measure write contention effect on splay scores"
    assert d["schema_version"] == "2.0"
    assert isinstance(d["provenance"], list)


# ── publication criterion ─────────────────────────────────────────────────────


def test_empirical_atom_always_publishes() -> None:
    atom = _atom(empirical=True)
    peers: List[ReasoningAtom] = []
    decision = publication_criterion(atom, peers)
    assert decision.should_publish is True
    assert decision.reason == "empirical"


def test_no_peers_publishes() -> None:
    atom = _atom(empirical=False)
    # No peers = no overlap to check; publish
    decision = publication_criterion(atom, [], embed_fn=None)
    assert decision.should_publish is True
    assert "no_peers" in decision.reason


def test_high_overlap_with_peer_next_focus_blocks_publish() -> None:
    # Peer is heading exactly where this atom points — same territory
    atom = _atom(
        content="splay decay accelerates under write pressure",
        empirical=False,
    )
    peer = _atom(
        author="agent_rocky",
        next_focus="splay decay accelerates under write pressure",
    )

    # Mock embed_fn that returns identical vectors for same content
    def _embed(text: str) -> List[float]:
        # Return same 4-d vector for splay-decay content, different otherwise
        if "splay" in text.lower():
            return [1.0, 0.0, 0.0, 0.0]
        return [0.0, 1.0, 0.0, 0.0]

    decision = publication_criterion(atom, [peer], embed_fn=_embed, threshold=0.4)
    assert decision.should_publish is False
    assert "overlap" in decision.reason


def test_low_overlap_with_peer_next_focus_allows_publish() -> None:
    atom = _atom(
        content="splay decay accelerates under write pressure",
        empirical=False,
    )
    peer = _atom(
        author="agent_rocky",
        next_focus="Qdrant embedding dimensionality is not bottleneck",
    )

    def _embed(text: str) -> List[float]:
        if "splay" in text.lower():
            return [1.0, 0.0, 0.0, 0.0]
        return [0.0, 1.0, 0.0, 0.0]

    decision = publication_criterion(atom, [peer], embed_fn=_embed, threshold=0.4)
    assert decision.should_publish is True
    assert "divergent" in decision.reason


# ── cosine similarity ─────────────────────────────────────────────────────────


def test_cosine_similarity_identical() -> None:
    v = [1.0, 0.0, 0.0]
    assert abs(_cosine_similarity(v, v) - 1.0) < 1e-6


def test_cosine_similarity_orthogonal() -> None:
    a = [1.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0]
    assert abs(_cosine_similarity(a, b)) < 1e-6


def test_cosine_similarity_opposite() -> None:
    a = [1.0, 0.0]
    b = [-1.0, 0.0]
    assert abs(_cosine_similarity(a, b) - (-1.0)) < 1e-6


# ── make_atom ─────────────────────────────────────────────────────────────────


def test_make_atom_sets_defaults() -> None:
    atom = make_atom(
        author="agent_natasha",
        thread="theory",
        content="Some content",
        next_focus="Some direction",
        provenance=[],
        empirical=True,
    )
    assert atom.schema_version == "2.0"
    assert atom.tension == []
    assert atom.run is None
    assert atom.ts  # non-empty


def test_make_atom_accepts_run_number() -> None:
    atom = make_atom(
        author="agent_natasha",
        thread="theory",
        content="Content",
        next_focus="Direction",
        provenance=[],
        empirical=False,
        run=42,
    )
    assert atom.run == 42
