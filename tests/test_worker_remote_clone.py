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
    _repository_context_env,
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
            "default_branch": "main",
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
    assert ctx["repository_origin_remote"] == (
        "https://x-access-token:<redacted>@gitea.omv.test/org/repo.git"
    )
    assert ctx["repository_canonical_remote"] == ctx["repository_origin_remote"]
    assert ctx["repository_canonical_branch"] == "main"
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


def test_remote_clone_uses_execution_contract_feature_branch_without_legacy_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _make_worker(tmp_path)
    fake = _FakeGit(head_sha="b" * 40)
    monkeypatch.setattr("mac.worker._run_git_in", fake.run_git_in)
    monkeypatch.setattr("mac.worker._run_git", fake.run_git)
    monkeypatch.setenv("MAC_TASK_REPO_DEFAULT_BRANCH", "main")

    task = _repo_task(repository_url="https://github.com/org/repo.git")
    task["metadata"]["origin"].pop("default_branch", None)
    task["metadata"]["origin"]["repository_contract"]["default_branch"] = (
        "feature/one"
    )
    task["metadata"]["execution_contract"]["repository_contract"] = {
        "schema": "mac.repository_contract.v1",
        "canonical_remote_url": "https://github.com/org/repo.git",
        "default_branch": "feature/one",
    }
    task_dir = tmp_path / "tasks" / "task-feature"
    task_dir.mkdir(parents=True)

    context = worker._prepare_repository_worktree(
        task, {"id": "lease-feature"}, task_dir
    )

    assert context is not None
    clone_args = fake.calls[0]["args"]
    branch_index = clone_args.index("--branch")
    assert clone_args[branch_index + 1] == "feature/one"
    assert "main" not in clone_args
    assert context["repository_canonical_branch"] == "feature/one"
    assert (
        _repository_context_env(context)["MAC_TASK_REPO_DEFAULT_BRANCH"]
        == "feature/one"
    )


def test_remote_clone_contract_without_branch_fails_before_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _make_worker(tmp_path)
    fake = _FakeGit()
    monkeypatch.setattr("mac.worker._run_git_in", fake.run_git_in)
    monkeypatch.setattr("mac.worker._run_git", fake.run_git)

    task = _repo_task(repository_url="https://github.com/org/repo.git")
    task["metadata"]["origin"]["repository_contract"].pop("default_branch")
    task["metadata"]["execution_contract"]["repository_contract"] = {
        "schema": "mac.repository_contract.v1",
        "canonical_remote_url": "https://github.com/org/repo.git",
    }
    task_dir = tmp_path / "tasks" / "task-missing-branch"
    task_dir.mkdir(parents=True)

    with pytest.raises(ValueError, match="has no canonical branch"):
        worker._prepare_repository_worktree(
            task, {"id": "lease-missing-branch"}, task_dir
        )

    assert fake.calls == []


def test_remote_clone_uses_contract_when_registered_path_is_hub_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _make_worker(tmp_path)
    fake = _FakeGit(head_sha="c" * 40)
    monkeypatch.setattr("mac.worker._run_git_in", fake.run_git_in)
    monkeypatch.setattr("mac.worker._run_git", fake.run_git)
    monkeypatch.delenv("MAC_TASK_REPO_URL", raising=False)

    canonical = "https://github.com/NVIDIA-dev/ova.git"
    task = _repo_task(repository_path="/hub-only/src/ova")
    task["metadata"]["origin"]["repository_name"] = "ova"
    task["metadata"]["origin"]["repository_contract"]["project"] = "ova"
    task["metadata"]["execution_contract"]["repository_contract"] = {
        "schema": "mac.repository_contract.v1",
        "canonical_remote_url": canonical,
        "default_branch": "main",
    }
    lease = {"id": "lease-contract"}
    task_dir = tmp_path / "tasks" / "task-contract"
    task_dir.mkdir(parents=True)

    ctx = worker._prepare_repository_worktree(task, lease, task_dir)

    assert ctx is not None
    assert ctx["checkout_policy"] == "k8s_task_owned_clone"
    assert ctx["repository_declared_path"] == "/hub-only/src/ova"
    assert ctx["repository_canonical_remote_url"] == canonical
    assert fake.calls[0]["args"][-2].endswith("github.com/NVIDIA-dev/ova.git")


def test_remote_clone_explicit_origin_url_overrides_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _make_worker(tmp_path)
    fake = _FakeGit()
    monkeypatch.setattr("mac.worker._run_git_in", fake.run_git_in)
    monkeypatch.setattr("mac.worker._run_git", fake.run_git)
    monkeypatch.delenv("MAC_TASK_REPO_URL", raising=False)

    explicit = "https://gitea.omv.test/org/override.git"
    task = _repo_task(
        repository_url=explicit,
        repository_path="/hub-only/src/repo",
    )
    task["metadata"]["origin"]["repository_name"] = "external-repo"
    task["metadata"]["origin"]["repository_contract"]["project"] = "external-repo"
    task["metadata"]["execution_contract"]["repository_contract"] = {
        "schema": "mac.repository_contract.v1",
        "canonical_remote_url": "https://github.com/org/canonical.git",
        "default_branch": "main",
    }
    lease = {"id": "lease-override"}
    task_dir = tmp_path / "tasks" / "task-override"
    task_dir.mkdir(parents=True)

    ctx = worker._prepare_repository_worktree(task, lease, task_dir)

    assert ctx is not None
    assert ctx["repository_canonical_remote_url"] == explicit


