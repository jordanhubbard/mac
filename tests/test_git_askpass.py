"""The controller-owned GitHub askpass helper.

The helper exists so a GitHub token reaches ``git`` through the child
environment only -- never argv, repository config, the task ledger, or a
persistent credential store.  The work-package landing service that used to be
its only in-tree caller is gone; ``mac.gitops.askpass_remote_auth`` (covered by
tests/test_token_not_in_argv.py) is the live one.  These tests pin the helper
binary's own behaviour, which is what keeps the token out of argv.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from mac.git_askpass import main


def _askpass_binary() -> Path:
    askpass = Path(sys.executable).with_name("mac-git-askpass")
    if not (askpass.is_file() and os.access(askpass, os.X_OK)):
        pytest.skip("askpass helper not installed in this environment")
    return askpass


def _isolated_git_environment(tmp_path: Path, token: str) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "xdg"),
        "LANG": "C",
        "LC_ALL": "C",
        "GCM_INTERACTIVE": "Never",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GH_TOKEN": token,
        "GIT_ASKPASS": str(_askpass_binary()),
    }


def test_askpass_fills_github_credentials_with_isolated_home(
    tmp_path: Path,
) -> None:
    token = "github-test-token-that-must-not-enter-argv"
    environment = _isolated_git_environment(tmp_path, token)
    completed = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    fields = dict(line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line)
    assert fields["username"] == "x-access-token"
    assert fields["password"] == token
    assert all(token not in argument for argument in completed.args)


def test_askpass_rejects_non_github_prompt_without_disclosing_token(
    tmp_path: Path,
) -> None:
    token = "github-test-token-that-must-stay-secret"
    completed = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=example.invalid\n\n",
        text=True,
        capture_output=True,
        check=False,
        env=_isolated_git_environment(tmp_path, token),
    )

    assert completed.returncode != 0
    assert token not in completed.stdout
    assert token not in completed.stderr


def test_askpass_reads_only_gh_token(tmp_path: Path) -> None:
    environment = _isolated_git_environment(tmp_path, "temporary")
    environment.pop("GH_TOKEN")
    environment["GITHUB_TOKEN"] = "wrong-token-source"
    completed = subprocess.run(
        [environment["GIT_ASKPASS"], "Username for 'https://github.com': "],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert "wrong-token-source" not in completed.stderr


def test_helper_answers_only_well_formed_single_github_prompts(monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "secret-token")

    assert main(["Username for 'https://github.com': "]) == 0
    assert main(["Password for 'https://x-access-token@github.com': "]) == 0
    # Not GitHub, wrong arity, or an unrecognised prompt: refuse.
    assert main(["Password for 'https://gitlab.com': "]) == 1
    assert main([]) == 1
    assert main(["a", "b"]) == 1
    assert main(["something unrelated"]) == 1


def test_helper_refuses_a_malformed_token(monkeypatch) -> None:
    for bad in ("", " padded ", "with\nnewline", "with\rreturn", "with\x00nul"):
        # os.environ refuses a NUL, so patch the mapping the helper reads.
        monkeypatch.setattr(os, "environ", {**os.environ, "GH_TOKEN": bad})
        assert main(["Password for 'https://github.com': "]) == 1
