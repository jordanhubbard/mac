"""Tests for the install-tailscale.sh userspace-networking fallback.

Acceptance criteria (task: tailscaled cannot start in a pod without
/dev/net/tun or CAP_NET_ADMIN):

- The capability probe classifies a node from two independent facts: whether a
  TUN character device exists and whether CAP_NET_ADMIN is in the capability
  *bounding* set (the set a root process cannot exceed).
- A node missing either fact classifies as 'userspace'; only a node with both
  classifies as 'tun'.
- Non-Linux nodes always classify as 'tun' -- macOS reaches the network through
  utun and has no /dev/net/tun contract to fail.
- An explicit TAILSCALE_NETWORK_MODE overrides the probe; an unknown value
  fails loudly instead of silently picking a mode.
- Userspace mode selects the flags that make tailscaled work without TUN or
  netfilter: --tun=userspace-networking, a SOCKS5 + outbound HTTP proxy
  listener, --netfilter-mode=off on `up`, and no MagicDNS takeover.
- The resolved mode reaches both supervisors (the supervisord program command
  line and the systemd /etc/default/tailscaled FLAGS) and is recorded in
  mac.env so callers know a userspace node's address is reachable only through
  its local proxy.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "deploy" / "install-tailscale.sh"
SCRIPT = SCRIPT_PATH.read_text(encoding="utf-8")

# CapBnd masks as the kernel prints them in /proc/<pid>/status.
CAP_BND_WITH_NET_ADMIN = "000001ffffffffff"
# Docker's default bounding set: root, but deliberately without CAP_NET_ADMIN.
CAP_BND_WITHOUT_NET_ADMIN = "00000000a80425fb"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract(func: str) -> str:
    m = re.search(r"^%s\(\) \{\n.*?^}$" % re.escape(func), SCRIPT, re.S | re.M)
    assert m, "could not extract function %s from install-tailscale.sh" % func
    return m.group(0)


def _functions(*names: str) -> str:
    return "\n\n".join(_extract(name) for name in names)


def _run_bash(snippet: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


def _status_file(tmp_path: Path, cap_bnd: str) -> Path:
    path = tmp_path / ("status-%s" % cap_bnd)
    path.write_text("Name:\tbash\nCapBnd:\t%s\nCapEff:\t%s\n" % (cap_bnd, cap_bnd), encoding="utf-8")
    return path


def _probe(tmp_path: Path, *, tun: bool, net_admin: bool, **extra: str) -> subprocess.CompletedProcess:
    """Run the script's probe-only path against fixture capabilities."""
    # /dev/null is a character device, so it models "a TUN device exists"
    # without needing a privileged host to create one.
    device = "/dev/null" if tun else str(tmp_path / "no-such-tun")
    cap = CAP_BND_WITH_NET_ADMIN if net_admin else CAP_BND_WITHOUT_NET_ADMIN
    env = {
        "MAC_TAILSCALE_PROBE_ONLY": "1",
        "TAILSCALE_TUN_DEVICE": device,
        "TAILSCALE_PROC_STATUS": str(_status_file(tmp_path, cap)),
        **extra,
    }
    return _run_bash("bash %s" % SCRIPT_PATH, env=env)


# ---------------------------------------------------------------------------
# Capability probe
# ---------------------------------------------------------------------------

def test_probe_reports_tun_mode_when_device_and_capability_are_present(tmp_path):
    result = _probe(tmp_path, tun=True, net_admin=True)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema"] == "mac.node_network_capability.v1"
    assert payload["tun_device"] is True
    assert payload["net_admin"] is True
    assert payload["mode"] == "tun"


def test_probe_falls_back_to_userspace_without_tun_device(tmp_path):
    """The gke-newhouse pod case: CreateTUN fails because /dev/net/tun is absent."""
    result = _probe(tmp_path, tun=False, net_admin=True)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["tun_device"] is False
    assert payload["mode"] == "userspace"


def test_probe_falls_back_to_userspace_without_net_admin(tmp_path):
    """The other half of the same pod: iptables is denied even for uid 0.

    A TUN device alone is not enough, so a node that cannot program netfilter
    must not be sent down the stock path.
    """
    result = _probe(tmp_path, tun=True, net_admin=False)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["net_admin"] is False
    assert payload["mode"] == "userspace"


