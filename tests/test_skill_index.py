"""
tests/test_skill_index.py — Unit tests for lib/memory_graph/skill_index.py

skill_index wraps pg_memory for fleet skill indexing. All PG I/O is mocked.

Coverage:
  - _skill_node_id(): deterministic SHA256-prefix IDs
  - _parse_skill(): frontmatter parsing (name, description, tags, related, version)
  - _parse_skill(): fallback when no frontmatter
  - _section_scope(): classifies header lines into general/machine/personal
  - _split_by_scope(): splits body into buckets
  - annotate_skill(): writes personal/machine annotation nodes + edges
  - index_skills(): missing skills_root returns early, nodes+edges written per skill
  - search_skills(): filters by 'skill' tag, shapes result, respects limit
  - fleet_skill_stats(): returns agent→count dict, handles error
  - get_skill(): fetches node + related edges, returns None when missing
"""

import hashlib
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_pg_stub():
    """Return a minimal pg_memory stub."""
    mod = types.ModuleType("pg_memory")
    mod.upsert_node = MagicMock(return_value=True)
    mod.upsert_edge = MagicMock(return_value=True)
    mod.fleet_search = MagicMock(return_value=[])
    mod.set_agent = MagicMock()
    mod._get_conn = MagicMock()
    return mod


@pytest.fixture(autouse=True)
def fresh_skill_index(tmp_path):
    """
    Reload skill_index with mocked pg_memory per test.
    Yields (skill_index_module, pg_stub).
    """
    lib_path = os.path.join(os.path.dirname(__file__), "..", "lib", "memory_graph")
    if lib_path not in sys.path:
        sys.path.insert(0, lib_path)

    pg_stub = _make_pg_stub()
    sys.modules["pg_memory"] = pg_stub

    for key in list(sys.modules.keys()):
        if "skill_index" in key:
            del sys.modules[key]

    import skill_index as si

    # Reset module-level SKILLS_ROOT to something controllable
    si.SKILLS_ROOT = tmp_path / "skills"

    yield si, pg_stub

    for key in list(sys.modules.keys()):
        if "skill_index" in key:
            del sys.modules[key]
    del sys.modules["pg_memory"]


def _write_skill(skills_root: Path, name: str, content: str) -> Path:
    """Create a SKILL.md at skills_root/<name>/SKILL.md."""
    d = skills_root / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "SKILL.md"
    p.write_text(content)
    return p


# ── _skill_node_id ─────────────────────────────────────────────────────────────


def test_skill_node_id_is_deterministic(fresh_skill_index):
    si, _ = fresh_skill_index
    id1 = si._skill_node_id("bullwinkle", "github")
    id2 = si._skill_node_id("bullwinkle", "github")
    assert id1 == id2


def test_skill_node_id_differs_by_agent(fresh_skill_index):
    si, _ = fresh_skill_index
    id_bw = si._skill_node_id("bullwinkle", "github")
    id_ro = si._skill_node_id("rocky", "github")
    assert id_bw != id_ro


def test_skill_node_id_differs_by_name(fresh_skill_index):
    si, _ = fresh_skill_index
    id_gh = si._skill_node_id("bullwinkle", "github")
    id_tdd = si._skill_node_id("bullwinkle", "tdd")
    assert id_gh != id_tdd


def test_skill_node_id_length(fresh_skill_index):
    si, _ = fresh_skill_index
    node_id = si._skill_node_id("bullwinkle", "github")
    assert len(node_id) == 16


def test_skill_node_id_matches_expected_hash(fresh_skill_index):
    si, _ = fresh_skill_index
    expected = hashlib.sha256("skill:bullwinkle:github".encode()).hexdigest()[:16]
    assert si._skill_node_id("bullwinkle", "github") == expected


# ── _parse_skill ──────────────────────────────────────────────────────────────

SKILL_WITH_FRONTMATTER = """\
---
name: my-skill
description: "Does something useful"
version: 2.1.0
metadata:
  hermes:
    tags: [git, github, ci]
    related_skills: [tdd, debugging]
---

# My Skill

Body content here.
"""

SKILL_NO_FRONTMATTER = """\
# Bare skill

No YAML frontmatter; name comes from directory.
"""


def test_parse_skill_reads_name(fresh_skill_index, tmp_path):
    si, _ = fresh_skill_index
    path = _write_skill(tmp_path, "my-skill", SKILL_WITH_FRONTMATTER)
    result = si._parse_skill(path)
    assert result is not None
    assert result["name"] == "my-skill"


