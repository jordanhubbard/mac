"""The judgement skill must name machinery that exists and stay indexed."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "judgement" / "SKILL.md"


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_the_skill_exists_and_is_indexed():
    assert SKILL.is_file()
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "skills/judgement/SKILL.md" in agents


def test_checklist_kinds_are_named_in_the_skill():
    text = _text()
    for kind in (
        "review_rejection_loop",
        "high_token_without_publication",
        "failed_dependency_deadlock",
        "stuck_reviewing",
        "semantic_reviewer_still_assigned",
        "excessive_reviewing_population",
        "too_many_gates",
        "orphaned_pull_request",
        "duplicate_pull_request",
        "unlanded_pull_request",
    ):
        assert kind in text


def test_the_skill_names_the_process_module():
    assert "src/mac/judgement.py" in _text()
    assert (ROOT / "src/mac/judgement.py").is_file()


def test_every_repository_path_it_names_exists():
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
    assert not missing, "the judgement skill names paths that do not exist: %s" % sorted(
        set(missing)
    )
