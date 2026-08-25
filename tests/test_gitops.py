from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple
from unittest import mock

import pytest

from mac.gitops import (
    PullRequestResult,
    _api_base_for,
    _parse_owner_repo,
    detect_host,
    open_pull_request,
    redact_git_remote_auth,
    redact_git_remote_auth_in_text,
)

# imports relocated from test_gitops_edges.py
import io
import subprocess
import urllib.error
from pathlib import Path
from mac import gitops


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/foo/bar.git", "github"),
        ("https://github.com/foo/bar", "github"),
        ("https://gitea.omv.a113.casa/vpogu/ivan-plugin", "gitea"),
        ("http://gitea.local:3000/owner/repo.git", "gitea"),
    ],
)
def test_detect_host(url: str, expected: str) -> None:
    assert detect_host(url) == expected


def test_parse_owner_repo() -> None:
    assert _parse_owner_repo("https://github.com/foo/bar.git") == ("foo", "bar")
    assert _parse_owner_repo("https://gitea.omv.a113.casa/vpogu/ivan-plugin") == (
        "vpogu",
        "ivan-plugin",
    )


def test_api_base_for_github() -> None:
    assert _api_base_for("github", "https://github.com/x/y") == "https://api.github.com"


def test_api_base_for_gitea_preserves_scheme_host_port() -> None:
    assert (
        _api_base_for("gitea", "https://gitea.omv.a113.casa/x/y")
        == "https://gitea.omv.a113.casa/api/v1"
    )
    assert _api_base_for("gitea", "http://gitea.local:3000/x/y") == "http://gitea.local:3000/api/v1"


def test_redact_git_remote_auth_hides_https_password() -> None:
    url = "https://x-access-token:secret-token@github.com/org/repo.git"

    redacted = redact_git_remote_auth(url)

    assert redacted == "https://x-access-token:<redacted>@github.com/org/repo.git"
    assert "secret-token" not in redacted


def test_redact_git_remote_auth_in_text_hides_embedded_password() -> None:
    text = "fatal: Authentication failed for 'https://x-access-token:secret-token@github.com/org/repo.git/'"

    redacted = redact_git_remote_auth_in_text(text)

    assert "secret-token" not in redacted
    assert "https://x-access-token:<redacted>@github.com/org/repo.git/" in redacted


class _FakeResponse:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_: Any) -> None:
        return None


def test_open_pull_request_github_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghp_xxx")
    captured: List[Tuple[str, Dict[str, Any]]] = []

    def fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        method = req.get_method()
        if method == "POST":
            body = json.loads(req.data.decode("utf-8"))
            captured.append((url, body))
            return _FakeResponse(
                {"number": 42, "html_url": "https://github.com/x/y/pull/42", "state": "open"}
            )
        return _FakeResponse({"default_branch": "main"})

    with mock.patch("mac.gitops.urllib.request.urlopen", side_effect=fake_urlopen):
        result = open_pull_request(
            "https://github.com/x/y.git",
            head="feat/foo",
            title="My PR",
            body="body",
        )

    assert isinstance(result, PullRequestResult)
    assert result.host == "github"
    assert result.number == 42
    assert result.url == "https://github.com/x/y/pull/42"
    assert captured, "no POST captured"
    url, body = captured[-1]
    assert url == "https://api.github.com/repos/x/y/pulls"
    assert body == {"title": "My PR", "body": "body", "head": "feat/foo", "base": "main"}


def test_open_pull_request_gitea_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "abc")
    captured: List[Tuple[str, Dict[str, Any]]] = []

    def fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        method = req.get_method()
        if method == "POST":
            body = json.loads(req.data.decode("utf-8"))
            captured.append((url, body))
            return _FakeResponse(
                {"number": 7, "html_url": "https://gitea.local/x/y/pulls/7", "state": "open"}
            )
        return _FakeResponse({"default_branch": "trunk"})

    with mock.patch("mac.gitops.urllib.request.urlopen", side_effect=fake_urlopen):
        result = open_pull_request(
            "https://gitea.local/x/y",
            head="mac/agent-1/task-abc",
        )

    assert result.host == "gitea"
    url, body = captured[-1]
    assert url == "https://gitea.local/api/v1/repos/x/y/pulls"
    assert body["base"] == "trunk"
    assert body["head"] == "mac/agent-1/task-abc"


def test_open_pull_request_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(ValueError, match="GH_TOKEN"):
        open_pull_request("https://github.com/x/y", head="b")

    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    with pytest.raises(ValueError, match="GITEA_TOKEN"):
        open_pull_request("https://gitea.local/x/y", head="b")