def test_parse_skill_reads_description(fresh_skill_index, tmp_path):
    si, _ = fresh_skill_index
    path = _write_skill(tmp_path, "my-skill", SKILL_WITH_FRONTMATTER)
    result = si._parse_skill(path)
    assert "Does something useful" in result["description"]


def test_parse_skill_reads_version(fresh_skill_index, tmp_path):
    si, _ = fresh_skill_index
    path = _write_skill(tmp_path, "my-skill", SKILL_WITH_FRONTMATTER)
    result = si._parse_skill(path)
    assert result["version"] == "2.1.0"


def test_parse_skill_reads_tags(fresh_skill_index, tmp_path):
    si, _ = fresh_skill_index
    path = _write_skill(tmp_path, "my-skill", SKILL_WITH_FRONTMATTER)
    result = si._parse_skill(path)
    assert "git" in result["tags"]
    assert "github" in result["tags"]
    assert "ci" in result["tags"]


def test_parse_skill_reads_related(fresh_skill_index, tmp_path):
    si, _ = fresh_skill_index
    path = _write_skill(tmp_path, "my-skill", SKILL_WITH_FRONTMATTER)
    result = si._parse_skill(path)
    assert "tdd" in result["related"]
    assert "debugging" in result["related"]


def test_parse_skill_no_frontmatter_uses_dirname(fresh_skill_index, tmp_path):
    si, _ = fresh_skill_index
    path = _write_skill(tmp_path, "bare-skill", SKILL_NO_FRONTMATTER)
    result = si._parse_skill(path)
    assert result is not None
    assert result["name"] == "bare-skill"


def test_parse_skill_content_includes_name_prefix(fresh_skill_index, tmp_path):
    si, _ = fresh_skill_index
    path = _write_skill(tmp_path, "my-skill", SKILL_WITH_FRONTMATTER)
    result = si._parse_skill(path)
    assert result["content"].startswith("SKILL:my-skill")


def test_parse_skill_returns_none_on_missing_file(fresh_skill_index, tmp_path):
    si, _ = fresh_skill_index
    result = si._parse_skill(tmp_path / "nonexistent" / "SKILL.md")
    assert result is None


# ── _section_scope ────────────────────────────────────────────────────────────


def test_section_scope_general(fresh_skill_index):
    si, _ = fresh_skill_index
    assert si._section_scope("## Overview") == "general"
    assert si._section_scope("### Core Workflow") == "general"


def test_section_scope_personal(fresh_skill_index):
    si, _ = fresh_skill_index
    assert si._section_scope("## Pitfalls") == "personal"
    assert si._section_scope("### Lessons Learned") == "personal"
    assert si._section_scope("## Preferences") == "personal"


def test_section_scope_machine(fresh_skill_index):
    si, _ = fresh_skill_index
    assert si._section_scope("## Install") == "machine"
    assert si._section_scope("### Setup") == "machine"
    assert si._section_scope("## Environment Variables") == "machine"


# ── _split_by_scope ───────────────────────────────────────────────────────────

BODY_WITH_SECTIONS = """\
## Overview

General overview text.

## Pitfalls

Watch out for this.

## Setup

Run these commands.
"""


def test_split_by_scope_general_bucket(fresh_skill_index):
    si, _ = fresh_skill_index
    buckets = si._split_by_scope(BODY_WITH_SECTIONS)
    general = "\n".join(buckets["general"])
    assert "Overview" in general
    assert "General overview" in general


def test_split_by_scope_personal_bucket(fresh_skill_index):
    si, _ = fresh_skill_index
    buckets = si._split_by_scope(BODY_WITH_SECTIONS)
    personal = "\n".join(buckets["personal"])
    assert "Watch out" in personal


def test_split_by_scope_machine_bucket(fresh_skill_index):
    si, _ = fresh_skill_index
    buckets = si._split_by_scope(BODY_WITH_SECTIONS)
    machine = "\n".join(buckets["machine"])
    assert "Run these commands" in machine


def test_split_by_scope_all_general_when_no_special_headers(fresh_skill_index):
    si, _ = fresh_skill_index
    buckets = si._split_by_scope("## Core\n\nAll general content.\n\n## Workflow\n\nSteps.")
    assert "All general content" in "\n".join(buckets["general"])
    assert buckets["personal"] == []
    assert buckets["machine"] == []


