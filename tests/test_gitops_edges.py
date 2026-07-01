"""Failure and recovery coverage for deterministic git publication."""

from __future__ import annotations

import io
import json
import subprocess
import urllib.error
from pathlib import Path

import pytest

from mac import gitops


def _cp(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def _target(tmp_path: Path, **extra):
    values = {
        "worktree": tmp_path,
        "canonical_remote_url": "https://github.com/org/repo.git",
        "remote": "https://github.com/org/repo.git",
        "remote_display": "https://github.com/org/repo.git",
        "canonical_branch": "main",
        "destination_branch": "task/change",
        "prepared_base_sha": "a" * 40,
        "task_head_sha": "b" * 40,
        "isolated_ref": "refs/mac/publication/test",
        "git_common_dir": tmp_path,
        "lock_path": tmp_path / "lock",
    }
    values.update(extra)
    return gitops.CanonicalPublicationTarget(**values)


def test_ref_host_token_auth_and_redaction_edges(monkeypatch) -> None:
    with pytest.raises(ValueError, match="exceeds"):
        gitops.validate_git_ref("x" * 513)
    for ref in ("/bad", "bad/", "bad.", "bad..ref", "bad//ref", "bad.lock"):
        with pytest.raises(ValueError, match="invalid shape"):
            gitops.validate_git_ref(ref)
    with pytest.raises(ValueError, match="disallowed"):
        gitops.validate_git_ref("bad@{ref")
    with pytest.raises(ValueError, match="parse host"):
        gitops.detect_host("/")
    monkeypatch.setenv("MAC_TASK_GIT_TOKEN", "fallback")
    assert gitops.token_for_host("other") == "fallback"
    assert gitops.inject_git_remote_auth("ssh://host/repo") == "ssh://host/repo"
    assert gitops.inject_git_remote_auth("https:///missing") == "https:///missing"
    assert gitops.inject_git_remote_auth("https://user@github.com/org/repo") == "https://user@github.com/org/repo"
    monkeypatch.delenv("MAC_TASK_GIT_TOKEN", raising=False)
    assert gitops.inject_git_remote_auth("https://github.com/org/repo") == "https://github.com/org/repo"
    assert gitops.redact_git_remote_auth("ssh://host/repo") == "ssh://host/repo"
    assert gitops.redact_git_remote_auth("https://host/repo") == "https://host/repo"
    assert gitops.redact_git_remote_auth("https://user@host/repo") == "https://<redacted>@host/repo"


def test_run_git_oserror_and_common_directory_paths(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gitops.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(OSError("missing")))
    assert gitops._run_git(tmp_path, ["status"]).returncode == 127
    monkeypatch.setattr(gitops, "_run_git", lambda *_a: _cp(1, stderr="bad"))
    assert gitops._git_common_directory(tmp_path)[0] is None
    monkeypatch.setattr(gitops, "_run_git", lambda *_a: _cp(stdout="missing-dir"))
    assert "not a directory" in gitops._git_common_directory(tmp_path)[1]
    (tmp_path / ".git-common").mkdir()
    monkeypatch.setattr(gitops, "_run_git", lambda *_a: _cp(stdout=".git-common"))
    assert gitops._git_common_directory(tmp_path)[0] == (tmp_path / ".git-common").resolve()


def test_resolve_publication_target_validation_sequence(monkeypatch, tmp_path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(ValueError, match="worktree is missing"):
        gitops.resolve_canonical_publication_target(
            worktree=missing, canonical_remote="/remote", canonical_branch="main",
            destination_branch="task", prepared_base_sha="a" * 40, isolation_key="key"
        )
    monkeypatch.setattr(gitops, "_run_git", lambda *_a: _cp(1))
    with pytest.raises(ValueError, match="not a git worktree"):
        gitops.resolve_canonical_publication_target(
            worktree=tmp_path, canonical_remote="/remote", canonical_branch="main",
            destination_branch="task", prepared_base_sha="a" * 40, isolation_key="key"
        )
    results = iter([_cp(stdout="true"), _cp(1)])
    monkeypatch.setattr(gitops, "_run_git", lambda *_a: next(results))
    with pytest.raises(ValueError, match="check-ref-format"):
        gitops.resolve_canonical_publication_target(
            worktree=tmp_path, canonical_remote="/remote", canonical_branch="main",
            destination_branch="task", prepared_base_sha="a" * 40, isolation_key="key"
        )


@pytest.mark.parametrize(
    ("results", "message"),
    [
        ([_cp(stdout="true"), _cp(), _cp(), _cp(1)], "prepared repository base is not a commit"),
        ([_cp(stdout="true"), _cp(), _cp(), _cp(stdout="a" * 40), _cp(1, stderr="head")], "could not resolve task HEAD"),
        ([_cp(stdout="true"), _cp(), _cp(), _cp(stdout="a" * 40), _cp(stdout="b" * 40), _cp(1)], "not an ancestor"),
        ([_cp(stdout="true"), _cp(), _cp(), _cp(stdout="a" * 40), _cp(stdout="b" * 40), _cp(), _cp(1)], "isolated publication ref"),
    ],
)
def test_resolve_publication_target_git_failures(monkeypatch, tmp_path, results, message) -> None:
    monkeypatch.setattr(gitops, "_run_git", lambda *_a: results.pop(0))
    with pytest.raises(ValueError, match=message):
        gitops.resolve_canonical_publication_target(
            worktree=tmp_path, canonical_remote="/remote", canonical_branch="main",
            destination_branch="task", prepared_base_sha="a" * 40, isolation_key="key"
        )


@pytest.mark.parametrize(
    ("results", "push", "message"),
    [
        ([_cp(1, stderr="head")], False, "resolve task HEAD"),
        ([_cp(stdout="c" * 40)], False, "HEAD changed"),
        ([_cp(stdout="b" * 40), _cp(1)], False, "prepared repository base"),
        ([_cp(stdout="b" * 40), _cp(), _cp(1, stderr="fetch"), _cp()], False, "fetch of canonical"),
        ([_cp(stdout="b" * 40), _cp(), _cp(), _cp(1, stderr="ref"), _cp()], False, "did not resolve"),
        ([_cp(stdout="b" * 40), _cp(), _cp(), _cp(stdout="c" * 40), _cp(), _cp(1, stderr="diff"), _cp()], False, "compute canonical diff"),
        ([_cp(stdout="b" * 40), _cp(), _cp(), _cp(stdout="c" * 40), _cp(), _cp(stdout="file\n"), _cp(), _cp(1, stderr="push")], True, "git push"),
        ([_cp(stdout="b" * 40), _cp(), _cp(), _cp(stdout="c" * 40), _cp(), _cp(stdout="file\n"), _cp(), _cp(stdout="ok"), _cp(stdout="wrong refs/heads/task")], True, "remote branch verification"),
    ],
)
def test_canonical_freshness_failure_matrix(monkeypatch, tmp_path, results, push, message) -> None:
    monkeypatch.setattr(gitops, "_run_git", lambda *_a: results.pop(0))
    result = gitops._canonical_freshness_locked(_target(tmp_path), push=push)
    assert result.ok is False
    assert message in result.error


def test_canonical_publication_lock_open_and_release_failures(monkeypatch, tmp_path) -> None:
    target = _target(tmp_path, lock_path=tmp_path / "missing" / "lock")
    result = gitops._canonical_publication_operation(target, push=False)
    assert "could not open publication lock" in result.error

    target = _target(tmp_path)
    monkeypatch.setattr(gitops, "_canonical_freshness_locked", lambda *_a, **_k: gitops.CanonicalFreshnessResult(True, target, head_sha="b" * 40))
    calls = []
    def flock(_file, operation):
        calls.append(operation)
        if operation == gitops.fcntl.LOCK_UN:
            raise OSError("unlock")
    monkeypatch.setattr(gitops.fcntl, "flock", flock)
    result = gitops._canonical_publication_operation(target, push=False)
    assert "release publication lock" in result.error


class _Response:
    def __init__(self, payload):
        self.payload = payload
    def read(self):
        return json.dumps(self.payload).encode()
    def __enter__(self):
        return self
    def __exit__(self, *_a):
        return None


def test_http_post_error_and_pull_request_existing_paths(monkeypatch) -> None:
    error = urllib.error.HTTPError("url", 422, "invalid", {}, io.BytesIO(b"already exists"))
    monkeypatch.setattr(gitops.urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(error))
    with pytest.raises(RuntimeError, match="already exists"):
        gitops._http_post_json("https://api", {}, {})

    monkeypatch.setattr(gitops, "_http_post_json", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("pull request already exists")))
    existing = gitops.PullRequestResult("github", 1, "url", "open")
    monkeypatch.setattr(gitops, "_find_existing_pr", lambda *_a: existing)
    assert gitops.open_pull_request(
        "https://github.com/org/repo", "task", base="main", github_token="token"
    ) is existing
    monkeypatch.setattr(gitops, "_find_existing_pr", lambda *_a: None)
    with pytest.raises(RuntimeError, match="already exists"):
        gitops.open_pull_request(
            "https://github.com/org/repo", "task", base="main", github_token="token"
        )


def test_find_existing_pr_shapes_and_gitea(monkeypatch) -> None:
    monkeypatch.setattr(gitops.urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(OSError("offline")))
    assert gitops._find_existing_pr("https://api", {}, "o", "r", "github", "head", "main") is None
    monkeypatch.setattr(gitops.urllib.request, "urlopen", lambda *_a, **_k: _Response({"not": "list"}))
    assert gitops._find_existing_pr("https://api", {}, "o", "r", "github", "head", "main") is None
    payload = ["bad", {"head": "bad"}, {"head": {"ref": "head"}, "number": 7, "url": "pr"}]
    monkeypatch.setattr(gitops.urllib.request, "urlopen", lambda *_a, **_k: _Response(payload))
    result = gitops._find_existing_pr("https://api", {}, "o", "r", "gitea", "head", "main")
    assert result.number == 7 and result.host == "gitea"