def test_remote_clone_invalid_contract_url_fails_closed_without_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _make_worker(tmp_path)
    fake = _FakeGit()
    monkeypatch.setattr("mac.worker._run_git_in", fake.run_git_in)
    monkeypatch.setattr("mac.worker._run_git", fake.run_git)
    monkeypatch.delenv("MAC_TASK_REPO_URL", raising=False)

    redaction_marker = "test-only-redaction-marker"
    credential_url = (
        "https://" + "operator:" + redaction_marker + "@" + "github.com/org/repo.git"
    )
    task = _repo_task(repository_path="/hub-only/src/repo")
    task["metadata"]["origin"]["repository_name"] = "external-repo"
    task["metadata"]["origin"]["repository_contract"]["project"] = "external-repo"
    task["metadata"]["execution_contract"]["repository_contract"] = {
        "schema": "mac.repository_contract.v1",
        "canonical_remote_url": credential_url,
        "default_branch": "main",
    }
    lease = {"id": "lease-invalid-contract"}
    task_dir = tmp_path / "tasks" / "task-invalid-contract"
    task_dir.mkdir(parents=True)

    with pytest.raises(ValueError) as raised:
        worker._prepare_repository_worktree(task, lease, task_dir)

    message = str(raised.value)
    assert "repository contract canonical_remote_url is invalid" in message
    assert redaction_marker not in message
    assert fake.calls == []


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
    # Create a fake .git dir so --git-common-dir resolution succeeds.
    (local_repo / ".git").mkdir()

    called_clone = {"count": 0}
    fetched_sha = "b" * 40

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
        if args[:1] == ["rev-parse"] and "--git-common-dir" in args:
            return _ok(stdout=".git\n")
        if args[:1] == ["fetch"]:
            return _ok()
        if args[:1] == ["rev-parse"] and any(a.startswith("refs/mac/fetch/") for a in args):
            return _ok(stdout=fetched_sha + "\n")
        if args[:2] == ["rev-parse", "HEAD"]:
            return _ok(stdout=fetched_sha + "\n")
        if args[:1] == ["rev-parse"] and "--verify" in args and any("^{commit}" in a for a in args):
            return _ok(stdout=fetched_sha + "\n")
        if args[:1] == ["rev-list"]:
            return _ok(stdout="0\t0\n")
        if args[:1] == ["update-ref"]:
            return _ok()
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
                    "default_branch": "main",
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


def test_inject_git_remote_auth_ssh_tokenized_when_token_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # New contract: an SSH remote is normalized to token-https when a token
    # exists for the host, so the worker fetch/finalizer push don't depend on
    # interactive SSH keys (the Permission denied (publickey) loop-blocker).
    monkeypatch.setenv("GH_TOKEN", "tok")
    url = "git@github.com:org/repo.git"
    assert _inject_git_remote_auth(url) == "https://x-access-token:tok@github.com/org/repo.git"


def test_inject_git_remote_auth_ssh_left_alone_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("GH_TOKEN", "GITHUB_TOKEN", "MAC_TASK_GIT_TOKEN"):
        monkeypatch.delenv(var, raising=False)
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


# ---------------------------------------------------------------------------
# Tests for the canonical-base fetch (mac-stale-base fix) - Attempt 3 fixes.
# Covers all 7 blockers from review ev_43ffef2672b54cb8a9e0822f4ee0bb82.
# ---------------------------------------------------------------------------

import subprocess as _subprocess
import threading as _threading


def _make_local_git_fixture(tmp_path: Path):
    """Create bare origin + clone for fetch-path tests.

    Returns (origin_path, seed_path, work_path, initial_sha).
    """
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    work = tmp_path / "work"

    _subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    _subprocess.run(["git", "init", str(seed)], check=True, capture_output=True)
    _subprocess.run(["git", "-C", str(seed), "config", "user.email", "mac-tests@example.invalid"], check=True, capture_output=True)
    _subprocess.run(["git", "-C", str(seed), "config", "user.name", "mac tests"], check=True, capture_output=True)
    (seed / "README.md").write_text("initial\n", encoding="utf-8")
    _subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True, capture_output=True)
    _subprocess.run(["git", "-C", str(seed), "commit", "-m", "initial"], check=True, capture_output=True)
    _subprocess.run(["git", "-C", str(seed), "branch", "-M", "main"], check=True, capture_output=True)
    _subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(origin)], check=True, capture_output=True)
    _subprocess.run(["git", "-C", str(seed), "push", "-u", "origin", "main"], check=True, capture_output=True)
    _subprocess.run(
        ["git", "clone", "--branch", "main", str(origin), str(work)],
        check=True, capture_output=True,
    )
    _subprocess.run(["git", "-C", str(work), "config", "user.email", "mac-tests@example.invalid"], check=True, capture_output=True)
    _subprocess.run(["git", "-C", str(work), "config", "user.name", "mac tests"], check=True, capture_output=True)
    initial_sha = _subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return origin, seed, work, initial_sha


