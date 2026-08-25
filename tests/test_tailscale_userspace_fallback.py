"""Tests for the install-tailscale.sh userspace-networking fallback.

Acceptance criteria (task: tailscaled cannot start in gke-newhouse pods --
no /dev/net/tun, no NET_ADMIN):

- tun_device_available reports failure when /dev/net/tun is absent and the
  node cannot modprobe one -- the exact state of an hgx-provisioned GKE pod
  that was never granted NET_ADMIN.
- tun_device_available reports success on macOS, which synthesises utun
  interfaces on demand and legitimately has no /dev/net/tun.
- detect_networking_mode picks 'userspace' from that probe, 'kernel' when a
  TUN device is usable, and honours an explicit
  MAC_DEPLOY_TAILSCALE_NETWORKING pin (rejecting unknown values).
- userspace mode adds --tun=userspace-networking plus the loopback
  SOCKS5/HTTP proxies to tailscaled, and turns netfilter off for
  `tailscale up`; kernel mode keeps the stock flags exactly as before.
- Both supervisor branches that own a daemon invocation apply those flags,
  and the resolved mode is recorded in mac.env.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "deploy" / "install-tailscale.sh").read_text(encoding="utf-8")


def _extract(func: str) -> str:
    m = re.search(
        r"^%s\(\) \{\n.*?^}$" % re.escape(func),
        SCRIPT,
        re.S | re.M,
    )
    assert m, "could not extract function %s from install-tailscale.sh" % func
    return m.group(0)


def _run_bash(snippet: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", snippet], capture_output=True, text=True)


# ---------------------------------------------------------------------------
# TUN probe
# ---------------------------------------------------------------------------


def _probe_snippet(
    *,
    tun_present: bool,
    modprobe_body: str = "exit 1",
    uname: str = "Linux",
    tail: str = "tun_device_available && echo kernel || echo userspace",
) -> str:
    """Drive tun_device_available against fake uname/sudo/modprobe.

    The device path is rewritten to a test-owned one so the outcome does not
    depend on whether the host happens to have /dev/net/tun -- CI runners do,
    the target pods do not.  ``/dev/null`` stands in for a present TUN device
    because it is a character device on every supported platform and needs no
    privileges to conjure, unlike ``mknod``.
    """
    probe = _extract("tun_device_available").replace("/dev/net/tun", '"$TUN_DEVICE_PATH"')
    return r"""
set -u
FAKE_BIN="$(mktemp -d)"
{ echo '#!/bin/sh'; echo 'echo %(uname)s'; } > "$FAKE_BIN/uname"
# A pod without NET_ADMIN has no passwordless sudo path to modprobe either.
{ echo '#!/bin/sh'; echo 'exit 1'; } > "$FAKE_BIN/sudo"
{ echo '#!/bin/sh'; echo '%(modprobe_body)s'; } > "$FAKE_BIN/modprobe"
chmod +x "$FAKE_BIN/uname" "$FAKE_BIN/sudo" "$FAKE_BIN/modprobe"
# Prepended, not replaced: the fakes shadow the real uname/sudo/modprobe
# while ordinary utilities stay reachable for the fake bodies.
PATH="$FAKE_BIN:$PATH"
export TUN_DEVICE_PATH="%(tun_path)s"

%(probe)s

