"""Tests for install-tailscale.sh supervisord branch selection.

Acceptance criteria (task: tailscaled under supervisord on no-systemd nodes):
- detect_supervisor selects 'supervisord' when systemd is absent but
  supervisorctl is present, regardless of whether systemctl is on PATH.
- detect_supervisor selects 'systemd' only when /run/systemd/system exists
  (the PID-1 ownership check), not merely when systemctl is on PATH.
- compute_tailscale_socket returns a fleet-scoped socket path under supervisord.
- compute_tailscale_socket returns an empty string for systemd / launchd so
  that those branches keep their default socket behavior unchanged.
- tailscale_socket_flag emits a --socket= flag for non-empty sockets and
  nothing at all for the empty-string case.
- The supervisord conf block uses the fleet-scoped socket path that matches
  what compute_tailscale_socket returns, so the client and the daemon agree.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "deploy" / "install-tailscale.sh").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Helper: extract a named bash function body from the script
# ---------------------------------------------------------------------------

def _extract(func: str) -> str:
    m = re.search(
        r"^%s\(\) \{\n.*?^}$" % re.escape(func),
        SCRIPT,
        re.S | re.M,
    )
    assert m, "could not extract function %s from install-tailscale.sh" % func
    return m.group(0)


def _run_bash(snippet: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# detect_supervisor: systemd absent → supervisord branch
# ---------------------------------------------------------------------------

def test_detect_supervisor_picks_supervisord_when_systemd_dir_absent():
    """The key container-node case: systemctl may be on PATH but
    /run/systemd/system does not exist (not PID 1).  supervisorctl present
    → must resolve to 'supervisord'.
    """
    fn = _extract("detect_supervisor")
    # Keep the production default literal covered by the source assertion
    # below, but make this behavioral test independent of the CI host's init
    # system. GitHub's Linux runners do have /run/systemd/system, whereas the
    # target container nodes intentionally do not.
    fn = fn.replace(
        "[ -d /run/systemd/system ]",
        '[ -d "$SYSTEMD_RUNTIME_DIR" ]',
        1,
    )
    # Override PATH so that a fake 'systemctl' and 'supervisorctl' are found
    # but NOT launchctl, and /run/systemd/system does not exist.
    snippet = r"""
SUPERVISOR_KIND=auto
%(fn)s

PATH_OVERRIDE="$(mktemp -d)"
SYSTEMD_RUNTIME_DIR="$PATH_OVERRIDE/missing-systemd-runtime"
# Fake supervisorctl (present) and systemctl (present but no /run/systemd/system)
printf '#!/bin/sh\nexit 0\n' > "$PATH_OVERRIDE/supervisorctl"
printf '#!/bin/sh\nexit 0\n' > "$PATH_OVERRIDE/systemctl"
chmod +x "$PATH_OVERRIDE/supervisorctl" "$PATH_OVERRIDE/systemctl"

