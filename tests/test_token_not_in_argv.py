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
    monkeypatch.setattr(
        gitops.Path, "is_file", lambda self: False, raising=False
    )

    url, env = gitops.askpass_remote_auth("https://github.com/o/r.git")

    assert env == {}
    assert url.startswith("https://")


def test_no_hub_git_invocation_interpolates_a_credential_into_argv():
    """A source-level guard. The failure is invisible in review -- the call
    looks ordinary, and only a live `ps` shows the token -- so the check has
    to be mechanical."""
    import re

    services = (Path(gitops.__file__).parent / "services.py").read_text(encoding="utf-8")
    offenders = []
    for match in re.finditer(r'"git",\s*"clone"[^\]]*\]', services, re.S):
        block = match.group(0)
        # auth_url from inject_git_remote_auth carries the credential; the
        # askpass form is a clean URL and is fine.
        if "inject_git_remote_auth" in services[: match.start()][-2000:]:
            offenders.append(block[:80])
    assert not offenders, (
        "a git clone is being handed a credential-bearing URL: %s" % offenders
    )
