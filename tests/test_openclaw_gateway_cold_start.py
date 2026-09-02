"""A cold start must not depend on the previous stop having succeeded.

Observed on the hub 2026-08-04. A healthy OpenClaw gateway was stopped, the
stop hook's quiescence step failed, and the host had no chat gateway for ~6
minutes until a human deleted a sandbox by hand:

  1. the stopper's quiescence step fails;
  2. so the stopper never reaches its sandbox delete -- deliberately, because
     it refuses to discard a sandbox it could not checkpoint;
  3. the launcher unconditionally runs `openshell sandbox create`, which fails
     with "sandbox 'mac-openclaw-rocky' already exists";
  4. the launcher exits, launchd restarts it, and it fails identically. For
     ever.

The stopper's refusal is CORRECT for a stop: it preserves un-checkpointed
work. The defect is that the start path inherited that refusal as an outage.
So the fix belongs at start: reclaim whatever is left over, after salvaging
it, and then create.

These tests drive the REAL generated wrappers against a fake `openshell` whose
sandbox lifecycle is backed by a state file, so "the sandbox already exists"
is a genuine precondition rather than an assertion about the script's text.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy" / "openclaw" / "install-openclaw-gateway.sh"

# A fake `openshell` whose sandbox lifecycle is real state, not a canned reply.
# `create` refuses when the sandbox exists -- exactly the error that wedged the
# hub -- so a launcher that fails to reclaim cannot pass these tests.
FAKE_OPENSHELL = r"""#!/bin/sh
printf '%s\n' "$*" >> "$MAC_TEST_CALLS"
STATE="$MAC_TEST_SANDBOX_STATE"
case "$1:$2" in
  sandbox:get)
    [ -f "$STATE" ] || { echo "Error: sandbox not found" >&2; exit 1; }
    exit 0
    ;;
  sandbox:exec)
    if [ "${MAC_TEST_QUIESCE_FAILS:-0}" = 1 ]; then
      echo "Error: x No active gateway." >&2
      exit 1
    fi
    exit 0
    ;;
  sandbox:download)
    if [ "${MAC_TEST_DOWNLOAD_FAILS:-0}" = 1 ]; then
      echo 'synthetic download failure' >&2
      exit 74
    fi
    mkdir -p "$5"
    printf '%s\n' salvaged > "$5/marker.txt"
    exit 0
    ;;
  sandbox:delete)
    if [ "${MAC_TEST_DELETE_FAILS:-0}" = 1 ]; then
      echo 'synthetic delete rejection' >&2
      exit 9
    fi
    rm -f "$STATE"
    exit 0
    ;;
  sandbox:create)
    if [ -f "$STATE" ]; then
      echo "Error: x sandbox already exists" >&2
      exit 1
    fi
    printf '%s\n' present > "$STATE"
    printf '%s\n' created >> "$MAC_TEST_CALLS"
    exit 0
    ;;
