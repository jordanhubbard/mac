from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "openshell" / "install-certifier-gateway-tunnel.sh"
LAUNCHD_LIFECYCLE = ROOT / "deploy" / "lib" / "launchd-lifecycle.sh"


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _launchd_lifecycle_functions() -> str:
    return LAUNCHD_LIFECYCLE.read_text(encoding="utf-8")


def _run_launchd_stop(tmp_path: Path, mode: str) -> subprocess.CompletedProcess[str]:
    case_dir = tmp_path / mode
    fake_bin = case_dir / "bin"
    fake_bin.mkdir(parents=True)
    count = case_dir / "count"
    calls = case_dir / "calls"
    count.write_text("0\n", encoding="utf-8")

    launchctl = fake_bin / "launchctl"
    launchctl.write_text(
        """#!/bin/sh
set -eu
value=$(sed -n '1p' "$FAKE_LAUNCHCTL_COUNT")
value=$((value + 1))
printf '%s\n' "$value" > "$FAKE_LAUNCHCTL_COUNT"
case "$1:$FAKE_LAUNCHCTL_MODE" in
  print:absent)
    echo 'Could not find service synthetic' >&2
    exit 113
    ;;
  print:delayed)
    if [ "$value" -lt 4 ]; then
      exit 0
    fi
    echo 'Could not find service synthetic' >&2
    exit 113
    ;;
  print:persistent|print:failed)
    exit 0
    ;;
  print:failed-then-absent)
    if [ "$value" -lt 3 ]; then
      exit 0
    fi
    echo 'Could not find service synthetic' >&2
    exit 113
    ;;
  print:inspect-error)
    echo 'synthetic launchctl transport failure' >&2
    exit 113
    ;;
  bootout:*)
    printf '%s\n' "$*" >> "$FAKE_LAUNCHCTL_CALLS"
    if [ "$FAKE_LAUNCHCTL_MODE" = failed ] || [ "$FAKE_LAUNCHCTL_MODE" = failed-then-absent ]; then
      echo 'synthetic bootout refusal' >&2
      exit 9
    fi
    exit 0
    ;;
esac
exit 64
""",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)

    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            "set -euo pipefail\n"
            "sleep() { SECONDS=$((SECONDS + 10)); }\n"
            + _launchd_lifecycle_functions()
            + "\n"
            + 'mac_launchd_stop_job_if_present "gui/501/com.mac.certifier" '
            '"com.mac.certifier"',
        ],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FAKE_LAUNCHCTL_MODE": mode,
            "FAKE_LAUNCHCTL_COUNT": str(count),
            "FAKE_LAUNCHCTL_CALLS": str(calls),
            "MAC_LAUNCHD_TRANSITION_TIMEOUT_SECONDS": "1.5",
            "MAC_LAUNCHD_COMMAND_TIMEOUT_SECONDS": "0.5",
            "MAC_LAUNCHD_POLL_INTERVAL_SECONDS": "0.01",
        },
        check=False,
        capture_output=True,
        text=True,
    )


def test_certifier_gateway_tunnel_is_loopback_only_and_fail_closed() -> None:
    script = _script_text()

    assert '"-L",' in script
    assert 'f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}"' in script
    assert '"BatchMode=yes"' in script
    assert '"PasswordAuthentication=no"' in script
    assert '"KbdInteractiveAuthentication=no"' in script
    assert '"StrictHostKeyChecking=yes"' in script
    assert '"ExitOnForwardFailure=yes"' in script
    assert '"KeepAlive": True' in script
    assert 'OPENSHELL_GATEWAY_ENDPOINT="$endpoint"' in script
    assert "mac_retry_bounded" in script
    assert '"$OPENSH_BIN" status' in script
    assert "certifier OpenShell tunnel did not become healthy" in script
    assert "openshell gateway select" not in script
    assert '. "$SCRIPT_DIR/../lib/launchd-lifecycle.sh"' in script