# The rewritten test-only path definitely does not exist.
PATH="$PATH_OVERRIDE" SUPERVISOR_KIND=auto detect_supervisor
""" % {"fn": fn}
    result = _run_bash(snippet)
    assert result.returncode == 0, result.stderr
    supervisor = result.stdout.strip()
    assert supervisor == "supervisord"


def test_detect_supervisor_requires_systemd_dir_not_just_systemctl():
    """detect_supervisor must NOT select systemd when systemctl is on PATH
    but /run/systemd/system is absent.  This is the exact GKE pod scenario.
    """
    fn = _extract("detect_supervisor")
    # We simulate the 'auto' branch by examining the function source:
    # the gate is `command -v systemctl && [ -d /run/systemd/system ]`
    assert "[ -d /run/systemd/system ]" in fn, (
        "detect_supervisor must check /run/systemd/system to guard against "
        "container nodes where systemctl exists but is not PID 1"
    )
    # The check must come BEFORE the supervisord branch
    systemd_pos = fn.index("[ -d /run/systemd/system ]")
    supervisord_pos = fn.index("supervisorctl")
    assert systemd_pos < supervisord_pos, (
        "systemd directory check must appear before the supervisorctl check"
    )


def test_detect_supervisor_explicit_override_supervisord():
    """When SUPERVISOR_KIND=supervisord is set explicitly, no auto-detection
    occurs and 'supervisord' is returned immediately.
    """
    fn = _extract("detect_supervisor")
    result = _run_bash(
        "SUPERVISOR_KIND=supervisord\n" + fn + "\ndetect_supervisor"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "supervisord"


def test_detect_supervisor_explicit_override_systemd():
    fn = _extract("detect_supervisor")
    result = _run_bash(
        "SUPERVISOR_KIND=systemd\n" + fn + "\ndetect_supervisor"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "systemd"


def test_detect_supervisor_rejects_unknown_override():
    fn = _extract("detect_supervisor")
    result = _run_bash(
        "SUPERVISOR_KIND=upstart\n" + fn + "\ndetect_supervisor"
    )
    assert result.returncode != 0
    assert "unsupported supervisor" in result.stderr


# ---------------------------------------------------------------------------
# compute_tailscale_socket: fleet-scoped path for supervisord
# ---------------------------------------------------------------------------

def test_compute_tailscale_socket_supervisord_uses_fleet_scoped_path():
    fn = _extract("compute_tailscale_socket")
    result = _run_bash(
        "FLEET_NAME=mac\n" + fn + "\ncompute_tailscale_socket supervisord"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/run/tailscale/mac.sock"


def test_compute_tailscale_socket_supervisord_uses_fleet_name_variable():
    """Socket path must embed FLEET_NAME, not a hard-coded 'mac'."""
    fn = _extract("compute_tailscale_socket")
    result = _run_bash(
        "FLEET_NAME=gke-fleet\n" + fn + "\ncompute_tailscale_socket supervisord"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/run/tailscale/gke-fleet.sock"


def test_compute_tailscale_socket_systemd_is_empty():
    fn = _extract("compute_tailscale_socket")
    result = _run_bash(
        "FLEET_NAME=mac\n" + fn + "\ncompute_tailscale_socket systemd"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", (
        "systemd should produce an empty socket path so tailscale uses its default"
    )


def test_compute_tailscale_socket_launchd_is_empty():
    fn = _extract("compute_tailscale_socket")
    result = _run_bash(
        "FLEET_NAME=mac\n" + fn + "\ncompute_tailscale_socket launchd"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


# ---------------------------------------------------------------------------
# tailscale_socket_flag: emits flag only when socket is set
# ---------------------------------------------------------------------------

def test_tailscale_socket_flag_emits_flag_when_socket_set():
    fn = _extract("tailscale_socket_flag")
    result = _run_bash(
        "TAILSCALE_SOCKET=/run/tailscale/mac.sock\n" + fn + "\ntailscale_socket_flag"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "--socket=/run/tailscale/mac.sock"


def test_tailscale_socket_flag_emits_nothing_when_socket_empty():
    fn = _extract("tailscale_socket_flag")
    result = _run_bash(
        "TAILSCALE_SOCKET=\n" + fn + "\ntailscale_socket_flag"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", (
        "tailscale_socket_flag must produce no output for empty socket "
        "(systemd/launchd should keep default socket behavior)"
    )


# ---------------------------------------------------------------------------
# Supervisord conf block: socket path consistency
# ---------------------------------------------------------------------------

def test_supervisord_conf_uses_fleet_name_sock_path():
    """The [program:...] conf written to conf.d must use the fleet-scoped
    socket path that matches compute_tailscale_socket output.
    """
    # The conf block in install-tailscale.sh is embedded in the supervisord
    # case arm.  Verify both paths are consistent.
    assert "--socket=/run/tailscale/${FLEET_NAME}.sock" in SCRIPT, (
        "supervisord conf must start tailscaled with "
        "--socket=/run/tailscale/${FLEET_NAME}.sock"
    )


def test_supervisord_conf_uses_fleet_scoped_state_dir():
    """State directory must also be fleet-scoped to survive pod restarts."""
    assert (
        "--state=/var/lib/${FLEET_NAME}/tailscale/tailscaled.state" in SCRIPT
    ), "tailscaled state file must be fleet-scoped for persistence across restarts"


def test_supervisord_conf_has_autorestart():
    """tailscaled must have autorestart=true so it comes back after a
    supervisord restart (the core 'survives pod restart' requirement).
    """
    assert "autorestart=true" in SCRIPT, (
        "supervisord program block must set autorestart=true"
    )


def test_supervisord_socket_path_matches_compute_socket():
    """The socket path written into the supervisord conf must exactly match
    what compute_tailscale_socket returns for 'supervisord'.
    """
    fn = _extract("compute_tailscale_socket")
    result = _run_bash(
        "FLEET_NAME=mac\n" + fn + "\ncompute_tailscale_socket supervisord"
    )
    computed_socket = result.stdout.strip()
    # The conf uses ${FLEET_NAME} shell variable expansion; verify the template
    # matches the pattern that compute_tailscale_socket produces.
    assert "/run/tailscale/" in computed_socket
    assert ".sock" in computed_socket
    # Conf template uses ${FLEET_NAME} which expands to the same base dir
    assert "--socket=/run/tailscale/${FLEET_NAME}.sock" in SCRIPT


# ---------------------------------------------------------------------------
# Socket flag thread-through: all tailscale client calls must use the flag
# ---------------------------------------------------------------------------

def test_all_tailscale_status_calls_use_socket_flag():
    """Every 'tailscale status' invocation in the script must go through
    the tailscale_socket_flag helper so supervisord nodes hit the right socket.
    """
    # Find raw 'tailscale status' without the $(tailscale_socket_flag) wrapper
    raw_calls = re.findall(
        r"\btailscale\s+status\b",
        SCRIPT,
    )
    # All should be wrapped: `tailscale $(tailscale_socket_flag) status`
    unwrapped = [
        c for c in re.finditer(r"\btailscale\s+(?!\$\(tailscale_socket_flag\))status\b", SCRIPT)
        if "tailscale_socket_flag" not in SCRIPT[max(0, c.start()-30):c.start()]
    ]
    assert len(unwrapped) == 0, (
        "Found tailscale status calls not wrapped with $(tailscale_socket_flag): "
        "supervisord nodes will fail because the client won't find the daemon socket"
    )


def test_all_tailscale_up_calls_use_socket_flag():
    """Every 'tailscale up' invocation must go through tailscale_socket_flag."""
    unwrapped = list(re.finditer(
        r"\btailscale\s+(?!\$\(tailscale_socket_flag\))up\b",
        SCRIPT,
    ))
    assert len(unwrapped) == 0, (
        "Found tailscale up calls not wrapped with $(tailscale_socket_flag)"
    )


def test_socket_computed_before_wait_loop():
    """TAILSCALE_SOCKET must be assigned before the socket-readiness wait loop
    so that the wait loop uses the correct socket path under supervisord.
    """
    socket_assign_pos = SCRIPT.index('TAILSCALE_SOCKET="$(compute_tailscale_socket')
    wait_loop_pos = SCRIPT.index("# Wait for tailscaled socket to be ready")
    assert socket_assign_pos < wait_loop_pos, (
        "TAILSCALE_SOCKET must be computed before the socket-readiness wait loop"
    )
