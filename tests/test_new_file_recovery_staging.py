"""New-file staging behaviour of the finalizer refusal-recovery service.

task_e2ce62d9 preserves the verified worktree and refusal manifest whenever a
git-finalizer refuses solely for uncommitted new files, and
``recover_from_new_file_refusal`` reconstitutes that work into a publishable
commit.  ``tests/test_new_file_recovery_wiring.py`` proves the *wiring* (finalize
-> recover -> re-finalize) with fully stubbed recovery; this module proves the
*staging mechanics* against a REAL git worktree and the exact preserved-state /
refusal-manifest JSON shapes the code reads:

* every preserved new file (untracked, staged-but-uncommitted, and the sorted
  deduplicated union) is ``git add``-ed, produces a non-empty staged diff, and
  is committed with the ``mac.new_file_recovery.v1`` recovery trailer/kind;
* recovery fails closed (rather than silently succeeding) when the refusal left
  no new files to stage; and
* ``_write_git_finalizer_refusal_manifest`` records the correct
  :class:`FinalizerRefusalKind` and round-trips through
  ``load_preserved_executor_state`` into ``untracked_files`` /
  ``staged_new_files``.

The publish seam (``push_runner``) is stubbed so the tests exercise real staging
and commit without a live remote, while ``sync_worktree_with_canonical`` and
``resolve_canonical_publication_target`` run against a real bare canonical repo.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import mac.executor_finalizer as ef
from mac.executor_finalizer import (
    PRESERVED_EXECUTOR_EVIDENCE_FILENAME,
    PRESERVED_EXECUTOR_WORKTREE_FILENAME,
    NEW_FILE_RECOVERY_SCHEMA,
    load_preserved_executor_state,
    recover_from_new_file_refusal,
)
from mac.repository_recovery import RepositoryRecoveryError
from mac.review_failure_classifier import FinalizerRefusalKind


TASK_ID = "task_abcdef0123456789abcdef0123456789"


# The production base/branch/lease resolvers consult MAC_TASK_REPO_* env vars
# before the task metadata (mirroring worker preparation).  The contract
# harness unsets MAC_* for hermetic runs, but a bare `pytest` invocation on a
# fleet host inherits the live values, so clear the ones these tests assert on
# to keep the task metadata authoritative regardless of environment.
@pytest.fixture(autouse=True)
def _hermetic_repo_env(monkeypatch):
    for var in (
        "MAC_TASK_REPO_BASE_SHA",
        "MAC_TASK_REPO_BRANCH",
        "MAC_TASK_REPO_LEASE_ID",
        "MAC_TASK_CANONICAL_REMOTE",
        "MAC_TASK_REPO_DEFAULT_BRANCH",
    ):
        monkeypatch.delenv(var, raising=False)


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


class _StubPublication:
    """Minimal duck-typed stand-in for a gitops publication result."""

    def __init__(self, head_sha: str) -> None:
        self.ok = True
        self.remote_verified = True
        self.head_sha = head_sha
        self.canonical_tip_sha = ""
        self.error = ""


def _staging_fixture(tmp_path: Path) -> tuple[Path, Path, str, dict]:
    """Build a real bare canonical + task worktree seeded with a base commit.

    Returns ``(workspace, worktree, base_sha, task)``.  Callers add new files to
    ``worktree`` and then write the preserved-state artifacts via
    ``_write_refusal`` before invoking recovery.
    """
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

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = {
        "id": TASK_ID,
        "title": "Recover preserved new files",
        "metadata": {
            "execution_contract": {
                "repository_contract": {
                    "canonical_remote_url": remote.as_uri(),
                    "default_branch": "main",
                }
            },
            "runtime": {
                "repository_base_sha": base,
                "repository_branch": "mac/staging-recovery",
                "repository_lease_id": "lease_deadbeef",
            },
        },
    }
    return workspace, worktree, base, task


def _write_refusal(
    workspace: Path,
    worktree: Path,
    task: dict,
    *,
    untracked: list[str],
    staged: list[str],
) -> None:
    """Preserve refusal artifacts using the real production writers.

    Exercising ``_write_git_finalizer_refusal_manifest`` (which delegates to
    ``_preserve_executor_state_before_refusal``) guarantees the preserved JSON
    shapes match exactly what recovery/loaders read back.
    """
    # A minimal prior executor evidence payload the refusal writer copies.
    (workspace / "mac-evidence.json").write_text(
        json.dumps({"schema": "mac.worker_evidence.v1", "evidence_type": "repo_change"}),
        encoding="utf-8",
    )
    status_stdout = _git(worktree, "status", "--porcelain")
    message = (
        ef._untracked_finalize_message(untracked)
        if untracked
        else ef._new_file_finalize_message(staged)
    )
    ef._write_git_finalizer_refusal_manifest(
        workspace,
        task,
        worktree,
        message,
        status_stdout=status_stdout,
        untracked_paths=untracked,
        staged_new_paths=staged,
    )


def test_recovers_untracked_new_files(tmp_path: Path):
    workspace, worktree, _base, task = _staging_fixture(tmp_path)
    (worktree / "src_new.py").write_text("VALUE = 1\n", encoding="utf-8")
    (worktree / "pkg").mkdir()
    (worktree / "pkg" / "mod.py").write_text("X = 2\n", encoding="utf-8")
    _write_refusal(
        workspace,
        worktree,
        task,
        untracked=["pkg/mod.py", "src_new.py"],
        staged=[],
    )

    published: list = []

    def push_runner(target):
        published.append(target)
        return _StubPublication(_git(worktree, "rev-parse", "HEAD"))

    result = recover_from_new_file_refusal(workspace, task, push_runner=push_runner)

    assert result["schema"] == NEW_FILE_RECOVERY_SCHEMA
    assert result["status"] == "complete"
    assert result["refusal_kind"] == FinalizerRefusalKind.untracked_new_files.value
    # Deduplicated + sorted order preserved.
    assert result["recovered_files"] == ["pkg/mod.py", "src_new.py"]
    assert result["remote_verified"] is True
    assert len(published) == 1

    # Real staging produced a non-empty commit containing exactly the new files.
    assert _git(worktree, "status", "--porcelain") == ""
    committed = _git(worktree, "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(committed) == ["pkg/mod.py", "src_new.py"]
    body = _git(worktree, "show", "-s", "--format=%B", "HEAD")
    assert "MAC-Recovery-Reason: new-file-finalizer-refusal" in body
    assert "MAC-Recovery-Kind: %s" % FinalizerRefusalKind.untracked_new_files.value in body
    assert TASK_ID in body


def test_recovers_staged_but_uncommitted_new_files(tmp_path: Path):
    workspace, worktree, _base, task = _staging_fixture(tmp_path)
    (worktree / "staged_new.py").write_text("Y = 3\n", encoding="utf-8")
    _git(worktree, "add", "staged_new.py")
    _write_refusal(
        workspace,
        worktree,
        task,
        untracked=[],
        staged=["staged_new.py"],
    )

    def push_runner(target):
        return _StubPublication(_git(worktree, "rev-parse", "HEAD"))

    result = recover_from_new_file_refusal(workspace, task, push_runner=push_runner)

    assert result["refusal_kind"] == FinalizerRefusalKind.staged_new_files.value
    assert result["recovered_files"] == ["staged_new.py"]
    assert _git(worktree, "status", "--porcelain") == ""
    committed = _git(worktree, "show", "--name-only", "--format=", "HEAD").split()
    assert committed == ["staged_new.py"]
    body = _git(worktree, "show", "-s", "--format=%B", "HEAD")
    assert "MAC-Recovery-Kind: %s" % FinalizerRefusalKind.staged_new_files.value in body


def test_recovers_union_of_untracked_and_staged_new_files(tmp_path: Path):
    workspace, worktree, _base, task = _staging_fixture(tmp_path)
    # One staged-but-uncommitted file, two untracked files; one path appears in
    # both lists to prove deduplication.
    (worktree / "both.py").write_text("B = 0\n", encoding="utf-8")
    (worktree / "only_staged.py").write_text("S = 1\n", encoding="utf-8")
    _git(worktree, "add", "both.py", "only_staged.py")
    (worktree / "only_untracked.py").write_text("U = 2\n", encoding="utf-8")
    _write_refusal(
        workspace,
        worktree,
        task,
        untracked=["only_untracked.py", "both.py"],
        staged=["only_staged.py", "both.py"],
    )

    def push_runner(target):
        return _StubPublication(_git(worktree, "rev-parse", "HEAD"))

    result = recover_from_new_file_refusal(workspace, task, push_runner=push_runner)

    # Sorted + deduplicated union across both preserved lists.
    assert result["recovered_files"] == ["both.py", "only_staged.py", "only_untracked.py"]
    committed = _git(worktree, "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(committed) == ["both.py", "only_staged.py", "only_untracked.py"]
    # untracked_files takes precedence in the manifest kind ordering.
    assert result["refusal_kind"] == FinalizerRefusalKind.untracked_new_files.value


def test_recovery_records_error_when_no_new_files_to_stage(tmp_path: Path):
    workspace, worktree, _base, task = _staging_fixture(tmp_path)
    # Preserve a refusal whose new-file lists are empty: recovery must fail
    # closed rather than commit an empty change and report success.
    _write_refusal(workspace, worktree, task, untracked=[], staged=[])

    def push_runner(target):  # pragma: no cover - must never be reached
        raise AssertionError("push must not run when there is nothing to recover")

    head_before = _git(worktree, "rev-parse", "HEAD")
    with pytest.raises(RepositoryRecoveryError, match="no new files to recover"):
        recover_from_new_file_refusal(workspace, task, push_runner=push_runner)

    # No commit was created; HEAD is unchanged from the seeded base.
    assert _git(worktree, "rev-parse", "HEAD") == head_before


@pytest.mark.parametrize(
    "untracked, staged, expected_kind",
    [
        (["new_a.py"], [], FinalizerRefusalKind.untracked_new_files),
        ([], ["new_b.py"], FinalizerRefusalKind.staged_new_files),
    ],
)
def test_refusal_manifest_roundtrips_kind_and_file_lists(
    tmp_path: Path, untracked, staged, expected_kind
):
    workspace, worktree, base, task = _staging_fixture(tmp_path)
    target = untracked or staged
    for path in target:
        (worktree / path).write_text("Z = 9\n", encoding="utf-8")
    if staged:
        _git(worktree, "add", *staged)
    _write_refusal(workspace, worktree, task, untracked=untracked, staged=staged)

    # The refusal writer records the correct structured kind at the top level.
    manifest = json.loads((workspace / "mac-evidence.json").read_text(encoding="utf-8"))
    assert manifest["finalizer_refusal_kind"] == expected_kind.value
    assert manifest["repo"]["untracked_files"] == untracked
    assert manifest["repo"]["staged_new_files"] == staged

    # Both preserved artifacts exist and round-trip through the loader.
    assert (workspace / PRESERVED_EXECUTOR_WORKTREE_FILENAME).exists()
    assert (workspace / PRESERVED_EXECUTOR_EVIDENCE_FILENAME).exists()

    preserved = load_preserved_executor_state(workspace)
    assert preserved.untracked_files == untracked
    assert preserved.staged_new_files == staged
    assert preserved.worktree_path == worktree
    assert preserved.base_sha == base
    assert preserved.task_branch == "mac/staging-recovery"
