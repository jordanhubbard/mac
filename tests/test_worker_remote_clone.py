"""Unit tests for the K8s-mode remote-clone branch of
``MacWorker._prepare_repository_worktree``.

These tests are deliberately narrow: they exercise the new code path
(no local source on disk, ``repository_url`` set or
``MAC_TASK_REPO_URL`` env present) without spinning up a real git
clone. The lower-level ``_run_git_in`` / ``_run_git`` helpers are
monkey-patched so the test runs without any network access. The
existing local-worktree branch is covered by ``test_worker.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from mac.hermes_adapter import MacApiClient
from mac.worker import (
    MacWorker,
    WorkerExecution,
    _inject_git_remote_auth,
    _repository_push_remote,
)


def _noop_transport(method: str, path: str, payload: Any) -> Any:
    return None


def _executor(_task: Dict[str, Any], _task_dir: Path) -> WorkerExecution:
    return WorkerExecution(0, "noop")


def _make_worker(tmp_path: Path) -> MacWorker:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return MacWorker(
        MacApiClient("http://mac.test", transport=_noop_transport),
        "agent-test",
        workspace,
        _executor,
        attestation_key="test-key",
    )


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr=stderr)


class _FakeGit:
    """Captures git invocations and returns canned ``CompletedProcess``."""

    def __init__(self, *, head_sha: str = "0" * 40, materialize: bool = True) -> None:
        self.head_sha = head_sha
        self.calls: List[Dict[str, Any]] = []
        self.materialize = materialize

    def run_git_in(self, cwd: Path, args: List[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append({"helper": "in", "cwd": str(cwd), "args": list(args)})
        if args and args[0] == "clone":
            # Materialise the destination directory so subsequent
            # _run_git(worktree, ...) calls don't blow up on a missing
            # path. Last positional argument is the target dir.
            if self.materialize:
                target = Path(args[-1])
                target.mkdir(parents=True, exist_ok=True)
            return _ok()
        return _ok()

    def run_git(self, repo: Path, args: List[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append({"helper": "repo", "cwd": str(repo), "args": list(args)})
        if args[:2] == ["rev-parse", "HEAD"]:
            return _ok(stdout=self.head_sha + "\n")
        if args[:1] == ["checkout"]:
            return _ok()
        return _ok()


def _repo_task(repository_url: str = "", repository_path: str = "") -> Dict[str, Any]:
    origin: Dict[str, Any] = {
        "type": "direct_task",
        "repository_contract": {
            "schema": "mac.repository_contract.v1",
            "project": "repo-beads-mac",
        },
    }
    if repository_path:
        origin["repository_path"] = repository_path
    if repository_url:
        origin["repository_url"] = repository_url
    return {
        "id": "task-1",
        "metadata": {
            "origin": origin,
            "execution_contract": {
                "schema": "mac.task_execution_contract.v1",
                "type": "repository",
            },
        },
    }


def test_remote_clone_happy_path_creates_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worker = _make_worker(tmp_path)
    fake = _FakeGit(head_sha="a" * 40)
    monkeypatch.setattr("mac.worker._run_git_in", fake.run_git_in)
    monkeypatch.setattr("mac.worker._run_git", fake.run_git)
    monkeypatch.setenv("MAC_TASK_GIT_TOKEN", "tok123")

    task = _repo_task(repository_url="https://gitea.omv.test/org/repo.git")
    lease = {"id": "lease-1"}
    task_dir = tmp_path / "tasks" / "task-1"
    task_dir.mkdir(parents=True)

    ctx = worker._prepare_repository_worktree(task, lease, task_dir)
    assert ctx is not None
    assert ctx["schema"] == "mac.repository_task_worktree.v1"
    assert ctx["checkout_policy"] == "k8s_task_owned_clone"
    assert ctx["repository_origin_remote"] == "https://gitea.omv.test/org/repo.git"
    assert ctx["repository_base_sha"] == "a" * 40
    assert ctx["repository_branch"].startswith("mac/")

    # First call should be the clone in task_dir cwd.
    assert fake.calls[0]["helper"] == "in"
    clone_args = fake.calls[0]["args"]
    assert clone_args[0] == "clone"
    assert "--depth=1" in clone_args
    assert "--branch" in clone_args
    # Token must have been injected into the auth URL we pass to git.
    auth_url = clone_args[-2]
    assert "x-access-token" in auth_url

    # A `checkout -b <branch>` should appear in the repo-mode calls.
    checkout_calls = [c for c in fake.calls if c["helper"] == "repo" and c["args"][:1] == ["checkout"]]
    assert checkout_calls, "expected `git checkout -b <branch>` after clone"
    assert checkout_calls[0]["args"][1] == "-b"


def test_remote_clone_prefers_local_path_when_both_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If origin carries both a real local path AND a remote_url, the
    local checkout wins. We assert this by stubbing the local-mode git
    calls (rev-parse --show-toplevel etc.) — the clone helper must NOT
    be invoked."""
    worker = _make_worker(tmp_path)

    # Build a fake local git checkout layout on disk so the
    # ``candidate.exists()`` check succeeds.
    local_repo = tmp_path / "host" / "checkout"
    local_repo.mkdir(parents=True)

    called_clone = {"count": 0}

    def fake_run_git_in(cwd: Path, args: List[str]) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "clone":
            called_clone["count"] += 1
        return _ok()

    def fake_run_git(repo: Path, args: List[str]) -> subprocess.CompletedProcess[str]:
        if args[:1] == ["rev-parse"] and "--show-toplevel" in args:
            return _ok(stdout=str(local_repo) + "\n")
        if args[:1] == ["rev-parse"] and "--is-inside-work-tree" in args:
            return _ok(stdout="true\n")
        if args[:1] == ["status"]:
            return _ok(stdout="")
        if args[:2] == ["rev-parse", "HEAD"]:
            return _ok(stdout=("b" * 40) + "\n")
        if args[:1] == ["worktree"]:
            return _ok()
        if args[:1] == ["remote"]:
            return _ok(stdout="git@host:org/repo.git\n")
        return _ok()

    monkeypatch.setattr("mac.worker._run_git_in", fake_run_git_in)
    monkeypatch.setattr("mac.worker._run_git", fake_run_git)

    task = _repo_task(
        repository_url="https://gitea.omv.test/org/repo.git",
        repository_path=str(local_repo),
    )
    lease = {"id": "lease-prefer-local"}
    task_dir = tmp_path / "tasks" / "task-prefer-local"
    task_dir.mkdir(parents=True)

    ctx = worker._prepare_repository_worktree(task, lease, task_dir)
    assert ctx is not None
    assert ctx["checkout_policy"] == "task_owned_git_worktree"
    assert called_clone["count"] == 0, "expected the local path to win when both are present"


