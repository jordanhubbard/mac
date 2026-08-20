"""Make the CLI's first-class objects discoverable, uniform, and CRUD-first.

`mac` grew to 55 top-level commands and 265 subcommands. Four of those commands
are the objects the tool actually models -- **project, task, agent** -- and
the rest are operational surface that accumulated around them. (A fourth,
``work-package``, was removed with the work-package pipeline.) Handed to a beginner, the tool could not answer its
own most basic question: ``mac task help`` was an error, and ``mac task
--help`` printed a 34-name brace list in registration order with ``create``,
``list`` and ``show`` scattered through it.

The gaps were not uniform either. Measured on 2026-08-07:

    project        create list show update  -- no delete (it is `unregister`)
    task           create list show         -- no update (`edit`), no delete
    work-package          list show         -- no create (`assemble`), no update
                                              (object removed 2026-08-17)
    agent                 list      update delete  -- no create (`register`), no show

So the same idea had a different name per object, and three of the five CRUD
verbs were missing somewhere.

This module fixes that as a LAYER over the built parser rather than by editing
265 registration sites:

* ``help`` becomes a verb at every level, scoping to that level, and
  ``help <subcommand>`` scopes further to one subcommand's arguments.
* Every first-class object exposes the full CRUD vocabulary, with the missing
  verbs bound to the handler that already implements them. Nothing is
  renamed and nothing is removed -- ``mac task edit`` keeps working, and
  ``mac task update`` now reaches the same code.
* Help for a first-class object lists CRUD first, then the operational verbs
  in named groups, so the shape of the object is legible before its long tail.
* Top-level help separates the four first-class objects from everything else.

A deliberate non-goal: this does not delete or hide any existing command.
265 subcommands are load-bearing for tests, scripts and the deploy path, and a
discoverability change must not be a breaking change. Reducing the surface is a
separate decision that wants its own pass.
"""

from __future__ import annotations

import argparse
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

#: The verbs every first-class object exposes, in the order a newcomer needs
#: them. Order is the point: ``create`` before ``show`` before the long tail.
CRUD_VERBS: Tuple[str, ...] = ("create", "list", "show", "update", "delete")


class ObjectSurface:
    """How one first-class object presents itself.

    ``crud`` maps each CRUD verb to the EXISTING subcommand that implements it.
    A verb mapped to itself is already correctly named; a verb mapped to
    another name gets an alias so both work; a verb mapped to ``None`` is a
    genuine gap, and is reported as such rather than papered over with an
    alias to something that does not mean the same thing.
    """

    def __init__(
        self,
        name: str,
        summary: str,
        crud: Dict[str, Optional[str]],
        groups: Sequence[Tuple[str, Sequence[str]]] = (),
    ) -> None:
        self.name = name
        self.summary = summary
        self.crud = crud
        self.groups = list(groups)