def _commit_to_origin(seed: Path, origin: Path, text: str) -> str:
    (seed / "README.md").write_text(text, encoding="utf-8")
    _subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True, capture_output=True)
    _subprocess.run(["git", "-C", str(seed), "commit", "-m", "update"], check=True, capture_output=True)
    _subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], check=True, capture_output=True)
    return _subprocess.run(
        ["git", "-C", str(seed), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _repo_task_local(repository_path: str, default_branch: str = "main") -> Dict[str, Any]:
    return {
        "id": "task-local",
        "metadata": {
            "origin": {
                "type": "direct_task",
                "repository_path": repository_path,
                "default_branch": default_branch,
                "repository_contract": {
                    "schema": "mac.repository_contract.v1",
                    "project": "test-project",
                    "default_branch": default_branch,
                },
            },
            "execution_contract": {
                "schema": "mac.task_execution_contract.v1",
                "type": "repository",
            },
        },
    }


def test_local_worktree_uses_fetched_canonical_head_not_stale_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local-path branch must fetch the canonical remote HEAD and base
    the task worktree on the fetched SHA, even when the local work clone
    is behind the remote. This is the core mac-stale-base regression fix.
    """
    monkeypatch.delenv("MAC_TASK_WORKTREE_SKIP_FETCH", raising=False)

    _origin, seed, work, stale_sha = _make_local_git_fixture(tmp_path)

    # Advance origin so work is one commit behind.
    new_sha = _commit_to_origin(seed, _origin, "updated\n")
    assert new_sha != stale_sha

    work_head = _subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert work_head == stale_sha, "work should still be on the old commit"

    worker = _make_worker(tmp_path)
    task = _repo_task_local(str(work))
    lease = {"id": "lease-fetch-test"}
    task_dir = tmp_path / "tasks" / "task-local"
    task_dir.mkdir(parents=True)

    ctx = worker._prepare_repository_worktree(task, lease, task_dir)
    assert ctx is not None
    assert ctx["checkout_policy"] == "task_owned_git_worktree"
    # The worktree base SHA must be the NEW canonical HEAD, not stale local.
    assert ctx["repository_base_sha"] == new_sha, (
        "worktree must be based on fetched remote HEAD %r, not stale local HEAD %r"
        % (new_sha, stale_sha)
    )
    # Context must carry audit fields.
    assert ctx["repository_local_prior_sha"] == stale_sha
    assert ctx["repository_canonical_branch"] == "main"
    assert ctx["repository_canonical_remote"] != ""
    # ahead/behind must be structured integers.
    assert isinstance(ctx.get("repository_ahead"), int), "repository_ahead must be int"
    assert isinstance(ctx.get("repository_behind"), int), "repository_behind must be int"
    assert ctx["repository_behind"] == 1, "local was 1 commit behind remote"

    # Verify the worktree itself is at the new SHA.
    worktree_head = _subprocess.run(
        ["git", "-C", ctx["repository_worktree"], "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert worktree_head == new_sha

    # Verify registered checkout (work) is unchanged.
    work_head_after = _subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert work_head_after == stale_sha, "registered checkout must not be modified"


def test_local_worktree_fetch_failure_falls_back_to_local_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the remote fetch fails, the worker must log a warning and fall back
    to the local HEAD so the task can still proceed (offline-friendly).
    The fallback SHA must be the pre-fetch local HEAD, and fetch must have
    been attempted before the fallback.
    """
    monkeypatch.delenv("MAC_TASK_WORKTREE_SKIP_FETCH", raising=False)

    local_repo = tmp_path / "work"
    local_repo.mkdir(parents=True)
    (local_repo / ".git").mkdir()
    local_sha = "c" * 40
    fetch_calls: List[Dict[str, Any]] = []
    log_events: List[str] = []

    def fake_run_git(repo: Path, args: List[str]) -> _subprocess.CompletedProcess[str]:
        if args[:1] == ["rev-parse"] and "--show-toplevel" in args:
            return _ok(stdout=str(local_repo) + "\n")
        if args[:1] == ["rev-parse"] and "--is-inside-work-tree" in args:
            return _ok(stdout="true\n")
        if args[:1] == ["status"]:
            return _ok(stdout="")
        if args[:1] == ["remote"] and "get-url" in args:
            return _ok(stdout="https://github.com/org/repo.git\n")
        if args[:1] == ["rev-parse"] and "--git-common-dir" in args:
            return _ok(stdout=".git\n")
        if args[:2] == ["rev-parse", "HEAD"]:
            return _ok(stdout=local_sha + "\n")
        if args[:1] == ["fetch"]:
            fetch_calls.append({"args": list(args)})
            return _subprocess.CompletedProcess(
                args=args, returncode=128, stdout="", stderr="fatal: unable to connect to remote",
            )
        if args[:1] == ["update-ref"]:
            return _ok()
        if args[:1] == ["rev-list"]:
            # local_sha == base_sha when falling back; 0 ahead, 0 behind.
            return _ok(stdout="0\t0\n")
        if args[:1] == ["worktree"]:
            return _ok()
        return _ok()

    monkeypatch.setattr("mac.worker._run_git", fake_run_git)
    monkeypatch.setattr("mac.worker._run_git_in", lambda cwd, args: _ok())

    worker = _make_worker(tmp_path)
    task = _repo_task_local(str(local_repo))
    lease = {"id": "lease-fetch-fail"}
    task_dir = tmp_path / "tasks" / "task-fetch-fail"
    task_dir.mkdir(parents=True)

    ctx = worker._prepare_repository_worktree(task, lease, task_dir)
    assert ctx is not None, "worker must succeed even when fetch fails"
    assert ctx["repository_base_sha"] == local_sha, (
        "on fetch failure, worker must fall back to local HEAD %r" % local_sha
    )
    assert fetch_calls, "fetch must have been attempted before the fallback"


def test_local_worktree_git_common_dir_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If git rev-parse --git-common-dir fails, the worker must raise
    RuntimeError (fail closed) and must NOT fall back to source_root as
    a non-shared lock location.
    """
    local_repo = tmp_path / "work"
    local_repo.mkdir(parents=True)

    def fake_run_git(repo: Path, args: List[str]) -> _subprocess.CompletedProcess[str]:
        if args[:1] == ["rev-parse"] and "--show-toplevel" in args:
            return _ok(stdout=str(local_repo) + "\n")
        if args[:1] == ["rev-parse"] and "--is-inside-work-tree" in args:
            return _ok(stdout="true\n")
        if args[:1] == ["status"]:
            return _ok(stdout="")
        if args[:1] == ["remote"] and "get-url" in args:
            return _ok(stdout="https://github.com/org/repo.git\n")
        if args[:1] == ["rev-parse"] and "--git-common-dir" in args:
            # Simulate failure: git-common-dir cannot be resolved.
            return _subprocess.CompletedProcess(
                args=args, returncode=128, stdout="", stderr="fatal: not a git repository",
            )
        return _ok()

    monkeypatch.setattr("mac.worker._run_git", fake_run_git)
    monkeypatch.setattr("mac.worker._run_git_in", lambda cwd, args: _ok())

    worker = _make_worker(tmp_path)
    task = _repo_task_local(str(local_repo))
    lease = {"id": "lease-gcd-fail"}
    task_dir = tmp_path / "tasks" / "task-gcd-fail"
    task_dir.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="could not resolve git common directory"):
        worker._prepare_repository_worktree(task, lease, task_dir)


def test_local_worktree_prior_head_failure_inside_lock_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If rev-parse HEAD fails AFTER the lock is acquired, the worker must
    raise RuntimeError (fail closed). HEAD is resolved inside the lock so
    concurrent fetches cannot race the read.
    """
    local_repo = tmp_path / "work"
    local_repo.mkdir(parents=True)
    (local_repo / ".git").mkdir()

    def fake_run_git(repo: Path, args: List[str]) -> _subprocess.CompletedProcess[str]:
        if args[:1] == ["rev-parse"] and "--show-toplevel" in args:
            return _ok(stdout=str(local_repo) + "\n")
        if args[:1] == ["rev-parse"] and "--is-inside-work-tree" in args:
            return _ok(stdout="true\n")
        if args[:1] == ["status"]:
            return _ok(stdout="")
        if args[:1] == ["remote"] and "get-url" in args:
            return _ok(stdout="https://github.com/org/repo.git\n")
        if args[:1] == ["rev-parse"] and "--git-common-dir" in args:
            return _ok(stdout=".git\n")
        if args[:2] == ["rev-parse", "HEAD"]:
            # Simulate HEAD resolution failure inside the lock.
            return _subprocess.CompletedProcess(
                args=args, returncode=128, stdout="", stderr="fatal: ambiguous argument 'HEAD'",
            )
        if args[:1] == ["update-ref"]:
            return _ok()
        return _ok()

    monkeypatch.setattr("mac.worker._run_git", fake_run_git)
    monkeypatch.setattr("mac.worker._run_git_in", lambda cwd, args: _ok())

    worker = _make_worker(tmp_path)
    task = _repo_task_local(str(local_repo))
    lease = {"id": "lease-head-fail"}
    task_dir = tmp_path / "tasks" / "task-head-fail"
    task_dir.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="could not resolve repository source HEAD"):
        worker._prepare_repository_worktree(task, lease, task_dir)


def test_local_worktree_invalid_canonical_url_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An invalid canonical_remote_url in the task contract must raise a
    ValueError (fail closed) — it must NOT silently fall back to 'origin'.
    """
    local_repo = tmp_path / "work"
    local_repo.mkdir(parents=True)

    def fake_run_git(repo: Path, args: List[str]) -> _subprocess.CompletedProcess[str]:
        if args[:1] == ["rev-parse"] and "--show-toplevel" in args:
            return _ok(stdout=str(local_repo) + "\n")
        if args[:1] == ["rev-parse"] and "--is-inside-work-tree" in args:
            return _ok(stdout="true\n")
        if args[:1] == ["status"]:
            return _ok(stdout="")
        return _ok()

    monkeypatch.setattr("mac.worker._run_git", fake_run_git)
    monkeypatch.setattr("mac.worker._run_git_in", lambda cwd, args: _ok())

    # Task with an invalid canonical_remote_url in the repository_contract.
    task: Dict[str, Any] = {
        "id": "task-invalid-url",
        "metadata": {
            "origin": {
                "type": "direct_task",
                "repository_path": str(local_repo),
                "repository_contract": {
                    "schema": "mac.repository_contract.v1",
                    "project": "test-project",
                    "canonical_remote_url": "not-a-valid-url",
                },
            },
            "execution_contract": {
                "schema": "mac.task_execution_contract.v1",
                "type": "repository",
            },
        },
    }
    worker = _make_worker(tmp_path)
    lease = {"id": "lease-invalid-url"}
    task_dir = tmp_path / "tasks" / "task-invalid-url"
    task_dir.mkdir(parents=True)

    with pytest.raises(ValueError, match="git remote URL does not match"):
        worker._prepare_repository_worktree(task, lease, task_dir)


def test_local_worktree_fetched_sha_not_valid_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the fetched ref resolves to a non-hex non-40-char string, the
    worker must raise RuntimeError (fail closed) before creating a worktree.
    """
    local_repo = tmp_path / "work"
    local_repo.mkdir(parents=True)
    (local_repo / ".git").mkdir()

    def fake_run_git(repo: Path, args: List[str]) -> _subprocess.CompletedProcess[str]:
        if args[:1] == ["rev-parse"] and "--show-toplevel" in args:
            return _ok(stdout=str(local_repo) + "\n")
        if args[:1] == ["rev-parse"] and "--is-inside-work-tree" in args:
            return _ok(stdout="true\n")
        if args[:1] == ["status"]:
            return _ok(stdout="")
        if args[:1] == ["remote"] and "get-url" in args:
            return _ok(stdout="https://github.com/org/repo.git\n")
        if args[:1] == ["rev-parse"] and "--git-common-dir" in args:
            return _ok(stdout=".git\n")
        if args[:1] == ["fetch"]:
            return _ok()
        if args[:1] == ["rev-parse"] and any(a.startswith("refs/mac/fetch/") for a in args):
            # Return a non-SHA string (tag name, not a commit SHA).
            return _ok(stdout="not-a-sha\n")
        if args[:2] == ["rev-parse", "HEAD"]:
            return _ok(stdout=("d" * 40) + "\n")
        if args[:1] == ["update-ref"]:
            return _ok()
        return _ok()

    monkeypatch.setattr("mac.worker._run_git", fake_run_git)
    monkeypatch.setattr("mac.worker._run_git_in", lambda cwd, args: _ok())

    worker = _make_worker(tmp_path)
    task = _repo_task_local(str(local_repo))
    lease = {"id": "lease-bad-sha"}
    task_dir = tmp_path / "tasks" / "task-bad-sha"
    task_dir.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="invalid commit SHA"):
        worker._prepare_repository_worktree(task, lease, task_dir)


