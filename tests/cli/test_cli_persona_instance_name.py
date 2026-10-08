"""The persona-instance commands are named for what they register.

A personality is a SOUL.md file in the agent's Hermes home. The command group
that binds a persona to an agent is named `persona-instance` for what it
registers.

`hermes` stays as an alias: the deploy script, the adapter's emitted command
strings and the integration docs all still spell it that way, and renaming
those is a separate, larger piece of work (task_2a7df680).
"""

from __future__ import annotations

import io
import sys

import pytest

from mac.cli import main


def _run(*args):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(list(args))
    finally:
        sys.stdout = old
    return rc, out.getvalue()


def test_the_group_is_reachable_by_its_new_name():
    rc, out = _run("admin", "persona-instance", "help")
    assert rc in (None, 0)
    assert "register" in out


def test_hermes_still_works_as_an_alias():
    """Breaking it would break the deploy script mid-release."""
    rc, out = _run("admin", "hermes", "help")
    assert rc in (None, 0)
    assert "register" in out