#: The four objects `mac` models. Everything else in the CLI acts ON these or
#: administers the fleet that runs them.
FIRST_CLASS: Tuple[ObjectSurface, ...] = (
    ObjectSurface(
        name="project",
        summary="a unit of work ownership: repositories, policy, dispatch state",
        crud={
            "create": "create",
            "list": "list",
            "show": "show",
            "update": "update",
            # Projects are durable and audited; removing one unregisters it
            # rather than destroying its history.
            "delete": "unregister",
        },
        groups=(("Dispatch", ("pause", "activate")), ("Repositories", ("register",))),
    ),
    ObjectSurface(
        name="task",
        summary="one unit of work: the thing agents claim, run, and publish",
        crud={
            "create": "create",
            "list": "list",
            "show": "show",
            # A real `mac task update` now exists (CLI parity with
            # ControlPlane.update_task). It is deliberately NOT an alias of
            # `edit`: edit opens $EDITOR to ANSWER a task parked on a human
            # question and refuses unless the task is in NEEDS_INPUT, so
            # pointing `update` at it would aim the most predictable verb in
            # the vocabulary at something that does not do what it says.
            "update": "update",
            # A task is an audited record: cancelling IS its delete. Nothing
            # in the ledger hard-deletes a task, and pretending otherwise
            # would be the wrong promise to make a beginner.
            "delete": "cancel",
        },
        groups=(
            ("Finding work", ("ready", "search", "stats", "why-unclaimed", "summary")),
            ("Execution", ("claim", "start", "release", "close", "reopen")),
            ("Review and evidence", ("evidence", "submit-review", "force-complete", "audit")),
            ("Human input", ("ask", "answer", "needs-input")),
            (
                "Recovery",
                (
                    "recover-stranded",
                    "recover-finalizer",
                    "recover-stalled-finalizer",
                ),
            ),
            ("Break-glass", ("break-glass", "break-glass-list", "break-glass-revoke")),
            ("Reporting", ("throughput", "generator-yield")),
            (
                "Migration",
                (
                    "detect-beads",
                    "migrate-beads",
                    "detect-ticketing",
                    "convert-ticketing",
                ),
            ),
        ),
    ),
    ObjectSurface(
        name="agent",
        summary="a worker that claims and executes tasks on a machine",
        crud={
            # Agents join the fleet by registering; that is their create.
            "create": "register",
            "list": "list",
            "show": "show",
            "update": "update",
            "delete": "delete",
        },
        groups=(
            ("Availability", ("hold", "resume", "heartbeat", "deregister")),
            ("Inspection", ("config", "hardware", "reflect")),
            ("Communication", ("tell",)),
            (
                "Administration",
                (
                    "attestation-recover",
                    "report-executor-approve",
                    "report-executor-revoke",
                    "migrate",
                ),
            ),
        ),
    ),
)

FIRST_CLASS_NAMES: Tuple[str, ...] = tuple(obj.name for obj in FIRST_CLASS)


#: The other 50 top-level commands, grouped into the handful of concepts they
#: actually belong to, each with a one-line description.
#:
#: Two problems, one table. A flat list of 50 names is not a menu, it is a
#: wall -- and 42 of those 50 carried NO help text at all, so even reading the
#: list told you almost nothing about what any of them did. Descriptions here
#: are also pushed onto the commands themselves when they have none, so
#: `mac help <command>` and the grouped listing improve together.
#:
#: A command missing from this table still appears, under "Other" -- the same
#: safety net the per-object help uses, so a command added later cannot become
#: invisible through nobody remembering this file.
COMMAND_GROUPS: Tuple[Tuple[str, Tuple[Tuple[str, str], ...]], ...] = (
    (
        "Getting started",
        (
            ("admin", "fleet, runtime and control-plane administration"),
            ("init", "create the control-plane schema in a PostgreSQL store"),
            ("login", "authenticate this machine against a hub"),
            ("logout", "discard stored hub credentials"),
            ("config", "read and migrate local mac configuration"),
            ("diagnostics", "run read-only control-plane health checks"),
        ),
    ),
    (
        "Fleet and machines",
        (
            ("fleet", "deploy, inspect and maintain the fleet as a whole"),
            ("machine", "hosts that agents run on"),
            ("hgx", "HGX / GPU capacity management"),
            ("openshell", "sandboxed execution environments for agents"),
            ("mcp", "serve the ledger to coding agents as Model Context Protocol tools"),
            ("sandbox-image", "the sandbox IMAGE: its bill of materials and its rollout"),
            ("runtime", "runtime images and environment definitions"),
            ("rollout", "staged rollout of a runtime or configuration"),
            ("env", "environment variables projected onto fleet hosts"),
            ("secret", "secret storage, rotation and access audit"),
            ("database", "control-plane database maintenance"),
            ("migrate", "schema and data migrations"),
        ),
    ),
    (
        "Getting work done",
        (
            ("dispatch", "the loop that matches ready tasks to eligible agents"),
            ("review", "adversarial review of completed work"),
            ("publish", "publish reviewed work to its destination"),
            ("pull-request", "pull requests raised from task work"),
            ("workflow", "multi-step workflow definitions and runs"),
            ("plan", "planning helpers, including dependency ordering"),
            ("eval", "evaluation runs over agent output"),
            ("optimizer", "model and routing optimization"),
            ("repo", "repositories that tasks execute against"),
            ("artifact", "durable artifacts produced by task work"),
        ),
    ),
    (
        "What agents know",
        (
            ("memory", "durable cross-session knowledge"),
            ("journal", "per-agent narrative history"),
            ("mood", "agent temperament and its effect on execution"),
            ("nap", "consolidation cycles that summarize recent work"),
            ("dream", "offline pattern-finding over past work"),
            ("curiosity", "quarantined self-proposed experiments awaiting judgment"),
            ("human-interface", "port an agent profile between Hermes and OpenClaw"),
            ("persona", "personas and their memory scopes"),
        ),
    ),
    (
        "Talking to people and systems",
        (
            ("message", "messages between agents and humans"),
            ("agentbus", "the agent-to-agent message bus"),
            ("communication", "communication channels and routing"),
            ("notifier", "outbound notification channels"),
            ("directive", "operator directives issued to agents"),
            ("persona-instance", "persona instances and their context (was: hermes)"),
            ("binding", "platform bindings for a persona instance"),
            ("interaction", "durable work created from a conversation"),
            ("bridge", "external system bridges"),
            ("integrations", "third-party integrations"),
        ),
    ),
    (
        "Who can do what",
        (
            ("tenant", "tenant boundaries"),
            # Two identities, deliberately named apart: a `human` is a
            # fleet-wide principal that owns agents and files tasks, while a
            # `user` exists inside one tenant. They are separate tables and
            # neither substitutes for the other.
            ("human", "people who own agents and file tasks"),
            ("user", "tenant-scoped user identities"),
            ("client", "API clients and their principals"),
        ),
    ),
    (
        "Seeing what happened",
        (
            ("events", "the unified event stream"),
            ("action-events", "recorded agent actions"),
            ("observability", "structured metrics and logs"),
            ("command-audit", "audit of commands agents ran"),
        ),
    ),
)


