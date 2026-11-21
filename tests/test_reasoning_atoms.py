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


# ── query_atoms ───────────────────────────────────────────────────────────────


def _make_qdrant_mock():
    """Build a minimal qdrant_client mock that satisfies lazy imports in reasoning_atoms."""
    import sys

    mock_module = MagicMock()
    # QdrantClient is what query_atoms does `from qdrant_client import QdrantClient`
    mock_client_cls = MagicMock()
    mock_module.QdrantClient = mock_client_cls
    # models sub-module
    mock_models = MagicMock()
    mock_module.models = mock_models
    mock_models.Filter = MagicMock(return_value=MagicMock(must=[]))
    mock_models.FieldCondition = MagicMock(return_value=MagicMock(key="author"))
    mock_models.MatchValue = MagicMock()
    return mock_module, mock_client_cls


def test_query_atoms_returns_hits() -> None:
    """query_atoms parses Qdrant search results into structured dicts."""
    import sys
    from mac.reasoning_atoms import query_atoms

    mock_hit = MagicMock()
    mock_hit.id = "test-uuid-1"
    mock_hit.score = 0.87
    mock_hit.payload = {
        "author": "agent_natasha",
        "thread": "shared-persistent-space",
        "content": "some insight",
        "next_focus": "next direction",
        "empirical": True,
        "ts": "2026-01-01T00:00:00+00:00",
        "schema_version": "2.0",
        "provenance": ["soul_abc"],
        "tension": [],
        "run": 5,
    }

    mock_module, mock_client_cls = _make_qdrant_mock()
    mock_instance = mock_client_cls.return_value
    mock_instance.search.return_value = [mock_hit]

    orig = sys.modules.pop("qdrant_client", None)
    sys.modules["qdrant_client"] = mock_module
    sys.modules["qdrant_client.models"] = mock_module.models
    try:
        results = query_atoms(
            "some insight",
            qdrant_url="http://localhost:6333",
            embed_fn=lambda t: [1.0] + [0.0] * 2047,
        )
    finally:
        if orig is None:
            sys.modules.pop("qdrant_client", None)
            sys.modules.pop("qdrant_client.models", None)
        else:
            sys.modules["qdrant_client"] = orig

    assert len(results) == 1
    r = results[0]
    assert r["id"] == "test-uuid-1"
    assert r["score"] == 0.87
    assert r["author"] == "agent_natasha"
    assert r["content"] == "some insight"
    assert r["next_focus"] == "next direction"
    assert r["empirical"] is True
    assert r["provenance"] == ["soul_abc"]
    assert r["run"] == 5


def test_query_atoms_author_filter_passed_to_qdrant() -> None:
    """author_filter results in a non-None filter passed to Qdrant search."""
    import sys
    from mac.reasoning_atoms import query_atoms

    mock_module, mock_client_cls = _make_qdrant_mock()

    # Capture Filter constructor calls to verify a filter is built
    filter_calls = []

    def capture_filter(**kwargs):
        filter_calls.append(kwargs)
        f = MagicMock()
        f.must = kwargs.get("must", [])
        return f

    mock_module.models.Filter.side_effect = capture_filter

    mock_instance = mock_client_cls.return_value
    mock_instance.search.return_value = []

    orig = sys.modules.pop("qdrant_client", None)
    sys.modules["qdrant_client"] = mock_module
    sys.modules["qdrant_client.models"] = mock_module.models
    try:
        query_atoms(
            "test",
            qdrant_url="http://localhost:6333",
            embed_fn=lambda t: [0.0] * 2048,
            author_filter="agent_bullwinkle",
        )
        call_kwargs = mock_instance.search.call_args[1]
        f = call_kwargs.get("query_filter")
        # A filter was passed (not None) when author_filter is specified
        assert f is not None
        # Filter constructor was called (conditions were built)
        assert len(filter_calls) == 1
    finally:
        if orig is None:
            sys.modules.pop("qdrant_client", None)
            sys.modules.pop("qdrant_client.models", None)
        else:
            sys.modules["qdrant_client"] = orig


def test_query_atoms_empty_collection_returns_empty_list() -> None:
    """Empty Qdrant result maps to empty list."""
    import sys
    from mac.reasoning_atoms import query_atoms

    mock_module, mock_client_cls = _make_qdrant_mock()
    mock_instance = mock_client_cls.return_value
    mock_instance.search.return_value = []

    orig = sys.modules.pop("qdrant_client", None)
    sys.modules["qdrant_client"] = mock_module
    sys.modules["qdrant_client.models"] = mock_module.models
    try:
        results = query_atoms(
            "anything",
            qdrant_url="http://localhost:6333",
            embed_fn=lambda t: [0.0] * 2048,
        )
    finally:
        if orig is None:
            sys.modules.pop("qdrant_client", None)
            sys.modules.pop("qdrant_client.models", None)
        else:
            sys.modules["qdrant_client"] = orig
    assert results == []