def test_probe_reads_the_bounding_set_not_the_effective_set(tmp_path):
    """CapEff can look complete on a node that still cannot program netfilter.

    tailscaled's stanza already runs as root, so the effective set is not the
    discriminating fact; the bounding set is.
    """
    status = tmp_path / "root-in-restricted-container.status"
    status.write_text(
        "Name:\tbash\nCapBnd:\t%s\nCapEff:\t%s\n"
        % (CAP_BND_WITHOUT_NET_ADMIN, CAP_BND_WITH_NET_ADMIN),
        encoding="utf-8",
    )
    result = _run_bash(
        "bash %s" % SCRIPT_PATH,
        env={
            "MAC_TAILSCALE_PROBE_ONLY": "1",
            "TAILSCALE_TUN_DEVICE": "/dev/null",
            "TAILSCALE_PROC_STATUS": str(status),
        },
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["net_admin"] is False
    assert payload["mode"] == "userspace"


def test_probe_needs_no_credentials(tmp_path):
    """Classification must not require an auth key: it is a read-only probe."""
    result = _probe(
        tmp_path,
        tun=False,
        net_admin=False,
        MAC_DEPLOY_TAILSCALE_AUTH_KEY="",
        HEADSCALE_URL="",
        HEADSCALE_PREAUTHKEY="",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["mode"] == "userspace"


def test_probe_flag_form_matches_env_form(tmp_path):
    env = {
        "TAILSCALE_TUN_DEVICE": str(tmp_path / "absent"),
        "TAILSCALE_PROC_STATUS": str(_status_file(tmp_path, CAP_BND_WITHOUT_NET_ADMIN)),
    }
    flagged = _run_bash("bash %s --print-network-capability" % SCRIPT_PATH, env=env)
    assert flagged.returncode == 0, flagged.stderr
    assert json.loads(flagged.stdout)["mode"] == "userspace"


def test_explicit_mode_overrides_the_probe(tmp_path):
    result = _probe(tmp_path, tun=False, net_admin=False, TAILSCALE_NETWORK_MODE="tun")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["mode"] == "tun"


def test_unknown_mode_fails_loudly(tmp_path):
    result = _probe(tmp_path, tun=True, net_admin=True, TAILSCALE_NETWORK_MODE="bogus")
    assert result.returncode != 0
    assert "unsupported TAILSCALE_NETWORK_MODE" in result.stderr
    assert result.stdout.strip() == ""


def test_non_linux_nodes_keep_the_tun_path(tmp_path):
    """macOS has no /dev/net/tun and never needs (or supports) this fallback."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uname = fake_bin / "uname"
    uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
    uname.chmod(0o755)
    result = _probe(
        tmp_path,
        tun=False,
        net_admin=False,
        PATH="%s:%s" % (fake_bin, os.environ["PATH"]),
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["mode"] == "tun"


# ---------------------------------------------------------------------------
# Mode selection: the flags that make a TUN-less node work
# ---------------------------------------------------------------------------

def _select_mode_snippet(mode: str) -> str:
    return r"""
TAILSCALE_NETWORK_MODE=%(mode)s
TAILSCALE_USERSPACE_PROXY_HOST=127.0.0.1
TAILSCALE_USERSPACE_PROXY_PORT=1055
TAILSCALED_EXTRA_FLAGS=""
TAILSCALE_UP_EXTRA_FLAGS=""
TAILSCALE_ACCEPT_DNS="true"

%(functions)s

select_network_mode >/dev/null
printf 'mode=%%s\n' "$TAILSCALE_NETWORK_MODE"
printf 'daemon=%%s\n' "$TAILSCALED_EXTRA_FLAGS"
printf 'up=%%s\n' "$TAILSCALE_UP_EXTRA_FLAGS"
printf 'dns=%%s\n' "$TAILSCALE_ACCEPT_DNS"
""" % {
        "mode": mode,
        "functions": _functions(
            "classify_network_mode",
            "userspace_proxy_address",
            "select_network_mode",
        ),
    }


def _selected(mode: str) -> dict:
    result = _run_bash(_select_mode_snippet(mode))
    assert result.returncode == 0, result.stderr
    return dict(line.split("=", 1) for line in result.stdout.strip().splitlines())


def test_userspace_mode_selects_the_userspace_engine_and_proxy():
    selected = _selected("userspace")
    assert selected["mode"] == "userspace"
    assert "--tun=userspace-networking" in selected["daemon"]
    assert "--socks5-server=127.0.0.1:1055" in selected["daemon"]
    assert "--outbound-http-proxy-listen=127.0.0.1:1055" in selected["daemon"]


def test_userspace_mode_disables_netfilter_and_magicdns():
    selected = _selected("userspace")
    assert selected["up"] == "--netfilter-mode=off"
    # There is no TUN interface to route MagicDNS answers through, so taking
    # over the host resolver would break name resolution rather than extend it.
    assert selected["dns"] == "false"


def test_tun_mode_changes_nothing():
    selected = _selected("tun")
    assert selected["mode"] == "tun"
    assert selected["daemon"] == ""
    assert selected["up"] == ""
    assert selected["dns"] == "true"


# ---------------------------------------------------------------------------
# The resolved mode must reach the daemon under both supervisors
# ---------------------------------------------------------------------------

def test_supervisord_program_command_carries_the_daemon_flags():
    stanza = SCRIPT.split("[program:${FLEET_NAME}-tailscaled]", 1)[1]
    command = next(
        line for line in stanza.splitlines() if line.startswith("command=")
    )
    assert "${TAILSCALED_EXTRA_FLAGS}" in command


def test_systemd_flags_file_replaces_only_the_flags_line(tmp_path):
    defaults = tmp_path / "tailscaled"
    defaults.write_text('PORT="41641"\nFLAGS="--stale"\n', encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    sudo = fake_bin / "sudo"
    # The real call is `sudo -n tee`; this stand-in drops the -n and execs so
    # the rendering can be checked without privilege.
    sudo.write_text('#!/bin/sh\n[ "$1" = "-n" ] && shift\nexec "$@"\n', encoding="utf-8")
    sudo.chmod(0o755)
    snippet = r"""
TAILSCALED_EXTRA_FLAGS="--tun=userspace-networking --socks5-server=127.0.0.1:1055"
TAILSCALED_DEFAULTS_FILE=%(file)s

%(functions)s

configure_systemd_daemon_flags
""" % {"file": defaults, "functions": _functions("configure_systemd_daemon_flags")}
    result = _run_bash(snippet, env={"PATH": "%s:%s" % (fake_bin, os.environ["PATH"])})
    assert result.returncode == 0, result.stderr
    rendered = defaults.read_text(encoding="utf-8")
    assert 'PORT="41641"' in rendered
    assert "--stale" not in rendered
    assert rendered.count("FLAGS=") == 1
    assert '--tun=userspace-networking' in rendered


def test_systemd_branch_writes_flags_before_starting_the_daemon():
    branch = SCRIPT.split("  systemd)", 1)[1].split(";;", 1)[0]
    assert "configure_systemd_daemon_flags" in branch
    assert branch.index("configure_systemd_daemon_flags") < branch.index("systemctl start")
    # A daemon already running with stock flags must be restarted, or the
    # userspace flags never take effect on a re-run.
    assert "systemctl restart tailscaled" in branch


def test_up_calls_carry_the_mode_dependent_flags():
    up_calls = [
        block
        for block in SCRIPT.split("run_tailscale $(tailscale_socket_flag) up")[1:]
    ]
    assert len(up_calls) == 2, "expected the headscale and cloud join branches"
    for call in up_calls:
        command = call.split("}", 1)[0]
        assert '--accept-dns="$TAILSCALE_ACCEPT_DNS"' in command
        assert "$TAILSCALE_UP_EXTRA_FLAGS" in command


# ---------------------------------------------------------------------------
# mac.env must record what a userspace node's address actually means
# ---------------------------------------------------------------------------

def _record_env(tmp_path: Path, mode: str) -> str:
    env_file = tmp_path / ("mac-%s.env" % mode)
    snippet = r"""
ENV_FILE=%(env_file)s
TAILSCALE_NETWORK_MODE=%(mode)s
TAILSCALE_USERSPACE_PROXY_HOST=127.0.0.1
TAILSCALE_USERSPACE_PROXY_PORT=1055

%(functions)s

record_network_mode_env
""" % {
        "env_file": env_file,
        "mode": mode,
        "functions": _functions(
            "set_env_key", "userspace_proxy_address", "record_network_mode_env"
        ),
    }
    result = _run_bash(snippet)
    assert result.returncode == 0, result.stderr
    return env_file.read_text(encoding="utf-8")


def test_userspace_mode_records_mode_and_proxy_in_mac_env(tmp_path):
    rendered = _record_env(tmp_path, "userspace")
    assert "MAC_TAILSCALE_NETWORK_MODE=userspace" in rendered
    assert "MAC_TAILSCALE_SOCKS5_PROXY=127.0.0.1:1055" in rendered
    assert "MAC_TAILSCALE_HTTP_PROXY=http://127.0.0.1:1055" in rendered


def test_tun_mode_records_the_mode_without_proxy_keys(tmp_path):
    rendered = _record_env(tmp_path, "tun")
    assert "MAC_TAILSCALE_NETWORK_MODE=tun" in rendered
    assert "PROXY" not in rendered


def test_mode_is_recorded_on_the_already_connected_path():
    """A node that is already up must still publish its mode."""
    block = SCRIPT.split("if tailscale_connected; then", 1)[1].split("\nfi\n", 1)[0]
    assert "record_network_mode_env" in block
    # Classification has to happen before that check, or the recorded mode
    # would be whatever the default was.
    assert SCRIPT.index("select_network_mode\n") < SCRIPT.index("if tailscale_connected; then")


def test_userspace_join_explains_the_reachability_consequence():
    assert "userspace-networking mode" in SCRIPT
    assert "MAC_TAILSCALE_SOCKS5_PROXY" in SCRIPT
    assert "CAP_NET_ADMIN" in SCRIPT