# ── annotate_skill ─────────────────────────────────────────────────────────────


def test_annotate_skill_personal_uses_fleet_visibility(fresh_skill_index):
    si, pg = fresh_skill_index
    ann_id = si.annotate_skill("github", "personal", "pitfall: foo", agent="bullwinkle")
    assert ann_id is not None
    # upsert_node should have been called with visibility:fleet tag
    call_kwargs = pg.upsert_node.call_args
    tags_arg = call_kwargs[1].get("tags") or call_kwargs[0][3]
    assert "visibility:fleet" in tags_arg


def test_annotate_skill_machine_uses_personal_visibility(fresh_skill_index):
    si, pg = fresh_skill_index
    si.annotate_skill("github", "machine", "path: /usr/local", agent="bullwinkle")
    call_kwargs = pg.upsert_node.call_args
    tags_arg = call_kwargs[1].get("tags") or call_kwargs[0][3]
    assert "visibility:personal" in tags_arg


def test_annotate_skill_writes_refines_edge(fresh_skill_index):
    si, pg = fresh_skill_index
    ann_id = si.annotate_skill("github", "personal", "learned this", agent="bullwinkle")
    # upsert_edge should have been called with relation="refines"
    pg.upsert_edge.assert_called_once()
    edge_call = pg.upsert_edge.call_args
    # upsert_edge called with relation= as keyword arg
    edge_relation = edge_call.kwargs.get("relation") or (
        edge_call.args[2] if len(edge_call.args) > 2 else None
    )
    assert edge_relation == "refines"


def test_annotate_skill_returns_none_on_pg_failure(fresh_skill_index):
    si, pg = fresh_skill_index
    pg.upsert_node.return_value = False
    result = si.annotate_skill("github", "personal", "content")
    assert result is None


# ── index_skills ──────────────────────────────────────────────────────────────


def test_index_skills_missing_root_returns_early(fresh_skill_index, tmp_path):
    si, pg = fresh_skill_index
    si.SKILLS_ROOT = tmp_path / "nonexistent"
    result = si.index_skills(agent="bullwinkle", skills_root=si.SKILLS_ROOT)
    assert result["indexed"] == 0
    assert result["errors"] == 0
    assert "error" in result


def test_index_skills_indexes_one_skill(fresh_skill_index, tmp_path):
    si, pg = fresh_skill_index
    skills_root = tmp_path / "skills"
    _write_skill(skills_root, "myfirstskill", SKILL_WITH_FRONTMATTER)

    result = si.index_skills(agent="bullwinkle", skills_root=skills_root, verbose=False)

    assert result["indexed"] == 1
    assert result["errors"] == 0
    pg.upsert_node.assert_called()


def test_index_skills_wires_related_edges(fresh_skill_index, tmp_path):
    si, pg = fresh_skill_index
    skills_root = tmp_path / "skills"
    # Skill A references skill B
    _write_skill(
        skills_root,
        "skill-a",
        """\
---
name: skill-a
description: "First skill"
metadata:
  hermes:
    tags: [a]
    related_skills: [skill-b]
---
Body A.
""",
    )
    _write_skill(
        skills_root,
        "skill-b",
        """\
---
name: skill-b
description: "Second skill"
metadata:
  hermes:
    tags: [b]
    related_skills: []
---
Body B.
""",
    )

    result = si.index_skills(agent="bullwinkle", skills_root=skills_root, verbose=False)

    assert result["indexed"] == 2
    # At least one edge should have been written
    assert pg.upsert_edge.call_count >= 1


def test_index_skills_counts_errors_when_pg_fails(fresh_skill_index, tmp_path):
    si, pg = fresh_skill_index
    pg.upsert_node.return_value = False  # all upserts fail
    skills_root = tmp_path / "skills"
    _write_skill(skills_root, "failing-skill", SKILL_NO_FRONTMATTER)

    result = si.index_skills(agent="bullwinkle", skills_root=skills_root, verbose=False)

    assert result["errors"] == 1
    assert result["indexed"] == 0


# ── search_skills ─────────────────────────────────────────────────────────────


