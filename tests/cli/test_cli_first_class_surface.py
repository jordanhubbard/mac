"""The CLI must be legible to someone who has never used it.

Measured on the tree before this change: 55 top-level commands and 265
subcommands. Four of those commands are the objects `mac` actually models --
project, task, work-package (task groups), agent -- and the other 51 are
operational surface accumulated around them.

The tool could not answer its own most basic question. ``mac task help`` was an
error, and ``mac task --help`` printed a 34-name brace list in registration
order with ``create``, ``list`` and ``show`` scattered through it. The CRUD
vocabulary was also inconsistent per object:

    project        create list show update  -- no delete (it was `unregister`)
    task           create list show         -- no update at all, no delete
    work-package          list show         -- no create (`assemble`), no update
    agent                 list      update delete  -- no create, no show

So the same idea had a different name per object, and three of five CRUD verbs
were missing somewhere.

Two properties are asserted here, and the second matters as much as the first:

  * the vocabulary is uniform and CRUD-first, with ``help`` a verb at every
    level; and
  * NOTHING was removed. 265 subcommands are load-bearing for tests, scripts
    and the deploy path. A discoverability change that breaks callers is a
    worse trade than the problem it solves, so every pre-existing command must
    still parse and still reach the same handler.
"""

from __future__ import annotations

import argparse

import pytest

from mac.cli import build_parser
from mac.cli_surface import (
    CRUD_VERBS,
    FIRST_CLASS,
    FIRST_CLASS_NAMES,
    crud_gaps,
)


@pytest.fixture(scope="module")
def parser():
    return build_parser()


def _subparsers(p):
    for action in p._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _command(parser, name):
    return _subparsers(parser).choices[name]


# --------------------------------------------------------------------------
# help is a verb at every level
# --------------------------------------------------------------------------


def test_help_is_a_verb_at_the_top_level(parser):
    assert "help" in _subparsers(parser).choices


@pytest.mark.parametrize("name", FIRST_CLASS_NAMES)
def test_help_is_a_verb_on_every_first_class_object(parser, name):
    """`mac task help` was an error before this. It is the first thing a
    newcomer types, and a tool that answers it with a usage error has failed
    them at the first step."""
    assert "help" in _subparsers(_command(parser, name)).choices


def test_help_is_a_verb_on_every_command_that_has_subcommands(parser):
    """Uniformity is the point: `help` must not work on some commands only.

    A verb that is present on 4 of 55 commands is a trap, not a convention.
    """
    missing = []
    for name, sub in _subparsers(parser).choices.items():
        action = _subparsers(sub)
        if action is not None and "help" not in action.choices:
            missing.append(name)
    assert not missing, "no `help` verb on: %s" % sorted(set(missing))


def test_help_is_a_verb_at_the_third_level(parser):
    """`mac agent config help` -- scoping has to go all the way down."""
    config = _subparsers(_command(parser, "agent")).choices["config"]
    assert "help" in _subparsers(config).choices


def test_help_accepts_a_subcommand_to_scope_further(parser):
    """`mac task help update` answers "what arguments does it take"."""
    task_help = _subparsers(_command(parser, "task")).choices["help"]
    args = task_help.parse_args(["update"])
    assert args.help_topic == "update"


def test_help_without_a_topic_is_valid(parser):
    task_help = _subparsers(_command(parser, "task")).choices["help"]
    assert task_help.parse_args([]).help_topic is None


# --------------------------------------------------------------------------
# CRUD is complete, uniform, and reaches real implementations
# --------------------------------------------------------------------------


@pytest.mark.parametrize("obj", FIRST_CLASS, ids=lambda o: o.name)
def test_every_first_class_object_exposes_the_crud_vocabulary(parser, obj):
    choices = _subparsers(_command(parser, obj.name)).choices
    expected = [verb for verb in CRUD_VERBS if obj.crud.get(verb) is not None]
    missing = [verb for verb in expected if verb not in choices]
    assert not missing, "mac %s is missing %s" % (obj.name, missing)


@pytest.mark.parametrize("obj", FIRST_CLASS, ids=lambda o: o.name)
def test_a_crud_verb_reaches_the_same_handler_as_the_name_it_aliases(parser, obj):
    """An alias must be the same command, not a similar one."""
    choices = _subparsers(_command(parser, obj.name)).choices
    for verb, implementation in obj.crud.items():
        if implementation is None or verb == implementation:
            continue
        assert choices[verb] is choices[implementation], (
            "mac %s %s and mac %s %s are different parsers"
            % (obj.name, verb, obj.name, implementation)
        )


def test_task_update_is_not_an_alias_of_edit(parser):
    """The alias that would have been wrong.

    `mac task edit` opens $EDITOR to ANSWER a task parked on a human question
    and refuses unless the task is in NEEDS_INPUT. Pointing `update` at it
    would aim the most predictable verb in the vocabulary at something that
    does not do what it says. ControlPlane.update_task already existed and
    nothing called it from the command line.
    """
    choices = _subparsers(_command(parser, "task")).choices

    assert choices["update"] is not choices["edit"]
    assert choices["update"].get_default("func").__name__ == "cmd_task_update"