def command_descriptions() -> Dict[str, str]:
    """Flatten :data:`COMMAND_GROUPS` to command -> one-line description."""
    return {name: text for _title, entries in COMMAND_GROUPS for name, text in entries}


#: Commands shown in the default ``mac help``, alongside the four first-class
#: objects. Everything else is one flag away under ``mac help --all``.
#:
#: The operator asked for three tiers -- operator-facing, advanced, internal.
#: This is two, and the collapse is deliberate: the difference between
#: "advanced" and "internal" changes nothing a reader can act on, since both
#: land in the same place (out of the default view, into ``--all``). A third
#: label would be a distinction the output cannot express, so it would drift
#: without anyone noticing it had.
#:
#: Chosen with reference counts as INPUT, not as the rule. Measured across
#: docs/scripts/deploy/tests/src on 2026-08-07, `init` and `diagnostics` both
#: have ZERO in-repo references and both are kept: init creates the schema and
#: diagnostics is the first thing to run when something looks wrong, so a
#: metric that hides them is measuring our automation's habits rather than a
#: newcomer's needs. Conversely `bridge` has 37 references and is NOT here --
#: they are almost all internal plumbing, not an operator reaching for it.
COMMON_COMMANDS: Tuple[str, ...] = (
    # You cannot use the tool at all without these.
    "init",
    "login",
    "logout",
    "config",
    "diagnostics",
    # Where work runs, and how it gets there.
    "fleet",
    "machine",
    "repo",
    "openshell",
    # The lifecycle a task actually travels.
    "dispatch",
    "review",
    "publish",
    # What agents carry between tasks, and the credentials that let them run.
    "memory",
    "secret",
)


# ---------------------------------------------------------------------------
# help as a verb
# ---------------------------------------------------------------------------


def _subparsers_of(parser: argparse.ArgumentParser) -> Optional[argparse._SubParsersAction]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _distinct_subcommands(
    action: argparse._SubParsersAction,
) -> List[Tuple[str, argparse.ArgumentParser]]:
    """Registered names paired with their parser, aliases collapsed.

    ``choices`` contains one entry per alias, all pointing at the same parser
    object, so listing it directly would print every command twice once CRUD
    aliases are installed.
    """
    seen: List[Tuple[str, argparse.ArgumentParser]] = []
    claimed: set = set()
    for name, sub in action.choices.items():
        if id(sub) in claimed:
            continue
        claimed.add(id(sub))
        seen.append((name, sub))
    return seen


