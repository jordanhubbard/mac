"""Isolation tests for repository source and learning seams."""

from __future__ import annotations

from pathlib import Path

import pytest

from mac.worker import MacWorker
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

    # The real emitter and the real delivery path, borrowed from MacWorker, so
    # these tests exercise the code that runs in the fleet rather than a
    # test-only stand-in -- including its best-effort failure handling.
    _emit_bus_event = MacWorker._emit_bus_event
    _post_observation = MacWorker._post_observation
    _observation_post_failures = 0
    _last_observation_failure_log_at = 0.0


def _broadcasts(worker, event_type=None):
    return [
        body
        for path, body in worker.client.posts
        if path == "/agentbus/broadcast"
        and (event_type is None or body["event_type"] == event_type)
    ]


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
    assert (
        _Worker(tmp_path)._resolve_repository_remote_url({}, {})
        == "git@github.com:example/project.git"
    )


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
        _Worker(tmp_path)._resolve_repository_remote_url({}, {"repository_url": secret_url})
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
    assert (
        worker._record_repository_access_learning(
            project="mac",
            task_id="task_1",
            review_id="review_1",
            remote="git@github.com:example/project.git",
            credential_source="GH_TOKEN",
            outcome="failure",
            error="denied",
        )
        is None
    )
    assert worker.logs[-1][0] == "worker.repository_access_learning.failed"


def test_is_disk_full_error_detects_git_enospc_markers() -> None:
    from mac.worker_repo_prep import _is_disk_full_error

    assert _is_disk_full_error("fatal: cannot create directory at 'src': No space left on device")
    assert _is_disk_full_error("error: write failed (errno 28)")
    assert _is_disk_full_error("ENOSPC while writing pack")
    # Unrelated failures must not trigger a spurious reclaim + retry.
    assert not _is_disk_full_error("fatal: 'branch' already exists")
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
    assert (
        worker._reclaim_disk_for_worktree(task_id="task_1", worktree_dir=tmp_path / "repo-lease")
        is False
    )


def test_reclaim_disk_for_worktree_is_best_effort_on_gc_error(tmp_path) -> None:
    worker = _Worker(tmp_path)

    def _boom():
        raise RuntimeError("gc failed")

    worker._gc_workspaces_once = _boom  # type: ignore[attr-defined]
    # A GC failure must not mask the original disk-full error, but still
    # signals a retry attempt so the caller re-runs the worktree add once.
    assert (
        worker._reclaim_disk_for_worktree(task_id="task_1", worktree_dir=tmp_path / "repo-lease")
        is True
    )
    assert worker.logs[-1][0] == "worker.repository.disk_reclaim_failed"


# ---------------------------------------------------------------------------
# Git events on the AgentBus broadcast channel
#
# The events are emitted from the worker's OWN git call sites, not scraped
# from logs, so what the fleet hears is what this worker actually did. The
# reason this matters is in CLAUDE.md: two agents in one checkout nearly
# destroyed each other's work, and the only mitigation today is a documented
# convention. A branch event lets a peer KNOW.
# ---------------------------------------------------------------------------


def _origin_repository(tmp_path: Path) -> Path:
    """A real local git repository with one commit on main."""
    import subprocess

    repo = tmp_path / "origin"
    repo.mkdir()
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    run("init", "--quiet", "--initial-branch=main")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "test")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    run("add", "README.md")
    run("commit", "--quiet", "-m", "initial")
    return repo


def test_creating_a_task_branch_broadcasts_exactly_one_git_event(tmp_path) -> None:
    origin = _origin_repository(tmp_path)
    worker = _Worker(tmp_path / "self-update")
    task_dir = tmp_path / "task"
    task_dir.mkdir()

    context = worker._prepare_repository_worktree_from_remote(
        {"id": "task_1", "project": "mac"},
        {"id": "lease_1"},
        task_dir,
        {},
        str(origin),
    )

    events = _broadcasts(worker, "git.branch_created")
    assert len(events) == 1
    event = events[0]
    assert event["agent_id"] == "agent_repo"
    assert event["task_id"] == "task_1"
    assert event["project"] == "mac"
    assert event["payload"]["branch"] == context["repository_branch"]
    assert event["payload"]["base_sha"] == context["repository_base_sha"]
    # One branch, one event: nothing else on the wire from this call.
    assert len(_broadcasts(worker)) == 1


def test_a_failed_checkout_broadcasts_nothing(tmp_path, monkeypatch) -> None:
    """Announcements describe what happened, never what was attempted."""
    import subprocess

    origin = _origin_repository(tmp_path)
    worker = _Worker(tmp_path / "self-update")
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    real_run_git = __import__("mac.worker", fromlist=["_run_git"])._run_git

    def _fail_checkout(repo, args, **kwargs):
        if args[:1] == ["checkout"]:
            return subprocess.CompletedProcess(args, 1, "", "checkout refused")
        return real_run_git(repo, args, **kwargs)

    monkeypatch.setattr("mac.worker._run_git", _fail_checkout)

    with pytest.raises(RuntimeError):
        worker._prepare_repository_worktree_from_remote(
            {"id": "task_1", "project": "mac"},
            {"id": "lease_1"},
            task_dir,
            {},
            str(origin),
        )

    assert _broadcasts(worker) == []


def test_a_hub_outage_does_not_break_the_git_path(tmp_path) -> None:
    """Awareness is best-effort; the work is not."""
    origin = _origin_repository(tmp_path)
    worker = _Worker(tmp_path / "self-update", fail_client=True)
    task_dir = tmp_path / "task"
    task_dir.mkdir()

    context = worker._prepare_repository_worktree_from_remote(
        {"id": "task_1", "project": "mac"},
        {"id": "lease_1"},
        task_dir,
        {},
        str(origin),
    )

    assert context["repository_branch"]
