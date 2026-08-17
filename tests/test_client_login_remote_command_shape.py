"""`mac admin login --ssh` must invoke the CLI the remote actually has.

PR #317 ("Refactor the mac CLI around its object model", 2026-08-09) moved
``client`` under ``admin``. ``client_login.py`` kept building the OLD remote
command, so every SSH enrollment failed against any host running #317 or later:

    $ mac admin login --ssh jkh@puck.local
    SSH enrollment command failed (exit 2); verify hub MAC installation and
    requested scopes

    $ ssh jkh@puck.local '~/.local/bin/mac --json client enroll --help'
    mac: `client` moved under `admin`. Run `mac admin client` (or
         `mac admin help` to see everything there).
    exit 2

Five call sites were stale, not one -- enroll, renew, and three revokes -- so
login, renewal AND logout were all broken over SSH.

Two failures compounded here, and the second is why it took a manual SSH to
diagnose:

1. The refactor moved the CLI but not its own in-tree callers. Nothing tied the
   two together, so the break was invisible until someone ran it against a real
   host.
2. ``_run_remote_json`` reported only the exit code and discarded the remote's
   stderr -- which said exactly what was wrong. The advice it printed instead
   ("verify hub MAC installation and requested scopes") was actively
   misleading: both were fine.

These tests pin the command shape without needing a remote, and pin that a
remote failure surfaces the remote's own words.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parent.parent / "src" / "mac" / "client_login.py"


def _text() -> str:
    return SOURCE.read_text(encoding="utf-8")


def test_no_remote_invocation_uses_the_pre_317_client_group():
    """Every remote `client` call must be prefixed with `admin`."""
    text = _text()
    stale = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.search(r'"client"\s*,', line):
            # Look back a few lines for the `admin` element that must precede it
            # in the same argv list.
            window = " ".join(lines[max(0, i - 3) : i + 1])
            if '"admin"' not in window:
                stale.append((i + 1, line.strip()))
    assert not stale, (
        "these remote invocations still use the pre-#317 `client` group and "
        "will fail with exit 2 against any host running #317 or later: %s"
        % stale
    )


def test_the_revoke_one_liner_is_also_admin_scoped():
    """The single-line argv is easy to miss in a bulk edit."""
    text = _text()
    assert '"admin", "client", "revoke"' in text, (
        "the inline logout revoke argv was not updated to the admin group"
    )


def test_a_failed_remote_command_surfaces_the_cli_guidance_line():
    """The exit code alone is not a diagnosis -- but stderr is not safe to echo.

    Enrollment carries tokens, and `test_remote_json_and_action_fail_closed`
    pins that a secret in the remote's output must never reach this message.
    That guarantee stands. What is surfaced is only the CLI's OWN guidance
    lines, which begin with "mac: ": program-authored, not data, and exactly
    the class of message that explains a command-shape mismatch.
    """
    text = _text()
    assert 'startswith("mac: ")' in text, (
        "_run_remote_json does not surface the remote CLI's guidance line; a "
        "caller sees only an exit code while the remote's own explanation "
        "(e.g. '`client` moved under `admin`') is thrown away"
    )
    assert "result.stdout" not in text.split("guidance = [")[1].split("]")[0], (
        "stdout must not be echoed: it carries the enrollment manifest"
    )
