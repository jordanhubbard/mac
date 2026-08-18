"""The CLI skill must describe the CLI that exists.

A hand-written command reference drifts the moment someone renames a verb, and
a reference that is confidently wrong is worse than none: it sends the reader
to run something that fails, on a live fleet, during an incident.

Every `mac ...` command the skill mentions is therefore checked against the
real parser. The claims about traps are prose and cannot be tested; the
commands can be, and those are what get typed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "mac-cli" / "SKILL.md"


def _command_tree() -> set[tuple[str, ...]]:
    """Every valid command path in the real parser."""
    sys.argv = ["mac"]
    from mac import cli as C

    parser = C.build_parser()
    found: set[tuple[str, ...]] = set()

    def walk(node, prefix: tuple[str, ...]) -> None:
        for action in node._actions:
            mapping = getattr(action, "_name_parser_map", None)
            if not mapping:
                continue
            for name, sub in mapping.items():
                path = prefix + (name,)
                found.add(path)
                walk(sub, path)

    walk(parser, ())
    return found


def _mentioned_commands(text: str) -> set[tuple[str, ...]]:
    """Commands the skill tells you to RUN, from its indented code blocks.

    Prose is excluded deliberately, for two reasons. It contains sentences that
    happen to start with the word mac ("Where mac is talking to"), and it names
    commands precisely BECAUSE they do not work -- `mac dispatch` prints a
    redirect, and warning about that is the point. Checking prose would force
    the skill to stop documenting the traps it exists to document.
    """
    commands: set[tuple[str, ...]] = set()
    for line in text.splitlines():
        if not line.startswith("    "):
            continue
        # Column-aligned trailing comments are prose, not arguments, so a run
        # of two or more spaces ends the command. Without this the description
        # beside a command is parsed as more subcommands.
        for chunk in re.split(r"\s{2,}", line.strip()):
            chunk = chunk.strip()
            if not chunk.startswith("mac "):
                continue
            parts: list[str] = []
            for token in chunk.split()[1:]:
                if not re.fullmatch(r"[a-z][a-z0-9-]*", token):
                    break
                parts.append(token)
            if parts:
                commands.add(tuple(parts))
    return commands


@pytest.fixture(scope="module")
def tree() -> set[tuple[str, ...]]:
    return _command_tree()


def test_the_skill_exists():
    assert SKILL.is_file(), "the mac CLI skill is missing"


def test_every_command_the_skill_names_exists(tree):
    """The whole point. Each miss here is a command someone would have typed."""
    text = SKILL.read_text(encoding="utf-8")
    missing = []
    for parts in sorted(_mentioned_commands(text)):
        # EXACT match required. An earlier version accepted any resolving
        # prefix, to tolerate trailing arguments -- which made the test
        # vacuous: `mac task unblock` passed because `mac task` exists. The
        # extractor already stops at the first non-word token, so the path it
        # produces is the command and nothing else.
        if parts not in tree:
            missing.append("mac " + " ".join(parts))
    assert not missing, (
        "the skill names commands that do not exist: %s" % ", ".join(missing)
    )


def _mentioned_flags(text: str) -> set[tuple[tuple[str, ...], str]]:
    """(command path, --flag) pairs the skill tells you to RUN.

    Commands were already checked against the parser; FLAGS were not, and a
    flag is what the reader actually types to change behaviour. The skill can
    document `mac task list --all-states` while no such option exists, send the
    reader to a usage error, and stay green.
    """
    pairs: set[tuple[tuple[str, ...], str]] = set()
    for line in text.splitlines():
        if not line.startswith("    "):
            continue
        for chunk in re.split(r"\s{2,}", line.strip()):
            tokens = chunk.strip().split()
            if not tokens or tokens[0] != "mac":
                continue
            path: list[str] = []
            flags: list[str] = []
            for token in tokens[1:]:
                if token.startswith("--"):
                    flags.append(token.split("=", 1)[0])
                elif re.fullmatch(r"[a-z][a-z0-9-]*", token) and not flags:
                    path.append(token)
                elif not flags:
                    break
            for flag in flags:
                if path:
                    pairs.add((tuple(path), flag))
    return pairs


def _options_for(path: tuple[str, ...]) -> set[str]:
    sys.argv = ["mac"]
    from mac import cli as C

    node = C.build_parser()
    for name in path:
        for action in node._actions:
            mapping = getattr(action, "_name_parser_map", None)
            if mapping and name in mapping:
                node = mapping[name]
                break
        else:
            return set()
    options = {o for action in node._actions for o in action.option_strings}
    # Global flags are declared on the root parser and accepted anywhere.
    root = C.build_parser()
    options |= {o for action in root._actions for o in action.option_strings}
    return options


def test_every_flag_the_skill_names_exists():
    """A documented flag that does not exist is a usage error on a live fleet."""
    text = SKILL.read_text(encoding="utf-8")
    missing = []
    for path, flag in sorted(_mentioned_flags(text)):
        if flag not in _options_for(path):
            missing.append("mac %s %s" % (" ".join(path), flag))
    assert not missing, (
        "the skill names flags that do not exist: %s" % ", ".join(missing)
    )


def test_the_traps_it_documents_are_real(tree):
    """Each of these cost a wrong command against a live fleet, so each is
    pinned: if the CLI changes to match the guess, the skill must stop warning
    about it."""
    # A paused project is activated, not resumed.
    assert ("project", "activate") in tree
    assert ("project", "resume") not in tree
    # Agents use the opposite pair.
    assert ("agent", "hold") in tree and ("agent", "resume") in tree
    # The first-class objects stayed at the top level.
    for obj in ("project", "task", "agent"):
        assert (obj,) in tree
    # And the rest really did move under admin.
    for moved in ("dispatch", "human", "memory", "machine", "fleet"):
        assert ("admin", moved) in tree, "admin %s should exist" % moved
        assert (moved,) not in tree, "%s should have moved under admin" % moved


def test_agent_update_still_cannot_set_status():
    """The skill tells the reader to use the hub API for this. If a --status
    flag is ever added, that advice becomes wrong."""
    sys.argv = ["mac"]
    from mac import cli as C

    parser = C.build_parser()

    def find(node, path):
        for action in node._actions:
            mapping = getattr(action, "_name_parser_map", None)
            if not mapping:
                continue
            if path[0] in mapping:
                sub = mapping[path[0]]
                return find(sub, path[1:]) if path[1:] else sub
        return None

    update = find(parser, ("agent", "update"))
    assert update is not None
    flags = {opt for action in update._actions for opt in action.option_strings}
    assert "--status" not in flags


# ---------------------------------------------------------------------------
# AGENTS.md is the entry point. A skill it does not name is a skill nobody
# reads, and a skill it names that does not exist is a broken instruction at
# the top of the file every agent starts from.
# ---------------------------------------------------------------------------

AGENTS = ROOT / "AGENTS.md"
SKILLS_DIR = ROOT / "skills"


def test_agents_md_points_at_every_skill():
    text = AGENTS.read_text(encoding="utf-8")
    for skill in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        rel = skill.relative_to(ROOT).as_posix()
        assert rel in text, "AGENTS.md does not mention %s" % rel


def test_agents_md_does_not_name_a_missing_skill():
    """The failure mode is worse than omission: an agent follows the pointer,
    finds nothing, and improvises."""
    text = AGENTS.read_text(encoding="utf-8")
    for match in re.finditer(r"skills/[A-Za-z0-9_-]+/SKILL\.md", text):
        assert (ROOT / match.group(0)).is_file(), "%s does not exist" % match.group(0)


def test_every_skill_declares_what_it_is_for():
    """Frontmatter name/description is how a skill is selected. Without it the
    file is only findable by someone who already knows it exists."""
    for skill in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        head = skill.read_text(encoding="utf-8").split("---")
        assert len(head) >= 3, "%s has no frontmatter" % skill.name
        assert "name:" in head[1] and "description:" in head[1], (
            "%s must declare name and description" % skill.name
        )