def test_https_remote_for_token_auth_rewrites_ssh_when_token_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mac.gitops import https_remote_for_token_auth

    monkeypatch.setenv("GH_TOKEN", "gho_test")
    # scp-like and ssh:// forms both rewrite to https for token auth.
    assert (
        https_remote_for_token_auth("git@github.com:jordanhubbard/mac.git")
        == "https://github.com/jordanhubbard/mac.git"
    )
    assert (
        https_remote_for_token_auth("ssh://git@github.com/jordanhubbard/mac.git")
        == "https://github.com/jordanhubbard/mac.git"
    )
    # https and local paths pass through untouched.
    assert https_remote_for_token_auth("https://github.com/x/y.git") == "https://github.com/x/y.git"
    assert https_remote_for_token_auth("/srv/git/mac.git") == "/srv/git/mac.git"


def test_https_remote_for_token_auth_keeps_ssh_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mac.gitops import https_remote_for_token_auth

    for var in ("GH_TOKEN", "GITHUB_TOKEN", "MAC_TASK_GIT_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    assert (
        https_remote_for_token_auth("git@github.com:jordanhubbard/mac.git")
        == "git@github.com:jordanhubbard/mac.git"
    )


def test_publish_clone_composes_token_auth_from_ssh_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hub publish path: SSH evidence remote + GH_TOKEN -> authed https."""
    from mac.gitops import https_remote_for_token_auth, inject_git_remote_auth

    monkeypatch.setenv("GH_TOKEN", "gho_test")
    url = inject_git_remote_auth(
        https_remote_for_token_auth("git@github.com:jordanhubbard/mac.git")
    )
    assert url.startswith("https://x-access-token:")
    assert url.endswith("@github.com/jordanhubbard/mac.git")


def test_inject_git_remote_auth_tokenizes_ssh_remote_in_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the worker canonical-branch fetch and finalizer push pass an
    SSH-form remote straight to inject_git_remote_auth. It must return a
    token-https URL when a token exists (previously left SSH untouched ->
    Permission denied (publickey) with no ~/.ssh)."""
    from mac.gitops import inject_git_remote_auth

    monkeypatch.setenv("GH_TOKEN", "gho_worker")
    url = inject_git_remote_auth("git@github.com:jordanhubbard/mac.git")
    assert url.startswith("https://x-access-token:")
    assert url.endswith("@github.com/jordanhubbard/mac.git")
    assert "gho_worker" in url


def test_inject_git_remote_auth_leaves_ssh_when_no_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mac.gitops import inject_git_remote_auth

    for var in ("GH_TOKEN", "GITHUB_TOKEN", "MAC_TASK_GIT_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    assert inject_git_remote_auth("git@github.com:x/y.git") == "git@github.com:x/y.git"


def test_strip_git_remote_auth_removes_redacted_or_live_userinfo() -> None:
    from mac.gitops import strip_git_remote_auth

    assert (
        strip_git_remote_auth("https://x-access-token:<redacted>@github.com/org/repo.git")
        == "https://github.com/org/repo.git"
    )
    assert (
        strip_git_remote_auth("https://user:secret@forge.example:8443/org/repo.git?x=1")
        == "https://forge.example:8443/org/repo.git?x=1"
    )
    assert strip_git_remote_auth("git@github.com:org/repo.git") == "git@github.com:org/repo.git"


# --- relocated from test_gitops_edges.py (coverage companion folded in) ---


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
    for var in ("GH_TOKEN", "GITHUB_TOKEN", "GITEA_TOKEN", "GIT_TOKEN"):
        monkeypatch.delenv(var, raising=False)
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
    assert (
        gitops.inject_git_remote_auth("ssh://host/repo")
        == "https://x-access-token:fallback@host/repo"
    )
    assert gitops.inject_git_remote_auth("https:///missing") == "https:///missing"
    assert (
        gitops.inject_git_remote_auth("https://user@github.com/org/repo")
        == "https://user@github.com/org/repo"
    )
    monkeypatch.delenv("MAC_TASK_GIT_TOKEN", raising=False)
    assert (
        gitops.inject_git_remote_auth("https://github.com/org/repo")
        == "https://github.com/org/repo"
    )
    assert gitops.redact_git_remote_auth("ssh://host/repo") == "ssh://host/repo"
    assert gitops.redact_git_remote_auth("https://host/repo") == "https://host/repo"
    assert gitops.redact_git_remote_auth("https://user@host/repo") == "https://<redacted>@host/repo"


def test_run_git_oserror_and_common_directory_paths(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        gitops.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(OSError("missing"))
    )
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
            worktree=missing,
            canonical_remote="/remote",
            canonical_branch="main",
            destination_branch="task",
            prepared_base_sha="a" * 40,
            isolation_key="key",
        )
    monkeypatch.setattr(gitops, "_run_git", lambda *_a: _cp(1))
    with pytest.raises(ValueError, match="not a git worktree"):
        gitops.resolve_canonical_publication_target(
            worktree=tmp_path,
            canonical_remote="/remote",
            canonical_branch="main",
            destination_branch="task",
            prepared_base_sha="a" * 40,
            isolation_key="key",
        )
    results = iter([_cp(stdout="true"), _cp(1)])
    monkeypatch.setattr(gitops, "_run_git", lambda *_a: next(results))
    with pytest.raises(ValueError, match="check-ref-format"):
        gitops.resolve_canonical_publication_target(
            worktree=tmp_path,
            canonical_remote="/remote",
            canonical_branch="main",
            destination_branch="task",
            prepared_base_sha="a" * 40,
            isolation_key="key",
        )


