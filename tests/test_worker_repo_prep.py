"""Isolation tests for repository source and learning seams."""

from __future__ import annotations

from pathlib import Path

import pytest

from mac.worker_repo_prep import RepoPrepMixin


class _Client:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.posts = []

    def post(self, path, body):
        if self.fail:
            raise RuntimeError("hub unavailable")
        self.posts.append((path, body))
        return {"id": "memory-1"}


class _Worker(RepoPrepMixin):
    def __init__(self, root: Path, *, fail_client: bool = False) -> None:
        self.self_update_repo = root
        self.agent_id = "agent_repo"
        self.client = _Client(fail=fail_client)
        self.logs = []

    def _observe_log(self, name, **kwargs):
        self.logs.append((name, kwargs))


def test_resolve_source_path_prefers_existing_declared_path(tmp_path) -> None:
    source = tmp_path / "repo"
    source.mkdir()
    worker = _Worker(tmp_path / "self-update")
    assert worker._resolve_repository_source_path({"repository_path": str(source)}) == source


def test_remote_url_prefers_task_origin(tmp_path) -> None:
    worker = _Worker(tmp_path)
    url = worker._resolve_repository_remote_url(
        {}, {"repository_url": "https://github.com/example/project.git"}
    )
    assert url == "https://github.com/example/project.git"


def test_remote_url_uses_contract_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "mac.worker._repository_contract_canonical_remote",
        lambda _task: "git@github.com:example/project.git",
    )
    assert _Worker(tmp_path)._resolve_repository_remote_url({}, {}) == "git@github.com:example/project.git"


def test_remote_url_uses_environment_last_and_allows_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("mac.worker._repository_contract_canonical_remote", lambda _task: "")
    monkeypatch.setenv("MAC_TASK_REPO_URL", "https://github.com/example/env.git")
    worker = _Worker(tmp_path)
    assert worker._resolve_repository_remote_url({}, {}) == "https://github.com/example/env.git"
    monkeypatch.delenv("MAC_TASK_REPO_URL")
    assert worker._resolve_repository_remote_url({}, {}) == ""


def test_invalid_remote_is_rejected_without_echoing_secret(tmp_path) -> None:
    secret_url = "https://token@example.com/org/repo.git"
    with pytest.raises(ValueError, match="value redacted") as exc:
        _Worker(tmp_path)._resolve_repository_remote_url(
            {}, {"repository_url": secret_url}
        )
    assert secret_url not in str(exc.value)


def test_repository_access_learning_records_secret_free_memory(tmp_path) -> None:
    worker = _Worker(tmp_path)
    result = worker._record_repository_access_learning(
        project="mac",
        task_id="task_1",
        review_id="review_1",
        remote="git@github.com:example/project.git",
        credential_source="GH_TOKEN",
        outcome="success",
    )
    assert result == {"id": "memory-1"}
    path, payload = worker.client.posts[0]
    assert path == "/memory"
    assert "git@github.com" not in str(payload)


def test_repository_access_learning_failure_is_best_effort(tmp_path) -> None:
    worker = _Worker(tmp_path, fail_client=True)
    assert worker._record_repository_access_learning(
        project="mac",
        task_id="task_1",
        review_id="review_1",
        remote="git@github.com:example/project.git",
        credential_source="GH_TOKEN",
        outcome="failure",
        error="denied",
    ) is None
    assert worker.logs[-1][0] == "worker.repository_access_learning.failed"


def test_is_disk_full_error_detects_git_enospc_markers() -> None:
    from mac.worker_repo_prep import _is_disk_full_error

    assert _is_disk_full_error(
        "fatal: cannot create directory at 'src': No space left on device"
    )
    assert _is_disk_full_error("error: write failed (errno 28)")
    assert _is_disk_full_error("ENOSPC while writing pack")
    # Unrelated failures must not trigger a spurious reclaim + retry.
    assert not _is_disk_full_error(
        "fatal: 'branch' already exists"
    )
    assert not _is_disk_full_error("")


def test_reclaim_disk_for_worktree_invokes_gc_when_available(tmp_path) -> None:
    worker = _Worker(tmp_path)
    calls = []

    def _fake_gc():
        calls.append(True)
        return {"status": "ok", "removed": 3}

    worker._gc_workspaces_once = _fake_gc  # type: ignore[attr-defined]
    reclaimed = worker._reclaim_disk_for_worktree(
        task_id="task_1", worktree_dir=tmp_path / "repo-lease"
    )
    assert reclaimed is True
    assert calls == [True]
    assert worker.logs[-1][0] == "worker.repository.disk_reclaim_attempted"


def test_reclaim_disk_for_worktree_noops_without_gc(tmp_path) -> None:
    worker = _Worker(tmp_path)
    # No _gc_workspaces_once attribute -> nothing to reclaim, no retry signalled.
    assert worker._reclaim_disk_for_worktree(
        task_id="task_1", worktree_dir=tmp_path / "repo-lease"
    ) is False


def test_reclaim_disk_for_worktree_is_best_effort_on_gc_error(tmp_path) -> None:
    worker = _Worker(tmp_path)

    def _boom():
        raise RuntimeError("gc failed")

    worker._gc_workspaces_once = _boom  # type: ignore[attr-defined]
    # A GC failure must not mask the original disk-full error, but still
    # signals a retry attempt so the caller re-runs the worktree add once.
    assert worker._reclaim_disk_for_worktree(
        task_id="task_1", worktree_dir=tmp_path / "repo-lease"
    ) is True
    assert worker.logs[-1][0] == "worker.repository.disk_reclaim_failed"