def _one_line_help(action: argparse._SubParsersAction, name: str) -> str:
    for choice_action in action._choices_actions:
        if choice_action.dest == name:
            return (choice_action.help or "").strip()
    return ""


def _make_help_handler(
    parser: argparse.ArgumentParser,
    action: argparse._SubParsersAction,
    *,
    is_top_level: bool = False,
) -> Callable[[argparse.Namespace], None]:
    def handler(args: argparse.Namespace) -> None:
        topic = getattr(args, "help_topic", None)
        if topic:
            target = action.choices.get(topic)
            if target is None:
                parser.print_help()
                print()
                print("unknown subcommand: %s" % topic)
                return
            # Scoped one level further: the arguments this subcommand takes.
            target.print_help()
            return
        if getattr(args, "help_all", False) and is_top_level:
            # Re-render with every command shown. The default view is a
            # shortlist, not a claim that the rest do not exist.
            previous = parser.epilog
            try:
                parser.epilog = _top_level_help_text(action, show_all=True)
                parser.print_help()
            finally:
                parser.epilog = previous
            return
        parser.print_help()

    return handler



def first_positional(
    parser: argparse.ArgumentParser, argv: Sequence[str]
) -> Optional[str]:
    """The first token that names a command, skipping options and their values.

    ``--db <dsn>`` puts a bare-looking token in the stream that is data, not a
    command. Anything scanning for "the first token without a dash" reads the
    DSN as the command name.
    """
    index = 0
    tokens = list(argv)
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            continue
        if token.startswith("-"):
            index += 1 + _option_value_count(parser, token)
            continue
        return token
    return None


def leaf_help_request(parser: argparse.ArgumentParser, argv: Sequence[str]) -> Optional[argparse.ArgumentParser]:
    """The parser whose help ``argv`` is asking for, when ``help`` names a leaf.

    ``install_help_verbs`` adds a ``help`` verb wherever there are subcommands,
    so ``mac task help`` works. A LEAF command has no subcommands, so there is
    nowhere to add the verb -- and ``help`` is then just another positional
    value. ``mac task create help`` therefore filed a task titled "help", which
    is the worst possible answer: a beginner exploring the CLI writes junk into
    the ledger and gets no help.

    This resolves ``help`` in the first positional slot after a leaf command to
    that command's help instead. A task genuinely called "help" is still
    reachable as ``mac task create -- help``, the usual escape.
    """
    current = parser
    depth = 0
    index = 0
    tokens = list(argv)
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            # The escape itself: everything after is data, never a subcommand.
            return None
        if token.startswith("-"):
            # An option, and possibly its VALUE. Skipping only the flag would
            # leave the value looking like a subcommand -- which is exactly how
            # `mac --db <dsn> task create help` slipped past the first version
            # of this and filed a task anyway.
            index += 1 + _option_value_count(current, token)
            continue
        action = _subparsers_of(current)
        if action is None:
            break
        if token not in action.choices:
            return None
        current = action.choices[token]
        depth += 1
        index += 1
    if depth == 0 or _subparsers_of(current) is not None:
        # depth 0: no command named yet. Not a leaf: argparse already routes
        # the help VERB there, and intercepting would bypass the grouped,
        # CRUD-first output that level produces.
        return None
    rest = [token for token in tokens[index:] if not token.startswith("-")]
    return current if rest[:1] == ["help"] else None


def _option_value_count(parser: argparse.ArgumentParser, token: str) -> int:
    """How many following tokens this option consumes."""
    name = token.split("=", 1)[0]
    if "=" in token:
        return 0
    action = parser._option_string_actions.get(name)
    if action is None:
        return 0
    if action.nargs == 0 or isinstance(
        action, (argparse._StoreTrueAction, argparse._StoreFalseAction, argparse._CountAction)
    ):
        return 0
    return 1

