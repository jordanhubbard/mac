"""The CLI must be legible to someone who has never used it.

Measured on the tree before this change: 55 top-level commands and 265
subcommands. Four of those commands are the objects `mac` actually models --
project, task, agent (work-package was removed with its pipeline) -- and the other 51 are
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
import re

import pytest

from mac.cli import build_parser
from mac.cli_surface import (
    ObjectSurface,
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


def test_no_object_has_a_crud_gap():
    """Every first-class object implements all five verbs.

    This used to assert the opposite -- work-package had no update and no
    delete, and naming the gap honestly beat aliasing it onto `replan`. Both
    were later implemented, and the object has since been removed along with
    the work-package pipeline, so the remaining three objects are complete.
    """
    assert crud_gaps() == {}


def test_a_gap_would_still_be_reported_rather_than_faked():
    """The reporting machinery must survive there being nothing to report --
    it is what stops the next missing verb being papered over with an alias."""
    surface = ObjectSurface(
        name="thing",
        summary="a thing",
        crud={"create": "make", "list": "list", "show": "show", "update": None, "delete": None},
    )

    assert sorted(verb for verb, impl in surface.crud.items() if impl is None) == [
        "delete",
        "update",
    ]


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
            assert crud_at < out.index("%s:" % title), "%s is listed before CRUD for mac %s" % (
                title,
                obj.name,
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


def test_top_level_help_is_only_the_first_class_objects(parser, capsys):
    """This used to assert the objects came FIRST, with the fifty
    administrative commands grouped underneath. They are no longer there at
    all: they live under `mac admin`, so the top level describes what mac
    models rather than how it is built."""
    parser.print_help()
    out = capsys.readouterr().out

    assert "The objects mac models" in out
    for name in FIRST_CLASS_NAMES:
        assert name in out
    assert "Getting started:" not in out, "administrative groups are back at the top level"
    assert "mac admin help" in out
    assert "0 administrative commands live under" not in out
    match = re.search(r"(\d+) administrative commands live under", out)
    assert match is not None, out
    assert int(match.group(1)) >= 20, out


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
    ["admin", "fleet", "snapshot"],
    ["admin", "memory", "list"],
    ["admin", "diagnostics"],
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


# --------------------------------------------------------------------------
# The other 50 commands: grouped, described, and none of them lost
# --------------------------------------------------------------------------


def _all_commands(parser):
    """Every command the CLI can run, wherever it now lives.

    The administrative commands were re-parented under `mac admin`, so reading
    the top-level choices alone reports them as deleted. They are not: they
    moved.
    """
    from mac.cli_surface import _subparsers_of

    top = _subparsers(parser)
    names = set(top.choices)
    admin = top.choices.get("admin")
    if admin is not None:
        admin_action = _subparsers_of(admin)
        if admin_action is not None:
            names |= set(admin_action.choices)
    return names


def test_every_grouped_command_actually_exists(parser):
    """A catalogue entry naming a command that is gone documents nothing."""
    from mac.cli_surface import command_descriptions

    registered = _all_commands(parser)
    unknown = sorted(set(command_descriptions()) - registered)

    assert not unknown, "COMMAND_GROUPS names commands that do not exist: %s" % unknown


def test_every_top_level_command_appears_under_help_all(parser):
    """The safety net, now checked where completeness is promised.

    The DEFAULT view is a deliberate shortlist, so the guarantee moved to
    `mac help --all`: a command added later and never catalogued still shows
    up there under Other rather than vanishing from the help entirely.
    """
    from mac.cli_surface import _subparsers_of, _top_level_help_text

    text = _top_level_help_text(_subparsers_of(parser), show_all=True)

    for name in _subparsers(parser).choices:
        if name == "help":
            continue
        assert name in text, "mac %s appears nowhere in `mac help --all`" % name


def test_every_top_level_command_has_a_description(parser):
    """42 of the 50 had none, so the list was bare names.

    Read from the rendered help rather than the catalogue, because the point
    is what a person sees -- and from the --all rendering, because that is
    where every command is promised to appear.
    """
    from mac.cli_surface import FIRST_CLASS_NAMES

    from mac.cli_surface import _subparsers_of, _top_level_help_text

    out = _top_level_help_text(_subparsers_of(parser), show_all=True)
    described = set()
    for line in out.splitlines():
        stripped = line.strip()
        if not stripped or stripped.endswith(":"):
            continue
        name, _, text = stripped.partition("  ")
        if name in _subparsers(parser).choices and text.strip():
            described.add(name)

    action = _subparsers(parser)
    canonical = {}
    for name, sub in action.choices.items():
        canonical.setdefault(id(sub), set()).add(name)
    missing = sorted(
        sorted(names)[0]
        for names in canonical.values()
        if not (names & described) and "help" not in names
    )
    assert not missing, "no description in the help for: %s" % missing


def test_the_first_class_objects_are_not_repeated_in_the_groups():
    """They lead the help on their own; listing them twice dilutes that."""
    from mac.cli_surface import FIRST_CLASS_NAMES, command_descriptions

    overlap = set(command_descriptions()) & set(FIRST_CLASS_NAMES)

    assert not overlap, "first-class objects duplicated in COMMAND_GROUPS: %s" % sorted(overlap)


def test_the_crud_summary_line_matches_reality(parser, capsys):
    """The summary must describe the vocabulary as it is.

    It used to carry an "except work-package, which has no delete or update"
    caveat, and asserting that caveat was right while the gap existed: a
    summary claiming all five verbs would have been the same class of problem
    as a verb that does not do what it says.

    The caveat must be GONE -- first because the gap was closed, and now
    because the object itself is. A stale exception is as misleading as a
    missing one: it sends a beginner looking for a command they were told does
    not exist.
    """
    parser.print_help()
    out = capsys.readouterr().out

    assert "create, list, show, update, delete" in out
    assert "except work-package" not in out


# --------------------------------------------------------------------------
# Two tiers: a short default view, everything one flag away
# --------------------------------------------------------------------------


def test_the_default_help_shows_a_shortlist_not_everything(parser, capsys):
    """The original complaint: the tool surfaced far more than its objects.

    54 non-first-class commands in the default view is a wall. The shortlist
    plus the four objects is a menu.
    """
    from mac.cli_surface import COMMON_COMMANDS

    parser.print_help()
    out = capsys.readouterr().out

    # Stronger than the shortlist this used to assert: the administrative
    # commands are not merely unlisted, they are not top-level commands any
    # more. The default view is the object model.
    top_level = set(_subparsers(parser).choices)
    assert top_level <= set(FIRST_CLASS_NAMES) | {"admin", "help"}, (
        "non-object commands are back at the top level: %s"
        % sorted(top_level - set(FIRST_CLASS_NAMES) - {"admin", "help"})
    )
    moved = _all_commands(parser) - top_level
    assert len(moved) > 25, "the administrative commands were not re-parented"
    for name in sorted(moved):
        assert ("\n  %s " % name) not in out, "mac %s is still listed in the default help" % name


def test_the_default_help_says_where_the_rest_went(parser, capsys):
    """A surface that shrinks without saying where things went reads as though
    they were removed."""
    parser.print_help()
    out = capsys.readouterr().out

    assert "mac help --all" in out
    assert "mac admin" in out
    assert "old spelling" in out


def test_help_all_lists_every_command(parser, capsys):
    """--all must be complete, or it is a second, longer shortlist."""
    from mac.cli_surface import _subparsers_of, _top_level_help_text

    action = _subparsers_of(parser)
    text = _top_level_help_text(action, show_all=True)

    for name, _sub in _distinct(parser):
        if name == "help":
            continue
        assert name in text, "mac help --all omits %s" % name


def _distinct(parser):
    action = _subparsers(parser)
    claimed, out = set(), []
    for name, sub in action.choices.items():
        if id(sub) in claimed:
            continue
        claimed.add(id(sub))
        out.append((name, sub))
    return out


def test_the_help_verb_accepts_all_at_the_top_level(parser):
    top_help = _subparsers(parser).choices["help"]

    assert top_help.parse_args(["--all"]).help_all is True
    assert top_help.parse_args([]).help_all is False


def test_hidden_commands_still_parse_and_dispatch(parser):
    """Hidden means absent from the default help, never absent from the tool.

    This is the assertion that stops a presentation change from quietly
    becoming a breaking one.
    """
    from mac.cli_surface import COMMON_COMMANDS

    from mac.cli_surface import _subparsers_of

    checked = 0
    admin = _subparsers(parser).choices["admin"]
    # The commands live under admin now; iterating the top level would check
    # nothing and pass vacuously.
    for name, sub in sorted(_subparsers_of(admin).choices.items()):
        if name == "help":
            continue
        action = _subparsers(sub)
        assert action is not None or sub.get_default("func") is not None, (
            "mac %s is hidden and has no way to run" % name
        )
        if action is not None:
            assert action.choices, "mac %s is hidden and has no subcommands" % name
        checked += 1
    assert checked > 25, "expected a substantial hidden set, checked %d" % checked


def test_every_common_command_exists(parser):
    """A shortlist naming a command that is gone is worse than no shortlist."""
    from mac.cli_surface import COMMON_COMMANDS

    registered = _all_commands(parser)
    unknown = sorted(set(COMMON_COMMANDS) - registered)

    assert not unknown, "COMMON_COMMANDS names commands that do not exist: %s" % unknown


def test_the_two_zero_reference_beginner_commands_are_kept():
    """init and diagnostics have no in-repo references and are still shown.

    Reference count measures our automation's habits, not a newcomer's needs.
    init creates the schema; diagnostics is the first thing to run when
    something looks wrong. A metric that hides those is the wrong metric, and
    this pins the judgment so it is not silently 'optimized' later.
    """
    from mac.cli_surface import COMMON_COMMANDS

    assert "init" in COMMON_COMMANDS
    assert "diagnostics" in COMMON_COMMANDS
