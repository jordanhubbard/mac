"""Regression tests locking in new-file staging behavior for the deterministic
host git finalizer.

These extend (without duplicating) the ``test_git_finalizer_*`` coverage in
``tests/test_task_executor.py`` and the ``_split_porcelain_status`` case in
``tests/test_executor_prompt_finalizer.py``. They pin the guarantee that a
worktree handed off with UNTRACKED and/or STAGED-BUT-UNCOMMITTED new files — on
their own or mixed with modified tracked files — is fully ``git add -A``ed and
committed by ``run_deterministic_git_finalizer`` before the clean/push gate, so
no "untracked files present at finalize time" refusal occurs, while gitignored
generated artifacts stay out of the commit.

Each finalizer scenario uses a real temporary git worktree plus a real local
bare origin. ``test.command`` is ``true`` so the publication path is exercised
without invoking the full suite.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mac import executor_finalizer as finalizer
from mac import task_executor as te


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def _prepare_repository_context(work: Path, monkeypatch) -> None:
    """Model the worker-prepared repository context the finalizer reads from the
    environment: the canonical base SHA recorded before the agent started plus a
    lease id. Without these, the freshness gate rejects the sandbox's inherited
    ``MAC_TASK_REPO_BASE_SHA`` (a SHA from a different repo) and never pushes.
    """
    base = _git(work, "rev-parse", "--verify", "main")
    assert base.returncode == 0 and base.stdout.strip(), base.stderr
    monkeypatch.setenv("MAC_TASK_REPO_BASE_SHA", base.stdout.strip())
    monkeypatch.setenv("MAC_TASK_REPO_DEFAULT_BRANCH", "main")
    monkeypatch.setenv("MAC_TASK_REPO_LEASE_ID", "lease-test")


def _seed_worktree(tmp_path: Path, *, branch: str, gitignore: str | None = None):
    """Create a bare origin + a clone checked out on a fresh feature branch."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    if gitignore is not None:
        (work / ".gitignore").write_text(gitignore, encoding="utf-8")
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "origin", "main")
    _git(work, "checkout", "-b", branch)
    return origin, work


def _finalizer_task(task_id: str, origin: Path) -> dict:
    return {
        "id": task_id,
        "metadata": {
            "publication_target": "git://main",
            "origin": {
                "repository_contract": {
                    "canonical_remote_url": origin.as_uri(),
                    "test": {"command": "true"},
                },
            },
        },
    }


