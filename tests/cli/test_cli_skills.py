"""`mac admin skills` -- publishing skills/ into a coding harness (ADR 0023).

These commands are local: they read `skills/` and write into a harness
configuration on this host, so none of them touches a hub. What matters at the
CLI boundary is that the install target is a decision the caller makes rather
than one mac infers, and that a refusal reads as a sentence rather than a
traceback.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from mac.cli import main

ROOT = Path(__file__).resolve().parents[2]


def _run(tmp_path, *args):
    """Run `mac --json <args>` and return (rc, parsed_output)."""

    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--json", *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None


def test_skills_list_names_each_skill_its_obligations_and_its_test(tmp_path):
    rc, payload = _run(tmp_path, "admin", "skills", "list")
    assert rc == 0
    names = {entry["name"] for entry in payload["skills"]}
    assert "mac-cli" in names and "agentbus-context" in names
    assert all(entry["tested"] for entry in payload["skills"]), (
        "publishing refuses an untested skill, so none may be listed untested"
    )
    obligations = {
        identifier
        for entry in payload["skills"]
        for identifier in entry["obligations"]
    }
    assert "claim-before-working" in obligations
    assert payload["version"]


def test_skills_render_writes_nothing_and_names_the_files_it_would_write(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    rc, payload = _run(tmp_path, "admin", "skills", "render", "--global", "--harness", "cursor")
    assert rc == 0
    [plugin] = payload["plugins"]
    assert plugin["harness"] == "cursor"
    assert any(
        item["path"] == ".cursor/rules/mac-fleet-obligations.mdc" for item in plugin["files"]
    )
    assert list(home.iterdir()) == [], "render must not write anything"


def test_skills_install_and_uninstall_round_trip_a_nominated_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_HOME", str(tmp_path / "machome"))
    project = tmp_path / "someone-elses-project"
    project.mkdir()
    (project / "AGENTS.md").write_text("# Their conventions\n", encoding="utf-8")

    rc, payload = _run(
        tmp_path, "admin", "skills", "install", "--repo", str(project), "--harness", "codex"
    )
    assert rc == 0
    assert payload["installed"][0]["harness"] == "codex"
    agents = (project / "AGENTS.md").read_text(encoding="utf-8")
    assert "# Their conventions" in agents, "a human's file survives the install"
    assert "claim-before-working" in agents
    assert (project / ".codex" / "skills" / "mac-cli" / "SKILL.md").exists()

    rc, report = _run(tmp_path, "admin", "skills", "status")
    assert rc == 0
    assert [item["harness"] for item in report["installs"]] == ["codex"]
    assert report["installs"][0]["stale"] is False

    rc, _ = _run(
        tmp_path, "admin", "skills", "uninstall", "--repo", str(project), "--harness", "codex"
    )
    assert rc == 0
    assert (project / "AGENTS.md").read_text(encoding="utf-8").strip() == "# Their conventions"
    assert not (project / ".codex").exists()


def test_skills_install_refuses_to_guess_a_target(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["admin", "skills", "install"])
    assert excinfo.value.code == 2
    assert "--global" in capsys.readouterr().err


def test_skills_install_refuses_this_repository_as_a_sentence(tmp_path, capsys):
    rc = main(["admin", "skills", "install", "--repo", str(ROOT)])
    assert rc == 1
    captured = capsys.readouterr()
    assert "source of skills/" in captured.err
    assert "Traceback" not in captured.err