def test_query_atoms_missing_qdrant_client_raises() -> None:
    """ImportError for qdrant-client surfaces as RuntimeError."""
    from mac.reasoning_atoms import query_atoms
    import builtins

    real_import = builtins.__import__

    def _block_qdrant(name, *args, **kwargs):
        if name == "qdrant_client":
            raise ImportError("No module named 'qdrant_client'")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_block_qdrant):
        with pytest.raises(RuntimeError, match="qdrant-client"):
            query_atoms("test", qdrant_url="http://localhost:6333")


# ── CLI query subcommand tests ─────────────────────────────────────────────────


def test_cli_query_subcommand_calls_query_atoms(monkeypatch: pytest.MonkeyPatch) -> None:
    """``python -m mac.reasoning_atoms query <text>`` calls query_atoms with query_text."""
    import sys
    from mac.reasoning_atoms import _cli

    captured: list[dict] = []

    def _mock_query(query_text: str, **kwargs: object) -> list[dict]:
        captured.append({"query_text": query_text, "kwargs": kwargs})
        return []

    monkeypatch.setattr("mac.reasoning_atoms.query_atoms", _mock_query)
    monkeypatch.setattr(sys, "argv", ["reasoning_atoms", "query", "shared persistent space"])

    _cli()

    assert len(captured) == 1
    assert captured[0]["query_text"] == "shared persistent space"