def test_remote_clone_uses_mac_task_repo_url_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _make_worker(tmp_path)
    fake = _FakeGit()
    monkeypatch.setattr("mac.worker._run_git_in", fake.run_git_in)
    monkeypatch.setattr("mac.worker._run_git", fake.run_git)
    monkeypatch.setenv("MAC_TASK_REPO_URL", "https://gitea.omv.test/org/env-repo.git")

    # Note: no repository_url, no repository_path. The function should
    # still return a result because _repository_task_origin now accepts
    # the case where one of the two is set — and resolve_repository_remote_url
    # falls back to MAC_TASK_REPO_URL.
    task: Dict[str, Any] = {
        "id": "task-env",
        "metadata": {
            "origin": {
                "type": "direct_task",
                # No repository_path. Set repository_url so
                # _repository_task_origin accepts the task; the URL value
                # itself does not matter — _resolve_repository_remote_url
                # consults the env first only when origin has none.
                "repository_url": "https://gitea.omv.test/org/env-repo.git",
                "repository_contract": {
                    "schema": "mac.repository_contract.v1",
                    "project": "repo-beads-mac",
                },
            },
            "execution_contract": {
                "schema": "mac.task_execution_contract.v1",
                "type": "repository",
            },
        },
    }
    lease = {"id": "lease-env"}
    task_dir = tmp_path / "tasks" / "task-env"
    task_dir.mkdir(parents=True)
    ctx = worker._prepare_repository_worktree(task, lease, task_dir)
    assert ctx is not None
    assert ctx["repository_origin_remote"] == "https://gitea.omv.test/org/env-repo.git"


