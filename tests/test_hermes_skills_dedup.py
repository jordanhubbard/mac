"""Tests for remove_shadowed_top_level_duplicates() in skills_sync.

Regression coverage for the 'Ambiguous skill name' error that occurs when
a skill exists at BOTH ~/.hermes/skills/<name>/ (legacy top-level) AND
~/.hermes/skills/<category>/<name>/ (categorized). The fix archives the
top-level duplicate so only the categorized version remains active.

Acceptance criteria (from task description):
  skill_view('writing-plans') succeeds without ambiguity error after
  remove_shadowed_top_level_duplicates() runs.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mac import hermes_vendor

pytestmark = pytest.mark.skipif(
    not hermes_vendor.is_vendored(), reason="no vendored Hermes snapshot present"
)


# ---------------------------------------------------------------------------
# Lazy import via the vendor path (mirrors pattern in test_fleet_tool.py)
# ---------------------------------------------------------------------------

def _get_skills_sync():
    hermes_vendor.ensure_on_path()
    from tools import skills_sync  # noqa: PLC0415
    return skills_sync


# ---------------------------------------------------------------------------
# Helpers to build a minimal fake skills tree
# ---------------------------------------------------------------------------

SKILL_MD_TEMPLATE = """---
name: {name}
description: Test skill for {name}
---
# {name}
This is a test skill.
"""


def _make_skill(base: Path, rel_path: str, name: str) -> Path:
    """Create a SKILL.md at base/rel_path/SKILL.md and return the dir."""
    skill_dir = base / rel_path
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        SKILL_MD_TEMPLATE.format(name=name), encoding="utf-8"
    )
    return skill_dir


# ---------------------------------------------------------------------------
# Fixture: redirect SKILLS_DIR to a tmp_path
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_skills_dir(tmp_path, monkeypatch):
    """Redirect SKILLS_DIR in skills_sync to a temporary directory."""
    ss = _get_skills_sync()
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    monkeypatch.setattr(ss, "SKILLS_DIR", skills_root)
    monkeypatch.setattr(ss, "MANIFEST_FILE", skills_root / ".bundled_manifest")
    return skills_root


# ---------------------------------------------------------------------------
# Tests for remove_shadowed_top_level_duplicates
# ---------------------------------------------------------------------------

class TestRemoveShadowedTopLevelDuplicates:
    """Unit tests for the deduplication helper."""

    def test_archives_top_level_when_categorized_exists(self, fake_skills_dir):
        """Top-level writing-plans/ is archived when software-development/writing-plans/ exists."""
        ss = _get_skills_sync()

        _make_skill(fake_skills_dir, "writing-plans", "writing-plans")
        _make_skill(
            fake_skills_dir,
            "software-development/writing-plans",
            "writing-plans",
        )

        archived = ss.remove_shadowed_top_level_duplicates(quiet=True)

        assert archived == ["writing-plans"], f"Expected ['writing-plans'], got {archived}"
        # Top-level copy is gone
        assert not (fake_skills_dir / "writing-plans").exists()
        # Moved to .archive/
        assert (fake_skills_dir / ".archive" / "writing-plans" / "SKILL.md").exists()
        # Categorized copy is untouched
        assert (
            fake_skills_dir / "software-development" / "writing-plans" / "SKILL.md"
        ).exists()

    def test_all_four_known_duplicates_archived(self, fake_skills_dir):
        """All four skills mentioned in the task description are handled."""
        ss = _get_skills_sync()

        skill_names = [
            "writing-plans",
            "systematic-debugging",
            "test-driven-development",
            "requesting-code-review",
        ]
        for name in skill_names:
            _make_skill(fake_skills_dir, name, name)
            _make_skill(fake_skills_dir, f"software-development/{name}", name)

        archived = ss.remove_shadowed_top_level_duplicates(quiet=True)

        assert sorted(archived) == sorted(skill_names)
        for name in skill_names:
            assert not (fake_skills_dir / name).exists(), f"{name} top-level should be gone"
            assert (fake_skills_dir / ".archive" / name / "SKILL.md").exists()
            assert (
                fake_skills_dir / "software-development" / name / "SKILL.md"
            ).exists()

    def test_no_action_when_no_categorized_copy(self, fake_skills_dir):
        """Top-level skill without any categorized copy is left alone."""
        ss = _get_skills_sync()

        _make_skill(fake_skills_dir, "standalone-skill", "standalone-skill")

        archived = ss.remove_shadowed_top_level_duplicates(quiet=True)

        assert archived == []
        assert (fake_skills_dir / "standalone-skill" / "SKILL.md").exists()

    def test_no_action_when_only_top_level_exists(self, fake_skills_dir):
        """Only top-level copy (no categorized duplicate) — must not be touched."""
        ss = _get_skills_sync()

        _make_skill(fake_skills_dir, "my-skill", "my-skill")

        archived = ss.remove_shadowed_top_level_duplicates(quiet=True)
        assert archived == []
        assert (fake_skills_dir / "my-skill" / "SKILL.md").exists()

    def test_category_dir_without_skill_md_is_not_a_candidate(self, fake_skills_dir):
        """A subdirectory without SKILL.md at the categorized level does not trigger dedup."""
        ss = _get_skills_sync()

        _make_skill(fake_skills_dir, "my-skill", "my-skill")
        # Category has the folder but NO SKILL.md
        (fake_skills_dir / "cat" / "my-skill").mkdir(parents=True)

        archived = ss.remove_shadowed_top_level_duplicates(quiet=True)
        assert archived == []
        assert (fake_skills_dir / "my-skill" / "SKILL.md").exists()

    def test_idempotent_when_already_archived(self, fake_skills_dir):
        """Running twice is safe — second run does nothing after first archived."""
        ss = _get_skills_sync()

        _make_skill(fake_skills_dir, "writing-plans", "writing-plans")
        _make_skill(
            fake_skills_dir,
            "software-development/writing-plans",
            "writing-plans",
        )

        first = ss.remove_shadowed_top_level_duplicates(quiet=True)
        assert "writing-plans" in first

        # After first run top-level is gone; running again should be a no-op
        second = ss.remove_shadowed_top_level_duplicates(quiet=True)
        assert second == []

    def test_empty_skills_dir_returns_empty(self, fake_skills_dir):
        """Empty skills directory returns empty list without crashing."""
        ss = _get_skills_sync()

        archived = ss.remove_shadowed_top_level_duplicates(quiet=True)
        assert archived == []

    def test_nonexistent_skills_dir_returns_empty(self, tmp_path, monkeypatch):
        """Skills dir that doesn't exist at all returns empty list."""
        ss = _get_skills_sync()

        nonexistent = tmp_path / "no_such_dir"
        monkeypatch.setattr(ss, "SKILLS_DIR", nonexistent)

        archived = ss.remove_shadowed_top_level_duplicates(quiet=True)
        assert archived == []

    def test_dotfiles_and_hidden_dirs_skipped(self, fake_skills_dir):
        """Hidden dirs (starting with '.') are never treated as skill dirs."""
        ss = _get_skills_sync()

        # Create a .hidden top-level dir with a SKILL.md — should be skipped
        hidden = fake_skills_dir / ".hidden-skill"
        hidden.mkdir()
        (hidden / "SKILL.md").write_text("---\nname: hidden\n---\n", encoding="utf-8")

        # Create a categorized copy that would match if dedup ran on it
        _make_skill(fake_skills_dir, "cat/.hidden-skill", "hidden-skill")

        archived = ss.remove_shadowed_top_level_duplicates(quiet=True)
        # .hidden-skill starts with '.' so it must be skipped
        assert ".hidden-skill" not in archived
        assert hidden.exists()

    def test_returns_only_folder_name_strings(self, fake_skills_dir):
        """Return value is a list of bare folder names (strings), not Path objects."""
        ss = _get_skills_sync()

        _make_skill(fake_skills_dir, "my-tool", "my-tool")
        _make_skill(fake_skills_dir, "devops/my-tool", "my-tool")

        archived = ss.remove_shadowed_top_level_duplicates(quiet=True)
        assert all(isinstance(x, str) for x in archived)
        assert archived == ["my-tool"]

    def test_category_dir_itself_is_not_archived(self, fake_skills_dir):
        """Category directories (which have no direct SKILL.md) are never archived."""
        ss = _get_skills_sync()

        # Create a category dir with a nested skill — category dir itself must not be archived
        _make_skill(fake_skills_dir, "software-development/writing-plans", "writing-plans")
        # Verify 'software-development' does NOT have a top-level SKILL.md
        assert not (fake_skills_dir / "software-development" / "SKILL.md").exists()

        archived = ss.remove_shadowed_top_level_duplicates(quiet=True)
        assert archived == []
        assert (fake_skills_dir / "software-development").exists()


