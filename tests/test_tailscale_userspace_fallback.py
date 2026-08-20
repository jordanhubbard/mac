"""Tests for install-tailscale.sh datapath selection (kernel TUN vs userspace).

Acceptance criteria (task: tailscaled cannot start in gke-newhouse pods --
no /dev/net/tun, no NET_ADMIN):

- On Linux, a missing /dev/net/tun selects Tailscale's userspace networking
  engine instead of letting tailscaled hard-fail on CreateTUN.
- A present /dev/net/tun but absent CAP_NET_ADMIN in the bounding set also
  selects userspace: the pod that cannot open the TUN device is the same pod
  whose iptables calls fail with "Permission denied (you must be root)", and
  `user=root` in the supervisord stanza cannot recover a capability the pod
  spec never granted.
- A capable Linux node keeps the stock kernel datapath, and a non-Linux node
  is never probed at all (macOS has no /dev/net/tun and no CapBnd, so probing
  would silently downgrade a healthy Mac).
- MAC_DEPLOY_TAILSCALE_TUN_MODE lets an operator force either mode, and an
  unrecognized value fails loudly rather than defaulting.
- The userspace daemon flags reach both the supervisord program line and the
  systemd EnvironmentFile, and `tailscale up` stops asking for routes and
  netfilter it provably cannot install.
- The resolved datapath and its proxy endpoints are recorded in mac.env,
  because a userspace-relay node is not reachable the same way a kernel-TUN
  node is and downstream steps must be able to tell them apart.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "deploy" / "install-tailscale.sh"
SCRIPT = SCRIPT_PATH.read_text(encoding="utf-8")

# Functions the datapath decision is built from, in dependency order.
_DATAPATH_FUNCS = (
    "have_tun_device",
    "have_net_admin",
    "resolve_tun_mode",
    "tailscaled_mode_flags",
    "tailscale_up_mode_flags",
)


def _extract(func: str) -> str:
    m = re.search(r"^%s\(\) \{\n.*?^}$" % re.escape(func), SCRIPT, re.S | re.M)
    assert m, "could not extract function %s from install-tailscale.sh" % func
    return m.group(0)


def _harness(*, overrides: str = "", env: str = "", call: str) -> subprocess.CompletedProcess:
    """Run the real datapath functions with explicit stubs for the probes.

    ``overrides`` is appended *after* the extracted definitions so a test can
    replace a probe (or ``uname``/``awk``) without editing the script.
    """
    body = "\n".join(
        [
            "set -uo pipefail",
            'TAILSCALE_TUN_MODE="${MAC_DEPLOY_TAILSCALE_TUN_MODE:-auto}"',
            'TAILSCALE_USERSPACE_SOCKS5_PORT="${MAC_DEPLOY_TAILSCALE_SOCKS5_PORT:-1055}"',
            'TAILSCALE_USERSPACE_HTTP_PROXY_PORT="${MAC_DEPLOY_TAILSCALE_HTTP_PROXY_PORT:-1055}"',
            'TAILSCALE_TUN_MODE_RESOLVED="${TAILSCALE_TUN_MODE_RESOLVED:-}"',
            *(_extract(name) for name in _DATAPATH_FUNCS),
            overrides,
            call,
        ]
    )
    return subprocess.run(
        ["bash", "-c", (env + "\n" if env else "") + body],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# resolve_tun_mode
# ---------------------------------------------------------------------------

def test_missing_dev_net_tun_selects_userspace() -> None:
    result = _harness(
        overrides="\n".join(
            [
                'uname() { printf "Linux\\n"; }',
                "have_tun_device() { return 1; }",
                "have_net_admin() { return 0; }",
            ]
        ),
        call="resolve_tun_mode",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["userspace", "no_dev_net_tun"]


def test_missing_net_admin_selects_userspace_even_with_tun_device() -> None:
    result = _harness(
        overrides="\n".join(
            [
                'uname() { printf "Linux\\n"; }',
                "have_tun_device() { return 0; }",
                "have_net_admin() { return 1; }",
            ]
        ),
        call="resolve_tun_mode",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["userspace", "no_cap_net_admin"]


def test_capable_linux_node_keeps_kernel_datapath() -> None:
    result = _harness(
        overrides="\n".join(
            [
                'uname() { printf "Linux\\n"; }',
                "have_tun_device() { return 0; }",
                "have_net_admin() { return 0; }",
            ]
        ),
        call="resolve_tun_mode",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["kernel", "tun_and_net_admin_present"]


def test_non_linux_host_is_never_probed() -> None:
    """macOS has no /dev/net/tun; probing it would downgrade a healthy Mac."""
    result = _harness(
        overrides="\n".join(
            [
                'uname() { printf "Darwin\\n"; }',
                'have_tun_device() { echo "probed" >&2; return 1; }',
                "have_net_admin() { return 1; }",
            ]
        ),
        call="resolve_tun_mode",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["kernel", "non_linux_host"]
    assert "probed" not in result.stderr


def test_operator_can_force_either_mode() -> None:
    for forced in ("kernel", "userspace"):
        result = _harness(
            env='export MAC_DEPLOY_TAILSCALE_TUN_MODE=%s' % forced,
            overrides="\n".join(
                [
                    'uname() { printf "Linux\\n"; }',
                    'have_tun_device() { echo "probed" >&2; return 1; }',
                    "have_net_admin() { return 1; }",
                ]
            ),
            call="resolve_tun_mode",
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == [forced, "forced_by_operator"]
        assert "probed" not in result.stderr


def test_unrecognized_forced_mode_fails_loudly() -> None:
    result = _harness(
        env="export MAC_DEPLOY_TAILSCALE_TUN_MODE=maybe",
        overrides='uname() { printf "Linux\\n"; }',
        call="resolve_tun_mode",
    )
    assert result.returncode != 0
    assert "MAC_DEPLOY_TAILSCALE_TUN_MODE" in result.stderr
    assert result.stdout.strip() == ""


# ---------------------------------------------------------------------------
# have_net_admin: CAP_NET_ADMIN is capability bit 12 of CapBnd
# ---------------------------------------------------------------------------

def _net_admin_with_capbnd(capbnd: str) -> int:
    return _harness(
        overrides='awk() { printf "%s\\n"; }' % capbnd,
        call="have_net_admin",
    ).returncode


def test_net_admin_reads_bit_twelve_of_the_bounding_set() -> None:
    # 0x...1000 has bit 12 set; 0x...0fff covers bits 0..11 only.
    assert _net_admin_with_capbnd("0000000000001000") == 0
    assert _net_admin_with_capbnd("0000003fffffffff") == 0
    assert _net_admin_with_capbnd("0000000000000fff") == 1
    assert _net_admin_with_capbnd("0000000000000000") == 1
    # The default container bounding set: NET_RAW (bit 13, 0x2000) is granted
    # but NET_ADMIN (bit 12) is not -- which is why a pod can hold a plausible
    # capability set, run as root, and still not be able to open a TUN device.
    assert _net_admin_with_capbnd("00000000a80425fb") == 1


def test_net_admin_fails_open_when_the_bounding_set_is_unreadable() -> None:
    """No CapBnd means "unknown", not "missing" -- do not downgrade blindly."""
    assert _net_admin_with_capbnd("") == 0
    assert _net_admin_with_capbnd("not-hex") == 0


# ---------------------------------------------------------------------------
# Flag construction
# ---------------------------------------------------------------------------

def test_userspace_daemon_flags_publish_local_proxies() -> None:
    result = _harness(
        env="export MAC_DEPLOY_TAILSCALE_SOCKS5_PORT=1080 MAC_DEPLOY_TAILSCALE_HTTP_PROXY_PORT=1081",
        overrides='TAILSCALE_TUN_MODE_RESOLVED="userspace"',
        call="tailscaled_mode_flags",
    )
    assert result.returncode == 0, result.stderr
    flags = result.stdout.split()
    assert "--tun=userspace-networking" in flags
    assert "--socks5-server=localhost:1080" in flags
    assert "--outbound-http-proxy-listen=localhost:1081" in flags


def test_kernel_mode_adds_no_daemon_flags() -> None:
    result = _harness(
        overrides='TAILSCALE_TUN_MODE_RESOLVED="kernel"',
        call="tailscaled_mode_flags",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_userspace_join_stops_requesting_routes_and_netfilter() -> None:
    userspace = _harness(
        overrides='TAILSCALE_TUN_MODE_RESOLVED="userspace"',
        call="tailscale_up_mode_flags",
    )
    assert userspace.returncode == 0, userspace.stderr
    flags = userspace.stdout.split()
    assert "--netfilter-mode=off" in flags
    assert "--accept-routes=false" in flags
    assert "--accept-dns=false" in flags

    kernel = _harness(
        overrides='TAILSCALE_TUN_MODE_RESOLVED="kernel"',
        call="tailscale_up_mode_flags",
    )
    assert kernel.returncode == 0, kernel.stderr
    assert kernel.stdout.split() == ["--accept-routes", "--accept-dns=true"]


# ---------------------------------------------------------------------------
# Wiring: the resolved flags have to reach the daemon and mac.env
# ---------------------------------------------------------------------------

def test_supervisord_program_line_carries_the_datapath_flags() -> None:
    command_line = next(
        line for line in SCRIPT.splitlines() if line.startswith("command=/usr/sbin/tailscaled")
    )
    assert "$(tailscaled_mode_flags)" in command_line


def test_systemd_branch_writes_the_flags_to_the_environment_file() -> None:
    systemd_branch = SCRIPT.split("  systemd)\n", 1)[1].split("    ;;", 1)[0]
    assert "/etc/default/tailscaled" in systemd_branch
    assert 'FLAGS="$(tailscaled_mode_flags)"' in systemd_branch


def test_join_uses_the_mode_flags_for_both_control_planes() -> None:
    assert SCRIPT.count("$(tailscale_up_mode_flags)") == 2
    for marker in ('--auth-key="$TAILSCALE_AUTH_KEY"', '--auth-key="$HEADSCALE_PREAUTHKEY"'):
        command = SCRIPT.split(marker, 1)[1].split("}", 1)[0]
        assert "$(tailscale_up_mode_flags)" in command


def test_resolved_datapath_is_recorded_in_mac_env() -> None:
    assert 'set_env_key "$ENV_FILE" MAC_TAILSCALE_TUN_MODE "$TAILSCALE_TUN_MODE_RESOLVED"' in SCRIPT
    assert "MAC_TAILSCALE_SOCKS5_PROXY" in SCRIPT
    assert "MAC_TAILSCALE_HTTP_PROXY" in SCRIPT


def test_probe_never_attempts_a_module_load() -> None:
    """modprobe is exactly what the pod is denied; retrying it only adds noise."""
    assert "modprobe" not in "\n".join(_extract(name) for name in _DATAPATH_FUNCS)
