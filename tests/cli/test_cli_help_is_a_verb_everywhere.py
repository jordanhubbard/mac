"""`help` must be a verb at EVERY level, including leaves.

install_help_verbs adds a `help` subcommand wherever there are subcommands, so
`mac task help` works. A leaf command has no subcommands, so there was nowhere
to add it -- and `help` became just another positional value.

`mac task create help` therefore filed a task titled "help": a beginner
exploring the CLI wrote junk into the ledger and learned nothing. That is worse
than an error, because it succeeds.
"""
from __future__ import annotations

import io
import re
import sys

import pytest

from mac.cli import build_parser, main
from mac.cli_surface import FIRST_CLASS, leaf_help_request
from mac.test_support import control_plane_on, dsn_for


def _run(tmp_path, *args):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", dsn_for(tmp_path), *args])
    finally:
        sys.stdout = old
    return rc, out.getvalue()


def test_help_after_a_leaf_command_prints_help(tmp_path):
    rc, text = _run(tmp_path, "task", "create", "help")

    assert rc == 0
    assert "usage: mac task create" in text


def test_help_after_a_leaf_command_creates_nothing(tmp_path):
    """The actual defect. It is not enough that help is printed; the task must
    not exist."""
    cp = control_plane_on(dsn_for(tmp_path))
    cp.create_project("mac", dispatch_paused=False)

    _run(tmp_path, "task", "create", "help", "--project", "mac")

    assert [task for task in cp.list_tasks() if task.title == "help"] == []


def test_the_escape_for_a_literal_help_value_is_documented(tmp_path):
    """Refusing to create a task called "help" without saying how would trade
    one surprise for another."""
    _rc, text = _run(tmp_path, "task", "create", "help")

    assert "-- help" in text


def test_a_group_level_help_verb_still_works(tmp_path):
    rc, text = _run(tmp_path, "task", "help")

    assert rc == 0
    assert "CRUD:" in text


def test_a_normal_task_title_is_untouched(tmp_path):
    """Only the exact word `help` in the first positional slot is intercepted."""
    parser = build_parser()

    assert leaf_help_request(parser, ["task", "create", "helpful thing"]) is None
    assert leaf_help_request(parser, ["task", "create", "help me"]) is None


def test_help_is_not_intercepted_where_it_is_a_real_subcommand():
    """At a group level argparse routes the help VERB; intercepting there would
    bypass the scoped, grouped output it produces."""
    parser = build_parser()

    assert leaf_help_request(parser, ["task", "help"]) is None


@pytest.mark.parametrize("obj", [obj.name for obj in FIRST_CLASS])
def test_every_first_class_subcommand_documents_itself(obj, tmp_path):
    """A beginner is told to run `mac <object> help`. A bare verb with nothing
    beside it tells them nothing, and there is nowhere else to look.

    Asserted against the RENDERED output rather than argparse internals:
    _install_grouped_help renders the text and clears argparse's own choice
    entries to control formatting, so inspecting _choices_actions reports every
    command as undocumented -- including ones that plainly are not.
    """
    _rc, text = _run(tmp_path, obj, "help")

    body = text.split("%s -- " % obj, 1)[-1]
    bare = [
        line.strip()
        for line in body.splitlines()
        # A listed command is indented; a bare one has nothing after the name.
        if re.fullmatch(r"  [a-z][a-z0-9-]*", line.rstrip())
    ]

    assert bare == [], "mac %s help lists these as bare verbs: %s" % (obj, bare)