def test_remote_clone_errors_when_neither_path_nor_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _make_worker(tmp_path)
    monkeypatch.delenv("MAC_TASK_REPO_URL", raising=False)
    task = {
        "id": "no-source",
        "metadata": {
            "origin": {
                "type": "direct_task",
                "repository_contract": {
                    "schema": "mac.repository_contract.v1",
                    "project": "repo-beads-mac",
                },
            },
            "execution_contract": {
                "schema": "mac.task_execution_contract.v1",
                "type": "repository",
            },
        },
    }
    lease = {"id": "lease-noop"}
    task_dir = tmp_path / "no-source"
    task_dir.mkdir()
    # No repository_path AND no repository_url -> the origin is rejected
    # at the _repository_task_origin gate and the worker treats the task
    # as not-a-repository-task (returns None).
    result = worker._prepare_repository_worktree(task, lease, task_dir)
    assert result is None


def test_remote_clone_errors_when_path_missing_and_no_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _make_worker(tmp_path)
    monkeypatch.delenv("MAC_TASK_REPO_URL", raising=False)
    # Use a project name that does NOT trigger the self-update-repo
    # fallback in _repository_source_candidates (otherwise the repo
    # path resolves to /Users/.../mac and the test exercises the wrong
    # branch — the "source is dirty" check fires before our new
    # "does not exist" branch.)
    task = {
        "id": "missing",
        "metadata": {
            "origin": {
                "type": "direct_task",
                "repository_path": "/nonexistent/path/that/should/never/exist",
                "repository_name": "not-mac-and-not-self-update",
                "repository_contract": {
                    "schema": "mac.repository_contract.v1",
                    "project": "some-other-project",
                },
            },
            "execution_contract": {
                "schema": "mac.task_execution_contract.v1",
                "type": "repository",
            },
        },
    }
    lease = {"id": "lease-missing"}
    task_dir = tmp_path / "missing-source"
    task_dir.mkdir()
    with pytest.raises(RuntimeError, match="repository source path does not exist"):
        worker._prepare_repository_worktree(task, lease, task_dir)


def test_inject_git_remote_auth_gitea(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "gtok")
    out = _inject_git_remote_auth("https://gitea.omv.test/org/repo.git")
    assert "x-access-token:gtok@gitea.omv.test" in out


def test_inject_git_remote_auth_github(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "ghtok")
    out = _inject_git_remote_auth("https://github.com/org/repo.git")
    assert "x-access-token:ghtok@github.com" in out


def test_inject_git_remote_auth_generic_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("MAC_TASK_GIT_TOKEN", "anytok")
    out = _inject_git_remote_auth("https://forge.example.com/x/y.git")
    assert "x-access-token:anytok@forge.example.com" in out


def test_inject_git_remote_auth_no_token_leaves_url_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("MAC_TASK_GIT_TOKEN", raising=False)
    url = "https://forge.example.com/x/y.git"
    assert _inject_git_remote_auth(url) == url


def test_inject_git_remote_auth_ssh_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAC_TASK_GIT_TOKEN", "tok")
    url = "git@github.com:org/repo.git"
    assert _inject_git_remote_auth(url) == url


def test_repository_push_remote_prefers_canonical_and_redacts_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghtok")
    task = {
        "metadata": {
            "origin": {
                "repository_contract": {
                    "canonical_remote_url": "https://github.com/org/repo.git",
                },
            },
        },
    }
    context = {"repository_origin_remote": "https://github.com/org/mirror.git"}

    remote, display = _repository_push_remote(task, context)

    assert remote == "https://x-access-token:ghtok@github.com/org/repo.git"
    assert display == "https://x-access-token:<redacted>@github.com/org/repo.git"
