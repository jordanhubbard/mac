"""CodeGraph: `make install` provisions it; the build never requires it.

Two separate properties, and they are not in tension:

  * `make install` INSTALLS CodeGraph when it is absent, because skipping it
    silently strands the user later -- `litai init`, the skills and the
    coding-CLI paths all expect it, and that failure surfaces far from the
    install that omitted it.
  * every other target DEGRADES without it, because nothing in the build,
    the test run or the deploy needs an index.

REPORTED FROM A REAL MACHINE. `make install` on puck.local:

    CodeGraph is required but 'codegraph' was not found on PATH
    make: *** [codegraph-sync] Error 127

Before this, `codegraph-sync` was a prerequisite of install, install-cli, install-gui,
build-cli, build-gui, test, coverage, setup and deploy, so exiting 127 when the
binary is absent made every one of those targets fail on a machine that had
never installed an optional developer tool.

WHY IT IS OPTIONAL, on the evidence rather than by assertion:

  * no index is committed -- `.codegraph/` is generated locally, so a fresh
    clone has never had one
  * `scripts/resolve-impacted-tests.py` treats an unavailable CodeGraph as a
    reason to FAIL CLOSED to a full test run, and CI has done exactly that:
    "sanity selection: full (codegraph_unavailable)"
  * nothing in the wheel build or the CLI link reads the index

So the absence of CodeGraph is a supported, already-tested condition
everywhere except the one script that refused to proceed without it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sync-codegraph.sh"


def _run(env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # A PATH with no codegraph on it, whatever the developer has installed.
    env["PATH"] = "/usr/bin:/bin"
    env["MAC_CODEGRAPH_BIN"] = "definitely-not-a-real-codegraph-binary"
    env.pop("MAC_REQUIRE_CODEGRAPH", None)
    env.update(env_extra or {})
    return subprocess.run(
        ["sh", str(SCRIPT)], capture_output=True, text=True, timeout=60, env=env
    )


def test_a_missing_codegraph_does_not_fail_the_build():
    """The reported bug. Exit 0, so `make install` proceeds."""
    result = _run()

    assert result.returncode == 0, (
        "sync-codegraph.sh exited %d; every make target that depends on it "
        "(install, build, test, deploy) fails on a machine without CodeGraph"
        % result.returncode
    )


def test_it_says_what_it_skipped_rather_than_passing_silently():
    """Degrading quietly is its own failure: a developer who wanted the index
    must be able to tell that they did not get one."""
    result = _run()

    assert "not on PATH" in result.stderr
    assert "skipping" in result.stderr.lower()


def test_it_says_the_build_does_not_need_it():
    """Without this the warning reads like a problem to fix before continuing."""
    result = _run()

    assert "does not need it" in result.stderr


def test_it_still_says_how_to_install_it():
    result = _run()

    assert "install.sh" in result.stderr


def test_strict_mode_restores_the_hard_failure():
    """A machine where the index is meant to exist can still demand it."""
    result = _run({"MAC_REQUIRE_CODEGRAPH": "1"})

    assert result.returncode == 127
    assert "MAC_REQUIRE_CODEGRAPH=1" in result.stderr


def test_strict_mode_is_off_by_default():
    """Opt-in, not opt-out: the default must be the one that installs."""
    assert _run().returncode == 0


@pytest.mark.parametrize(
    "target", ["install", "install-cli", "install-gui", "build-cli", "test"]
)
def test_the_affected_targets_still_depend_on_the_sync(target):
    """The fix is to the script, NOT to the wiring. These targets should still
    refresh the index when CodeGraph IS present -- degrading is about the
    machine that lacks it, not about dropping the step."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    line = next(
        line for line in makefile.splitlines() if line.startswith(target + ":")
    )
    assert "codegraph-sync" in line or target == "install"


# --------------------------------------------------------------------------
# `make install` provisions it
# --------------------------------------------------------------------------

INSTALLER = ROOT / "scripts" / "install-codegraph.sh"


