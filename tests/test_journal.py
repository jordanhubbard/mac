"""Agent memory journaling: snapshot soul+memory state, run a backup hook, and
restore — the guard against irreversible soul loss."""

from __future__ import annotations

import json
from pathlib import Path

from mac import journal


def _make_agent_home(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "SOUL.md").write_text("# SOUL\nI'm Rocky.\n", encoding="utf-8")
    (home / "USER.md").write_text("user likes terse replies\n", encoding="utf-8")
    (home / "MEMORY.md").write_text("the deploy was flaky on 2026-06-01\n", encoding="utf-8")
    (home / "config.yaml").write_text("model: {provider: custom}\n", encoding="utf-8")
    mem = home / "memories"
    mem.mkdir(exist_ok=True)
    (mem / "2026-06-01.md").write_text("woke up, fixed a tunnel\n", encoding="utf-8")
    # a STATE_ENTRY that does NOT exist (mood files) must be silently skipped


def test_snapshot_captures_state_and_manifest(tmp_path):
    home = tmp_path / ".hermes"
    root = tmp_path / ".mac" / "journal"
    _make_agent_home(home)

    m = journal.snapshot(home=home, root=root, date="2026-06-04", agent_id="rocky", run_hook=False)

    dest = root / "2026-06-04"
    assert (dest / "SOUL.md").read_text().startswith("# SOUL")
    assert (dest / "memories" / "2026-06-01.md").exists()
    assert set(m["captured"]) == {"SOUL.md", "USER.md", "MEMORY.md", "memories", "config.yaml"}
    # per-file checksums recorded for every file, manifest written
    assert "SOUL.md" in m["files"] and "memories/2026-06-01.md" in m["files"]
    assert m["agent_id"] == "rocky" and m["version"] == "mac.agent_journal.v1"
    assert json.loads((dest / "manifest.json").read_text())["date"] == "2026-06-04"


def test_backup_hook_runs_with_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    root = tmp_path / ".mac" / "journal"
    _make_agent_home(home)
    marker = tmp_path / "uploaded.txt"
    monkeypatch.setenv(
        "MAC_JOURNAL_BACKUP_HOOK", 'echo "$MAC_JOURNAL_AGENT $MAC_JOURNAL_DATE" > "%s"' % marker
    )

    m = journal.snapshot(home=home, root=root, date="2026-06-04", agent_id="natasha")

    assert m["hook"]["ran"] is True and m["hook"]["exit_code"] == 0
    assert marker.read_text().strip() == "natasha 2026-06-04"


def test_hook_failure_does_not_break_snapshot(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    root = tmp_path / ".mac" / "journal"
    _make_agent_home(home)
    monkeypatch.setenv("MAC_JOURNAL_BACKUP_HOOK", "exit 7")

    m = journal.snapshot(home=home, root=root, date="2026-06-04", agent_id="rocky")
    # the local journal is still written; the hook failure is just recorded
    assert (root / "2026-06-04" / "SOUL.md").exists()
    assert m["hook"]["exit_code"] == 7


def test_list_journals(tmp_path):
    home = tmp_path / ".hermes"
    root = tmp_path / ".mac" / "journal"
    _make_agent_home(home)
    journal.snapshot(home=home, root=root, date="2026-06-03", agent_id="rocky", run_hook=False)
    journal.snapshot(home=home, root=root, date="2026-06-04", agent_id="rocky", run_hook=False)

    listed = journal.list_journals(root)
    assert [j["date"] for j in listed] == ["2026-06-03", "2026-06-04"]
    assert all(j["files"] >= 5 for j in listed)


def test_restore_reverts_state_and_keeps_safety_backup(tmp_path):
    home = tmp_path / ".hermes"
    root = tmp_path / ".mac" / "journal"
    _make_agent_home(home)
    journal.snapshot(home=home, root=root, date="2026-06-04", agent_id="rocky", run_hook=False)

    # the agent's soul gets wiped to a bland default (the regression we're guarding against)
    (home / "SOUL.md").write_text("# SOUL\ngeneric default\n", encoding="utf-8")

    # dry-run changes nothing
    dry = journal.restore("2026-06-04", home=home, root=root, dry_run=True)
    assert dry["dry_run"] and "SOUL.md" in dry["would_restore"]
    assert "generic default" in (home / "SOUL.md").read_text()

    res = journal.restore("2026-06-04", home=home, root=root)
    assert "I'm Rocky." in (home / "SOUL.md").read_text()  # restored
    assert res["safety_backup"].startswith("pre-restore-")  # current state saved first
    assert (
        (root / res["safety_backup"] / "SOUL.md").read_text().startswith("# SOUL\ngeneric default")
    )


def test_snapshot_prefers_openclaw_workspace_when_root_has_no_soul(tmp_path):
    home = tmp_path / "openclaw"
    workspace = home / "workspace"
    _make_agent_home(workspace)
    root = tmp_path / "journal"

    m = journal.snapshot(home=home, root=root, date="2026-08-26", agent_id="natasha", run_hook=False)

    dest = root / "2026-08-26"
    assert (dest / "SOUL.md").read_text().startswith("# SOUL")
    assert "SOUL.md" in m["captured"]
    assert m["hermes_home"] == str(workspace)