# ---------------------------------------------------------------------------
# Integration: sync_skills includes shadowed_archived in its return dict
# ---------------------------------------------------------------------------

class TestSyncSkillsIncludesShadowedArchived:
    """Verify sync_skills() returns shadowed_archived key."""

    def test_sync_skills_result_has_shadowed_archived_key(
        self, fake_skills_dir, tmp_path, monkeypatch
    ):
        """sync_skills() always returns a shadowed_archived list (may be empty)."""
        ss = _get_skills_sync()

        # Point bundled dir to something that exists but is empty so sync is a no-op
        empty_bundled = tmp_path / "bundled"
        empty_bundled.mkdir()
        monkeypatch.setattr(ss, "_get_bundled_dir", lambda: empty_bundled)
        monkeypatch.setattr(ss, "_get_optional_dir", lambda: tmp_path / "optional")

        result = ss.sync_skills(quiet=True)
        assert "shadowed_archived" in result
        assert isinstance(result["shadowed_archived"], list)

    def test_sync_skills_archives_duplicates_automatically(
        self, fake_skills_dir, tmp_path, monkeypatch
    ):
        """sync_skills() calls remove_shadowed_top_level_duplicates() and reports results."""
        ss = _get_skills_sync()

        # Build a pre-existing duplicate in the fake skills dir
        _make_skill(fake_skills_dir, "writing-plans", "writing-plans")
        _make_skill(
            fake_skills_dir, "software-development/writing-plans", "writing-plans"
        )

        # Point bundled dir to an empty dir so no new skills are copied
        empty_bundled = tmp_path / "bundled"
        empty_bundled.mkdir()
        monkeypatch.setattr(ss, "_get_bundled_dir", lambda: empty_bundled)
        monkeypatch.setattr(ss, "_get_optional_dir", lambda: tmp_path / "optional")

        result = ss.sync_skills(quiet=True)

        assert "writing-plans" in result["shadowed_archived"]
        assert not (fake_skills_dir / "writing-plans").exists()
        assert (fake_skills_dir / ".archive" / "writing-plans" / "SKILL.md").exists()

    def test_no_bundled_dir_returns_shadowed_archived_key(
        self, fake_skills_dir, tmp_path, monkeypatch
    ):
        """sync_skills() early return (no bundled dir) still has shadowed_archived key."""
        ss = _get_skills_sync()

        missing_bundled = tmp_path / "missing"
        monkeypatch.setattr(ss, "_get_bundled_dir", lambda: missing_bundled)

        result = ss.sync_skills(quiet=True)
        assert "shadowed_archived" in result
        assert isinstance(result["shadowed_archived"], list)