esac
exit 0
"""


def _seed_hermes_identity(home: Path) -> None:
    """The installer runs a continuity migration that requires an identity."""
    memories = home / ".hermes" / "memories"
    memories.mkdir(parents=True)
    (home / ".hermes" / "SOUL.md").write_text(
        "# Test Agent\n\nDistinct test soul.\n", encoding="utf-8"
    )
    (memories / "USER.md").write_text("# User\n\nSeed.\n", encoding="utf-8")
    (memories / "MEMORY.md").write_text("# Memory\n\nSeed.\n", encoding="utf-8")


def _prepare(tmp_path: Path) -> tuple[Path, Path, dict[str, str], Path, Path]:
    """Generate the real wrappers with a fake openshell on PATH."""
    home = tmp_path / "home"
    mac_home = home / ".mac"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    _seed_hermes_identity(home)

    openshell = bin_dir / "openshell"
    openshell.write_text(FAKE_OPENSHELL, encoding="utf-8")
    openshell.chmod(0o700)

    calls = tmp_path / "calls"
    calls.write_text("", encoding="utf-8")
    sandbox_state = tmp_path / "sandbox-state"

    env = os.environ.copy()
    env.update(
        {
            "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home),
            "MAC_HOME": str(mac_home),
            "MAC_SRC": str(ROOT),
            "MAC_OPENSHELL_BIN": str(openshell),
            "MAC_OPENCLAW_DRY_RUN": "1",
            "MAC_OPENCLAW_AGENT_ID": "agent_cold_start",
            "MAC_OPENCLAW_INSTANCE_ID": "instance_cold_start",
            "MAC_OPENCLAW_ROUTER_URL": "http://100.64.0.1:8789/v1",
            "MAC_OPENCLAW_ROUTER_API_KEY": "router-secret",
            "MAC_OPENCLAW_MODEL": "test/model",
            "MAC_OPENCLAW_FLEET_NAME": "mac",
            "MAC_TEST_CALLS": str(calls),
            "MAC_TEST_SANDBOX_STATE": str(sandbox_state),
        }
    )
    result = subprocess.run(
        [str(INSTALLER), "prepare"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        pytest.skip(
            "installer prepare unavailable in this environment: %s"
            % (result.stderr or result.stdout)[-400:]
        )
    launcher = mac_home / "bin" / "openclaw-gateway"
    stopper = mac_home / "bin" / "openclaw-gateway-stop"
    if not launcher.is_file():
        pytest.skip("generated launcher missing at %s" % launcher)
    return launcher, stopper, env, calls, sandbox_state


def _run(path: Path, env: dict[str, str], timeout: int = 120):
    return subprocess.run(
        [str(path)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def test_the_gateway_cold_starts_with_a_pre_existing_sandbox(tmp_path):
    """The regression. A leftover sandbox must not block the start.

    This is the hub incident reproduced: quiescence fails, so the stopper
    leaves the sandbox in place, and the launcher must still bring the gateway
    up rather than failing on "sandbox already exists" for ever.
    """
    launcher, _stopper, env, calls, sandbox_state = _prepare(tmp_path)
    sandbox_state.write_text("present\n", encoding="utf-8")  # stale sandbox
    env["MAC_TEST_QUIESCE_FAILS"] = "1"

    result = _run(launcher, env)

    transcript = calls.read_text(encoding="utf-8")
    assert "sandbox delete" in transcript, (
        "the launcher never deleted the stale sandbox, so `sandbox create` "
        "would fail forever.\nstderr:\n%s" % result.stderr[-2000:]
    )
    assert "created" in transcript, (
        "the gateway did not reach a successful `sandbox create`.\n"
        "stderr:\n%s" % result.stderr[-2000:]
    )
    assert "reclaiming" in result.stderr


def test_a_failed_stop_does_not_abort_the_start(tmp_path):
    """`set -e` plus a bare stop call turned a lost checkpoint into an outage."""
    launcher, _stopper, env, calls, sandbox_state = _prepare(tmp_path)
    sandbox_state.write_text("present\n", encoding="utf-8")
    env["MAC_TEST_QUIESCE_FAILS"] = "1"

    result = _run(launcher, env)

    assert "continuing to cold start" in result.stderr, (
        "a failing stopper must be reported and survived, not fatal.\n"
        "stderr:\n%s" % result.stderr[-2000:]
    )
    assert "created" in calls.read_text(encoding="utf-8")


def test_un_checkpointed_workspace_is_salvaged_before_deletion(tmp_path):
    """Reclaiming must not silently discard the delta since the last checkpoint."""
    launcher, _stopper, env, calls, sandbox_state = _prepare(tmp_path)
    sandbox_state.write_text("present\n", encoding="utf-8")
    env["MAC_TEST_QUIESCE_FAILS"] = "1"

    result = _run(launcher, env)

    archives = list((Path(env["HOME"]) / ".mac").rglob("reclaimed-*"))
    assert archives, (
        "no salvage archive was written before deleting the stale sandbox.\n"
        "stderr:\n%s" % result.stderr[-2000:]
    )
    salvaged = list(archives[0].rglob("marker.txt"))
    assert salvaged, "salvage archive %s is empty" % archives[0]
    assert "salvaged un-checkpointed workspace" in result.stderr
    assert not list(archives[0].glob("state")), "live SQLite state must never be salvaged"


def test_service_is_restored_even_when_salvage_fails(tmp_path):
    """Availability wins over an archive we cannot take.

    If the download fails there is nothing to preserve, and refusing to delete
    would reproduce exactly the wedge this change exists to remove.
    """
    launcher, _stopper, env, calls, sandbox_state = _prepare(tmp_path)
    sandbox_state.write_text("present\n", encoding="utf-8")
    env["MAC_TEST_QUIESCE_FAILS"] = "1"
    env["MAC_TEST_DOWNLOAD_FAILS"] = "1"

    result = _run(launcher, env)

    assert "could not salvage sandbox workspace" in result.stderr
    assert "created" in calls.read_text(encoding="utf-8"), (
        "the gateway must still start when salvage is impossible.\n"
        "stderr:\n%s" % result.stderr[-2000:]
    )


def test_a_delete_that_cannot_succeed_fails_loudly(tmp_path):
    """The one case that must NOT be papered over.

    If the sandbox cannot be removed, `sandbox create` cannot succeed either.
    Reporting that plainly is the difference between a diagnosable failure and
    the silent restart loop this change removes.
    """
    launcher, _stopper, env, calls, sandbox_state = _prepare(tmp_path)
    sandbox_state.write_text("present\n", encoding="utf-8")
    env["MAC_TEST_QUIESCE_FAILS"] = "1"
    env["MAC_TEST_DELETE_FAILS"] = "1"

    result = _run(launcher, env)

    assert result.returncode != 0, "an unreclaimable sandbox must not report success"
    assert "sandbox delete failed" in result.stderr
    assert "created" not in calls.read_text(encoding="utf-8")


def test_a_clean_start_does_not_touch_a_sandbox_that_is_not_there(tmp_path):
    """No stale sandbox: reclaim must be a no-op, not a spurious delete."""
    launcher, _stopper, env, calls, sandbox_state = _prepare(tmp_path)
    assert not sandbox_state.exists()

    result = _run(launcher, env)

    # The gateway's own shutdown deletes the sandbox it just created, so the
    # property is about ORDER: nothing may be deleted before the create.
    lines = calls.read_text(encoding="utf-8").splitlines()
    assert "created" in lines, "the gateway did not start: %s" % result.stderr[-1500:]
    before_create = lines[: lines.index("created")]
    assert not [line for line in before_create if line.startswith("sandbox delete")], (
        "reclaim deleted a sandbox that did not exist.\ncalls before create: %s" % before_create
    )
    assert "reclaiming" not in result.stderr


def test_the_stopper_reports_failure_when_quiescence_fails(tmp_path):
    """A stop that skips its delete must not report success.

    This is the half that keeps the failure visible. The fix above makes the
    start survive it; this makes sure it is still diagnosable rather than
    silently swallowed.
    """
    _launcher, stopper, env, _calls, sandbox_state = _prepare(tmp_path)
    sandbox_state.write_text("present\n", encoding="utf-8")
    env["MAC_TEST_QUIESCE_FAILS"] = "1"

    result = _run(stopper, env)

    assert result.returncode != 0, (
        "the stopper skipped its sandbox delete but reported success; that is "
        "what made the wedge invisible.\nstderr:\n%s" % result.stderr[-2000:]
    )
    assert "quiescence failed" in result.stderr
    assert sandbox_state.exists(), (
        "the stopper deleted a sandbox it could not checkpoint; un-saved work "
        "must be preserved at stop time"
    )