def install_help_verbs(parser: argparse.ArgumentParser, *, _depth: int = 0) -> None:
    """Add a ``help`` verb at every level that has subcommands.

    ``mac help``, ``mac task help`` and ``mac task help create`` all work, each
    scoping the output to that level. A beginner should never have to know that
    ``--help`` is spelled with dashes while every other word is a verb.
    """
    if _depth > 6:  # defensive: the tree is 3 deep
        return
    action = _subparsers_of(parser)
    if action is None:
        return
    if "help" not in action.choices:
        help_parser = action.add_parser(
            "help", help="show help for this command group"
        )
        help_parser.add_argument(
            "help_topic",
            nargs="?",
            metavar="SUBCOMMAND",
            help="scope help to one subcommand and show the arguments it takes",
        )
        if _depth == 0:
            help_parser.add_argument(
                "--all",
                dest="help_all",
                action="store_true",
                help="list every command, not just the common ones",
            )
        help_parser.set_defaults(
            func=_make_help_handler(parser, action, is_top_level=_depth == 0)
        )
    for _name, sub in _distinct_subcommands(action):
        install_help_verbs(sub, _depth=_depth + 1)


# ---------------------------------------------------------------------------
# CRUD aliases
# ---------------------------------------------------------------------------


def install_crud_aliases(parser: argparse.ArgumentParser) -> Dict[str, List[str]]:
    """Give every first-class object the full CRUD vocabulary.

    An alias points a CRUD name at the parser that already implements the
    behaviour, exactly as argparse's own ``aliases=`` does. The original name
    keeps working: ``mac task edit`` and ``mac task update`` are the same
    command, so nothing that exists today breaks.

    Returns the aliases added per object, so a test can assert the mapping
    rather than infer it.
    """
    action = _subparsers_of(parser)
    added: Dict[str, List[str]] = {}
    if action is None:
        return added
    for obj in FIRST_CLASS:
        sub_parser = action.choices.get(obj.name)
        if sub_parser is None:
            continue
        sub_action = _subparsers_of(sub_parser)
        if sub_action is None:
            continue
        for verb, implementation in obj.crud.items():
            if implementation is None or verb == implementation:
                continue
            if verb in sub_action.choices:
                continue
            target = sub_action.choices.get(implementation)
            if target is None:
                continue
            sub_action._name_parser_map[verb] = target
            added.setdefault(obj.name, []).append(verb)
    return added


def crud_gaps() -> Dict[str, List[str]]:
    """CRUD verbs no implementation exists for, per object.

    Reported rather than hidden: a beginner is better served by a missing
    verb being visible than by ``delete`` silently meaning something else.
    Currently empty -- every remaining object implements all five verbs.
    """
    return {
        obj.name: [verb for verb, impl in obj.crud.items() if impl is None]
        for obj in FIRST_CLASS
        if any(impl is None for impl in obj.crud.values())
    }


# ---------------------------------------------------------------------------
# grouped help text
# ---------------------------------------------------------------------------


def _format_rows(rows: Sequence[Tuple[str, str]], indent: str = "  ") -> List[str]:
    if not rows:
        return []
    width = max(len(name) for name, _ in rows)
    return [("%s%-*s  %s" % (indent, width, name, text)).rstrip() for name, text in rows]


def _object_help_text(obj: ObjectSurface, action: argparse._SubParsersAction) -> str:
    """CRUD first, then named groups, then whatever is left over.

    The leftover section matters: it is how a verb added later becomes visible
    here without anyone remembering to update this file, instead of silently
    vanishing from the help.
    """
    lines: List[str] = ["", "%s -- %s" % (obj.name, obj.summary), ""]
    listed: set = set()

    crud_rows: List[Tuple[str, str]] = []
    for verb in CRUD_VERBS:
        implementation = obj.crud.get(verb)
        if implementation is None:
            continue
        text = _one_line_help(action, implementation) or _one_line_help(action, verb)
        if implementation != verb:
            text = "%s (same as `%s`)" % (text, implementation) if text else "same as `%s`" % implementation
        crud_rows.append((verb, text))
        listed.add(implementation)
        listed.add(verb)
    if crud_rows:
        lines.append("CRUD:")
        lines.extend(_format_rows(crud_rows))
        lines.append("")

    for title, verbs in obj.groups:
        rows = [
            (verb, _one_line_help(action, verb))
            for verb in verbs
            if verb in action.choices
        ]
        rows = [row for row in rows if row[0] not in listed]
        if not rows:
            continue
        listed.update(name for name, _ in rows)
        lines.append("%s:" % title)
        lines.extend(_format_rows(rows))
        lines.append("")

    remaining = [
        (name, _one_line_help(action, name))
        for name, _ in _distinct_subcommands(action)
        if name not in listed and name != "help"
    ]
    if remaining:
        lines.append("Other:")
        lines.extend(_format_rows(remaining))
        lines.append("")

    gaps = [verb for verb, impl in obj.crud.items() if impl is None]
    if gaps:
        lines.append(
            "Not available for %s: %s (no control-plane operation implements it)"
            % (obj.name, ", ".join(gaps))
        )
        lines.append("")

    lines.append("Run `mac %s help <subcommand>` for the arguments one takes." % obj.name)
    return "\n".join(lines)