def _run_finalizer(tmp_path, monkeypatch, work: Path, task: dict):
    _prepare_repository_context(work, monkeypatch)
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(work))
    ws = tmp_path / "ws"
    ws.mkdir()
    te.run_deterministic_git_finalizer(ws, task)
    return json.loads((ws / "mac-evidence.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. Untracked new SOURCE *and* new TEST files are both committed.
# ---------------------------------------------------------------------------
def test_finalizer_commits_untracked_new_source_and_test_files(tmp_path, monkeypatch):
    origin, work = _seed_worktree(tmp_path, branch="task/untracked-pair")
    # Two genuinely new, never-tracked files handed off untracked: one source,
    # one test. Both must land in the finalizer commit — no untracked refusal.
    (work / "new_feature.py").write_text("def feature():\n    return 1\n", encoding="utf-8")
    (work / "tests").mkdir()
    (work / "tests" / "test_new_feature.py").write_text(
        "from new_feature import feature\n\n\ndef test_feature():\n    assert feature() == 1\n",
        encoding="utf-8",
    )

    manifest = _run_finalizer(
        tmp_path, monkeypatch, work, _finalizer_task("t-untracked-pair", origin)
    )

    assert manifest["status"] == "complete"
    assert manifest["repo"]["pushed"] is True
    assert manifest["repo"]["dirty"] is False
    files_changed = manifest["repo"]["files_changed"]
    assert "new_feature.py" in files_changed
    assert "tests/test_new_feature.py" in files_changed
    # No untracked refusal: the worktree is clean and both files are committed.
    assert _git(work, "status", "--porcelain").stdout == ""
    assert _git(work, "show", "HEAD:new_feature.py").stdout == "def feature():\n    return 1\n"
    assert _git(work, "cat-file", "-e", "HEAD:tests/test_new_feature.py").returncode == 0


# ---------------------------------------------------------------------------
# 2. Staged-but-not-committed new files are committed, not refused.
# ---------------------------------------------------------------------------
def test_finalizer_commits_staged_but_uncommitted_new_files(tmp_path, monkeypatch):
    origin, work = _seed_worktree(tmp_path, branch="task/staged-only")
    (work / "staged_source.py").write_text("VALUE = 42\n", encoding="utf-8")
    (work / "staged_test.py").write_text("def test_value():\n    assert True\n", encoding="utf-8")
    # Add to the index but never commit — the sandbox handoff state this pins.
    _git(work, "add", "staged_source.py", "staged_test.py")
    porcelain = _git(work, "status", "--porcelain").stdout
    assert "A  staged_source.py" in porcelain  # precondition: staged-new, uncommitted

    manifest = _run_finalizer(tmp_path, monkeypatch, work, _finalizer_task("t-staged-only", origin))

    assert manifest["status"] == "complete"
    assert manifest["repo"]["pushed"] is True
    assert manifest["repo"]["dirty"] is False
    files_changed = manifest["repo"]["files_changed"]
    assert "staged_source.py" in files_changed
    assert "staged_test.py" in files_changed
    assert _git(work, "status", "--porcelain").stdout == ""
    assert _git(work, "show", "HEAD:staged_source.py").stdout == "VALUE = 42\n"


# ---------------------------------------------------------------------------
# 3. A mix of untracked-new, staged-new, and modified-tracked files lands in a
#    single finalizer commit.
# ---------------------------------------------------------------------------
def test_finalizer_commits_mixed_untracked_staged_and_modified_together(tmp_path, monkeypatch):
    origin, work = _seed_worktree(tmp_path, branch="task/mixed")
    # Seed a tracked file to later modify.
    (work / "tracked.py").write_text("ORIGINAL = 1\n", encoding="utf-8")
    _git(work, "add", "tracked.py")
    _git(work, "commit", "-m", "add tracked")

    # Untracked new file.
    (work / "brand_new.py").write_text("print('new')\n", encoding="utf-8")
    # Staged-new file.
    (work / "staged_new.py").write_text("print('staged')\n", encoding="utf-8")
    _git(work, "add", "staged_new.py")
    # Modified tracked file (working-tree modification, not staged).
    (work / "tracked.py").write_text("ORIGINAL = 2\n", encoding="utf-8")

    head_before = _git(work, "rev-parse", "HEAD").stdout.strip()

    manifest = _run_finalizer(tmp_path, monkeypatch, work, _finalizer_task("t-mixed", origin))

    assert manifest["status"] == "complete"
    assert manifest["repo"]["pushed"] is True
    assert manifest["repo"]["dirty"] is False
    files_changed = manifest["repo"]["files_changed"]
    assert "brand_new.py" in files_changed
    assert "staged_new.py" in files_changed
    assert "tracked.py" in files_changed
    # All three arrived in ONE new commit created by the finalizer.
    head_after = _git(work, "rev-parse", "HEAD").stdout.strip()
    assert head_after != head_before
    committed = _git(
        work, "diff-tree", "--no-commit-id", "--name-only", "-r", head_after
    ).stdout.split()
    assert set(committed) == {"brand_new.py", "staged_new.py", "tracked.py"}
    assert _git(work, "status", "--porcelain").stdout == ""
    assert _git(work, "show", "HEAD:tracked.py").stdout == "ORIGINAL = 2\n"


# ---------------------------------------------------------------------------
# 4. _split_porcelain_status classification, extending the existing minimal case
#    with renames/copies/deletes and blank-line handling.
# ---------------------------------------------------------------------------
def test_split_porcelain_status_classifies_new_staged_and_tracked_entries():
    status = (
        "?? untracked_a.py\n"
        "?? dir/untracked_b.py\n"
        "A  staged_new.py\n"
        "AM staged_then_modified.py\n"
        " M modified_tracked.py\n"
        "M  staged_modified_tracked.py\n"
        "D  deleted_tracked.py\n"
        "\n"  # blank lines are ignored
    )
    tracked, untracked, staged_new = finalizer._split_porcelain_status(status)

    assert untracked == ["untracked_a.py", "dir/untracked_b.py"]
    # Every non-'??' line is tracked; blank lines dropped.
    assert tracked == [
        "A  staged_new.py",
        "AM staged_then_modified.py",
        " M modified_tracked.py",
        "M  staged_modified_tracked.py",
        "D  deleted_tracked.py",
    ]
    # Only index-added ('A' in XY, no rename) entries are staged-new.
    assert staged_new == ["staged_new.py", "staged_then_modified.py"]


def test_split_porcelain_status_treats_renames_as_tracked_not_new():
    # A rename ('R') carries an 'A'-like new path but must NOT be flagged as a
    # brand-new staged file; the guard excludes any XY containing 'R'.
    status = "R  old_name.py -> new_name.py\nC  base.py -> copy.py\n"
    tracked, untracked, staged_new = finalizer._split_porcelain_status(status)

    assert untracked == []
    assert tracked == ["R  old_name.py -> new_name.py", "C  base.py -> copy.py"]
    # Rename is excluded; copy ('C', no 'R') is treated as staged-new.
    assert staged_new == ["base.py -> copy.py"]


def test_split_porcelain_status_empty_input_is_all_empty():
    assert finalizer._split_porcelain_status("") == ([], [], [])
    assert finalizer._split_porcelain_status(None) == ([], [], [])


# ---------------------------------------------------------------------------
# 5. Gitignored generated artifacts stay uncommitted while intended new source
#    is still staged and committed (guard against over-staging).
# ---------------------------------------------------------------------------
def test_finalizer_stages_new_source_but_never_commits_gitignored_artifacts(tmp_path, monkeypatch):
    origin, work = _seed_worktree(
        tmp_path, branch="task/ignore-guard", gitignore="generated/\n*.log\n"
    )
    # Intended new source (untracked) MUST be committed.
    (work / "intended_source.py").write_text("KEEP = True\n", encoding="utf-8")
    # Gitignored directory artifact MUST NOT be committed.
    (work / "generated").mkdir()
    (work / "generated" / "artifact.bin").write_text("noise\n", encoding="utf-8")
    # Gitignored file-pattern artifact MUST NOT be committed.
    (work / "debug.log").write_text("log noise\n", encoding="utf-8")

    manifest = _run_finalizer(
        tmp_path, monkeypatch, work, _finalizer_task("t-ignore-guard", origin)
    )

    assert manifest["status"] == "complete"
    assert manifest["repo"]["pushed"] is True
    files_changed = manifest["repo"]["files_changed"]
    assert "intended_source.py" in files_changed
    # Over-staging guard: gitignored artifacts never entered the commit.
    assert "generated/artifact.bin" not in files_changed
    assert "debug.log" not in files_changed
    assert _git(work, "cat-file", "-e", "HEAD:intended_source.py").returncode == 0
    assert _git(work, "cat-file", "-e", "HEAD:generated/artifact.bin").returncode != 0
    assert _git(work, "cat-file", "-e", "HEAD:debug.log").returncode != 0
    # The ignored files still exist untracked-and-ignored; git status stays clean
    # because ``git add -A`` respects .gitignore.
    assert _git(work, "status", "--porcelain").stdout == ""
