"""Tests for install-tailscale.sh userspace-networking fallback.

Acceptance criteria (task: tailscaled cannot start in gke-newhouse pods):
- A node with no /dev/net/tun selects userspace networking instead of letting
  tailscaled fail with CreateTUN("tailscale0").
- A node whose capability bounding set excludes CAP_NET_ADMIN selects
  userspace networking, which is the same pod-spec defect seen as
  "iptables: Permission denied (you must be root)" under a root daemon.
- net_admin_capable reads the *bounding* set, so an unprivileged caller that
  escalates via sudo is judged by what the container permits, not by what
  this shell currently holds.
- macOS is never pushed into userspace mode by the absence of /dev/net/tun,
  because tailscaled uses utun there and no such device node exists.
- An explicit MAC_DEPLOY_TAILSCALE_NETWORK_MODE wins over detection, and an
  unknown value is rejected rather than silently treated as "auto".
- Userspace mode adds the daemon flags that make the mesh usable without a
  TUN device, and drops the `tailscale up` flags that need one.
- Kernel mode is byte-for-byte unchanged: no extra daemon flags, and the
  historical --accept-routes/--accept-dns=true join flags.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "deploy" / "install-tailscale.sh").read_text(encoding="utf-8")

# A capability bounding set with every bit set below 38 -- CAP_NET_ADMIN (12)
# is present. This is what an ordinary privileged Linux host reports.
FULL_BOUNDING_SET = "0000003fffffffff"
# Capabilities 0..8 only: CAP_NET_ADMIN is absent. This is the shape a
# restricted container (the gke-newhouse pod) reports.
RESTRICTED_BOUNDING_SET = "00000000000001ff"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract(func: str) -> str:
    m = re.search(
        r"^%s\(\) \{\n.*?^}$" % re.escape(func),
        SCRIPT,
        re.S | re.M,
    )
    assert m, "could not extract function %s from install-tailscale.sh" % func
    return m.group(0)


def _run_bash(snippet: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
    )


def _net_admin_capable(status_file: str) -> str:
    """net_admin_capable with its /proc path redirected at a fixture.

    The production literal stays asserted by
    test_net_admin_capable_reads_the_bounding_set_from_proc, so the rewrite
    cannot hide a change of source.
    """
    fn = _extract("net_admin_capable")
    assert "/proc/self/status" in fn
    return fn.replace("/proc/self/status", status_file, 1)


def _select_network_mode(*, tun_path: str, uname_dir: str | None = None) -> str:
    """select_network_mode with the TUN device path redirected at a fixture."""
    fn = _extract("select_network_mode")
    assert "/dev/net/tun" in fn
    fn = fn.replace("/dev/net/tun", tun_path)
    if uname_dir:
        fn = "PATH=%s:$PATH\n" % uname_dir + fn
    return fn


def _fake_uname(tmp_path: Path, kernel_name: str) -> str:
    directory = tmp_path / ("uname-%s" % kernel_name)
    directory.mkdir()
    script = directory / "uname"
    script.write_text("#!/bin/sh\nprintf '%s\\n'\n" % kernel_name, encoding="utf-8")
    script.chmod(0o755)
    return str(directory)


def _status_file(tmp_path: Path, name: str, bounding: str | None) -> str:
    path = tmp_path / name
    if bounding is None:
        return str(path)  # deliberately absent
    path.write_text(
        "Name:\ttailscaled\nCapBnd:\t%s\nCapEff:\t0000000000000000\n" % bounding,
        encoding="utf-8",
    )
    return str(path)


# ---------------------------------------------------------------------------
# net_admin_capable
# ---------------------------------------------------------------------------

def test_net_admin_capable_true_when_bounding_set_has_the_bit(tmp_path: Path) -> None:
    status = _status_file(tmp_path, "full", FULL_BOUNDING_SET)
    result = _run_bash(_net_admin_capable(status) + "\nnet_admin_capable")
    assert result.returncode == 0, result.stderr


def test_net_admin_capable_false_when_bounding_set_lacks_the_bit(tmp_path: Path) -> None:
    """The gke-newhouse pod case: root, but CAP_NET_ADMIN is not grantable."""
    status = _status_file(tmp_path, "restricted", RESTRICTED_BOUNDING_SET)
    result = _run_bash(_net_admin_capable(status) + "\nnet_admin_capable")
    assert result.returncode != 0


def test_net_admin_capable_false_for_an_empty_bounding_set(tmp_path: Path) -> None:
    status = _status_file(tmp_path, "empty", "0000000000000000")
    result = _run_bash(_net_admin_capable(status) + "\nnet_admin_capable")
    assert result.returncode != 0


def test_net_admin_capable_true_when_the_status_file_is_absent(tmp_path: Path) -> None:
    """No /proc/self/status (any non-Linux host) must not force userspace."""
    status = _status_file(tmp_path, "missing", None)
    result = _run_bash(_net_admin_capable(status) + "\nnet_admin_capable")
    assert result.returncode == 0, result.stderr


def test_net_admin_capable_reads_the_bounding_set_from_proc() -> None:
    """The effective set is meaningless here: this script runs unprivileged
    and escalates with `sudo -n`, so only the bounding set says what the
    container will ever permit.
    """
    fn = _extract("net_admin_capable")
    assert "CapBnd" in fn, "net_admin_capable must read the capability bounding set"
    assert "/proc/self/status" in fn
    assert "CapEff" not in fn, (
        "reading CapEff would report 'incapable' on every unprivileged node "
        "that can still escalate through sudo"
    )


# ---------------------------------------------------------------------------
# select_network_mode: detection
# ---------------------------------------------------------------------------

def test_select_network_mode_picks_kernel_when_tun_and_cap_are_present(tmp_path: Path) -> None:
    # /dev/null is a character device, so `[ -c ... ]` sees a usable node.
    fn = _select_network_mode(tun_path="/dev/null", uname_dir=_fake_uname(tmp_path, "Linux"))
    status = _status_file(tmp_path, "full-kernel", FULL_BOUNDING_SET)
    snippet = "NETWORK_MODE_REQUEST=auto\n%s\n%s\nselect_network_mode" % (
        _net_admin_capable(status),
        fn,
    )
    result = _run_bash(snippet)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "kernel"


def test_select_network_mode_picks_userspace_when_tun_device_is_missing(tmp_path: Path) -> None:
    """The reported failure: /dev/net/tun does not exist in the pod."""
    missing = str(tmp_path / "no-such-tun")
    fn = _select_network_mode(tun_path=missing, uname_dir=_fake_uname(tmp_path, "Linux"))
    status = _status_file(tmp_path, "full-notun", FULL_BOUNDING_SET)
    snippet = "NETWORK_MODE_REQUEST=auto\n%s\n%s\nselect_network_mode" % (
        _net_admin_capable(status),
        fn,
    )
    result = _run_bash(snippet)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "userspace"
    # The probed path is rewritten to the fixture, so match the explanation.
    assert "is absent — using userspace networking" in result.stderr


def test_select_network_mode_picks_userspace_without_net_admin(tmp_path: Path) -> None:
    """A TUN node can exist while the pod still cannot program netfilter."""
    fn = _select_network_mode(tun_path="/dev/null", uname_dir=_fake_uname(tmp_path, "Linux"))
    status = _status_file(tmp_path, "restricted-tun", RESTRICTED_BOUNDING_SET)
    snippet = "NETWORK_MODE_REQUEST=auto\n%s\n%s\nselect_network_mode" % (
        _net_admin_capable(status),
        fn,
    )
    result = _run_bash(snippet)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "userspace"
    assert "CAP_NET_ADMIN" in result.stderr


def test_select_network_mode_keeps_kernel_on_non_linux(tmp_path: Path) -> None:
    """macOS has no /dev/net/tun and needs none; probing for it must not
    demote every Mac in the fleet to userspace relaying.
    """
    missing = str(tmp_path / "no-such-tun-darwin")
    fn = _select_network_mode(tun_path=missing, uname_dir=_fake_uname(tmp_path, "Darwin"))
    snippet = "NETWORK_MODE_REQUEST=auto\n%s\nselect_network_mode" % fn
    result = _run_bash(snippet)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "kernel"


def test_select_network_mode_probes_the_real_device_path() -> None:
    fn = _extract("select_network_mode")
    assert "[ ! -c /dev/net/tun ]" in fn, (
        "detection must test the actual TUN device node, not merely whether "
        "the tun module is listed"
    )
    assert 'uname -s' in fn


# ---------------------------------------------------------------------------
# select_network_mode: explicit override
# ---------------------------------------------------------------------------

def test_select_network_mode_honors_an_explicit_userspace_request() -> None:
    fn = _extract("select_network_mode")
    result = _run_bash("NETWORK_MODE_REQUEST=userspace\n" + fn + "\nselect_network_mode")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "userspace"


def test_select_network_mode_honors_an_explicit_kernel_request(tmp_path: Path) -> None:
    """Forcing kernel mode must skip detection entirely, even on a node that
    auto-detection would have demoted.
    """
    missing = str(tmp_path / "no-such-tun-forced")
    fn = _select_network_mode(tun_path=missing, uname_dir=_fake_uname(tmp_path, "Linux"))
    result = _run_bash("NETWORK_MODE_REQUEST=kernel\n" + fn + "\nselect_network_mode")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "kernel"


def test_select_network_mode_rejects_an_unknown_request() -> None:
    fn = _extract("select_network_mode")
    result = _run_bash("NETWORK_MODE_REQUEST=proxy-only\n" + fn + "\nselect_network_mode")
    assert result.returncode != 0
    assert "unsupported network mode" in result.stderr


def test_network_mode_request_comes_from_the_deploy_variable() -> None:
    assert 'NETWORK_MODE_REQUEST="${MAC_DEPLOY_TAILSCALE_NETWORK_MODE:-auto}"' in SCRIPT
    assert 'USERSPACE_PROXY_ADDR="${MAC_DEPLOY_TAILSCALE_PROXY_ADDR:-localhost:1055}"' in SCRIPT


# ---------------------------------------------------------------------------
# Mode flags
# ---------------------------------------------------------------------------

def test_tailscaled_mode_flags_are_empty_in_kernel_mode() -> None:
    fn = _extract("tailscaled_mode_flags")
    result = _run_bash(
        "USERSPACE_PROXY_ADDR=localhost:1055\n" + fn + "\ntailscaled_mode_flags kernel"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", (
        "kernel-mode nodes must keep their historical tailscaled command line"
    )


def test_tailscaled_mode_flags_select_userspace_networking() -> None:
    fn = _extract("tailscaled_mode_flags")
    result = _run_bash(
        "USERSPACE_PROXY_ADDR=localhost:1055\n" + fn + "\ntailscaled_mode_flags userspace"
    )
    assert result.returncode == 0, result.stderr
    flags = result.stdout.split()
    assert "--tun=userspace-networking" in flags, (
        "without this flag tailscaled still tries to CreateTUN and dies"
    )
    assert "--socks5-server=localhost:1055" in flags
    assert "--outbound-http-proxy-listen=localhost:1055" in flags


def test_tailscaled_mode_flags_honor_a_custom_proxy_address() -> None:
    fn = _extract("tailscaled_mode_flags")
    result = _run_bash(
        "USERSPACE_PROXY_ADDR=127.0.0.1:9055\n" + fn + "\ntailscaled_mode_flags userspace"
    )
    assert result.returncode == 0, result.stderr
    assert "--socks5-server=127.0.0.1:9055" in result.stdout
    assert "--outbound-http-proxy-listen=127.0.0.1:9055" in result.stdout


def test_tailscale_up_mode_flags_keep_the_historical_kernel_flags() -> None:
    fn = _extract("tailscale_up_mode_flags")
    result = _run_bash(fn + "\ntailscale_up_mode_flags kernel")
    assert result.returncode == 0, result.stderr
    flags = result.stdout.split()
    assert flags == ["--accept-routes", "--accept-dns=true"]


def test_tailscale_up_mode_flags_drop_netfilter_and_dns_in_userspace() -> None:
    fn = _extract("tailscale_up_mode_flags")
    result = _run_bash(fn + "\ntailscale_up_mode_flags userspace")
    assert result.returncode == 0, result.stderr
    flags = result.stdout.split()
    assert "--netfilter-mode=off" in flags, (
        "a pod without CAP_NET_ADMIN cannot program iptables; asking tailscale "
        "to try is the 'Permission denied (you must be root)' log"
    )
    assert "--accept-dns=false" in flags
    assert "--accept-routes" not in flags, (
        "there is no interface to install subnet routes on in userspace mode"
    )


# ---------------------------------------------------------------------------
# Wiring: the resolved mode must actually reach the daemon and the join
# ---------------------------------------------------------------------------

def test_mode_is_resolved_before_the_daemon_is_started() -> None:
    mode_pos = SCRIPT.index('NETWORK_MODE="$(select_network_mode)"')
    flags_pos = SCRIPT.index('tailscaled_extra_flags="$(tailscaled_mode_flags')
    start_pos = SCRIPT.index("case \"$SUPERVISOR_KIND\" in\n  systemd)")
    assert mode_pos < flags_pos < start_pos


def test_supervisord_daemon_command_carries_the_mode_flags() -> None:
    """The supervisord stanza is the branch the containerized nodes take."""
    assert (
        "command=/usr/sbin/tailscaled --state=/var/lib/${FLEET_NAME}/tailscale/"
        "tailscaled.state --socket=/run/tailscale/${FLEET_NAME}.sock --port=41641 "
        "${tailscaled_extra_flags}" in SCRIPT
    )


def test_systemd_records_the_mode_flags_where_the_unit_reads_them() -> None:
    """A systemd node in userspace mode needs the flags in /etc/default so the
    setting survives the restart and every later one.
    """
    assert "sudo -n tee /etc/default/tailscaled" in SCRIPT
    assert 'FLAGS="--state=/var/lib/tailscale/tailscaled.state --port=41641 ${tailscaled_extra_flags}"' in SCRIPT
    assert "sudo -n systemctl restart tailscaled" in SCRIPT


def test_launchd_refuses_userspace_rather_than_ignoring_it() -> None:
    assert "userspace networking is not supported under launchd" in SCRIPT


def test_both_join_paths_use_the_mode_flags() -> None:
    """Neither control-plane branch may keep hard-coded --accept-routes."""
    assert 'up_mode_flags="$(tailscale_up_mode_flags "$NETWORK_MODE")"' in SCRIPT
    assert SCRIPT.count("$up_mode_flags >/dev/null 2>&1") == 2
    join_section = SCRIPT.split("# -- Join the network --", 1)[1]
    assert "--accept-routes" not in join_section, (
        "route acceptance is mode-dependent and must come from "
        "tailscale_up_mode_flags"
    )


def test_the_resolved_mode_is_recorded_in_the_env_file() -> None:
    assert 'set_env_key "$ENV_FILE" MAC_TAILSCALE_NETWORK_MODE "$NETWORK_MODE"' in SCRIPT


def test_userspace_mode_publishes_its_outbound_proxy() -> None:
    """A userspace node has no interface, so anything of ours that dials a
    mesh IP -- a worker reaching the hub URL -- has to go via the proxy.
    """
    assert (
        'set_env_key "$ENV_FILE" MAC_TAILSCALE_SOCKS5_PROXY "socks5://${USERSPACE_PROXY_ADDR}"'
        in SCRIPT
    )
    assert (
        'set_env_key "$ENV_FILE" MAC_TAILSCALE_HTTP_PROXY "http://${USERSPACE_PROXY_ADDR}"'
        in SCRIPT
    )
    proxy_block = SCRIPT.split('if [ "$NETWORK_MODE" = "userspace" ]; then', 1)[1]
    assert "MAC_TAILSCALE_SOCKS5_PROXY" in proxy_block, (
        "the proxy keys must be written only for userspace nodes"
    )


def test_new_env_keys_are_registered() -> None:
    """New MAC_* names must be in the generated registry, or the contract
    preflight rejects the change as a stale registry.
    """
    import json

    registry = json.loads(
        (ROOT / "src/mac/data/env_config_registry.json").read_text(encoding="utf-8")
    )
    names = {record["name"] for record in registry}
    for name in (
        "MAC_DEPLOY_TAILSCALE_NETWORK_MODE",
        "MAC_DEPLOY_TAILSCALE_PROXY_ADDR",
        "MAC_TAILSCALE_NETWORK_MODE",
        "MAC_TAILSCALE_SOCKS5_PROXY",
        "MAC_TAILSCALE_HTTP_PROXY",
    ):
        assert name in names, "%s is missing from the generated env registry" % name
