from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _function() -> str:
    script = (
        (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "deploy" / "fleet-node-install.sh").read_text(encoding="utf-8")
    )
    start = script.index("configure_github_https_credentials()")
    end = script.index("\n# On a brand-new spoke", start)
    return script[start:end]


def _run(tmp_path: Path, token: str = "token", gh_exit: int = 0):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    calls = tmp_path / "calls"
    gh = bin_dir / "gh"
    gh.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > "{calls}"\nexit {gh_exit}\n',
        encoding="utf-8",
    )
    gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin"}
    if token:
        env["GH_TOKEN"] = token
    else:
        env.pop("GH_TOKEN", None)
    result = subprocess.run(
        [
            "bash",
            "-c",
            'log(){ printf "%s\\n" "$*"; }; '
            + _function()
            + "\nconfigure_github_https_credentials",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    return result, calls


def test_configures_gh_as_scoped_https_credential_helper(tmp_path: Path) -> None:
    result, calls = _run(tmp_path)
    assert result.returncode == 0
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "auth",
        "setup-git",
        "--hostname",
        "github.com",
    ]
    assert "credential helper configured" in result.stdout


def test_skips_without_token_and_never_logs_secret(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, token="")
    assert result.returncode == 0
    assert not calls.exists()
    assert "GH_TOKEN absent" in result.stdout

    secret = "credential-value-must-not-appear"
    result, _ = _run(tmp_path / "secret", token=secret)
    assert secret not in result.stdout + result.stderr


def test_setup_failure_is_nonfatal(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, gh_exit=1)
    assert result.returncode == 0
    assert "WARNING" in result.stdout


def test_deploy_configures_https_after_ssh_review_key() -> None:
    script = (
        (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "deploy" / "fleet-node-install.sh").read_text(encoding="utf-8")
    )
    assert "install_github_review_key\nconfigure_github_https_credentials\n" in script
