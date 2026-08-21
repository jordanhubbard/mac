"""The persona-instance commands are named for what they register.

Every agent runs OpenClaw; a personality is a SOUL.md file
(`human_interface_profile.IDENTITY_FILES`). The command group that binds a
persona to an agent was still called `hermes`, after a runtime nothing uses, so
staffing an OpenClaw fleet meant typing `mac admin hermes register` in front of
an audience.

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