@pytest.mark.parametrize(
    ("results", "message"),
    [
        ([_cp(stdout="true"), _cp(), _cp(), _cp(1)], "prepared repository base is not a commit"),
        (
            [_cp(stdout="true"), _cp(), _cp(), _cp(stdout="a" * 40), _cp(1, stderr="head")],
            "could not resolve task HEAD",
        ),
        (
            [_cp(stdout="true"), _cp(), _cp(), _cp(stdout="a" * 40), _cp(stdout="b" * 40), _cp(1)],
            "not an ancestor",
        ),
        (
            [
                _cp(stdout="true"),
                _cp(),
                _cp(),
                _cp(stdout="a" * 40),
                _cp(stdout="b" * 40),
                _cp(),
                _cp(1),
            ],
            "isolated publication ref",
        ),
    ],
)
def test_resolve_publication_target_git_failures(monkeypatch, tmp_path, results, message) -> None:
    monkeypatch.setattr(gitops, "_run_git", lambda *_a: results.pop(0))
    with pytest.raises(ValueError, match=message):
        gitops.resolve_canonical_publication_target(
            worktree=tmp_path,
            canonical_remote="/remote",
            canonical_branch="main",
            destination_branch="task",
            prepared_base_sha="a" * 40,
            isolation_key="key",
        )


@pytest.mark.parametrize(
    ("results", "push", "message"),
    [
        ([_cp(1, stderr="head")], False, "resolve task HEAD"),
        ([_cp(stdout="c" * 40)], False, "HEAD changed"),
        ([_cp(stdout="b" * 40), _cp(1)], False, "prepared repository base"),
        ([_cp(stdout="b" * 40), _cp(), _cp(1, stderr="fetch"), _cp()], False, "fetch of canonical"),
        (
            [_cp(stdout="b" * 40), _cp(), _cp(), _cp(1, stderr="ref"), _cp()],
            False,
            "did not resolve",
        ),
        (
            [
                _cp(stdout="b" * 40),
                _cp(),
                _cp(),
                _cp(stdout="c" * 40),
                _cp(),
                _cp(1, stderr="diff"),
                _cp(),
            ],
            False,
            "compute canonical diff",
        ),
        (
            [
                _cp(stdout="b" * 40),
                _cp(),
                _cp(),
                _cp(stdout="c" * 40),
                _cp(),
                _cp(stdout="file\n"),
                _cp(),
                _cp(1, stderr="push"),
            ],
            True,
            "git push",
        ),
        (
            [
                _cp(stdout="b" * 40),
                _cp(),
                _cp(),
                _cp(stdout="c" * 40),
                _cp(),
                _cp(stdout="file\n"),
                _cp(),
                _cp(stdout="ok"),
                _cp(stdout="wrong refs/heads/task"),
            ],
            True,
            "remote branch verification",
        ),
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
    monkeypatch.setattr(
        gitops,
        "_canonical_freshness_locked",
        lambda *_a, **_k: gitops.CanonicalFreshnessResult(True, target, head_sha="b" * 40),
    )
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
    monkeypatch.setattr(
        gitops.urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(error)
    )
    with pytest.raises(RuntimeError, match="already exists"):
        gitops._http_post_json("https://api", {}, {})
    monkeypatch.setattr(
        gitops,
        "_http_post_json",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("pull request already exists")),
    )
    existing = gitops.PullRequestResult("github", 1, "url", "open")
    monkeypatch.setattr(gitops, "_find_existing_pr", lambda *_a: existing)
    assert (
        gitops.open_pull_request(
            "https://github.com/org/repo", "task", base="main", github_token="token"
        )
        is existing
    )
    monkeypatch.setattr(gitops, "_find_existing_pr", lambda *_a: None)
    with pytest.raises(RuntimeError, match="already exists"):
        gitops.open_pull_request(
            "https://github.com/org/repo", "task", base="main", github_token="token"
        )


def test_find_existing_pr_shapes_and_gitea(monkeypatch) -> None:
    monkeypatch.setattr(
        gitops.urllib.request,
        "urlopen",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("offline")),
    )
    assert gitops._find_existing_pr("https://api", {}, "o", "r", "github", "head", "main") is None
    monkeypatch.setattr(
        gitops.urllib.request, "urlopen", lambda *_a, **_k: _Response({"not": "list"})
    )
    assert gitops._find_existing_pr("https://api", {}, "o", "r", "github", "head", "main") is None
    payload = ["bad", {"head": "bad"}, {"head": {"ref": "head"}, "number": 7, "url": "pr"}]
    monkeypatch.setattr(gitops.urllib.request, "urlopen", lambda *_a, **_k: _Response(payload))
    result = gitops._find_existing_pr("https://api", {}, "o", "r", "gitea", "head", "main")
    assert result.number == 7 and result.host == "gitea"