def test_certifier_argument_errors_are_bash_32_compatible() -> None:
    result = subprocess.run(
        [
            "/bin/bash",
            str(SCRIPT),
            "--target",
            "user@example.test",
            "--ssh-port",
            "invalid",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "invalid ssh_port" in result.stderr
    assert "bad substitution" not in result.stderr


@pytest.mark.parametrize(
    ("mode", "succeeds", "error", "bootout_expected"),
    (
        ("delayed", True, "", True),
        ("absent", True, "", False),
        ("persistent", False, "remained loaded after bootout", True),
        ("failed", False, "launchctl bootout failed", True),
        ("failed-then-absent", True, "", True),
        ("inspect-error", False, "could not inspect launchd job", False),
    ),
)
def test_launchd_stop_is_bounded_and_fails_closed(
    tmp_path: Path,
    mode: str,
    succeeds: bool,
    error: str,
    bootout_expected: bool,
) -> None:
    result = _run_launchd_stop(tmp_path, mode)
    assert (result.returncode == 0) is succeeds, result.stderr
    if error:
        assert error in result.stderr
    calls = tmp_path / mode / "calls"
    assert calls.exists() is bootout_expected
    if bootout_expected:
        assert calls.read_text(encoding="utf-8") == (
            "bootout gui/501/com.mac.certifier\n"
        )
    if mode == "inspect-error":
        assert "exit 113" in result.stderr
        assert "synthetic launchctl transport failure" in result.stderr


def test_install_and_remove_share_proved_launchd_retirement() -> None:
    script = _script_text()
    lifecycle = _launchd_lifecycle_functions()
    assert 'if [ "$rc" -eq 113 ]; then' in lifecycle
    assert '*"Could not find service"*' in lifecycle
    assert "time.monotonic()" in lifecycle
    assert "start_new_session=True" in lifecycle
    assert "MAC_LAUNCHD_TRANSITION_TIMEOUT_SECONDS" in lifecycle

    remove = script.split('if [ "$REMOVE" = "1" ]; then', 1)[1].split(
        '[[ "$TARGET" =~', 1
    )[0]
    stop = remove.index(
        'mac_launchd_stop_job_if_present "$domain/$LABEL" "$LABEL"'
    )
    unlink = remove.index('rm -f "$plist"')
    reported = remove.index('echo "removed $LABEL"')
    assert stop < unlink < reported

    install = script.split('chmod 600 "$tmp_plist"', 1)[1]
    stop = install.index(
        'mac_launchd_stop_job_if_present "$domain/$LABEL" "$LABEL"'
    )
    replace = install.index(
        'mac_launchd_transaction_replace "$tmp_plist" "$plist"'
    )
    bootstrap = install.index(
        'mac_launchd_bootstrap_job "$domain" "$plist" "$domain/$LABEL" "$LABEL"'
    )
    health = install.index('"$OPENSH_BIN" status')
    commit = install.index("mac_launchd_transaction_commit", health)
    assert stop < replace < bootstrap < health < commit


@pytest.mark.parametrize(
    ("mode", "succeeds"),
    (("absent", True), ("inspect-error", False)),
)
def test_real_remove_branch_sources_shared_lifecycle_and_propagates_state(
    tmp_path: Path,
    mode: str,
    succeeds: bool,
) -> None:
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    label = "com.mac.integration-certifier"
    plist = home / "Library" / "LaunchAgents" / f"{label}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("old generation\n", encoding="utf-8")
    fake_bin.mkdir()
    (fake_bin / "uname").write_text("#!/bin/sh\necho Darwin\n", encoding="utf-8")
    (fake_bin / "id").write_text(
        '#!/bin/sh\n[ "${1:-}" = -u ] && { echo 501; exit 0; }\nexit 64\n',
        encoding="utf-8",
    )
    (fake_bin / "launchctl").write_text(
        """#!/bin/sh
set -eu
if [ "$1" != print ]; then exit 64; fi
if [ "$FAKE_REMOVE_MODE" = absent ]; then
  echo 'Could not find service synthetic' >&2
  exit 113
fi
echo 'synthetic launchctl transport failure' >&2
exit 70
""",
        encoding="utf-8",
    )
    for command in ("uname", "id", "launchctl"):
        (fake_bin / command).chmod(0o755)

    result = subprocess.run(
        [str(SCRIPT), "--remove", "--label", label],
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FAKE_REMOVE_MODE": mode,
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert (result.returncode == 0) is succeeds, result.stderr
    assert plist.exists() is (not succeeds)
    if succeeds:
        assert f"removed {label}" in result.stdout
    else:
        assert "could not inspect launchd job" in result.stderr
        assert f"removed {label}" not in result.stdout


def test_linux_gateway_firewall_allows_only_exact_openshell_bridge() -> None:
    bootstrap = (
        ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh"
    ).read_text(encoding="utf-8")

    assert "chain=MAC_OPENSH_GW" in bootstrap
    assert '-i lo -j RETURN' in bootstrap
    assert '-i "$bridge_iface" -j RETURN' in bootstrap
    assert '-i docker0 -j RETURN' not in bootstrap
    assert "-i 'br+' -j RETURN" not in bootstrap
    assert 'network_name="openshell-docker"' in bootstrap
    assert 'bridge_iface="br-${network_id:0:12}"' in bootstrap
    assert '"$ipt" -A "$chain" -j DROP' in bootstrap
    assert '-C INPUT -p tcp --dport 17670 -j "$chain"' in bootstrap
