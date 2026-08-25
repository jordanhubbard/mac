from __future__ import annotations

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _function_source() -> str:
    deploy = (
        (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "deploy" / "fleet-node-install.sh").read_text(encoding="utf-8")
    )
    start = deploy.index("remove_managed_github_review_key_config()")
    end = deploy.index("# On a brand-new spoke", start)
    return deploy[start:end]


def _mock_ssh_tools(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tools = {
        "ssh": """#!/usr/bin/env bash
if printf '%s\n' "$@" | grep -q 'mac_github_review_id'; then
  mode="${MOCK_CANDIDATE_AUTH:-fail}"
else
  mode="${MOCK_AMBIENT_AUTH:-fail}"
fi
if [ "$mode" = pass ]; then
  echo "Hi test! You've successfully authenticated, but GitHub does not provide shell access."
  exit 1
fi
echo 'git@github.com: Permission denied (publickey).' >&2
exit 255
""",
    }
    for name, content in tools.items():
        path = bin_dir / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
    return bin_dir


def _run_installer(
    tmp_path: Path,
    *,
    candidate_auth: str,
    ambient_auth: str,
    agent: str = "hub",
    manager: str = "hub",
) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(parents=True)
    (ssh_dir / "config").write_text(
        "Host keep.example\n  User keep\n\n"
        "# mac GitHub review deploy key\n"
        "Host github.com\n"
        "  IdentityFile ~/.ssh/mac_github_review_id\n"
        "  IdentitiesOnly yes\n",
        encoding="utf-8",
    )
    (ssh_dir / "mac_github_review_id").write_bytes(b"onboarded-key")
    mock_bin = _mock_ssh_tools(tmp_path)
    script = (
        "set -euo pipefail\n"
        "log(){ printf '%s\\n' \"$*\"; }\n" + _function_source() + "\ninstall_github_review_key\n"
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{mock_bin}:{os.environ['PATH']}",
        "GITHUB_REVIEW_KEY_B64": "ignored-secret-stream-candidate",
        "MOCK_CANDIDATE_AUTH": candidate_auth,
        "MOCK_AMBIENT_AUTH": ambient_auth,
        "AGENT": agent,
        "SHARED_SERVICES_MANAGER_AGENT": manager,
    }
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_unverified_onboarded_key_falls_back_without_rewriting_ambient_identity(tmp_path):
    result = _run_installer(
        tmp_path,
        candidate_auth="fail",
        ambient_auth="pass",
    )

    assert result.returncode == 0, result.stderr
    config = (tmp_path / "home" / ".ssh" / "config").read_text(encoding="utf-8")
    assert "Host keep.example" in config
    assert config.count("# mac GitHub review deploy key") == 1
    assert (tmp_path / "home" / ".ssh" / "mac_github_review_id").read_bytes() == b"onboarded-key"
    assert "verified onboarded ambient GitHub SSH identity" in result.stdout


def test_hub_deploy_fails_early_when_no_github_identity_is_authorized(tmp_path):
    result = _run_installer(
        tmp_path,
        candidate_auth="fail",
        ambient_auth="fail",
    )

    assert result.returncode != 0
    assert "hub cannot authenticate to github.com for review publication" in result.stderr


def test_authorized_onboarded_review_key_is_verified_without_rewrite(tmp_path):
    result = _run_installer(
        tmp_path,
        candidate_auth="pass",
        ambient_auth="fail",
    )

    assert result.returncode == 0, result.stderr
    key = tmp_path / "home" / ".ssh" / "mac_github_review_id"
    assert key.read_bytes() == b"onboarded-key"
    config = (tmp_path / "home" / ".ssh" / "config").read_text(encoding="utf-8")
    assert config.count("# mac GitHub review deploy key") == 1
    assert "IdentityFile ~/.ssh/mac_github_review_id" in config
    assert "IdentitiesOnly yes" in config
    assert "verified onboarded GitHub review identity" in result.stdout


def test_github_auth_probe_cannot_consume_remote_deploy_stdin():
    source = _function_source()

    assert "ssh -n -F /dev/null" in source