def test_search_skills_filters_by_skill_tag(fresh_skill_index):
    si, pg = fresh_skill_index
    pg.fleet_search.return_value = [
        {
            "id": "s1",
            "content": "SKILL:github\nDoes github things\n\nRelated: tdd",
            "agent": "bullwinkle",
            "score": 1.5,
            "tags": ["skill", "git"],
        },
        {
            "id": "m1",
            "content": "Some memory node",
            "agent": "rocky",
            "score": 2.0,
            "tags": ["memory"],
        },  # not a skill
    ]

    results = si.search_skills("git")

    assert len(results) == 1
    assert results[0]["name"] == "github"
    assert results[0]["agent"] == "bullwinkle"


def test_search_skills_parses_related_names(fresh_skill_index):
    si, pg = fresh_skill_index
    pg.fleet_search.return_value = [
        {
            "id": "s1",
            "content": "SKILL:github\nDesc\n\nTags: git\nRelated: tdd, code-review",
            "agent": "bullwinkle",
            "score": 1.0,
            "tags": ["skill"],
        },
    ]

    results = si.search_skills("github")

    assert "tdd" in results[0]["related"]
    assert "code-review" in results[0]["related"]


def test_search_skills_respects_limit(fresh_skill_index):
    si, pg = fresh_skill_index
    pg.fleet_search.return_value = [
        {
            "id": f"s{i}",
            "content": f"SKILL:skill{i}\nDesc{i}\n\nRelated:",
            "agent": "bullwinkle",
            "score": 1.0,
            "tags": ["skill"],
        }
        for i in range(20)
    ]

    results = si.search_skills("anything", limit=3)
    assert len(results) == 3


def test_search_skills_returns_empty_when_none_found(fresh_skill_index):
    si, pg = fresh_skill_index
    pg.fleet_search.return_value = []

    results = si.search_skills("obscure query")
    assert results == []


# ── fleet_skill_stats ─────────────────────────────────────────────────────────


def test_fleet_skill_stats_returns_agent_counts(fresh_skill_index):
    si, pg = fresh_skill_index
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [("bullwinkle", 5), ("natasha", 12)]
    mock_conn.cursor.return_value = mock_cursor
    pg._get_conn.return_value = mock_conn

    stats = si.fleet_skill_stats()

    assert stats["bullwinkle"] == 5
    assert stats["natasha"] == 12


def test_fleet_skill_stats_returns_error_on_exception(fresh_skill_index):
    si, pg = fresh_skill_index
    pg._get_conn.side_effect = Exception("pg unreachable")

    stats = si.fleet_skill_stats()

    assert "error" in stats


# ── get_skill ─────────────────────────────────────────────────────────────────


def test_get_skill_returns_none_when_not_found(fresh_skill_index):
    si, pg = fresh_skill_index
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = None
    mock_cursor.fetchall.return_value = []
    mock_conn.cursor.return_value = mock_cursor
    pg._get_conn.return_value = mock_conn

    result = si.get_skill("nonexistent-skill")
    assert result is None


def test_get_skill_returns_dict_when_found(fresh_skill_index):
    si, pg = fresh_skill_index
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (
        "nodeid1",
        "SKILL:github\nDoes git stuff\n\nTags: git\nRelated:",
        "bullwinkle",
        1.5,
        ["skill", "git"],
    )
    mock_cursor.fetchall.return_value = []
    mock_conn.cursor.return_value = mock_cursor
    pg._get_conn.return_value = mock_conn

    result = si.get_skill("github")

    assert result is not None
    assert result["name"] == "github"
    assert result["agent"] == "bullwinkle"
    assert result["score"] == 1.5
    assert result["node_id"] == "nodeid1"


def test_get_skill_includes_related_from_edges(fresh_skill_index):
    si, pg = fresh_skill_index
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (
        "nodeid1",
        "SKILL:github\nDoes git stuff\n\nRelated:",
        "bullwinkle",
        1.0,
        ["skill"],
    )
    # Edge-joined related skill
    mock_cursor.fetchall.return_value = [
        ("SKILL:tdd\nTest-driven development", "bullwinkle"),
    ]
    mock_conn.cursor.return_value = mock_cursor
    pg._get_conn.return_value = mock_conn

    result = si.get_skill("github")

    assert "tdd" in result["related"]


def test_get_skill_returns_error_dict_on_exception(fresh_skill_index):
    si, pg = fresh_skill_index
    pg._get_conn.side_effect = Exception("connection lost")

    result = si.get_skill("any-skill")

    assert result is not None
    assert "error" in result