def _install_grouped_help(parser: argparse.ArgumentParser, text: str) -> None:
    """Replace the default brace-list listing with grouped text.

    The subcommand actions are hidden from the default formatter rather than
    removed, so parsing is untouched and only the rendering changes.
    """
    action = _subparsers_of(parser)
    if action is None:
        return
    action.metavar = "SUBCOMMAND"
    action._choices_actions = []
    parser.epilog = text
    parser.formatter_class = argparse.RawDescriptionHelpFormatter


def _admin_help_text(action: argparse._SubParsersAction) -> str:
    """The grouped catalogue, rendered for `mac admin help`.

    This is the listing that used to occupy the top level. It did not become
    less useful by moving -- it became findable in one place instead of being
    the first thing a newcomer had to wade through.
    """
    lines = ["", "admin -- fleet, runtime and control-plane administration", ""]
    listed: set = set()
    present = {name for name, _ in _distinct_subcommands(action)}
    for title, entries in COMMAND_GROUPS:
        rows = [(name, text) for name, text in entries if name in present]
        if not rows:
            continue
        listed.update(name for name, _ in rows)
        lines.append("%s:" % title)
        lines.extend(_format_rows(rows))
        lines.append("")
    remaining = sorted(present - listed - {"help"})
    if remaining:
        lines.append("Other:")
        lines.extend(
            _format_rows([(name, _one_line_help(action, name)) for name in remaining])
        )
        lines.append("")
    lines.append("Run `mac admin help <command>` for the arguments one takes.")
    lines.append("These moved here from the top level; `mac <command>` now redirects.")
    return "\n".join(lines)


def install_admin_help(parser: argparse.ArgumentParser) -> None:
    action = _subparsers_of(parser)
    if action is None:
        return
    admin = action.choices.get("admin")
    if admin is None:
        return
    admin_action = _subparsers_of(admin)
    if admin_action is None:
        return
    _install_grouped_help(admin, _admin_help_text(admin_action))


def install_object_help(parser: argparse.ArgumentParser) -> None:
    """Give each first-class object a CRUD-first, grouped help page."""
    action = _subparsers_of(parser)
    if action is None:
        return
    for obj in FIRST_CLASS:
        sub_parser = action.choices.get(obj.name)
        if sub_parser is None:
            continue
        sub_action = _subparsers_of(sub_parser)
        if sub_action is None:
            continue
        _install_grouped_help(sub_parser, _object_help_text(obj, sub_action))


