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
    assert (
        _api_base_for("gitea", "http://gitea.local:3000/x/y")
        == "http://gitea.local:3000/api/v1"
    )


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
    assert (
        https_remote_for_token_auth("https://github.com/x/y.git")
        == "https://github.com/x/y.git"
    )
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
    assert (
        inject_git_remote_auth("git@github.com:x/y.git")
        == "git@github.com:x/y.git"
    )


def test_strip_git_remote_auth_removes_redacted_or_live_userinfo() -> None:
    from mac.gitops import strip_git_remote_auth

    assert strip_git_remote_auth(
        "https://x-access-token:<redacted>@github.com/org/repo.git"
    ) == "https://github.com/org/repo.git"
    assert strip_git_remote_auth(
        "https://user:secret@forge.example:8443/org/repo.git?x=1"
    ) == "https://forge.example:8443/org/repo.git?x=1"
    assert strip_git_remote_auth("git@github.com:org/repo.git") == "git@github.com:org/repo.git"