%(tail)s
""" % {
        "uname": uname,
        "modprobe_body": modprobe_body,
        "tun_path": "/dev/null" if tun_present else "$FAKE_BIN/absent-tun",
        "probe": probe,
        "tail": tail,
    }


def test_tun_probe_fails_without_device_and_without_modprobe() -> None:
    """The gke-newhouse pod case: no device node, no capability to load one."""
    result = _run_bash(_probe_snippet(tun_present=False))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "userspace"


def test_tun_probe_succeeds_on_darwin_without_dev_net_tun() -> None:
    """macOS has no /dev/net/tun and still does kernel networking via utun."""
    result = _run_bash(_probe_snippet(tun_present=False, uname="Darwin"))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "kernel", (
        "a missing /dev/net/tun on macOS must not force userspace networking"
    )


def test_tun_probe_only_consults_dev_net_tun_on_linux() -> None:
    probe = _extract("tun_device_available")
    assert '[ "$(uname -s)" = "Linux" ] || return 0' in probe


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------


def _mode_snippet(pin: str, *, tun_present: bool, modprobe_body: str = "exit 1") -> str:
    resolve = _extract("detect_networking_mode")
    return _probe_snippet(
        tun_present=tun_present,
        modprobe_body=modprobe_body,
        tail="MAC_DEPLOY_TAILSCALE_NETWORKING=%s\n%s\ndetect_networking_mode" % (pin, resolve),
    )


def test_auto_resolves_to_userspace_when_tun_is_unavailable() -> None:
    result = _run_bash(_mode_snippet("auto", tun_present=False))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "userspace"


def test_auto_resolves_to_kernel_when_tun_is_available() -> None:
    result = _run_bash(_mode_snippet("auto", tun_present=True))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "kernel"


def test_auto_resolves_to_kernel_when_modprobe_can_load_the_module() -> None:
    """Device absent but loadable: a privileged node must stay on kernel TUN."""
    result = _run_bash(
        _mode_snippet(
            "auto",
            tun_present=False,
            # A successful modprobe is only believed if the device node then
            # appears, so the fake has to produce one.
            modprobe_body='ln -s /dev/null "$TUN_DEVICE_PATH"',
        )
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "kernel"


def test_successful_modprobe_without_a_device_still_falls_back() -> None:
    """A modprobe that reports success but leaves no device is not enough."""
    result = _run_bash(_mode_snippet("auto", tun_present=False, modprobe_body="exit 0"))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "userspace"


def test_explicit_userspace_pin_skips_the_probe() -> None:
    result = _run_bash(_mode_snippet("userspace", tun_present=True))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "userspace"


def test_explicit_kernel_pin_skips_the_probe() -> None:
    result = _run_bash(_mode_snippet("kernel", tun_present=False))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "kernel"


def test_unknown_networking_pin_is_rejected() -> None:
    result = _run_bash(_mode_snippet("socks", tun_present=True))
    assert result.returncode != 0
    assert "unsupported MAC_DEPLOY_TAILSCALE_NETWORKING" in result.stderr


def test_networking_mode_defaults_to_auto() -> None:
    assert 'MAC_DEPLOY_TAILSCALE_NETWORKING="${MAC_DEPLOY_TAILSCALE_NETWORKING:-auto}"' in SCRIPT


# ---------------------------------------------------------------------------
# Daemon flags
# ---------------------------------------------------------------------------


def _flags(func: str, mode: str, *, port: str = "1055") -> subprocess.CompletedProcess:
    return _run_bash(
        "TAILSCALE_NETWORKING_MODE=%s\nMAC_DEPLOY_TAILSCALE_PROXY_PORT=%s\n%s\n%s"
        % (mode, port, _extract(func), func)
    )


def test_userspace_daemon_flags_drop_tun_and_publish_proxies() -> None:
    result = _flags("tailscaled_networking_args", "userspace")
    assert result.returncode == 0, result.stderr
    flags = result.stdout.strip()
    assert "--tun=userspace-networking" in flags
    assert "--socks5-server=localhost:1055" in flags
    assert "--outbound-http-proxy-listen=localhost:1055" in flags


def test_userspace_proxy_port_is_configurable() -> None:
    result = _flags("tailscaled_networking_args", "userspace", port="1080")
    assert result.returncode == 0, result.stderr
    assert "--socks5-server=localhost:1080" in result.stdout
    assert "--outbound-http-proxy-listen=localhost:1080" in result.stdout


def test_kernel_daemon_flags_are_empty() -> None:
    """Nodes with TUN must keep the stock tailscaled invocation."""
    result = _flags("tailscaled_networking_args", "kernel")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_userspace_up_flags_turn_netfilter_off() -> None:
    """The iptables 'Permission denied' storm is what netfilter-mode=off ends."""
    result = _flags("tailscale_up_networking_flags", "userspace")
    assert result.returncode == 0, result.stderr
    flags = result.stdout.strip()
    assert "--netfilter-mode=off" in flags
    assert "--accept-dns=false" in flags
    assert "--accept-routes" not in flags, (
        "userspace networking has no kernel routing table to accept routes into"
    )


def test_kernel_up_flags_are_unchanged() -> None:
    result = _flags("tailscale_up_networking_flags", "kernel")
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["--accept-routes", "--accept-dns=true"]


# ---------------------------------------------------------------------------
# Wiring: both daemon-owning branches and the join
# ---------------------------------------------------------------------------


def test_supervisord_conf_applies_the_networking_flags() -> None:
    command_line = next(
        line for line in SCRIPT.splitlines() if line.startswith("command=/usr/sbin/tailscaled")
    )
    assert "$(tailscaled_networking_args)" in command_line, (
        "the supervisord tailscaled stanza is the one that runs on GKE pods; "
        "it must carry the resolved networking flags"
    )


def test_systemd_userspace_branch_writes_flags_and_restarts() -> None:
    """/etc/default/tailscaled is the packaged unit's supported flag hook."""
    assert "sudo -n tee /etc/default/tailscaled" in SCRIPT
    assert 'FLAGS="$(tailscaled_networking_args)"' in SCRIPT
    assert "sudo -n systemctl restart tailscaled" in SCRIPT
    # The kernel path must still use plain `start`, unchanged from before.
    assert "sudo -n systemctl start tailscaled" in SCRIPT


def test_both_join_paths_use_the_mode_specific_flags() -> None:
    assert SCRIPT.count("$(tailscale_up_networking_flags)") == 2, (
        "cloud and headscale joins must both honour the resolved mode"
    )
    assert "--accept-routes \\" not in SCRIPT, (
        "kernel-only flags must come from tailscale_up_networking_flags, "
        "not be hard-coded into the up invocations"
    )


def test_resolved_mode_is_recorded_in_mac_env() -> None:
    assert 'set_env_key "$ENV_FILE" MAC_TAILSCALE_NETWORKING_MODE' in SCRIPT
    assert 'set_env_key "$ENV_FILE" MAC_TAILSCALE_SOCKS5_PROXY' in SCRIPT
    assert 'set_env_key "$ENV_FILE" MAC_TAILSCALE_HTTP_PROXY' in SCRIPT


def test_userspace_fallback_warns_about_reduced_reachability() -> None:
    """Silently degrading reachability would be worse than the hard failure."""
    assert "falling back to Tailscale userspace networking" in SCRIPT
    assert "the host does not route" in SCRIPT


def test_script_parses() -> None:
    result = subprocess.run(
        ["bash", "-n", str(ROOT / "deploy" / "install-tailscale.sh")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
