from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
import subprocess
from pathlib import Path

import mac.gitops as gitops
import pytest
from mac.gitops import (
    check_canonical_freshness,
    guarded_push,
    resolve_canonical_publication_target,
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    origin = tmp_path / "origin.git"
    canonical = tmp_path / "canonical.git"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(tmp_path, "init", "--bare", str(canonical))
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "config", "user.email", "test@example.invalid")
    _git(work, "config", "user.name", "test")
    (work / "README.md").write_text("base\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "base")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "origin", "main")
    _git(work, "push", canonical.as_uri(), "main")
    base = _git(work, "rev-parse", "HEAD").stdout.strip()
    _git(work, "checkout", "-b", "task/change")
    (work / "feature.py").write_text("print('feature')\n", encoding="utf-8")
    _git(work, "add", "feature.py")
    _git(work, "commit", "-m", "feature")
    return origin, canonical, work, base


def test_guarded_push_uses_checked_canonical_target_and_cleans_ref(tmp_path: Path) -> None:
    origin, canonical, work, base = _fixture(tmp_path)

    target = resolve_canonical_publication_target(
        worktree=work,
        canonical_remote=canonical.as_uri(),
        canonical_branch="main",
        destination_branch="task/change",
        prepared_base_sha=base,
        isolation_key="task-1-lease-1",
    )
    preflight = check_canonical_freshness(target)
    result = guarded_push(target)

    assert preflight.ok is True
    assert result.ok is True
    assert result.remote_verified is True
    assert result.canonical_tip_sha == base
    assert result.files_changed == ("feature.py",)
    assert target.lock_path.name == "mac_prepare_worktree.lock"
    assert target.task_head_sha == result.head_sha
    assert (
        _git(tmp_path, "ls-remote", str(canonical), "refs/heads/task/change")
        .stdout.strip()
        .startswith(result.head_sha)
    )
    assert _git(tmp_path, "ls-remote", str(origin), "refs/heads/task/change").stdout.strip() == ""
    assert _git(work, "for-each-ref", "refs/mac/publication").stdout.strip() == ""


def test_guarded_push_blocks_head_that_omits_new_canonical_tip(tmp_path: Path) -> None:
    _origin, canonical, work, base = _fixture(tmp_path)
    advance = tmp_path / "advance"
    _git(tmp_path, "clone", str(canonical), str(advance))
    _git(advance, "config", "user.email", "test@example.invalid")
    _git(advance, "config", "user.name", "test")
    _git(advance, "checkout", "main")
    (advance / "canonical.py").write_text("# newer\n", encoding="utf-8")
    _git(advance, "add", "canonical.py")
    _git(advance, "commit", "-m", "advance canonical")
    _git(advance, "push", "origin", "main")

    target = resolve_canonical_publication_target(
        worktree=work,
        canonical_remote=canonical.as_uri(),
        canonical_branch="main",
        destination_branch="task/change",
        prepared_base_sha=base,
        isolation_key="task-stale-lease-1",
    )
    result = guarded_push(target)

    assert result.ok is False
    assert result.remote_verified is False
    assert "not an ancestor" in result.error
    assert (
        _git(tmp_path, "ls-remote", str(canonical), "refs/heads/task/change").stdout.strip() == ""
    )
    assert _git(work, "for-each-ref", "refs/mac/publication").stdout.strip() == ""


def test_freshness_check_requires_prepared_base_context(tmp_path: Path) -> None:
    _origin, canonical, work, _base = _fixture(tmp_path)

    with pytest.raises(ValueError, match="prepared repository base SHA is missing"):
        resolve_canonical_publication_target(
            worktree=work,
            canonical_remote=canonical.as_uri(),
            canonical_branch="main",
            destination_branch="task/change",
            prepared_base_sha="",
            isolation_key="task-missing-context",
        )


def test_temporary_ref_cleanup_failure_blocks_push(tmp_path: Path, monkeypatch) -> None:
    _origin, canonical, work, base = _fixture(tmp_path)
    real_run_git = gitops._run_git

    def fail_cleanup(worktree: Path, args: list[str]):
        if args[:2] == ["update-ref", "-d"]:
            return subprocess.CompletedProcess(["git", *args], 1, "", "simulated cleanup failure")
        return real_run_git(worktree, args)

    monkeypatch.setattr(gitops, "_run_git", fail_cleanup)

    target = resolve_canonical_publication_target(
        worktree=work,
        canonical_remote=canonical.as_uri(),
        canonical_branch="main",
        destination_branch="task/change",
        prepared_base_sha=base,
        isolation_key="task-cleanup-failure",
    )
    result = guarded_push(target)

    assert result.ok is False
    assert "could not clean isolated canonical fetch ref" in result.error
    assert (
        _git(tmp_path, "ls-remote", str(canonical), "refs/heads/task/change").stdout.strip() == ""
    )


def test_publication_lock_failure_blocks_push(tmp_path: Path, monkeypatch) -> None:
    _origin, canonical, work, base = _fixture(tmp_path)

    def fail_lock(_file, operation: int) -> None:
        if operation == gitops.fcntl.LOCK_EX:
            raise OSError("simulated lock failure")

    monkeypatch.setattr(gitops.fcntl, "flock", fail_lock)
    target = resolve_canonical_publication_target(
        worktree=work,
        canonical_remote=canonical.as_uri(),
        canonical_branch="main",
        destination_branch="task/change",
        prepared_base_sha=base,
        isolation_key="task-lock-failure",
    )

    result = guarded_push(target)

    assert result.ok is False
    assert "could not acquire publication lock" in result.error
    assert (
        _git(tmp_path, "ls-remote", str(canonical), "refs/heads/task/change").stdout.strip() == ""
    )


def test_target_is_immutable_unique_and_secret_free(tmp_path: Path, monkeypatch) -> None:
    _origin, _canonical, work, base = _fixture(tmp_path)
    monkeypatch.setenv("GH_TOKEN", "publication-secret")
    first = resolve_canonical_publication_target(
        worktree=work,
        canonical_remote="https://github.com/example/project.git",
        canonical_branch="main",
        destination_branch="task/change",
        prepared_base_sha=base,
        isolation_key="task-1-lease-1",
    )
    second = resolve_canonical_publication_target(
        worktree=work,
        canonical_remote="https://github.com/example/project.git",
        canonical_branch="main",
        destination_branch="task/change",
        prepared_base_sha=base,
        isolation_key="task-1-lease-2",
    )

    assert first.isolated_ref != second.isolated_ref
    assert "publication-secret" not in repr(first)
    evidence = gitops.CanonicalFreshnessResult(True, first).evidence()
    assert "publication-secret" not in json.dumps(evidence)
    assert evidence["remote"] == (
        "https://x-access-token:<redacted>@github.com/example/project.git"
    )
    with pytest.raises(FrozenInstanceError):
        first.canonical_branch = "other"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# sync_worktree_with_canonical: pre-test rebase onto the advanced canonical tip
# --------------------------------------------------------------------------- #


def _advance_canonical(tmp_path: Path, canonical: Path, filename: str, content: str) -> str:
    """Land a new commit on the canonical branch (simulates a peer publishing)."""
    other = tmp_path / ("peer-" + filename.replace("/", "-"))
    # --branch main: the bare canonical's HEAD symref still points at the
    # host git's init.defaultBranch (master on stock git), so a default clone
    # checks out nothing and the peer's commit lands on the wrong branch —
    # failed only on fleet sandboxes, passed on dev machines whose gitconfig
    # sets init.defaultBranch=main.
    _git(tmp_path, "clone", "--branch", "main", canonical.as_uri(), str(other))
    _git(other, "config", "user.email", "peer@example.invalid")
    _git(other, "config", "user.name", "peer")
    (other / filename).write_text(content, encoding="utf-8")
    _git(other, "add", filename)
    _git(other, "commit", "-m", "peer change: %s" % filename)
    _git(other, "push", "origin", "main")
    return _git(other, "rev-parse", "HEAD").stdout.strip()


def test_sync_worktree_rebases_onto_advanced_canonical(tmp_path: Path) -> None:
    # A peer published to canonical while the task worked. Without the sync the
    # freshness gate rejects ("canonical tip is not an ancestor of task HEAD");
    # with it the worktree rebases cleanly and publication proceeds.
    origin, canonical, work, base = _fixture(tmp_path)
    tip = _advance_canonical(tmp_path, canonical, "peer.txt", "peer\n")

    result = gitops.sync_worktree_with_canonical(work, canonical.as_uri(), "main")
    assert result["status"] == "rebased"
    assert result["canonical_tip"] == tip
    # The canonical tip is now an ancestor of HEAD and the task's work survives.
    _git(work, "merge-base", "--is-ancestor", tip, "HEAD")
    assert (work / "feature.py").exists() and (work / "peer.txt").exists()

    target = resolve_canonical_publication_target(
        worktree=work,
        canonical_remote=canonical.as_uri(),
        canonical_branch="main",
        destination_branch="task/change",
        prepared_base_sha=base,
        isolation_key="task-1-lease-1",
    )
    assert check_canonical_freshness(target).ok


def test_sync_worktree_fresh_when_canonical_unmoved(tmp_path: Path) -> None:
    origin, canonical, work, base = _fixture(tmp_path)
    result = gitops.sync_worktree_with_canonical(work, canonical.as_uri(), "main")
    assert result["status"] == "fresh"
    head_before = _git(work, "rev-parse", "HEAD").stdout.strip()
    assert head_before  # HEAD untouched by a fresh sync


def test_sync_worktree_conflict_aborts_and_preserves_work(tmp_path: Path) -> None:
    # The peer landed a CONFLICTING edit. The sync must not leave a rebase in
    # progress or lose the task's commits — abort, report, let the freshness
    # gate fail with its precise error.
    origin, canonical, work, base = _fixture(tmp_path)
    _advance_canonical(tmp_path, canonical, "feature.py", "print('peer conflicting')\n")
    head_before = _git(work, "rev-parse", "HEAD").stdout.strip()

    result = gitops.sync_worktree_with_canonical(work, canonical.as_uri(), "main")
    assert result["status"] == "conflict"
    assert _git(work, "rev-parse", "HEAD").stdout.strip() == head_before
    assert not (work / ".git" / "rebase-merge").exists()
    assert not (work / ".git" / "rebase-apply").exists()
    assert _git(work, "status", "--porcelain").stdout.strip() == ""


def test_sync_worktree_no_remote_is_skipped(tmp_path: Path) -> None:
    origin, canonical, work, base = _fixture(tmp_path)
    assert gitops.sync_worktree_with_canonical(work, "", "main")["status"] == "skipped"


def test_guarded_push_phase_budget_bounds_publication_lock_wait(tmp_path: Path) -> None:
    _origin, canonical, work, base = _fixture(tmp_path)
    target = resolve_canonical_publication_target(
        worktree=work,
        canonical_remote=canonical.as_uri(),
        canonical_branch="main",
        destination_branch="task/lock-timeout",
        prepared_base_sha=base,
        isolation_key="task-lock-timeout",
    )
    with target.lock_path.open("a+", encoding="utf-8") as held_lock:
        gitops.fcntl.flock(held_lock, gitops.fcntl.LOCK_EX)
        result = guarded_push(target, timeout=2)

    assert result.ok is False
    assert "timed out acquiring canonical publication lock" in result.error
