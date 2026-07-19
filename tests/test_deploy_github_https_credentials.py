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


def _run(
    tmp_path: Path,
    token: str = "token",
    gh_exit: int = 0,
    deploy_token: str = "",
):
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
    if deploy_token:
        env["MAC_DEPLOY_GH_TOKEN"] = deploy_token
    else:
        env.pop("MAC_DEPLOY_GH_TOKEN", None)
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
        "status",
        "--hostname",
        "github.com",
    ]
    assert "credential verified without changing host Git configuration" in result.stdout


def test_promotes_secret_stream_deploy_token_before_required_preflight(
    tmp_path: Path,
) -> None:
    result, calls = _run(tmp_path, token="", deploy_token="streamed-token")
    assert result.returncode == 0
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "auth",
        "status",
        "--hostname",
        "github.com",
    ]
    assert "credential verified" in result.stdout
    assert "streamed-token" not in result.stdout + result.stderr


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


def test_typed_phase2_consumes_credential_receipt_before_source_replacement() -> None:
    script = (
        (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "deploy" / "fleet-node-install.sh").read_text(encoding="utf-8")
    )
    pre_mutation = script.split(
        'if [ "$NODE_ACTION" = arm-phase2 ] || [ "$NODE_ACTION" = apply-phase2 ]; then',
        1,
    )[1].split("capture_darwin_launchd_prestate", 1)[0]
    assert pre_mutation.index("validate_typed_prerequisite_bundle") < pre_mutation.index(
        "configure_github_https_credentials"
    )
    after_intent = script.split('write_deploy_manifest "pre" "$MANIFEST_PRE"', 1)[1]
    assert "configure_github_https_credentials" not in after_intent
