from __future__ import annotations

import os
import subprocess
from pathlib import Path

from mac.landing_service import LandingService, RepositoryEndpoint
from mac.work_package_pipeline_runtime import controller_git_credential_environment


def _isolated_git_environment(tmp_path: Path, token: str) -> dict[str, str]:
    credentials = controller_git_credential_environment(
        "write",
        {"source": "https://github.com/jordanhubbard/mac.git"},
        environ={
            "GH_TOKEN": token,
            "GIT_ASKPASS": "/tmp/untrusted-ambient-helper",
            "GIT_CONFIG_COUNT": "1",
        },
    )
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
        **credentials,
    }


def test_package_askpass_fills_github_credentials_with_isolated_home(
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
    fields = dict(
        line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line
    )
    assert fields["username"] == "x-access-token"
    assert fields["password"] == token
    assert all(token not in argument for argument in completed.args)
    assert environment["GIT_ASKPASS"] != "/tmp/untrusted-ambient-helper"
    assert "GIT_CONFIG_COUNT" not in environment


def test_package_askpass_rejects_non_github_prompt_without_disclosing_token(
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


def test_package_askpass_reads_only_gh_token(tmp_path: Path) -> None:
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


def test_landing_push_keeps_token_out_of_argv_and_uses_isolated_git_home(
    tmp_path: Path,
) -> None:
    token = "github-test-token-that-must-not-enter-argv"

    class RecordingRunner:
        def __init__(self) -> None:
            self.args: list[str] = []
            self.env: dict[str, str] = {}

        def run(self, args, *, cwd, env):
            self.args = list(args)
            self.env = dict(env)
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="", stderr=""
            )

    runner = RecordingRunner()
    endpoint = RepositoryEndpoint("repo", "https://github.com/jordanhubbard/mac.git")
    service = LandingService(
        object(),
        owner="credential-test",
        git_runner=runner,
        credential_environment=lambda operation, repository: (
            controller_git_credential_environment(
                operation,
                repository,
                environ={
                    "GH_TOKEN": token,
                    "GIT_ASKPASS": "/tmp/untrusted-ambient-helper",
                    "GIT_CONFIG_COUNT": "1",
                },
            )
        ),
    )
    service._git(
        [
            "push",
            "--dry-run",
            endpoint.remote_url,
            "HEAD:refs/mac/credential-test",
        ],
        cwd=tmp_path,
        endpoint=endpoint,
        operation="write",
    )

    assert all(token not in argument for argument in runner.args)
    assert all("x-access-token" not in argument for argument in runner.args)
    assert runner.env["GH_TOKEN"] == token
    assert Path(runner.env["GIT_ASKPASS"]).name == "mac-git-askpass"
    assert runner.env["HOME"] != os.environ.get("HOME")
    assert runner.env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert runner.env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_CONFIG_COUNT" not in runner.env


def test_landing_git_error_redacts_authenticated_url() -> None:
    token = "github-test-token-that-must-stay-secret"
    result = subprocess.CompletedProcess(
        args=["git", "push"],
        returncode=128,
        stdout="",
        stderr=(
            "fatal: https://x-access-token:%s@github.com/jordanhubbard/mac.git failed"
            % token
        ),
    )

    detail = LandingService._git_error("canonical push", result)
    assert token not in detail
    assert "<redacted>" in detail