def test_mac_task_worktree_skip_fetch_env_is_not_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAC_TASK_WORKTREE_SKIP_FETCH must NOT re-enable local-HEAD fallback.
    The env var was removed; when set it has no effect on the normal path.
    The worker still fetches and uses the remote HEAD.
    """
    monkeypatch.setenv("MAC_TASK_WORKTREE_SKIP_FETCH", "1")

    local_repo = tmp_path / "work"
    local_repo.mkdir(parents=True)
    (local_repo / ".git").mkdir()
    fetched_sha = "e" * 40
    fetch_calls: List[Dict[str, Any]] = []

    def fake_run_git(repo: Path, args: List[str]) -> _subprocess.CompletedProcess[str]:
        if args[:1] == ["rev-parse"] and "--show-toplevel" in args:
            return _ok(stdout=str(local_repo) + "\n")
        if args[:1] == ["rev-parse"] and "--is-inside-work-tree" in args:
            return _ok(stdout="true\n")
        if args[:1] == ["status"]:
            return _ok(stdout="")
        if args[:1] == ["rev-parse"] and "--git-common-dir" in args:
            return _ok(stdout=".git\n")
        if args[:1] == ["fetch"]:
            fetch_calls.append({"args": list(args)})
            return _ok()
        if args[:1] == ["rev-parse"] and any(a.startswith("refs/mac/fetch/") for a in args):
            return _ok(stdout=fetched_sha + "\n")
        if args[:2] == ["rev-parse", "HEAD"]:
            return _ok(stdout=("f" * 40) + "\n")
        if args[:1] == ["rev-parse"] and "--verify" in args and any("^{commit}" in a for a in args):
            return _ok(stdout=fetched_sha + "\n")
        if args[:1] == ["rev-list"]:
            return _ok(stdout="0\t1\n")
        if args[:1] == ["update-ref"]:
            return _ok()
        if args[:1] == ["worktree"]:
            return _ok()
        if args[:1] == ["remote"]:
            return _ok(stdout="git@host:org/repo.git\n")
        return _ok()

    monkeypatch.setattr("mac.worker._run_git", fake_run_git)
    monkeypatch.setattr("mac.worker._run_git_in", lambda cwd, args: _ok())

    worker = _make_worker(tmp_path)
    task = _repo_task_local(str(local_repo))
    lease = {"id": "lease-skip-ignored"}
    task_dir = tmp_path / "tasks" / "task-skip-ignored"
    task_dir.mkdir(parents=True)

    ctx = worker._prepare_repository_worktree(task, lease, task_dir)
    assert ctx is not None
    # Must use the fetched SHA, NOT the local HEAD.
    assert ctx["repository_base_sha"] == fetched_sha
    # Fetch MUST have been called (env var has no effect).
    assert fetch_calls, "fetch must be called even when MAC_TASK_WORKTREE_SKIP_FETCH=1"


def test_local_worktree_non_main_default_branch(
    tmp_path: Path,
) -> None:
    """A repository whose default branch is not 'main' (e.g. 'develop')
    must be fetched from the correct branch.
    """
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    work = tmp_path / "work"

    _subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    _subprocess.run(["git", "init", str(seed)], check=True, capture_output=True)
    _subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@example.invalid"], check=True, capture_output=True)
    _subprocess.run(["git", "-C", str(seed), "config", "user.name", "tester"], check=True, capture_output=True)
    (seed / "README.md").write_text("initial\n", encoding="utf-8")
    _subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True, capture_output=True)
    _subprocess.run(["git", "-C", str(seed), "commit", "-m", "initial"], check=True, capture_output=True)
    _subprocess.run(["git", "-C", str(seed), "branch", "-M", "develop"], check=True, capture_output=True)
    _subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(origin)], check=True, capture_output=True)
    _subprocess.run(["git", "-C", str(seed), "push", "-u", "origin", "develop"], check=True, capture_output=True)
    _subprocess.run(
        ["git", "clone", "--branch", "develop", str(origin), str(work)],
        check=True, capture_output=True,
    )
    _subprocess.run(["git", "-C", str(work), "config", "user.email", "t@example.invalid"], check=True, capture_output=True)
    _subprocess.run(["git", "-C", str(work), "config", "user.name", "tester"], check=True, capture_output=True)

    # Push a new commit to origin/develop so work is behind.
    (seed / "README.md").write_text("updated\n", encoding="utf-8")
    _subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True, capture_output=True)
    _subprocess.run(["git", "-C", str(seed), "commit", "-m", "update"], check=True, capture_output=True)
    _subprocess.run(["git", "-C", str(seed), "push", "origin", "develop"], check=True, capture_output=True)
    canonical_sha = _subprocess.run(
        ["git", "-C", str(seed), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    worker = _make_worker(tmp_path)
    task = _repo_task_local(str(work), default_branch="develop")
    lease = {"id": "lease-develop"}
    task_dir = tmp_path / "tasks" / "task-develop"
    task_dir.mkdir(parents=True)

    ctx = worker._prepare_repository_worktree(task, lease, task_dir)
    assert ctx is not None
    assert ctx["repository_canonical_branch"] == "develop"
    assert ctx["repository_base_sha"] == canonical_sha


def test_local_worktree_execution_contract_feature_branch_is_exact_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _origin, seed, work, initial_sha = _make_local_git_fixture(tmp_path)

    _subprocess.run(
        ["git", "-C", str(seed), "checkout", "-b", "feature/one", initial_sha],
        check=True,
        capture_output=True,
    )
    (seed / "README.md").write_text("feature\n", encoding="utf-8")
    _subprocess.run(
        ["git", "-C", str(seed), "commit", "-am", "feature"],
        check=True,
        capture_output=True,
    )
    _subprocess.run(
        ["git", "-C", str(seed), "push", "origin", "feature/one"],
        check=True,
        capture_output=True,
    )
    feature_sha = _subprocess.run(
        ["git", "-C", str(seed), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    _subprocess.run(
        ["git", "-C", str(seed), "checkout", "main"],
        check=True,
        capture_output=True,
    )
    (seed / "README.md").write_text("main diverged\n", encoding="utf-8")
    _subprocess.run(
        ["git", "-C", str(seed), "commit", "-am", "main divergence"],
        check=True,
        capture_output=True,
    )
    _subprocess.run(
        ["git", "-C", str(seed), "push", "origin", "main"],
        check=True,
        capture_output=True,
    )
    main_sha = _subprocess.run(
        ["git", "-C", str(seed), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert main_sha != feature_sha

    task = _repo_task_local(str(work))
    task["metadata"]["origin"].pop("default_branch")
    task["metadata"]["origin"]["repository_contract"]["default_branch"] = (
        "feature/one"
    )
    task["metadata"]["execution_contract"]["repository_contract"] = {
        "schema": "mac.repository_contract.v1",
        "default_branch": "feature/one",
    }
    monkeypatch.setenv("MAC_TASK_REPO_DEFAULT_BRANCH", "main")
    worker = _make_worker(tmp_path)
    task_dir = tmp_path / "tasks" / "task-production-feature"
    task_dir.mkdir(parents=True)

    context = worker._prepare_repository_worktree(
        task, {"id": "lease-production-feature"}, task_dir
    )

    assert context is not None
    assert context["repository_canonical_branch"] == "feature/one"
    assert context["repository_base_sha"] == feature_sha
    assert context["repository_base_sha"] != main_sha
    assert (
        _repository_context_env(context)["MAC_TASK_REPO_DEFAULT_BRANCH"]
        == "feature/one"
    )


def test_local_worktree_concurrent_preparation_no_race(
    tmp_path: Path,
) -> None:
    """Two concurrent task preparations on the same checkout must each
    produce a distinct worktree based on the canonical remote HEAD and
    must not corrupt each other's ref resolution or worktree metadata.
    """
    _origin, seed, work, initial_sha = _make_local_git_fixture(tmp_path)

    results: List[Dict[str, Any]] = []
    errors: List[Exception] = []

    def _prepare(lease_id: str) -> None:
        try:
            w = _make_worker(tmp_path)
            task = _repo_task_local(str(work))
            task = dict(task)
            task["id"] = "task-%s" % lease_id
            lease = {"id": lease_id}
            task_dir = tmp_path / "tasks" / lease_id
            task_dir.mkdir(parents=True, exist_ok=True)
            ctx = w._prepare_repository_worktree(task, lease, task_dir)
            results.append(ctx or {})
        except Exception as exc:
            errors.append(exc)

    t1 = _threading.Thread(target=_prepare, args=("lease-t1",))
    t2 = _threading.Thread(target=_prepare, args=("lease-t2",))
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    assert not errors, "concurrent preparation raised errors: %s" % errors
    assert len(results) == 2
    # Both must use the same canonical SHA.
    assert results[0]["repository_base_sha"] == initial_sha
    assert results[1]["repository_base_sha"] == initial_sha
    # Worktree paths must be distinct.
    assert results[0]["repository_worktree"] != results[1]["repository_worktree"]


def test_local_worktree_context_carries_redacted_remote_and_integer_audit_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The context dict must carry:
    - repository_canonical_remote: a redacted URL (not literal '<remote>',
      not a raw URL with credentials), obtained via _redact_git_remote_auth
    - repository_local_prior_sha: the pre-fetch local HEAD
    - repository_canonical_branch: the branch that was fetched
    - repository_ahead / repository_behind: structured integer fields
    """
    local_repo = tmp_path / "work"
    local_repo.mkdir(parents=True)
    (local_repo / ".git").mkdir()
    prior_sha = "aa" * 20
    fetched_sha = "bb" * 20

    def fake_run_git(repo: Path, args: List[str]) -> _subprocess.CompletedProcess[str]:
        if args[:1] == ["rev-parse"] and "--show-toplevel" in args:
            return _ok(stdout=str(local_repo) + "\n")
        if args[:1] == ["rev-parse"] and "--is-inside-work-tree" in args:
            return _ok(stdout="true\n")
        if args[:1] == ["status"]:
            return _ok(stdout="")
        if args[:1] == ["rev-parse"] and "--git-common-dir" in args:
            return _ok(stdout=".git\n")
        if args[:1] == ["fetch"]:
            return _ok()
        if args[:1] == ["rev-parse"] and any(a.startswith("refs/mac/fetch/") for a in args):
            return _ok(stdout=fetched_sha + "\n")
        if args[:2] == ["rev-parse", "HEAD"]:
            return _ok(stdout=prior_sha + "\n")
        if args[:1] == ["rev-parse"] and "--verify" in args and any("^{commit}" in a for a in args):
            return _ok(stdout=fetched_sha + "\n")
        if args[:1] == ["rev-list"]:
            return _ok(stdout="0\t2\n")
        if args[:1] == ["update-ref"]:
            return _ok()
        if args[:1] == ["worktree"]:
            return _ok()
        if args[:1] == ["remote"]:
            # Return a URL — but the context should carry the redacted form.
            return _ok(stdout="https://github.com/org/repo.git\n")
        return _ok()

    monkeypatch.setattr("mac.worker._run_git", fake_run_git)
    monkeypatch.setattr("mac.worker._run_git_in", lambda cwd, args: _ok())

    worker = _make_worker(tmp_path)
    task = _repo_task_local(str(local_repo))
    lease = {"id": "lease-audit-ctx"}
    task_dir = tmp_path / "tasks" / "task-audit-ctx"
    task_dir.mkdir(parents=True)

    test_token = "mac-hermetic-redaction-secret"
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    monkeypatch.delenv("MAC_TASK_GIT_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", test_token)

    ctx = worker._prepare_repository_worktree(task, lease, task_dir)
    assert ctx is not None
    # canonical_remote must NOT be the literal placeholder '<remote>'.
    assert ctx["repository_canonical_remote"] != "<remote>", (
        "canonical_remote must be a real redacted URL, not the placeholder '<remote>'"
    )
    # It must be a valid (possibly redacted) URL that doesn't leak raw credentials.
    display = str(ctx["repository_canonical_remote"])
    assert test_token not in display
    assert "x-access-token:<redacted>@" in display
    # Audit fields.
    assert ctx["repository_local_prior_sha"] == prior_sha
    assert ctx["repository_canonical_branch"] == "main"
    assert ctx["repository_base_sha"] == fetched_sha
    # ahead/behind must be integers, not formatted strings.
    assert isinstance(ctx.get("repository_ahead"), int), (
        "repository_ahead must be int, got %r" % type(ctx.get("repository_ahead"))
    )
    assert isinstance(ctx.get("repository_behind"), int), (
        "repository_behind must be int, got %r" % type(ctx.get("repository_behind"))
    )
    assert ctx["repository_ahead"] == 0
    assert ctx["repository_behind"] == 2


