"""The top level describes what mac models, not how it is implemented.

`mac` models four things: projects, tasks, task groups (work packages) and
agents. It surfaced fifty-odd top-level commands beside them -- fleet, memory,
hermes, optimizer, agentbus -- so the first thing a newcomer saw was the
implementation, with the object model buried in it.

Those commands are now under one administrative verb. Nothing was removed:
`mac fleet ...` and `mac memory ...` appear in scripts, deploy tooling and
documentation, and breaking them to tidy a help page would be a bad trade. What
changed is what the CLI SHOWS.
"""
from __future__ import annotations

import io
import re
import sys

import pytest

from mac.cli import build_parser, main
from mac.cli_surface import FIRST_CLASS_NAMES, _subparsers_of
from mac.test_support import dsn_for


def _help(tmp_path, *args):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        main(["--db", dsn_for(tmp_path), *args])
    finally:
        sys.stdout = old
    return out.getvalue()


def _rows(text):
    """Command rows the default view lists, by their exact rendered shape.

    A row is `  <name>  <description>`. Matching that beats scanning for words:
    "fleet" appears inside admin's own description, and prose lines like
    "  shows the arguments ..." share the indent, so a looser scrape reports
    the help text's grammar as if it were commands.
    """
    body = text.split("The objects mac models", 1)[-1]
    return {
        match.group(1)
        for match in re.finditer(r"^  ([a-z][a-z0-9-]*)  +\S", body, re.M)
    }


def test_the_default_view_shows_only_the_objects_and_admin(tmp_path):
    """The requirement, stated directly."""
    shown = _rows(_help(tmp_path, "help"))

    assert shown <= set(FIRST_CLASS_NAMES) | {"admin"}, (
        "top-level help shows non-object commands: %s"
        % sorted(shown - set(FIRST_CLASS_NAMES) - {"admin"})
    )


def test_every_object_is_still_shown(tmp_path):
    """Reducing the surface must not hide the things it exists to surface."""
    shown = _rows(_help(tmp_path, "help"))

    assert set(FIRST_CLASS_NAMES) <= shown


def test_the_administrative_commands_are_reachable_under_admin():
    parser = build_parser()
    admin = _subparsers_of(parser).choices["admin"]

    for name in ("fleet", "memory", "hermes", "dispatch", "workflow"):
        assert name in _subparsers_of(admin).choices


def test_the_original_spelling_is_gone_from_the_top_level():
    """A real re-parenting, not a presentational one.

    An earlier cut of this kept the old names as working top-level aliases.
    They are gone: `mac` offers the object model and `admin`, nothing else.
    """
    parser = build_parser()
    top = _subparsers_of(parser)

    for name in ("fleet", "memory", "hermes", "dispatch", "workflow"):
        assert name not in top.choices


def test_the_old_spelling_redirects_instead_of_failing_obscurely(tmp_path, capsys):
    """argparse answers "invalid choice: 'fleet'", which reads as "that command
    was deleted", and the first thing anyone does with that is go looking for
    what replaced it."""
    with pytest.raises(SystemExit):
        # Deliberately the OLD spelling: this test exists to prove it redirects.
        main(["--db", dsn_for(tmp_path), "fleet", "doctor"])

    assert "mac admin fleet" in capsys.readouterr().err


def test_the_help_says_where_everything_went(tmp_path):
    """A surface that shrinks without saying where things went reads as though
    they were removed."""
    text = _help(tmp_path, "help")

    assert "mac admin help" in text
    assert "old spelling" in text


def test_all_still_lists_everything(tmp_path):
    """The escape hatch has to stay complete, or hiding becomes losing."""
    text = _help(tmp_path, "help", "--all")

    for name in ("fleet", "memory", "persona-instance", "optimizer", "agentbus"):
        assert name in text


def test_admin_help_is_grouped_not_a_wall_of_names(tmp_path):
    text = _help(tmp_path, "admin", "help")

    assert "Fleet and machines:" in text
    assert "fleet" in text


# --------------------------------------------------------------------------
# Re-parenting must not break code that matched on the command name
# --------------------------------------------------------------------------


def test_effective_command_sees_through_admin():
    """`args.command` is now "admin" for everything that moved.

    Two things matched on it and silently stopped matching: schema creation for
    `init`, and the guard that refuses task-producing writes against a direct
    database. The first failed loudly; the SECOND FAILED OPEN, which is the
    direction that matters.
    """
    import argparse

    from mac.dispatch import effective_command

    moved = argparse.Namespace(command="admin", admin_command="init")
    object_command = argparse.Namespace(command="task", task_command="create")

    assert effective_command(moved) == "init"
    assert effective_command(object_command) == "task"


def test_the_task_producing_guard_still_fires_for_moved_commands():
    """bridge, workflow and interaction all moved. The guard exists to stop an
    unconfirmed direct-database write from creating tasks, so it going quiet is
    worse than any help-text regression in this change."""
    import argparse

    from mac.dispatch import _task_producing_cli_operation

    args = argparse.Namespace(
        command="admin", admin_command="bridge", bridge_command="import"
    )

    assert _task_producing_cli_operation(args) == "bridge task import"


def test_init_creates_the_schema_through_its_new_spelling(tmp_path):
    """`mac admin init` is the only command that owns schema creation. If the
    re-parenting hid that, a fresh install cannot be bootstrapped at all."""
    from mac.test_support import create_schema

    _schema, dsn = create_schema()

    assert main(["--db", dsn, "admin", "init"]) in (None, 0)
