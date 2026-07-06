from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mac.repository_recovery import (
    RepositoryRecoveryError,
    inspect_finalizer_recovery,
    recover_finalizer_worktree,
)


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def _recovery_fixture(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], check=True, capture_output=True)
    _git(seed, "config", "user.email", "tests@example.invalid")
    _git(seed, "config", "user.name", "tests")
    (seed / "README.md").write_text("base\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "base")
    _git(seed, "branch", "-M", "main")
    _git(seed, "push", "origin", "main")
    base = _git(seed, "rev-parse", "HEAD")

    worktree = tmp_path / "worktree"
    subprocess.run(["git", "clone", str(remote), str(worktree)], check=True, capture_output=True)
    _git(worktree, "checkout", "main")
    _git(worktree, "config", "user.email", "tests@example.invalid")
    _git(worktree, "config", "user.name", "tests")
    (worktree / "README.md").write_text("recovered\n", encoding="utf-8")
    (worktree / "new_module.py").write_text("VALUE = 1\n", encoding="utf-8")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_id = "task_1234567890abcdef1234567890abcdef"
    task = {
        "id": task_id,
        "title": "Preserved task",
        "metadata": {
            "execution_contract": {
                "repository_contract": {
                    "canonical_remote_url": remote.as_uri(),
                    "default_branch": "main",
                    "test": {"command": "true"},
                }
            }
        },
    }
    (workspace / "task.json").write_text(json.dumps({"task": task}), encoding="utf-8")
    (workspace / "repository-worktree.json").write_text(
        json.dumps(
            {
                "repository_worktree": str(worktree),
                "repository_branch": "mac/recovery-test",
                "repository_base_sha": base,
                "repository_canonical_branch": "main",
                "repository_canonical_remote_url": remote.as_uri(),
            }
        ),
        encoding="utf-8",
    )
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "problems": [
            "untracked files present at finalize time — agent must commit ALL new files before declaring done: new_module.py",
            "repository finalizer had local errors; refusing to push",
        ],
        "repo": {
            "head_sha": base,
            "dirty": True,
            "pushed": False,
            "remote_ref": "refs/heads/mac/recovery-test",
            "files_changed": ["README.md"],
        },
        "tests": [{"command": "true", "returncode": 0, "status": "pass"}],
        "codegraph": {
            "schema": "mac.codegraph_audit.v1",
            "status": "pass",
            "reason": "affected_computed",
            "relevant_files": ["README.md"],
            "commands": [],
        },
    }
    (workspace / "mac-evidence.json").write_text(json.dumps(manifest), encoding="utf-8")
    (workspace / "worker-result.json").write_text(
        json.dumps({"returncode": 0, "summary": "done"}), encoding="utf-8"
    )
    return workspace, worktree, remote, base


def _passing_codegraph(_worktree: Path, files: list[str]):
    return {
        "schema": "mac.codegraph_audit.v1",
        "status": "pass",
        "reason": "affected_computed",
        "relevant_files": files,
        "commands": [
            {"argv": ["codegraph", "sync"], "returncode": 0},
            {"argv": ["codegraph", "affected"], "returncode": 0},
        ],
    }


def test_inspect_finalizer_recovery_is_read_only_and_lists_exact_new_files(tmp_path: Path):
    workspace, worktree, _remote, base = _recovery_fixture(tmp_path)

    plan = inspect_finalizer_recovery(workspace)

    assert plan["eligible"] is True
    assert plan["head_sha"] == base
    assert plan["new_files"] == ["new_module.py"]
    assert plan["changed_files"] == ["README.md", "new_module.py"]
    assert _git(worktree, "rev-parse", "HEAD") == base
    assert "?? new_module.py" in _git(worktree, "status", "--short")


def test_recovery_requires_exact_new_file_approval(tmp_path: Path):
    workspace, _worktree, _remote, _base = _recovery_fixture(tmp_path)

    with pytest.raises(RepositoryRecoveryError, match="must exactly match"):
        inspect_finalizer_recovery(
            workspace,
            approved_new_files=["different.py"],
        )


def test_recovery_rejects_stale_head_and_semantic_failure(tmp_path: Path):
    workspace, worktree, _remote, _base = _recovery_fixture(tmp_path)
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-m", "unexpected head")

    with pytest.raises(RepositoryRecoveryError, match="HEAD no longer matches"):
        inspect_finalizer_recovery(workspace)

    # Restore the original shape and prove non-protocol failures are rejected.
    workspace, _worktree, _remote, _base = _recovery_fixture(tmp_path / "second")
    manifest_path = workspace / "mac-evidence.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["problems"].append("repository contract test failed")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RepositoryRecoveryError, match="non-recoverable"):
        inspect_finalizer_recovery(workspace)


def test_execute_revalidates_commits_with_provenance_and_pushes(tmp_path: Path):
    workspace, worktree, remote, base = _recovery_fixture(tmp_path)
    calls = []

    def passing_test(command: str, cwd: Path):
        calls.append((command, cwd))
        return subprocess.CompletedProcess(["bash", "-lc", command], 0, "pass\n", "")

    result = recover_finalizer_worktree(
        workspace,
        approved_new_files=["new_module.py"],
        original_evidence_id="ev_original",
        execute=True,
        test_runner=passing_test,
        codegraph_runner=_passing_codegraph,
    )

    assert result["status"] == "complete"
    assert result["remote_verified"] is True
    assert result["original_evidence_id"] == "ev_original"
    assert calls == [("true", worktree)]
    assert _git(worktree, "status", "--porcelain") == ""
    message = _git(worktree, "show", "-s", "--format=%B", "HEAD")
    assert "MAC-Original-Evidence: ev_original" in message
    assert "MAC-Recovery-Reason: new-file-finalizer-refusal" in message
    remote_line = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/mac/recovery-test"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert remote_line == result["recovery_head_sha"]
    assert base != result["recovery_head_sha"]
    assert (workspace / "recovery-evidence.json").exists()

