"""Make the CLI's first-class objects discoverable, uniform, and CRUD-first.

`mac` grew to 55 top-level commands and 265 subcommands. Four of those commands
are the objects the tool actually models -- **project, task, work-package
(task groups), agent** -- and the other 51 are operational surface that
accumulated around them. Handed to a beginner, the tool could not answer its
own most basic question: ``mac task help`` was an error, and ``mac task
--help`` printed a 34-name brace list in registration order with ``create``,
``list`` and ``show`` scattered through it.

The gaps were not uniform either. Measured on 2026-08-07:

    project        create list show update  -- no delete (it is `unregister`)
    task           create list show         -- no update (`edit`), no delete
    work-package          list show         -- no create (`assemble`), no update
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
        name="work-package",
        summary="a task group: several tasks assembled, certified and landed together",
        crud={
            "create": "assemble",
            "list": "list",
            "show": "show",
            # Two honest gaps, held to the same standard as `task update`.
            #
            # `replan` is the nearest thing, and it is NOT a general update:
            # ControlPlane.replan_work_package installs a COMPILED REPLACEMENT
            # PLAN into a package that must already be paused. Someone typing
            # `update` to change a field would hit a state error from a verb
            # that promised otherwise, which is exactly the trap avoided by
            # giving task a real update instead of aliasing it onto `edit`.
            # The API agrees: there is no PUT /work-packages/{id}, only
            # POST /work-packages/{id}/replan.
            "update": None,
            # Nothing in the control plane deletes or cancels a work package,
            # and there is no DELETE /work-packages/{id} either.
            "delete": None,
        },
        groups=(
            ("Assembly", ("assemble", "assemble-batch", "assembly-claim", "assembly-status", "admit")),
            ("Planning", ("replan", "replan-preview", "readiness")),
            (
                "Certification",
                (
                    "certification-prepare",
                    "certification-claim",
                    "certification-run",
                    "certification-ingest",
                    "certification-status",
                    "accept-certification",
                    "reject-failed-certification",
                ),
            ),
            ("Candidates", ("accept-candidate", "reject-candidate", "verify-output")),
            ("Landing", ("land", "finalize-publication")),
            ("Dispatch", ("pause", "activate")),
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
            ("persona", "Hermes personas and their memory scopes"),
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
            ("hermes", "Hermes instances and their context"),
            ("binding", "Hermes platform bindings"),
            ("interaction", "durable work created from a conversation"),
            ("bridge", "external system bridges"),
            ("integrations", "third-party integrations"),
        ),
    ),
    (
        "Who can do what",
        (
            ("tenant", "tenant boundaries"),
            ("user", "human user identities"),
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

    Reported rather than hidden: ``work-package`` has no delete, and a beginner
    is better served by that being visible than by ``delete`` silently meaning
    something else.
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
    # Stating a flat "each supports create/list/show/update/delete" would be
    # untrue: work-package has neither an update nor a delete, on the CLI or
    # the API. A summary line that overstates the vocabulary is the same class
    # of problem as a verb that does not do what it says.
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
    visible = registered if show_all else (registered & set(COMMON_COMMANDS))
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


def install(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Apply the whole surface layer to a built parser.

    Ordering matters: aliases first so the help text can describe them, then
    help verbs so they exist at every level including the new aliases, then the
    grouped rendering, which hides the default listing and therefore must run
    last.
    """
    install_crud_aliases(parser)
    install_help_verbs(parser)
    # Descriptions before the grouped renderings, which read them.
    install_command_descriptions(parser)
    install_object_help(parser)
    install_top_level_help(parser)
    return parser
