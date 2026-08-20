"""The release skill must describe machinery that exists.

A release checklist is read once a quarter, under time pressure, by someone who
did not write it. That is the worst possible moment to discover it names a
script that was renamed or a make target that was deleted -- the reader is
mid-release, and the failure looks like their mistake rather than the document's.

The prose about *why* each step exists cannot be tested. The things the reader
will actually type can: the files it points at, the make targets it invokes, and
the single-sourced version it tells them to bump.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "cut-a-release" / "SKILL.md"


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_the_skill_exists_and_is_indexed():
    """An unindexed skill is one nobody reads: AGENTS.md is the entry point."""
    assert SKILL.is_file()
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "skills/cut-a-release/SKILL.md" in agents


def test_every_repository_path_it_names_exists():
    """Same rule the guide is held to: a backticked path asserts it exists."""
    text = _text()
    root_names = {entry.name for entry in ROOT.iterdir()}
    missing = []
    for token in re.findall(r"`([^`\n]+)`", text):
        if "/" not in token or any(ch in token for ch in " \t<>*$\"'|"):
            continue
        if "://" in token or token.startswith(("/", "~", "#", "http")):
            continue
        if token.split("/", 1)[0] not in root_names:
            continue
        if not (ROOT / token.rstrip("/")).exists():
            missing.append(token)
    assert not missing, "the release skill names paths that do not exist: %s" % sorted(
        set(missing)
    )


def test_every_make_target_it_invokes_is_real():
    """`make docs-check` failing because the target was renamed is a bad way to
    learn that the release checklist is stale."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    declared = set(re.findall(r"^([a-zA-Z0-9_-]+):", makefile, re.M))
    invoked = set(re.findall(r"\bmake ([a-z][a-z0-9-]*)", _text()))
    unknown = sorted(invoked - declared)
    assert not unknown, "the release skill invokes make targets that do not exist: %s" % unknown


def test_the_version_bump_it_describes_is_still_single_sourced():
    """The skill tells the reader to edit exactly one line. If the version stops
    being single-sourced, that instruction silently ships a half-bumped release.
    """
    assert '__version__' in (ROOT / "src" / "mac" / "__init__.py").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in pyproject, (
        "pyproject no longer derives the version dynamically; the release skill's "
        "one-line bump instruction is now wrong"
    )


def test_it_still_points_at_the_publish_script():
    """The publish step is the one part that is a command rather than prose, so
    it is the part that breaks silently if the script moves."""
    assert "scripts/publish-deck-to-slides.py" in _text()
    assert (ROOT / "scripts" / "publish-deck-to-slides.py").is_file()
