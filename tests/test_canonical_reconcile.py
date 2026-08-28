"""Canonical HEAD reconcile is a recorded look, not an auto-kill."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from mac import executor_finalizer as finalizer
from mac.canonical_reconcile import (
    build_reconcile_snapshot,
    expected_head_sha_from_task,
    head_sha_matches,
    implicated_paths,
    reconcile_evidence_problems,
    render_reconcile_section,
    sibling_landings_from_bus,
)
from mac.executor_prompt import build_task_prompt


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def test_implicated_paths_take_backticks_and_slash_tokens_not_urls():
    paths = implicated_paths(
        "stop `scripts/release.sh` from pushing main",
        "See https://github.com/acme/widgets/blob/main/scripts/release.sh and src/mac/cli.py.",
    )
    assert "scripts/release.sh" in paths
    assert "src/mac/cli.py" in paths
    assert not any(item.startswith("https://") for item in paths)


def test_implicated_paths_ignore_version_tokens_without_slash():
    assert implicated_paths("Release v3.4.11", "bump the version") == []


def test_sibling_landings_exclude_this_task_and_are_not_already_published():
    context = {
        "events": [
            {
                "event_type": "git.merged",
                "task_id": "task_mine",
                "payload": {"sha": "aaa", "pr_number": 1},
            },
            {
                "event_type": "git.merged",
                "task_id": "task_theirs",
                "payload": {"sha": "bbb", "pr_number": 107},
            },
        ]
    }
    landings = sibling_landings_from_bus(context, task_id="task_mine")
    assert len(landings) == 1
    assert landings[0]["task_id"] == "task_theirs"
    assert landings[0]["pr_number"] == 107


def test_reconcile_evidence_fail_open_without_prepared_head():
    assert (
        reconcile_evidence_problems(
            {"evidence_type": "repo_change"},
            "repo_change",
            "",
        )
        == []
    )


def test_repo_change_without_decision_fails_when_head_is_expected():
    problems = reconcile_evidence_problems(
        {"evidence_type": "repo_change"},
        "repo_change",
        "abcdef1234567890",
    )
    assert any("canonical_reconcile" in item for item in problems)


def test_still_valid_allows_repo_change_against_prepared_head():
    manifest = {
        "evidence_type": "repo_change",
        "canonical_reconcile": {
            "decision": "still_valid",
            "head_sha": "abcdef1234567890",
            "reason": "scripts/release.sh still pushes origin main",
        },
    }
    assert reconcile_evidence_problems(manifest, "repo_change", "abcdef1234567890") == []
    assert head_sha_matches("abcdef1", "abcdef1234567890")


def test_already_satisfied_allows_no_change_and_rejects_repo_change():
    manifest = {
        "evidence_type": "no_change",
        "canonical_reconcile": {
            "decision": "already_satisfied",
            "head_sha": "abcdef1234567890",
            "reason": "the requested push path is already a PR flow",
        },
    }
    assert reconcile_evidence_problems(manifest, "no_change", "abcdef1234567890") == []
    assert reconcile_evidence_problems(manifest, "repo_change", "abcdef1234567890")


def test_needs_restatement_is_a_no_change_decision():
    manifest = {
        "evidence_type": "no_change",
        "canonical_reconcile": {
            "decision": "needs_restatement",
            "head_sha": "abcdef1234567890",
            "reason": "the statement names a file that no longer exists",
        },
    }
    assert reconcile_evidence_problems(manifest, "no_change", "abcdef1234567890") == []


def test_render_reconcile_section_empty_without_snapshot():
    assert render_reconcile_section({"id": "t1", "title": "x"}) == ""


def test_render_and_prompt_include_reconcile_facts_when_snapshot_attached(tmp_path):
    task = {
        "id": "task_a0d06a48",
        "title": "stop `scripts/release.sh` from pushing main",
        "description": "git push origin main is still there",
        "metadata": {
            "execution_contract": {"type": "repository"},
            "runtime": {
                "canonical_reconcile": {
                    "schema": "mac.canonical_reconcile.v1",
                    "head_sha": "abc1234567890def",
                    "canonical_branch": "main",
                    "implicated_paths": ["scripts/release.sh"],
                    "recent_commits": [
                        {
                            "sha": "def4567890ab",
                            "subject": "Release v3.4.11",
                            "paths": ["scripts/release.sh"],
                        }
                    ],
                    "sibling_landings": [
                        {
                            "event_type": "git.merged",
                            "task_id": "task_other",
                            "pr_number": 107,
                            "sha": "eee111222333",
                        }
                    ],
                }
            },
        },
    }
    section = render_reconcile_section(task)
    assert "Canonical HEAD reconcile" in section
    assert "still_valid" in section
    assert "already_satisfied" in section
    assert "needs_restatement" in section
    assert "scripts/release.sh" in section
    assert "Do not treat these as already_published" in section
    prompt = build_task_prompt(task, tmp_path / "task.json")
    assert "Canonical HEAD reconcile" in prompt
    assert "repository tasks use evidence_type=repo_change" in prompt


def test_non_repo_prompt_omits_reconcile_when_no_snapshot(tmp_path):
    prompt = build_task_prompt(
        {"id": "t1", "title": "answer a question", "metadata": {}},
        tmp_path / "task.json",
    )
    assert "Canonical HEAD reconcile" not in prompt


def test_host_still_valid_fills_missing_decision_from_snapshot():
    from mac.canonical_reconcile import host_still_valid_reconcile

    task = {"metadata": {"runtime": {"canonical_reconcile": {"head_sha": "abcdef1234567890"}}}}
    stamped = host_still_valid_reconcile(task, {})
    assert stamped["decision"] == "still_valid"
    assert stamped["head_sha"] == "abcdef1234567890"


def test_host_still_valid_does_not_override_already_satisfied():
    from mac.canonical_reconcile import host_still_valid_reconcile

    existing = {
        "decision": "already_satisfied",
        "head_sha": "abcdef1234567890",
        "reason": "already a PR flow",
    }
    assert host_still_valid_reconcile({"metadata": {}}, existing)["decision"] == "already_satisfied"
    task = {"metadata": {"runtime": {"canonical_reconcile": {"head_sha": "deadbeefcafebabe"}}}}
    assert expected_head_sha_from_task(task) == "deadbeefcafebabe"


def test_snapshot_lists_recent_commits_on_implicated_paths(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    scripts = repo / "scripts"
    scripts.mkdir()
    (scripts / "release.sh").write_text("git push origin main\n", encoding="utf-8")
    _git(repo, "add", "scripts/release.sh")
    _git(repo, "commit", "-m", "add release.sh")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    snapshot = build_reconcile_snapshot(
        repo,
        {
            "id": "task_mine",
            "title": "fix `scripts/release.sh`",
            "description": "",
        },
        head_sha=head,
        canonical_branch="main",
        bus_context={"events": []},
    )
    assert snapshot["schema"] == "mac.canonical_reconcile.v1"
    assert snapshot["implicated_paths"] == ["scripts/release.sh"]
    assert snapshot["recent_commits"]
    assert snapshot["recent_commits"][0]["sha"] == head


def _seed_worktree(tmp_path: Path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "origin", "main")
    return origin, work


def test_finalizer_no_change_already_satisfied_does_not_open_a_pr(tmp_path, monkeypatch):
    origin, work = _seed_worktree(tmp_path)
    head = _git(work, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(work))
    monkeypatch.setenv("MAC_TASK_REPO_BASE_SHA", head)
    monkeypatch.setenv("MAC_TASK_REPO_LEASE_ID", "lease-test")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "mac-evidence.json").write_text(
        json.dumps(
            {
                "schema": "mac.worker_evidence.v1",
                "status": "complete",
                "evidence_type": "no_change",
                "reason": "HEAD already uses gh pr create",
                "canonical_reconcile": {
                    "decision": "already_satisfied",
                    "head_sha": head,
                    "reason": "HEAD already uses gh pr create",
                },
            }
        ),
        encoding="utf-8",
    )
    opened = {"called": False}

    def _should_not_open(*_args, **_kwargs):
        opened["called"] = True
        return {"opened": True, "number": 99}

    monkeypatch.setattr(finalizer, "open_task_pull_request", _should_not_open)
    task = {
        "id": "task_satisfied",
        "metadata": {
            "publication_target": "git://main",
            "origin": {
                "repository_contract": {
                    "canonical_remote_url": origin.as_uri(),
                    "test": {"command": "true"},
                }
            },
        },
    }
    finalizer.run_deterministic_git_finalizer(ws, task)
    manifest = json.loads((ws / "mac-evidence.json").read_text(encoding="utf-8"))
    assert manifest["evidence_type"] == "no_change"
    assert manifest["status"] == "complete"
    assert manifest["repo"]["pushed"] is False
    assert manifest["push"]["status"] == "skipped"
    assert not opened["called"]
    assert manifest["canonical_reconcile"]["decision"] == "already_satisfied"
    names = {item["name"]: item["status"] for item in manifest["checks"]}
    assert names["canonical_head_matches_prepared_base"] == "pass"


def test_finalizer_rejects_no_change_when_the_tree_is_dirty(tmp_path, monkeypatch):
    origin, work = _seed_worktree(tmp_path)
    head = _git(work, "rev-parse", "HEAD").stdout.strip()
    (work / "README.md").write_text("hello\ndirty\n", encoding="utf-8")
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(work))
    monkeypatch.setenv("MAC_TASK_REPO_BASE_SHA", head)
    monkeypatch.setenv("MAC_TASK_REPO_LEASE_ID", "lease-test")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "mac-evidence.json").write_text(
        json.dumps(
            {
                "schema": "mac.worker_evidence.v1",
                "status": "complete",
                "evidence_type": "no_change",
                "reason": "already done",
                "canonical_reconcile": {
                    "decision": "already_satisfied",
                    "head_sha": head,
                    "reason": "already done",
                },
            }
        ),
        encoding="utf-8",
    )
    task = {
        "id": "task_dirty",
        "metadata": {
            "publication_target": "git://main",
            "origin": {
                "repository_contract": {
                    "canonical_remote_url": origin.as_uri(),
                    "test": {"command": "true"},
                }
            },
        },
    }
    finalizer.run_deterministic_git_finalizer(ws, task)
    manifest = json.loads((ws / "mac-evidence.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "fail"
    assert manifest["repo"]["pushed"] is False
    assert any("clean worktree" in item for item in manifest["problems"])
