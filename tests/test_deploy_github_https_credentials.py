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
    resolver_start = script.index("onboarded_command_path()")
    resolver_end = script.index("\ninstall_fleet_registry()", resolver_start)
    configure_start = script.index("github_credentials_are_required()")
    configure_end = script.index("\n# On a brand-new spoke", configure_start)
    return (
        script[resolver_start:resolver_end]
        + "\n"
        + script[configure_start:configure_end]
    )


def _strip_comments(script: str) -> str:
    return "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )


def _run(
    tmp_path: Path,
    token: str = "token",
    gh_exit: int = 0,
    deploy_token: str = "",
    agent: str = "spoke",
    manager: str = "hub",
    credentials_required: str = "0",
    install_gh: bool = True,
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    calls = tmp_path / "calls"
    gh = bin_dir / "gh"
    if install_gh:
        gh.write_text(
            f'#!/bin/sh\nprintf "%s\\n" "$@" > "{calls}"\nexit {gh_exit}\n',
            encoding="utf-8",
        )
        gh.chmod(0o755)
    # An empty onboarded PATH is how "gh was never onboarded" is expressed; the
    # ambient /usr/bin must not leak a real gh into that case.
    onboarded = f"{bin_dir}:/usr/bin:/bin" if install_gh else str(bin_dir)
    env = {
        **os.environ,
        "ONBOARDED_COMMAND_PATH": onboarded,
        "PATH": "/usr/bin:/bin",
        "AGENT": agent,
        "SHARED_SERVICES_MANAGER_AGENT": manager,
        "GITHUB_CREDENTIALS_REQUIRED": credentials_required,
    }
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


# ---------------------------------------------------------------------------
# The hub's GitHub gate. It used to be install_github_review_key(), which
# required `ssh -T git@github.com` to succeed against ~/.ssh/mac_github_review_id
# — a credential no push, PR-open, or remote-config path in this tree ever read.
# It hard-failed first-hub bootstrap one log line after the HTTPS token had
# already been verified. The gate now tests the credential the hub actually
# publishes reviews with.
# ---------------------------------------------------------------------------


def test_hub_bootstrap_succeeds_on_https_token_alone(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, agent="hub", manager="hub")

    assert result.returncode == 0, result.stderr
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "auth",
        "status",
        "--hostname",
        "github.com",
    ]
    assert "credential verified" in result.stdout


def test_hub_bootstrap_fails_closed_without_a_github_token(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, token="", agent="hub", manager="hub")

    assert result.returncode != 0
    assert not calls.exists()
    assert "GH_TOKEN absent" in result.stdout
    assert "the hub publishes reviews to github.com over HTTPS" in result.stdout
    assert "MAC_DEPLOY_GH_TOKEN / GH_TOKEN" in result.stderr


def test_hub_bootstrap_fails_closed_when_github_rejects_the_token(
    tmp_path: Path,
) -> None:
    result, _ = _run(tmp_path, gh_exit=1, agent="hub", manager="hub")

    assert result.returncode != 0
    assert "GitHub rejected it" in result.stdout
    assert "the hub publishes reviews to github.com over HTTPS" in result.stdout


def test_hub_bootstrap_fails_closed_without_an_onboarded_gh_cli(
    tmp_path: Path,
) -> None:
    result, _ = _run(tmp_path, agent="hub", manager="hub", install_gh=False)

    assert result.returncode != 0
    assert "gh CLI not found" in result.stdout


def test_single_node_deploy_is_its_own_hub_and_is_gated(tmp_path: Path) -> None:
    # SHARED_SERVICES_MANAGER_AGENT defaults to AGENT on a single-node deploy.
    result, _ = _run(tmp_path, token="", agent="solo", manager="solo")

    assert result.returncode != 0
    assert "the hub publishes reviews to github.com over HTTPS" in result.stdout


def test_spoke_without_a_token_still_proceeds(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, token="", agent="spoke", manager="hub")

    assert result.returncode == 0, result.stderr
    assert not calls.exists()
    assert "skipping optional GitHub HTTPS credential setup" in result.stdout


def test_spoke_that_declares_required_credentials_still_fails_closed(
    tmp_path: Path,
) -> None:
    result, _ = _run(
        tmp_path,
        token="",
        agent="spoke",
        manager="hub",
        credentials_required="1",
    )

    assert result.returncode != 0
    assert "configured to require GitHub repository credentials" in result.stdout


def test_no_ssh_identity_gate_remains_on_the_bootstrap_path() -> None:
    """The vestigial SSH review identity must not come back.

    Nothing downstream ever read ~/.ssh/mac_github_review_id: the node never
    installed the streamed key, gitops.py rewrites SSH remotes to HTTPS
    whenever a token is present, and the string "review publication" appeared
    only in the gate's own error message.
    """
    installer = (ROOT / "deploy" / "fleet-node-install.sh").read_text(
        encoding="utf-8"
    )
    controller = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(
        encoding="utf-8"
    )
    # The comment above the replacement gate names what was removed and why, so
    # assert against code only.
    installer_code = _strip_comments(installer)
    controller_code = _strip_comments(controller)

    assert "install_github_review_key" not in installer_code
    assert "github_ssh_auth_succeeds" not in installer_code
    assert "remove_managed_github_review_key_config" not in installer_code
    assert "ssh -T git@github.com" not in installer_code
    assert "review publication" not in installer_code
    assert "mac_github_review_id" not in installer_code
    # The controller no longer generates, streams, or plumbs the unused key.
    assert "ensure_local_github_review_key" not in controller_code
    assert "GITHUB_REVIEW_KEY_B64" not in controller_code
    assert "GITHUB_REVIEW_KEY_B64" not in installer_code

    bootstrap_gates = installer.split(
        'if [ "$NODE_ACTION" = arm-phase2 ] || [ "$NODE_ACTION" = apply-phase2 ]; then',
        1,
    )[1].split("capture_darwin_launchd_prestate", 1)[0]
    assert "install_github_review_key" not in bootstrap_gates
    assert "configure_github_https_credentials" in bootstrap_gates


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
