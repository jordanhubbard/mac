from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess


ROOT = Path(__file__).resolve().parents[1]
MIGRATOR = ROOT / "deploy" / "openclaw" / "migrate-hermes-continuity.py"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(home: Path, *, proposal: Path | None = None, check: bool = True):
    command = [
        str(MIGRATOR),
        "--hermes-home",
        str(home / ".hermes"),
        "--workspace",
        str(home / ".mac/openclaw/workspace"),
        "--state-dir",
        str(home / ".mac/openclaw/state"),
        "--migration-dir",
        str(home / ".mac/openclaw/migration"),
        "--agent-id",
        "agent_test",
        "--public-identity",
        "Testy",
    ]
    if proposal:
        command += ["--identity-proposal", str(proposal)]
    return subprocess.run(command, text=True, capture_output=True, check=check, timeout=20)


def test_imports_identity_history_skills_and_cron_without_mutating_hermes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    hermes = home / ".hermes"
    memories = hermes / "memories"
    skills = hermes / "skills" / "fleet-lore"
    cron = hermes / "cron"
    curiosity = hermes / "curiosity" / "quarantine"
    memories.mkdir(parents=True)
    skills.mkdir(parents=True)
    cron.mkdir(parents=True)
    curiosity.mkdir(parents=True)
    (hermes / "SOUL.md").write_text("# Testy\n\nDry humor and careful judgment.\n", encoding="utf-8")
    (hermes / "USER.md").write_text("legacy user variant\n", encoding="utf-8")
    (memories / "USER.md").write_text("# User\n\nCall the operator J.\n", encoding="utf-8")
    (memories / "MEMORY.md").write_text("# Memory\n\nThe fleet values evidence.\n", encoding="utf-8")
    (skills / "SKILL.md").write_text(
        "# Fleet lore\n\nAPI_TOKEN=actual-secret-like-value-that-must-not-migrate\n",
        encoding="utf-8",
    )
    jobs = {
        "jobs": [
            {
                "id": "dream-1",
                "name": "dream-cycle",
                "enabled": True,
                "prompt": "Reflect on durable learning.",
                "schedule": {"kind": "cron", "expr": "0 * * * *"},
                "deliver": "local",
            },
            {
                "id": "old-1",
                "name": "old-disabled",
                "enabled": False,
                "prompt": "Preserve but do not run.",
                "schedule": {"kind": "cron", "expr": "0 9 * * *"},
            },
        ]
    }
    (cron / "jobs.json").write_text(json.dumps(jobs), encoding="utf-8")
    (curiosity / "candidate.json").write_text(
        '{"hypothesis":"preserve me","token":"github_pat_abcdefghijklmnopqrstuvwxyz123456"}\n',
        encoding="utf-8",
    )
    database = hermes / "state.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE messages (id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp TEXT)"
        )
        connection.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?)",
            (
                "m1",
                "session-1",
                "user",
                "Remember this safely: github_pat_abcdefghijklmnopqrstuvwxyz123456",
                "2026-07-01T12:00:00Z",
            ),
        )
    before = {path.relative_to(hermes): _digest(path) for path in hermes.rglob("*") if path.is_file()}

    result = _run(home)

    report = json.loads(result.stdout)
    workspace = home / ".mac/openclaw/workspace"
    assert report["status"] == "completed"
    assert report["mode"] == "hermes_import"
    assert report["source_preserved"] is True
    assert report["counts"]["history_messages"] == 1
    assert report["counts"]["cron_jobs"] == 2
    assert report["counts"]["cron_jobs_enabled"] == 1
    assert report["counts"]["skill_files"] == 1
    assert report["counts"]["curiosity_files_preserved"] == 1
    assert "Dry humor" in (workspace / "SOUL.md").read_text(encoding="utf-8")
    assert "Call the operator J" in (workspace / "USER.md").read_text(encoding="utf-8")
    assert "legacy user variant" in (
        workspace / "memory/hermes-legacy/.hermes-USER.md"
    ).read_text(encoding="utf-8")
    assert "actual-secret-like-value-that-must-not-migrate" not in (
        workspace / "skills/fleet-lore/SKILL.md"
    ).read_text(encoding="utf-8")
    history = (workspace / "memory/hermes-history/2026-07-01.md").read_text(encoding="utf-8")
    assert "github_pat_" not in history
    assert "[REDACTED_SECRET]" in history
    preserved_curiosity = (
        home
        / ".mac/openclaw/state/mac-curiosity/hermes-import/curiosity/quarantine/candidate.json"
    ).read_text(encoding="utf-8")
    assert "preserve me" in preserved_curiosity
    assert "github_pat_" not in preserved_curiosity
    cron_plan = json.loads((home / ".mac/openclaw/migration/cron-plan.json").read_text())
    assert [(job["name"], job["enabled"]) for job in cron_plan["jobs"]] == [
        ("dream-cycle", True),
        ("old-disabled", False),
    ]
    after = {path.relative_to(hermes): _digest(path) for path in hermes.rglob("*") if path.is_file()}
    assert after == before


def test_rerun_preserves_openclaw_edits_and_records_candidate(tmp_path: Path) -> None:
    home = tmp_path / "home"
    hermes = home / ".hermes"
    hermes.mkdir(parents=True)
    soul = hermes / "SOUL.md"
    soul.write_text("original soul\n", encoding="utf-8")
    _run(home)
    workspace_soul = home / ".mac/openclaw/workspace/SOUL.md"
    workspace_soul.write_text("manual OpenClaw edit\n", encoding="utf-8")
    soul.write_text("new Hermes source\n", encoding="utf-8")

    report = json.loads(_run(home).stdout)

    assert workspace_soul.read_text(encoding="utf-8") == "manual OpenClaw edit\n"
    conflict = next(item for item in report["conflicts"] if item["path"] == "SOUL.md")
    assert Path(conflict["candidate"]).read_text(encoding="utf-8") == "new Hermes source\n"


def test_blank_agent_requires_and_applies_valid_mentor_proposal(tmp_path: Path) -> None:
    home = tmp_path / "home"
    failed = _run(home, check=False)
    assert failed.returncode == 4
    assert "no valid mentor personality proposal" in failed.stdout

    proposal = home / "proposal.json"
    proposal.parent.mkdir(parents=True, exist_ok=True)
    proposal.write_text(
        json.dumps(
            {
                "name": "Quill",
                "role": "Evidence cartographer",
                "vibe": "Precise, curious, quietly funny",
                "emoji": "🪶",
                "soul": "# Quill\n\nMap claims to evidence and learn from corrections.",
                "user": "# User\n\nPreferences are unknown; learn them rather than inventing them.",
                "memory": "# Memory\n\nCreated to complement the fleet through evidence mapping.",
                "mentor_agent_id": "agent_rocky",
            }
        ),
        encoding="utf-8",
    )
    report = json.loads(_run(home, proposal=proposal).stdout)
    workspace = home / ".mac/openclaw/workspace"
    assert report["mode"] == "mentor_bootstrap"
    assert "Quill" in (workspace / "IDENTITY.md").read_text(encoding="utf-8")
    provenance = json.loads((home / ".mac/openclaw/migration/personality-provenance.json").read_text())
    assert provenance["mentor_agent_id"] == "agent_rocky"
