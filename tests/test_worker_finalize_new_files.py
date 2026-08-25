"""Regression: the success-path dirty gate must stage/commit untracked new
files BEFORE checking cleanliness, so a task that leaves intended new source or
test files does not waste an attempt on a "repository worktree has uncommitted
changes" refusal (task_1965d289821d45dd86af10b52123e298)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict

from mac import worker
from mac.worker import MacWorker


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _init_worktree(root: Path) -> Path:
    repo = root / "worktree"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "existing.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _write_context(task_dir: Path, worktree: Path) -> None:
    (task_dir / "repository-worktree.json").write_text(
        json.dumps(
            {
                "repository_worktree": str(worktree),
                "repository_branch": "master",
            }
        ),
        encoding="utf-8",
    )


def _write_task(task_dir: Path, task_id: str) -> None:
    (task_dir / "task.json").write_text(
        json.dumps({"task": {"id": task_id, "title": "add a new module"}}),
        encoding="utf-8",
    )


def _manifest(head_sha: str) -> Dict[str, Any]:
    return {
        "schema": worker.VERIFICATION_SCHEMA,
        "status": "complete",
        "evidence_type": "repo_change",
        "signed_by": "agent-test",
        "signature": "sig",
        "repo": {"head_sha": head_sha, "dirty": False},
        "verification": {
            "schema": worker.VERIFICATION_SCHEMA,
            "status": "complete",
            "evidence_type": "repo_change",
            "signed_by": "agent-test",
            "signature": "sig",
            "repo": {"head_sha": head_sha, "dirty": False},
        },
    }


def _make_worker(tmp_path: Path) -> MacWorker:
    return MacWorker.__new__(MacWorker)  # type: ignore[call-arg]


def test_untracked_new_files_are_committed_before_dirty_gate(tmp_path: Path):
    worktree = _init_worktree(tmp_path)
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    task_id = "task_newfile"
    _write_task(task_dir, task_id)
    _write_context(task_dir, worktree)

    base_head = _git(worktree, "rev-parse", "HEAD")
    # The agent left an intended NEW file untracked in the worktree.
    (worktree / "new_module.py").write_text("VALUE = 1\n", encoding="utf-8")

    manifest = _manifest(base_head)
    (task_dir / "mac-evidence.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )

    w = _make_worker(tmp_path)
    evidence = {"metadata": {"verification": manifest["verification"]}}
    problems = w._execution_submission_problems(task_dir, evidence)

    assert "repository worktree has uncommitted changes" not in problems
    assert not any("head_sha does not match" in p for p in problems)
    # The untracked file was committed and the worktree is now clean.
    assert _git(worktree, "status", "--porcelain") == ""
    new_head = _git(worktree, "rev-parse", "HEAD")
    assert new_head != base_head
    tracked = _git(worktree, "ls-files")
    assert "new_module.py" in tracked
    # The evidence manifest on disk was reconciled to the freshly committed HEAD.
    on_disk = json.loads((task_dir / "mac-evidence.json").read_text(encoding="utf-8"))
    assert on_disk["repo"]["head_sha"] == new_head


def test_clean_worktree_is_not_touched(tmp_path: Path):
    worktree = _init_worktree(tmp_path)
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _write_task(task_dir, "task_clean")
    _write_context(task_dir, worktree)

    head = _git(worktree, "rev-parse", "HEAD")
    manifest = _manifest(head)
    (task_dir / "mac-evidence.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )

    w = _make_worker(tmp_path)
    evidence = {"metadata": {"verification": manifest["verification"]}}
    problems = w._execution_submission_problems(task_dir, evidence)

    assert "repository worktree has uncommitted changes" not in problems
    assert _git(worktree, "rev-parse", "HEAD") == head


def test_modified_tracked_files_are_committed_before_dirty_gate(tmp_path: Path):
    """`git add -A` must also stage/commit MODIFIED tracked files, not just new
    untracked ones, so an edit-only task does not trip the dirty gate either."""
    worktree = _init_worktree(tmp_path)
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _write_task(task_dir, "task_modified")
    _write_context(task_dir, worktree)

    base_head = _git(worktree, "rev-parse", "HEAD")
    # The agent modified an existing tracked file but left it uncommitted.
    (worktree / "existing.txt").write_text("edited\n", encoding="utf-8")

    manifest = _manifest(base_head)
    (task_dir / "mac-evidence.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )

    w = _make_worker(tmp_path)
    evidence = {"metadata": {"verification": manifest["verification"]}}
    problems = w._execution_submission_problems(task_dir, evidence)

    assert "repository worktree has uncommitted changes" not in problems
    assert not any("head_sha does not match" in p for p in problems)
    assert _git(worktree, "status", "--porcelain") == ""
    new_head = _git(worktree, "rev-parse", "HEAD")
    assert new_head != base_head
    on_disk = json.loads((task_dir / "mac-evidence.json").read_text(encoding="utf-8"))
    assert on_disk["repo"]["head_sha"] == new_head


def test_commit_dirty_worktree_records_task_local_identity(tmp_path: Path):
    """The finalizer commit must carry a stable task-local commit identity so
    the auto-commit is attributable and does not depend on ambient git config."""
    worktree = _init_worktree(tmp_path)
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _write_task(task_dir, "task_identity")
    _write_context(task_dir, worktree)

    (worktree / "new_module.py").write_text("VALUE = 1\n", encoding="utf-8")

    w = _make_worker(tmp_path)
    problems: list[str] = []
    w._commit_dirty_repository_worktree(
        "task_identity", {"id": "task_identity", "title": "add a new module"}, worktree, problems
    )

    assert problems == []
    author_name = _git(worktree, "log", "-1", "--format=%an")
    author_email = _git(worktree, "log", "-1", "--format=%ae")
    assert author_name == "MAC fleet"
    assert author_email == "mac-fleet@nvidia.com"
    subject = _git(worktree, "log", "-1", "--format=%s")
    assert "task_identity" in subject


def test_commit_dirty_worktree_is_idempotent_noop_when_clean(tmp_path: Path):
    """Calling the committer twice must not create an empty second commit."""
    worktree = _init_worktree(tmp_path)
    (worktree / "new_module.py").write_text("VALUE = 1\n", encoding="utf-8")

    w = _make_worker(tmp_path)
    problems: list[str] = []
    w._commit_dirty_repository_worktree(
        "task_noop", {"id": "task_noop", "title": "x"}, worktree, problems
    )
    head_after_first = _git(worktree, "rev-parse", "HEAD")

    # Second invocation on a now-clean worktree is a no-op.
    w._commit_dirty_repository_worktree(
        "task_noop", {"id": "task_noop", "title": "x"}, worktree, problems
    )
    head_after_second = _git(worktree, "rev-parse", "HEAD")

    assert problems == []
    assert head_after_first == head_after_second


def test_staged_new_files_are_committed_before_dirty_gate(tmp_path: Path):
    """A STAGED-BUT-NOT-COMMITTED new file (added to the index but never
    committed) must also be committed by the finalizer before the dirty gate,
    so it does not trip "repository worktree has uncommitted changes" and waste
    an attempt on an otherwise-successful task."""
    worktree = _init_worktree(tmp_path)
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _write_task(task_dir, "task_staged")
    _write_context(task_dir, worktree)

    base_head = _git(worktree, "rev-parse", "HEAD")
    # The agent created a NEW file and staged it, but never committed it.
    (worktree / "staged_module.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(worktree, "add", "staged_module.py")
    # Sanity: the file is staged-new (index add) and not yet committed.
    porcelain = _git(worktree, "status", "--porcelain")
    assert porcelain.startswith("A  ") and "staged_module.py" in porcelain

    manifest = _manifest(base_head)
    (task_dir / "mac-evidence.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )

    w = _make_worker(tmp_path)
    evidence = {"metadata": {"verification": manifest["verification"]}}
    problems = w._execution_submission_problems(task_dir, evidence)

    assert "repository worktree has uncommitted changes" not in problems
    assert not any("head_sha does not match" in p for p in problems)
    # The staged file was committed and the worktree is now clean.
    assert _git(worktree, "status", "--porcelain") == ""
    new_head = _git(worktree, "rev-parse", "HEAD")
    assert new_head != base_head
    tracked = _git(worktree, "ls-files")
    assert "staged_module.py" in tracked
    on_disk = json.loads((task_dir / "mac-evidence.json").read_text(encoding="utf-8"))
    assert on_disk["repo"]["head_sha"] == new_head


def test_mixed_untracked_staged_and_modified_are_committed_together(tmp_path: Path):
    """A mix of an untracked new file + a staged-new file + a modified tracked
    file must all be committed together in a SINGLE finalizer commit, so no
    class of change is left behind to trip the dirty gate."""
    worktree = _init_worktree(tmp_path)
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _write_task(task_dir, "task_mixed")
    _write_context(task_dir, worktree)

    base_head = _git(worktree, "rev-parse", "HEAD")
    # Untracked new file (never added).
    (worktree / "untracked_module.py").write_text("U = 1\n", encoding="utf-8")
    # Staged-new file (added to the index, not committed).
    (worktree / "staged_module.py").write_text("S = 1\n", encoding="utf-8")
    _git(worktree, "add", "staged_module.py")
    # Modified tracked file (edited, uncommitted).
    (worktree / "existing.txt").write_text("edited\n", encoding="utf-8")

    manifest = _manifest(base_head)
    (task_dir / "mac-evidence.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )

    w = _make_worker(tmp_path)
    evidence = {"metadata": {"verification": manifest["verification"]}}
    problems = w._execution_submission_problems(task_dir, evidence)

    assert "repository worktree has uncommitted changes" not in problems
    assert not any("head_sha does not match" in p for p in problems)
    # Everything committed; worktree clean afterwards.
    assert _git(worktree, "status", "--porcelain") == ""
    new_head = _git(worktree, "rev-parse", "HEAD")
    assert new_head != base_head
    # Exactly ONE new commit on top of base (single synchronized commit).
    count = _git(worktree, "rev-list", "--count", "%s..%s" % (base_head, new_head))
    assert count == "1"
    # All three changes landed in that single commit.
    committed = _git(worktree, "show", "--name-only", "--format=", new_head).split()
    assert "untracked_module.py" in committed
    assert "staged_module.py" in committed
    assert "existing.txt" in committed
    on_disk = json.loads((task_dir / "mac-evidence.json").read_text(encoding="utf-8"))
    assert on_disk["repo"]["head_sha"] == new_head


def test_split_repository_porcelain_status_classifies_lines():
    """`_split_repository_porcelain_status` must classify porcelain lines into
    (tracked, untracked, staged_new) consistently with how the finalizer commits
    each class: untracked ("?? "), staged-new ("A "/"C " without rename), and
    other tracked modifications."""
    status_text = (
        "?? untracked_module.py\n"
        "A  staged_new.py\n"
        "C  copied_new.py\n"
        " M modified_tracked.txt\n"
        "M  staged_modified.txt\n"
        "R  old.txt -> renamed.txt\n"
    )
    tracked_lines, untracked_paths, staged_new_paths = worker._split_repository_porcelain_status(
        status_text
    )

    assert untracked_paths == ["untracked_module.py"]
    # Additions/copies without a rename marker are staged-new.
    assert "staged_new.py" in staged_new_paths
    assert "copied_new.py" in staged_new_paths
    # A rename is a tracked change, not a staged-new addition.
    assert all("renamed.txt" != p and "old.txt -> renamed.txt" != p for p in staged_new_paths)
    # Everything that is not an untracked ("?? ") line is a tracked line.
    assert "A  staged_new.py"[3:] not in untracked_paths
    for expected_tracked in (
        "A  staged_new.py",
        "C  copied_new.py",
        " M modified_tracked.txt",
        "M  staged_modified.txt",
        "R  old.txt -> renamed.txt",
    ):
        assert expected_tracked in tracked_lines
    # The untracked line is NOT counted among tracked lines.
    assert "?? untracked_module.py" not in tracked_lines