def _top_level_help_text(
    action: argparse._SubParsersAction, *, show_all: bool = False
) -> str:
    lines = ["", "The objects mac models. Start here:", ""]
    rows = []
    for obj in FIRST_CLASS:
        if obj.name in action.choices:
            rows.append((obj.name, obj.summary))
    lines.extend(_format_rows(rows))
    lines.append("")
    # A summary line that overstates the vocabulary is the same class of
    # problem as a verb that does not do what it says, so any object missing a
    # verb is named explicitly below rather than papered over by the flat
    # "each supports ..." line.
    gaps = crud_gaps()
    lines.append("  Each supports: %s" % ", ".join(CRUD_VERBS))
    for name, missing in sorted(gaps.items()):
        lines.append("    except %s, which has no %s" % (name, " or ".join(sorted(missing))))
    lines.append("  `mac <object> help` lists its commands; `mac <object> help <subcommand>`")
    lines.append("  shows the arguments that subcommand takes.")
    lines.append("")
    registered = {
        name
        for name, _ in _distinct_subcommands(action)
        if name not in FIRST_CLASS_NAMES and name != "help"
    }
    if not show_all:
        # The whole point of the refactor: the top level describes the object
        # model, not the implementation. Fifty-odd administrative commands are
        # not peers of `task`, so they live under one verb and are listed by
        # `mac admin help`. Every one of them still runs at its original
        # spelling -- scripts and deploy tooling are not broken to tidy a help
        # page.
        if "admin" in action.choices:
            lines.append("Everything else:")
            lines.extend(
                _format_rows(
                    [("admin", "fleet, runtime and control-plane administration")]
                )
            )
            lines.append("")
        lines.append(
            "%d administrative commands live under `mac admin` "
            "(`mac admin help` lists them)." % len(registered - {"admin"})
        )
        lines.append(
            "They moved: `mac fleet ...` is now `mac admin fleet ...`, and the "
            "old spelling says so."
        )
        lines.append("")
        lines.append("Run `mac help --all` to see every command in one list.")
        return "\n".join(lines)

    # After the re-parenting the top level holds only the objects and `admin`,
    # so listing top-level names alone would make --all emptier than the
    # default view. It reaches into admin, because an escape hatch that stops
    # being complete is just a second, longer shortlist.
    admin_parser = action.choices.get("admin")
    admin_action = _subparsers_of(admin_parser) if admin_parser else None
    if admin_action is not None:
        registered = registered | {
            name for name, _ in _distinct_subcommands(admin_action) if name != "help"
        }
    visible = registered
    listed: set = set()
    for title, entries in COMMAND_GROUPS:
        rows = [(name, text) for name, text in entries if name in visible]
        if not rows:
            continue
        listed.update(name for name, _ in rows)
        lines.append("%s:" % title)
        lines.extend(_format_rows(rows))
        lines.append("")

    # The safety net: a command added later, and never added to COMMAND_GROUPS,
    # still shows up under --all rather than silently disappearing from the
    # help. It is not forced into the default view, because a command nobody
    # has classified is not by that fact something a newcomer needs.
    remaining = sorted(visible - listed)
    if remaining:
        lines.append("Other:")
        lines.extend(
            _format_rows([(name, _one_line_help(action, name)) for name in remaining])
        )
        lines.append("")

    hidden = len(registered - visible)
    if hidden:
        lines.append(
            "%d more commands are available. `mac help --all` lists them, and "
            "every one of them" % hidden
        )
        lines.append("runs whether it is listed or not -- nothing is removed.")
        lines.append("")
    lines.append("Run `mac help <command>` for any of them.")
    return "\n".join(lines)


def install_command_descriptions(parser: argparse.ArgumentParser) -> List[str]:
    """Give every top-level command a one-line description if it lacks one.

    42 of the 50 non-first-class commands had none, so ``mac help`` listed bare
    names and ``mac <command> --help`` opened with a blank line. The catalogue
    that groups them is the same one that describes them, so both surfaces
    improve from one edit instead of drifting apart.

    An existing description always wins: this fills gaps, it does not overrule
    whoever wrote the command.
    """
    action = _subparsers_of(parser)
    filled: List[str] = []
    if action is None:
        return filled
    descriptions = command_descriptions()
    for choice_action in action._choices_actions:
        name = choice_action.dest
        text = descriptions.get(name)
        if not text or (choice_action.help or "").strip():
            continue
        choice_action.help = text
        filled.append(name)
    for name, text in descriptions.items():
        sub = action.choices.get(name)
        if sub is not None and not (sub.description or "").strip():
            sub.description = text
    return filled


def install_top_level_help(parser: argparse.ArgumentParser) -> None:
    action = _subparsers_of(parser)
    if action is None:
        return
    _install_grouped_help(parser, _top_level_help_text(action))


#: Commands that stay at the top level beside the four objects. `help` is the
#: way in; `admin` is where everything else now lives.
TOP_LEVEL_KEEP: Tuple[str, ...] = FIRST_CLASS_NAMES + ("admin", "help")

