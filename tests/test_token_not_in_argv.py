"""A git credential must not reach argv.

Observed live on the hub 2026-08-11, in `ps` output readable by any user on
the box:

    git clone --no-tags --branch main -- https://x-access-token:<PAT>@github.com/...

git_askpass.py exists precisely to prevent that -- its docstring says the
credential "never enters argv, repository config, the task ledger, or a
persistent credential store" -- but the hub publish and hub verify paths built
their URL with inject_git_remote_auth and passed it straight to git.

The exposure is not theoretical: it burned a freshly rotated token the same
day it was installed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from mac import gitops


@pytest.fixture(autouse=True)
def _github_token(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_TESTTOKENVALUE0000000000000000000000")


def test_the_url_carries_no_credential(monkeypatch, tmp_path):
    askpass = Path(sys.executable).with_name("mac-git-askpass")
    if not (askpass.is_file() and os.access(askpass, os.X_OK)):
        pytest.skip("askpass helper not installed in this environment")

    url, env = gitops.askpass_remote_auth("https://github.com/jordanhubbard/mac.git")

    assert "ghp_" not in url
    assert "x-access-token" not in url
    assert env["GH_TOKEN"].startswith("ghp_")
    assert env["GIT_ASKPASS"].endswith("mac-git-askpass")


def test_a_remote_with_no_token_is_untouched(monkeypatch):
    """SSH remotes and public clones must behave exactly as before."""
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    url, env = gitops.askpass_remote_auth("https://github.com/public/repo.git")

    assert env == {}
    assert url == "https://github.com/public/repo.git"


def test_a_url_that_already_has_credentials_is_left_alone():
    """Rewriting one would either duplicate or silently replace the caller's
    own credential."""
    given = "https://someone:secret@github.com/o/r.git"

    url, env = gitops.askpass_remote_auth(given)

    assert url == given
    assert env == {}


def test_the_helper_degrades_rather_than_failing(monkeypatch):
    """A missing askpass helper must not stop a publication. It falls back to
    the old URL form -- still redacted in logs and evidence, exposed only in
    argv, which is exactly the pre-existing behaviour."""
    monkeypatch.setattr(gitops.Path, "is_file", lambda self: False, raising=False)

    url, env = gitops.askpass_remote_auth("https://github.com/o/r.git")

    assert env == {}
    assert url.startswith("https://")


def test_the_hub_never_builds_a_credential_bearing_url():
    """A source-level guard, because the failure is invisible in review: the
    call looks ordinary and only a live `ps` shows the token.

    The previous form of this test scanned for a literal `"git", "clone"`
    argv list. The publish path builds `["clone", ...]` and lets its runner
    prepend `git`, so the pattern sailed straight past the very call that had
    been leaking -- observed live on the hub on 2026-08-13, two days after the
    verify path was fixed:

        git clone --no-tags --branch main -- https://x-access-token:<PAT>@...

    So the rule is now about the credential, not about how the argv happens to
    be spelled: the hub does not construct a URL with a credential in it at
    all. askpass_remote_auth returns a clean URL plus the environment that
    authenticates it, which is the only form that cannot reach argv.
    """

    services = (Path(gitops.__file__).parent / "services.py").read_text(encoding="utf-8")

    assert "inject_git_remote_auth" not in services, (
        "services.py builds a credential-bearing URL; use "
        "gitops.askpass_remote_auth, which returns a clean URL and the "
        "environment that authenticates it"
    )