# ---------------------------------------------------------------------------
# Focused tests for the three corrections applied on top of the salvage diff.
# (correction-1: non-directory common-dir, lock-failure, correction-2:
#  malformed ahead-behind, correction-3: non-commit fetched object)
# ---------------------------------------------------------------------------


def test_common_dir_is_file_not_directory_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the resolved --git-common-dir path exists but is a FILE (not a
    directory), the worker must raise RuntimeError and must never fall back
    to locking its parent directory.  Locking the parent would not protect
    concurrent access to the shared git object store.
    """
    local_repo = tmp_path / "work"
    local_repo.mkdir(parents=True)
    # Create a .git FILE (as in a git-worktree linked checkout), then point
    # the fake --git-common-dir resolver at a file path.
    git_file = local_repo / ".git"
    git_file.write_text("gitdir: /nonexistent/common.git\n", encoding="utf-8")
    # We'll make the fake return a path to a file, not a directory.
    common_file = tmp_path / "common_as_file"
    common_file.write_text("not a directory\n", encoding="utf-8")

    def fake_run_git(repo: Path, args: List[str]) -> _subprocess.CompletedProcess[str]:
        if args[:1] == ["rev-parse"] and "--show-toplevel" in args:
            return _ok(stdout=str(local_repo) + "\n")
        if args[:1] == ["rev-parse"] and "--is-inside-work-tree" in args:
            return _ok(stdout="true\n")
        if args[:1] == ["status"]:
            return _ok(stdout="")
        if args[:1] == ["remote"] and "get-url" in args:
            return _ok(stdout="https://github.com/org/repo.git\n")
        if args[:1] == ["rev-parse"] and "--git-common-dir" in args:
            # Return an absolute path to a file, not a directory.
            return _ok(stdout=str(common_file) + "\n")
        return _ok()

    monkeypatch.setattr("mac.worker._run_git", fake_run_git)
    monkeypatch.setattr("mac.worker._run_git_in", lambda cwd, args: _ok())

    worker = _make_worker(tmp_path)
    task = _repo_task_local(str(local_repo))
    lease = {"id": "lease-gcd-file"}
    task_dir = tmp_path / "tasks" / "task-gcd-file"
    task_dir.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="is not a directory"):
        worker._prepare_repository_worktree(task, lease, task_dir)


def test_lock_acquisition_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If fcntl.flock raises OSError (e.g. the file is on a filesystem that
    does not support advisory locks), the worker must propagate the error
    (fail closed) and must not proceed to fetch or create a worktree.
    """
    local_repo = tmp_path / "work"
    local_repo.mkdir(parents=True)
    (local_repo / ".git").mkdir()

    def fake_run_git(repo: Path, args: List[str]) -> _subprocess.CompletedProcess[str]:
        if args[:1] == ["rev-parse"] and "--show-toplevel" in args:
            return _ok(stdout=str(local_repo) + "\n")
        if args[:1] == ["rev-parse"] and "--is-inside-work-tree" in args:
            return _ok(stdout="true\n")
        if args[:1] == ["status"]:
            return _ok(stdout="")
        if args[:1] == ["remote"] and "get-url" in args:
            return _ok(stdout="https://github.com/org/repo.git\n")
        if args[:1] == ["rev-parse"] and "--git-common-dir" in args:
            return _ok(stdout=".git\n")
        if args[:1] == ["update-ref"]:
            return _ok()
        return _ok()

    def fake_flock(fd: int, op: int) -> None:
        raise OSError(37, "No locks available")

    monkeypatch.setattr("mac.worker._run_git", fake_run_git)
    monkeypatch.setattr("mac.worker._run_git_in", lambda cwd, args: _ok())
    monkeypatch.setattr("fcntl.flock", fake_flock)

    worker = _make_worker(tmp_path)
    task = _repo_task_local(str(local_repo))
    lease = {"id": "lease-lock-fail"}
    task_dir = tmp_path / "tasks" / "task-lock-fail"
    task_dir.mkdir(parents=True)

    with pytest.raises(OSError):
        worker._prepare_repository_worktree(task, lease, task_dir)