def test_cli_query_prints_results(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """``query`` subcommand prints JSON-serialisable results to stdout."""
    import json
    import sys
    from mac.reasoning_atoms import _cli

    fake_hit = {
        "id": "uuid-1",
        "score": 0.91,
        "author": "agent_bullwinkle",
        "thread": "compounding-theorem",
        "content": "The fleet compounds knowledge only when each run produces an external artifact.",
        "next_focus": "measure compound rate empirically",
        "empirical": True,
        "ts": "2026-11-20T10:00:00+00:00",
        "schema_version": "2.0",
        "provenance": ["soul_abc"],
        "tension": [],
        "run": 42,
    }

    monkeypatch.setattr("mac.reasoning_atoms.query_atoms", lambda *a, **kw: [fake_hit])
    monkeypatch.setattr(sys, "argv", ["reasoning_atoms", "query", "compounding"])

    _cli()

    out = capsys.readouterr().out
    # Output should contain the author and score
    assert "agent_bullwinkle" in out
    assert "0.91" in out
    assert "compounding-theorem" in out


def test_cli_query_author_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--author`` flag is forwarded to query_atoms as author_filter."""
    import sys
    from mac.reasoning_atoms import _cli

    captured: list[dict] = []

    def _mock_query(query_text: str, **kwargs: object) -> list[dict]:
        captured.append(kwargs)
        return []

    monkeypatch.setattr("mac.reasoning_atoms.query_atoms", _mock_query)
    monkeypatch.setattr(
        sys, "argv", ["reasoning_atoms", "query", "some topic", "--author", "agent_rocky"]
    )

    _cli()

    assert captured[0].get("author_filter") == "agent_rocky"


def test_cli_query_limit_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--limit N`` is forwarded to query_atoms as limit=N."""
    import sys
    from mac.reasoning_atoms import _cli

    captured: list[dict] = []

    def _mock_query(query_text: str, **kwargs: object) -> list[dict]:
        captured.append(kwargs)
        return []

    monkeypatch.setattr("mac.reasoning_atoms.query_atoms", _mock_query)
    monkeypatch.setattr(sys, "argv", ["reasoning_atoms", "query", "topic", "--limit", "3"])

    _cli()

    assert captured[0].get("limit") == 3


def test_cli_query_empty_results(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """``query`` with no results prints a 'no atoms found' message."""
    import sys
    from mac.reasoning_atoms import _cli

    monkeypatch.setattr("mac.reasoning_atoms.query_atoms", lambda *a, **kw: [])
    monkeypatch.setattr(sys, "argv", ["reasoning_atoms", "query", "nothing"])

    _cli()

    out = capsys.readouterr().out
    assert "no atoms found" in out.lower() or out.strip() == "[]"


def test_cli_query_thread_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--thread`` flag is forwarded to query_atoms as thread_filter."""
    import sys
    from mac.reasoning_atoms import _cli

    captured: list[dict] = []

    monkeypatch.setattr(
        "mac.reasoning_atoms.query_atoms",
        lambda query_text, **kw: captured.append(kw) or [],  # type: ignore[misc]
    )
    monkeypatch.setattr(
        sys, "argv", ["reasoning_atoms", "query", "some query", "--thread", "compounding-theorem"]
    )

    _cli()

    assert captured[0].get("thread_filter") == "compounding-theorem"


# ── write subcommand tests ─────────────────────────────────────────────────────


def _make_valid_atom_dict() -> dict:
    """Helper: minimal valid v2 atom as a dict."""
    return {
        "author": "agent_bullwinkle",
        "thread": "test-thread",
        "content": "Empirical observation: hybrid search reduces miss rate by 15%.",
        "next_focus": "run A/B test with larger query set",
        "provenance": ["soul_node_abc123"],
        "empirical": True,
    }


def test_cli_write_calls_write_atom_to_qdrant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempdirFixture
) -> None:
    """``write <file>`` reads JSON and calls write_atom_to_qdrant, prints point ID."""
    import sys
    import json
    from mac.reasoning_atoms import _cli

    atom_file = tmp_path / "atom.json"
    atom_file.write_text(json.dumps(_make_valid_atom_dict()))

    written: list = []

    def _mock_write(atom, *, point_id=None, qdrant_url=None, embed_fn=None):
        written.append(atom)
        return "test-point-uuid-1234"

    monkeypatch.setattr("mac.reasoning_atoms.write_atom_to_qdrant", _mock_write)
    monkeypatch.setattr(sys, "argv", ["reasoning_atoms", "write", str(atom_file)])

    from io import StringIO

    captured_out = StringIO()
    monkeypatch.setattr(sys, "stdout", captured_out)

    _cli()

    assert len(written) == 1
    assert written[0].author == "agent_bullwinkle"
    assert "test-point-uuid-1234" in captured_out.getvalue()


def test_cli_write_prints_point_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempdirFixture, capsys: pytest.CaptureFixture
) -> None:
    """``write`` output includes the point UUID returned by write_atom_to_qdrant."""
    import sys
    import json
    from mac.reasoning_atoms import _cli

    atom_file = tmp_path / "atom.json"
    atom_file.write_text(json.dumps(_make_valid_atom_dict()))

    monkeypatch.setattr(
        "mac.reasoning_atoms.write_atom_to_qdrant", lambda atom, **kw: "dead-beef-0000"
    )
    monkeypatch.setattr(sys, "argv", ["reasoning_atoms", "write", str(atom_file)])
    _cli()

    out = capsys.readouterr().out
    assert "dead-beef-0000" in out


def test_cli_write_invalid_atom_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempdirFixture
) -> None:
    """``write`` with an atom missing required fields exits with code 1."""
    import sys
    import json
    from mac.reasoning_atoms import _cli

    bad = {"author": "agent_bullwinkle"}  # missing content, thread, next_focus, provenance
    atom_file = tmp_path / "bad.json"
    atom_file.write_text(json.dumps(bad))

    monkeypatch.setattr(sys, "argv", ["reasoning_atoms", "write", str(atom_file)])

    with pytest.raises(SystemExit) as exc_info:
        _cli()
    assert exc_info.value.code != 0


def test_cli_write_forwards_qdrant_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempdirFixture
) -> None:
    """``--qdrant-url`` flag is forwarded to write_atom_to_qdrant."""
    import sys
    import json
    from mac.reasoning_atoms import _cli

    atom_file = tmp_path / "atom.json"
    atom_file.write_text(json.dumps(_make_valid_atom_dict()))

    captured: list = []

    def _mock_write(atom, *, point_id=None, qdrant_url=None, embed_fn=None):
        captured.append(qdrant_url)
        return "some-id"

    monkeypatch.setattr("mac.reasoning_atoms.write_atom_to_qdrant", _mock_write)
    monkeypatch.setattr(
        sys,
        "argv",
        ["reasoning_atoms", "write", str(atom_file), "--qdrant-url", "http://localhost:6333"],
    )
    _cli()

    assert captured[0] == "http://localhost:6333"


def test_cli_write_dry_run_does_not_call_qdrant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempdirFixture, capsys: pytest.CaptureFixture
) -> None:
    """``--dry-run`` validates and prints the atom but does NOT call write_atom_to_qdrant."""
    import sys
    import json
    from mac.reasoning_atoms import _cli

    atom_file = tmp_path / "atom.json"
    atom_file.write_text(json.dumps(_make_valid_atom_dict()))

    write_called: list = []

    monkeypatch.setattr(
        "mac.reasoning_atoms.write_atom_to_qdrant",
        lambda atom, **kw: write_called.append(True) or "should-not-appear",
    )
    monkeypatch.setattr(sys, "argv", ["reasoning_atoms", "write", str(atom_file), "--dry-run"])
    _cli()

    out = capsys.readouterr().out
    assert not write_called, "write_atom_to_qdrant should not be called in dry-run mode"
    assert "dry" in out.lower() or "agent_bullwinkle" in out