def _install(env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = "/usr/bin:/bin"
    env.pop("MAC_SKIP_CODEGRAPH_INSTALL", None)
    env.update(env_extra or {})
    return subprocess.run(
        ["sh", str(INSTALLER)], capture_output=True, text=True, timeout=60, env=env
    )


def test_install_depends_on_provisioning_codegraph():
    """The reason this exists: an install without CodeGraph is an install that
    fails later, at `litai init`, far from the cause."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    line = next(l for l in makefile.splitlines() if l.startswith("install:"))

    assert "install-codegraph" in line
    assert "install-codegraph:" in makefile


def test_an_already_installed_codegraph_is_left_alone():
    """No reinstall, no network call, on the common path."""
    result = _install({"MAC_CODEGRAPH_BIN": "sh"})

    assert result.returncode == 0
    assert "already installed" in result.stdout


def test_it_can_be_declined():
    """Air-gapped machines, or an operator choosing their own method."""
    result = _install({
        "MAC_CODEGRAPH_BIN": "definitely-not-real-codegraph",
        "MAC_SKIP_CODEGRAPH_INSTALL": "1",
    })

    assert result.returncode == 0
    assert "skipping" in result.stderr
    # Declining must still say what it will cost, and when.
    assert "litai init" in result.stderr


def test_declining_still_leaves_a_working_build():
    """The CLI the user asked for must install either way."""
    result = _install({
        "MAC_CODEGRAPH_BIN": "definitely-not-real-codegraph",
        "MAC_SKIP_CODEGRAPH_INSTALL": "1",
    })

    assert result.returncode == 0
    assert "without it" in result.stderr


def test_the_installer_url_is_overridable():
    """A mirror, or a pinned revision, without editing the script."""
    text = INSTALLER.read_text(encoding="utf-8")

    assert "MAC_CODEGRAPH_INSTALLER_URL" in text


def test_it_announces_the_network_fetch_rather_than_doing_it_quietly():
    """`curl | sh` is a supply-chain action. An operator must be able to see it
    happen, and to decline."""
    text = INSTALLER.read_text(encoding="utf-8")

    assert 'echo "codegraph: not found; installing from' in text
    assert "MAC_SKIP_CODEGRAPH_INSTALL=1 to decline" in text


# --------------------------------------------------------------------------
# a stale venv is recreated, not reused
# --------------------------------------------------------------------------


def test_the_venv_interpreter_is_checked_not_just_the_bootstrapping_one():
    """Two different interpreters, and only one was ever checked.

    `sys.version_info < MIN_PYTHON` guards the interpreter running
    bootstrap-project.py. An EXISTING .venv was then reused unconditionally, so
    one built years ago on 3.9 survived every `make install` as long as the
    interpreter you invoked it with was modern -- and editable installs landed
    in an interpreter mac does not support. The failure arrives later, as an
    import or syntax error somewhere unrelated to installing.
    """
    source = (ROOT / "scripts" / "bootstrap-project.py").read_text(encoding="utf-8")

    assert "def venv_python_is_supported(" in source
    assert "venv_python_is_supported()" in source


def test_an_unreadable_venv_is_not_deleted():
    """Deleting someone's environment on a failed probe is worse than
    proceeding: an unreadable venv is a different problem."""
    source = (ROOT / "scripts" / "bootstrap-project.py").read_text(encoding="utf-8")
    body = source.split("def venv_python_is_supported(")[1].split("\ndef ")[0]

    assert body.count("return True") >= 2, (
        "both the non-zero-exit and unparseable-version paths must return True "
        "so a probe failure never destroys an environment"
    )


def test_the_minimum_is_stated_once():
    """A version floor written twice drifts."""
    source = (ROOT / "scripts" / "bootstrap-project.py").read_text(encoding="utf-8")

    assert "MIN_PYTHON = (3, 11)" in source
    assert "(3, 11)" not in source.replace("MIN_PYTHON = (3, 11)", "")