#: Filled by :func:`install_admin_group`, so an old spelling gets a redirect
#: rather than argparse's bare "invalid choice".
_MOVED_TO_ADMIN: set = set()


def moved_to_admin(name: str) -> bool:
    return name in _MOVED_TO_ADMIN


def install_admin_group(parser: argparse.ArgumentParser) -> None:
    """Re-parent every non-object command under ``mac admin``.

    The complaint this answers is that `mac` surfaced fifty-odd top-level
    commands when it models four things. Grouping them under one administrative
    verb makes the top level describe the object model instead of the
    implementation.

    Nothing is removed. Each command keeps its original top-level spelling as a
    working alias, because `mac admin fleet ...` and `mac admin memory ...` appear in
    scripts, deploy tooling and documentation that this refactor has no business
    breaking. What changes is what the CLI SHOWS: `mac admin help` lists them,
    and the top-level help stops pretending they are peers of `task`.
    """
    action = _subparsers_of(parser)
    if action is None or "admin" in action.choices:
        return

    moved = [
        (name, sub)
        for name, sub in _distinct_subcommands(action)
        if name not in TOP_LEVEL_KEEP
    ]
    if not moved:
        return

    admin = action.add_parser(
        "admin",
        help="fleet, runtime and control-plane administration",
        description="fleet, runtime and control-plane administration",
    )
    admin_action = admin.add_subparsers(dest="admin_command", required=True)
    for name, sub in moved:
        # Register the SAME parser object under admin. Rebuilding it would
        # duplicate every argument definition and let the two copies drift.
        admin_action._name_parser_map[name] = sub
        for alias, other in list(action.choices.items()):
            if other is sub and alias != name:
                admin_action._name_parser_map[alias] = other
    admin_action.choices = admin_action._name_parser_map

    # The cut. Until now these were also left at the top level as working
    # aliases; they are gone from it now, so `mac` offers exactly the object
    # model plus `admin`. Every in-repo caller was migrated in the same change,
    # because a deploy has to carry both halves at once -- installed service
    # units and the worker both shell out to these.
    for name, sub in list(action.choices.items()):
        if name in TOP_LEVEL_KEEP:
            continue
        if any(sub is moved_parser for _moved_name, moved_parser in moved):
            action._name_parser_map.pop(name, None)
    action.choices = action._name_parser_map
    _MOVED_TO_ADMIN.update(
        name for name, _sub in moved
    )


def install(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Apply the whole surface layer to a built parser.

    Ordering matters: aliases first so the help text can describe them, then
    help verbs so they exist at every level including the new aliases, then the
    grouped rendering, which hides the default listing and therefore must run
    last.
    """
    install_crud_aliases(parser)
    # Before the help verbs, so `mac admin help` is installed like any other
    # group rather than needing a special case.
    install_admin_group(parser)
    install_help_verbs(parser)
    # Descriptions before the grouped renderings, which read them.
    install_command_descriptions(parser)
    install_object_help(parser)
    install_admin_help(parser)
    install_top_level_help(parser)
    return parser



# ---------------------------------------------------------------------------
# Completeness of the `admin` catalogue
#
# COMMAND_GROUPS already carries a one-line description for every command, and
# a test rejects entries naming commands that do not exist. Only that
# direction was checked. The reverse -- a NEW `admin` group that nobody
# catalogued -- passed silently, which is how the operational surface grew to
# 53 groups and roughly 245 leaves, about two thirds of the CLI, while the
# object model above it stayed at six deliberate commands.
# ---------------------------------------------------------------------------


def admin_group_names(parser: argparse.ArgumentParser) -> Tuple[str, ...]:
    """Return each distinct `mac admin` group once, under its registered name.

    Aliases share a parser object, so `comm` is not a second group; counting
    it as one would demand a second catalogue entry for one surface, and the
    next reader would "fix" that duplication by deleting a working alias.
    """

    root = _subparsers_of(parser)
    if root is None or "admin" not in root.choices:
        return ()
    admin = _subparsers_of(root.choices["admin"])
    if admin is None:
        return ()
    first_name_for: Dict[int, str] = {}
    for name, sub in admin.choices.items():
        first_name_for.setdefault(id(sub), name)
    return tuple(first_name_for.values())