def test_malformed_ahead_behind_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If rev-list --left-right --count produces malformed output (not
    exactly two integers), the worker must raise RuntimeError (fail closed).
    Evidence must never emit null counts.
    """
    local_repo = tmp_path / "work"
    local_repo.mkdir(parents=True)
    (local_repo / ".git").mkdir()
    fetched_sha = "ab" * 20

    def fake_run_git(repo: Path, args: List[str]) -> _subprocess.CompletedProcess[str]:
        if args[:1] == ["rev-parse"] and "--show-toplevel" in args:
            return _ok(stdout=str(local_repo) + "\n")
        if args[:1] == ["rev-parse"] and "--is-inside-work-tree" in args:
            return _ok(stdout="true\n")
        if args[:1] == ["status"]:
            return _ok(stdout="")
        if args[:1] == ["remote"] and "get-url" in args:
            return _ok(stdout="https://github.com/org/repo.git\n")
        if args[:1] == ["rev-parse"] and "--git-common-dir" in args:
            return _ok(stdout=".git\n")
        if args[:1] == ["fetch"]:
            return _ok()
        if args[:1] == ["rev-parse"] and any(a.startswith("refs/mac/fetch/") for a in args):
            return _ok(stdout=fetched_sha + "\n")
        if args[:2] == ["rev-parse", "HEAD"]:
            return _ok(stdout=("cc" * 20) + "\n")
        if args[:1] == ["rev-parse"] and "--verify" in args and any("^{commit}" in a for a in args):
            return _ok(stdout=fetched_sha + "\n")
        if args[:1] == ["rev-list"]:
            # Return malformed output: only one number, not two.
            return _ok(stdout="42\n")
        if args[:1] == ["update-ref"]:
            return _ok()
        return _ok()

    monkeypatch.setattr("mac.worker._run_git", fake_run_git)
    monkeypatch.setattr("mac.worker._run_git_in", lambda cwd, args: _ok())

    worker = _make_worker(tmp_path)
    task = _repo_task_local(str(local_repo))
    lease = {"id": "lease-bad-ab"}
    task_dir = tmp_path / "tasks" / "task-bad-ab"
    task_dir.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="malformed output"):
        worker._prepare_repository_worktree(task, lease, task_dir)


def test_failed_ahead_behind_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If rev-list --left-right --count exits non-zero, the worker must
    raise RuntimeError (fail closed).  Evidence must never emit null counts.
    """
    local_repo = tmp_path / "work"
    local_repo.mkdir(parents=True)
    (local_repo / ".git").mkdir()
    fetched_sha = "dd" * 20

    def fake_run_git(repo: Path, args: List[str]) -> _subprocess.CompletedProcess[str]:
        if args[:1] == ["rev-parse"] and "--show-toplevel" in args:
            return _ok(stdout=str(local_repo) + "\n")
        if args[:1] == ["rev-parse"] and "--is-inside-work-tree" in args:
            return _ok(stdout="true\n")
        if args[:1] == ["status"]:
            return _ok(stdout="")
        if args[:1] == ["remote"] and "get-url" in args:
            return _ok(stdout="https://github.com/org/repo.git\n")
        if args[:1] == ["rev-parse"] and "--git-common-dir" in args:
            return _ok(stdout=".git\n")
        if args[:1] == ["fetch"]:
            return _ok()
        if args[:1] == ["rev-parse"] and any(a.startswith("refs/mac/fetch/") for a in args):
            return _ok(stdout=fetched_sha + "\n")
        if args[:2] == ["rev-parse", "HEAD"]:
            return _ok(stdout=("ee" * 20) + "\n")
        if args[:1] == ["rev-parse"] and "--verify" in args and any("^{commit}" in a for a in args):
            return _ok(stdout=fetched_sha + "\n")
        if args[:1] == ["rev-list"]:
            # Simulate rev-list failure.
            return _subprocess.CompletedProcess(
                args=args, returncode=128, stdout="", stderr="fatal: bad revision range",
            )
        if args[:1] == ["update-ref"]:
            return _ok()
        return _ok()

    monkeypatch.setattr("mac.worker._run_git", fake_run_git)
    monkeypatch.setattr("mac.worker._run_git_in", lambda cwd, args: _ok())

    worker = _make_worker(tmp_path)
    task = _repo_task_local(str(local_repo))
    lease = {"id": "lease-rev-list-fail"}
    task_dir = tmp_path / "tasks" / "task-rev-list-fail"
    task_dir.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="could not compute ahead/behind"):
        worker._prepare_repository_worktree(task, lease, task_dir)