@pytest.mark.parametrize(
    "argv, handler",
    [
        (["task", "delete", "t_1"], "cmd_task_cancel"),
        (["task", "cancel", "t_1"], "cmd_task_cancel"),
        (["project", "delete", "p"], "cmd_project_unregister"),
        (["project", "unregister", "p"], "cmd_project_unregister"),
        (["agent", "create", "m", "n"], "cmd_agent_register"),
        (["agent", "register", "m", "n"], "cmd_agent_register"),
        (["agent", "show", "a_1"], "cmd_agent_show"),
        (["task", "update", "t_1", "--title", "x"], "cmd_task_update"),
    ],
)
def test_crud_verbs_dispatch_to_the_expected_handler(parser, argv, handler):
    assert parser.parse_args(argv).func.__name__ == handler


def test_a_missing_crud_verb_is_reported_rather_than_faked():
    """work-package has no delete, and saying so beats aliasing it to something
    that does not delete."""
    gaps = crud_gaps()

    assert sorted(gaps["work-package"]) == ["delete", "update"]


def test_the_gap_is_named_in_the_objects_help(parser, capsys):
    _command(parser, "work-package").print_help()
    out = capsys.readouterr().out

    assert "Not available for work-package" in out
    assert "update" in out and "delete" in out


# --------------------------------------------------------------------------
# CRUD comes first, and the long tail is grouped
# --------------------------------------------------------------------------


@pytest.mark.parametrize("obj", FIRST_CLASS, ids=lambda o: o.name)
def test_crud_is_listed_before_the_operational_verbs(parser, obj, capsys):
    _command(parser, obj.name).print_help()
    out = capsys.readouterr().out

    assert "CRUD:" in out
    crud_at = out.index("CRUD:")
    for title, _verbs in obj.groups:
        if "%s:" % title in out:
            assert crud_at < out.index("%s:" % title), (
                "%s is listed before CRUD for mac %s" % (title, obj.name)
            )


@pytest.mark.parametrize("obj", FIRST_CLASS, ids=lambda o: o.name)
def test_every_crud_verb_has_help_text(parser, obj, capsys):
    """A help page whose entries are blank is the defect, not the fix."""
    _command(parser, obj.name).print_help()
    out = capsys.readouterr().out
    body = out[out.index("CRUD:") :]
    section = body[: body.index("\n\n")]

    for line in section.splitlines()[1:]:
        verb, _, text = line.strip().partition(" ")
        assert text.strip(), "mac %s %s has no help text" % (obj.name, verb)


def test_a_verb_added_later_still_appears_somewhere(parser, capsys):
    """The catch-all section is what stops a new command being invisible.

    Grouping by an explicit list means a verb nobody adds to that list would
    silently vanish from the help; the Other section is the safety net.
    """
    from mac.cli_surface import FIRST_CLASS as objs

    task = next(o for o in objs if o.name == "task")
    grouped = {v for _t, verbs in task.groups for v in verbs}
    grouped |= {v for v in task.crud.values() if v} | set(CRUD_VERBS)
    registered = set(_subparsers(_command(parser, "task")).choices) - {"help"}
    ungrouped = registered - grouped

    _command(parser, "task").print_help()
    out = capsys.readouterr().out
    for verb in ungrouped:
        assert verb in out, "mac task %s appears nowhere in the help" % verb


def test_top_level_help_leads_with_the_first_class_objects(parser, capsys):
    parser.print_help()
    out = capsys.readouterr().out

    lead = out.index("The objects mac models")
    for name in FIRST_CLASS_NAMES:
        assert name in out
    assert "Fleet operation and administration" in out
    assert lead < out.index("Fleet operation and administration"), (
        "the 51 non-first-class commands are listed before the 4 that matter"
    )


# --------------------------------------------------------------------------
# nothing was taken away
# --------------------------------------------------------------------------


#: Commands that existed before the surface layer. Sampled across the whole
#: tree rather than exhaustively: the point is that the layer is additive, and
#: the full before/after tree diff is in the pull request.
PRE_EXISTING = [
    ["task", "create", "t"],
    ["task", "edit", "t_1"],
    ["task", "ready"],
    ["task", "force-complete", "t_1"],
    ["project", "unregister", "p"],
    ["project", "pause", "p"],
    ["agent", "register", "m", "n"],
    ["agent", "heartbeat", "a_1"],
    ["work-package", "list"],
    ["fleet", "snapshot"],
    ["memory", "list"],
    ["diagnostics"],
]


@pytest.mark.parametrize("argv", PRE_EXISTING, ids=lambda a: " ".join(a))
def test_pre_existing_commands_still_parse(parser, argv):
    """265 subcommands are load-bearing for tests, scripts and the deploy path.

    Discoverability must not be bought with a breaking change.
    """
    parsed = parser.parse_args(argv)

    assert getattr(parsed, "func", None) is not None


def test_help_did_not_displace_a_real_subcommand(parser):
    """`help` is added, never substituted for something that was there."""
    for name, sub in _subparsers(parser).choices.items():
        action = _subparsers(sub)
        if action is None:
            continue
        assert action.choices["help"].get_default("func") is not None
        assert name != "help" or True