def test_non_commit_fetched_object_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If rev-parse --verify <ref>^{commit} fails, the fetched object is not
    a commit (e.g. an annotated tag or blob) and the worker must raise
    RuntimeError (fail closed) before creating a worktree.
    """
    local_repo = tmp_path / "work"
    local_repo.mkdir(parents=True)
    (local_repo / ".git").mkdir()
    # A well-formed 40-hex SHA that nonetheless resolves to a non-commit object.
    fetched_sha = "ff" * 20

    def fake_run_git(repo: Path, args: List[str]) -> _subprocess.CompletedProcess[str]:
        if args[:1] == ["rev-parse"] and "--show-toplevel" in args:
            return _ok(stdout=str(local_repo) + "\n")
        if args[:1] == ["rev-parse"] and "--is-inside-work-tree" in args:
            return _ok(stdout="true\n")
        if args[:1] == ["status"]:
            return _ok(stdout="")
        if args[:1] == ["remote"] and "get-url" in args:
            return _ok(stdout="https://github.com/org/repo.git\n")
        if args[:1] == ["rev-parse"] and "--git-common-dir" in args:
            return _ok(stdout=".git\n")
        if args[:1] == ["fetch"]:
            return _ok()
        if args[:1] == ["rev-parse"] and "--verify" in args and any("^{commit}" in a for a in args):
            # Simulate: the ref exists as an object but is not a commit.
            return _subprocess.CompletedProcess(
                args=args, returncode=128, stdout="", stderr="fatal: Not a valid object name",
            )
        if args[:1] == ["rev-parse"] and any(a.startswith("refs/mac/fetch/") for a in args):
            # Returns a well-formed 40-hex SHA (passes hex check).
            return _ok(stdout=fetched_sha + "\n")
        if args[:2] == ["rev-parse", "HEAD"]:
            return _ok(stdout=("11" * 20) + "\n")
        if args[:1] == ["update-ref"]:
            return _ok()
        return _ok()

    monkeypatch.setattr("mac.worker._run_git", fake_run_git)
    monkeypatch.setattr("mac.worker._run_git_in", lambda cwd, args: _ok())

    worker = _make_worker(tmp_path)
    task = _repo_task_local(str(local_repo))
    lease = {"id": "lease-non-commit"}
    task_dir = tmp_path / "tasks" / "task-non-commit"
    task_dir.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="does not resolve to a commit object"):
        worker._prepare_repository_worktree(task, lease, task_dir)


def test_repo_snapshot_stores_canonical_remote_not_redacted_display() -> None:
    """Regression: the reviewer clones from evidence repo.remote_url and first
    validates it. It must be the canonical remote (git@… or clean https), NOT
    the redacted display ('…:<redacted>@…'), whose '<redacted>' fails
    _validate_git_remote_url and left every review unable to produce a verdict
    once inject_git_remote_auth began tokenizing SSH remotes."""
    from mac.worker import _repository_context_repo_snapshot, _validate_git_remote_url

    context = {
        "repository_worktree": "",
        "repository_branch": "mac/agent/task-x",
        "repository_base_sha": "0" * 40,
        "repository_canonical_remote_url": "git@github.com:jordanhubbard/mac.git",
        "repository_origin_remote": "https://x-access-token:<redacted>@github.com/jordanhubbard/mac.git",
    }
    repo = _repository_context_repo_snapshot(context)
    assert repo["remote_url"] == "git@github.com:jordanhubbard/mac.git"
    assert "<redacted>" not in repo["remote_url"]
    # The stored value survives the reviewer's validation gate.
    assert _validate_git_remote_url(repo["remote_url"]) == "git@github.com:jordanhubbard/mac.git"


def test_repo_snapshot_falls_back_to_origin_remote_when_no_canonical() -> None:
    from mac.worker import _repository_context_repo_snapshot

    context = {
        "repository_worktree": "",
        "repository_branch": "b",
        "repository_base_sha": "0" * 40,
        "repository_origin_remote": "https://github.com/org/repo.git",
    }
    assert _repository_context_repo_snapshot(context)["remote_url"] == "https://github.com/org/repo.git"


def test_manifest_enrichment_replaces_redacted_display_remote_with_canonical() -> None:
    from mac.worker import _enrich_verification_manifest_from_repository_context

    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "repo": {
            "remote_url": "https://x-access-token:<redacted>@github.com/org/repo.git",
        },
    }
    context = {
        "repository_canonical_remote_url": "git@github.com:org/repo.git",
        "repository_origin_remote": "https://x-access-token:<redacted>@github.com/org/repo.git",
    }

    enriched = _enrich_verification_manifest_from_repository_context(manifest, context)

    assert enriched["repo"]["remote_url"] == "git@github.com:org/repo.git"
    assert "<redacted>" not in enriched["repo"]["remote_url"]


def test_review_clone_prefers_task_contract_over_redacted_executor_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical_remote = "file:///tmp/canonical.git"
    redacted_remote = "https://x-access-token:<redacted>@github.com/org/repo.git"
    head_sha = "ab" * 20
    task_detail = {
        "task": {
            "id": "task-contract-remote",
            "project": "demo",
            "metadata": {
                "execution_contract": {
                    "repository_contract": {
                        "canonical_remote_url": canonical_remote,
                    }
                }
            },
        },
        "evidence": [
            {
                "id": "ev-contract-remote",
                "metadata": {
                    "verification": {
                        "repo": {
                            "head_sha": head_sha,
                            "base_sha": "cd" * 20,
                            "remote_ref": "refs/heads/mac/contract-remote",
                            "remote_url": redacted_remote,
                        }
                    }
                },
            }
        ],
    }
    commands: list[list[str]] = []

    def successful_git(argv, *args, **kwargs):
        command = list(argv)
        commands.append(command)
        if command[:3] == ["git", "clone", "--no-checkout"]:
            Path(command[-1]).mkdir(parents=True)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("mac.worker.subprocess.run", successful_git)
    worker = _make_worker(tmp_path)
    task_dir = tmp_path / "review-contract-remote"
    task_dir.mkdir()

    context = worker._prepare_review_repository_worktree(
        task_dir,
        task_detail,
        "ev-contract-remote",
        "review-contract-remote",
    )

    clone = next(command for command in commands if command[:3] == ["git", "clone", "--no-checkout"])
    assert clone[4] == canonical_remote
    assert redacted_remote not in clone
    assert context is not None
    assert context["repository_origin_remote"] == canonical_remote


def test_local_worktree_same_lease_debris_is_reclaimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worktree dir left by an interrupted prior run of the SAME lease is our
    own debris (leases are exclusive) — preparation must reclaim it and
    re-prepare, not raise. Hard-failing wedged tasks (worker_exception ->
    blocked) after every mid-attempt worker restart, observed live on every
    fleet deploy that restarted agents while they executed."""
    monkeypatch.delenv("MAC_TASK_WORKTREE_SKIP_FETCH", raising=False)
    _origin, _seed, work, sha = _make_local_git_fixture(tmp_path)

    worker = _make_worker(tmp_path)
    task = _repo_task_local(str(work))
    lease = {"id": "lease-restart-test"}
    task_dir = tmp_path / "tasks" / "task-restart"
    task_dir.mkdir(parents=True)

    first = worker._prepare_repository_worktree(task, lease, task_dir)
    assert first is not None
    stale_dir = Path(first["repository_worktree"])
    assert stale_dir.exists()
    # Leave debris behind (simulates the worker dying mid-attempt) and make it
    # distinguishable from a fresh checkout.
    (stale_dir / "half-done.txt").write_text("in progress\n", encoding="utf-8")

    second = worker._prepare_repository_worktree(task, lease, task_dir)
    assert second is not None, "same-lease debris must be reclaimed, not fatal"
    fresh_dir = Path(second["repository_worktree"])
    assert fresh_dir == stale_dir
    assert not (fresh_dir / "half-done.txt").exists(), "reclaim must re-prepare cleanly"
    head = _subprocess.run(
        ["git", "-C", str(fresh_dir), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert head == sha
