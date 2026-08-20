"""The guide must describe software that exists.

This repository's README described `src/mac/ui/app.ts`, its compiled `app.js`
and a vendored xterm tree for some time after all three were deleted. The docs
gates check GENERATED references and EXECUTABLE shell blocks; prose naming a
deleted file is exactly what they do not cover, which is why nobody noticed.

It happened again with the vendored Hermes tree, and this file did not catch it
either: the root README was not among the pages checked, and the check matched
only backticked paths ending in a source-file suffix -- so an extension-less
`src/mac/_hermes` and a markdown link to a deleted deploy/hermes/SNAPSHOT.md
both went straight through. README.md is now checked, and so are both shapes.

Documentation that quietly diverges is worse than none, because it is trusted.
These tests check the parts of the guide a machine can check: that every file
it names exists, that every `mac` command it shows resolves against the real
parser, and that the states and transitions it documents match models.py.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "guide"
# README.md is here because it is the page most readers start from and the one
# the original bug was in. It was outside this gate until the Hermes removal
# repeated the xterm mistake in it, unnoticed, for three days.
PAGES = sorted(GUIDE.glob("*.md")) + [ROOT / "CONTRIBUTING.md", ROOT / "README.md"]

CODE_SUFFIXES = ("py", "ts", "tsx", "toml", "sql", "json", "sh", "md")

_BACKTICK = re.compile(r"`([^`\n]+)`")
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in PAGES)


def _repo_root_names() -> set[str]:
    return {entry.name for entry in ROOT.iterdir()}


def _is_repo_path(token: str, root_names: set[str]) -> bool:
    """Is this backticked token claiming a path in this repository?

    Two ways to qualify, and both are needed. A source-file suffix catches
    `foo/bar.py` under a directory that never existed. A first segment that
    exists at the repository root catches an extension-less directory --
    `src/mac/_hermes` -- which a suffix-only rule skips, which is exactly how
    the Hermes prose survived.

    Requiring one of the two is what keeps prose out. The README backticks
    `read/write/agent/dispatch/secret/admin` for token scopes: slashes, no
    spaces, no suffix, and `read` is not a directory here, so it is not a path.
    """
    if "/" not in token:
        return False
    if any(ch in token for ch in " \t<>*$\"'|"):
        return False  # placeholders, globs, shell fragments, prose
    if "://" in token or token.startswith(("/", "~", "#", "http")):
        return False  # URLs, HTTP routes, home-relative, absolute
    stem = token.rstrip("/")
    if "." in stem and stem.rsplit(".", 1)[-1] in CODE_SUFFIXES:
        return True
    return token.split("/", 1)[0] in root_names


def test_the_guide_exists_and_is_indexed():
    assert (GUIDE / "README.md").is_file()
    for name in (
        "01-architecture.md",
        "02-getting-started.md",
        "03-advanced.md",
        "04-ui.md",
        "05-developer-guide.md",
    ):
        assert (GUIDE / name).is_file(), name
        assert name in (GUIDE / "README.md").read_text(encoding="utf-8")


def test_every_file_the_guide_names_exists():
    """The README-describing-deleted-files bug, prevented.

    Both shapes it actually took, because the first version of this check caught
    neither. When `src/mac/_hermes` and `deploy/hermes/` were deleted, the README
    went on naming the tree in backticks -- no file suffix, so a suffix-only
    regex skipped it -- and linking [snapshot contract](deploy/hermes/SNAPSHOT.md),
    which was never in backticks at all. The regex matched two paths in that
    README, both of which existed, and the gate passed.
    """
    root_names = _repo_root_names()
    missing = []
    for page in PAGES:
        text = page.read_text(encoding="utf-8")

        for token in _BACKTICK.findall(text):
            if _is_repo_path(token, root_names) and not (ROOT / token.rstrip("/")).exists():
                missing.append("%s names `%s`" % (page.name, token))

        for raw in _MD_LINK.findall(text):
            target = raw.split("#", 1)[0].strip()
            if not target or "://" in target:
                continue
            if target.startswith(("http", "mailto:", "#", "/")):
                continue
            if any(ch in target for ch in " <>*$"):
                continue
            # Relative to the PAGE, not the root: docs/guide/README.md links
            # 01-architecture.md as a sibling, and README.md links docs/ from
            # the top. Resolving both against ROOT would invent failures.
            if not (page.parent / target).exists():
                missing.append("%s links %s" % (page.name, target))

    assert not missing, (
        "documentation names files that do not exist:\n  "
        + "\n  ".join(sorted(set(missing)))
        + "\n\nA backticked repository path asserts the path exists. When writing about "
        "something deliberately removed, leave it unbackticked."
    )


def test_every_documented_task_state_is_real():
    from mac.models import TaskState

    real = {state.value for state in TaskState}
    text = _text()
    # Every state name the guide uses in a state-machine context must be real.
    for documented in (
        "open",
        "waiting",
        "blocked",
        "claimed",
        "running",
        "needs_review",
        "needs_input",
        "reviewing",
        "completed",
        "failed",
        "cancelled",
    ):
        assert documented in real
        assert documented in text, "state %s is undocumented" % documented


def test_the_documented_transitions_are_legal():
    """The architecture page draws a state diagram. Every edge in it must be an
    edge the control plane actually allows, or the diagram teaches a move that
    will be refused."""
    from mac.models import TASK_TRANSITIONS

    diagram = (GUIDE / "01-architecture.md").read_text(encoding="utf-8")
    edges = re.findall(r"^\s{4}(\w+) --> (\w+)$", diagram, re.M)
    assert edges, "no state-diagram edges found; the diagram or this test moved"

    illegal = [
        "%s -> %s" % (src, dst)
        for src, dst in edges
        if src not in ("[*]",)
        and dst not in ("[*]",)
        and dst not in TASK_TRANSITIONS.get(src, set())
    ]

    assert not illegal, "the diagram shows transitions the control plane refuses: %s" % illegal


def test_completed_is_documented_as_the_only_terminal_state():
    """`failed` and `cancelled` both allow -> open. Documenting them as terminal
    would hide the retry path, which is how a stuck task gets recovered."""
    from mac.models import TASK_TRANSITIONS

    assert not TASK_TRANSITIONS["completed"]
    assert "open" in TASK_TRANSITIONS["failed"]
    assert "open" in TASK_TRANSITIONS["cancelled"]
    assert "only truly terminal state" in _text()


def test_every_mac_command_shown_resolves():
    """A documented command that does not exist sends the reader to a usage
    error, mid-incident.

    This checks the VERB, not just the object. An earlier version accepted a
    command when any prefix of it resolved, which meant `mac task frobnicate`
    passed on the strength of `mac task` -- it could only ever catch a bad
    object, which is the mistake nobody makes.
    """
    sys.argv = ["mac"]
    import argparse

    from mac.cli import build_parser

    def children(node):
        """Verbs one level below `node`.

        Subparsers are the usual shape, but not the only one: `mac admin login`
        takes `status`/`renew` as a positional with `choices`, so a checker that
        walks only `_name_parser_map` reports a real command as missing.
        """
        subs, choices = {}, set()
        for action in node._actions:
            mapping = getattr(action, "_name_parser_map", None)
            if mapping:
                subs.update(mapping)
            elif not action.option_strings and action.choices:
                choices |= {str(c) for c in action.choices}
        return subs, choices

    def command_lines(text):
        """Only what is shown AS a command: inline code spans and fenced blocks.

        Checking raw prose does not work -- "the mac task ledger" is a noun
        phrase, not a command, and no stopword list separates the two reliably.
        """
        for span in re.findall(r"`([^`\n]+)`", text):
            yield span
        for block in re.findall(r"```[a-z]*\n(.*?)```", text, re.S):
            for line in block.splitlines():
                yield line.strip().lstrip("$ ").strip()

    top_subs, _ = children(build_parser())
    unknown = []
    for page in PAGES:
        for line in command_lines(page.read_text(encoding="utf-8")):
            match = re.match(r"mac ((?:[a-z][a-z0-9-]*)(?: [a-z][a-z0-9-]*)*)", line)
            if not match:
                continue
            parts = match.group(1).split()
            if parts[0] not in top_subs:
                continue
            node, path = top_subs[parts[0]], [parts[0]]
            for token in parts[1:]:
                subs, choices = children(node)
                if token in subs:
                    node, _ = subs[token], path.append(token)
                    continue
                if token in choices or token == "help":
                    path.append(token)
                    break
                # Not a verb here. If this parser has no verbs at all the token
                # is an argument (`mac task show <id>`); otherwise it is wrong.
                if subs or choices:
                    unknown.append("%s: mac %s" % (page.name, " ".join(path + [token])))
                break

    assert not unknown, "commands that do not exist:\n  " + "\n  ".join(sorted(set(unknown)))


def test_the_known_gaps_are_still_gaps():
    """The guide tells operators to work around three gaps. If one is fixed and
    the page still claims it, the page is now misinformation.

    This is a canary, not a prohibition: closing a gap SHOULD fail here, and the
    fix is to update the page in the same change.
    """
    advanced = (GUIDE / "03-advanced.md").read_text(encoding="utf-8")
    # UPDATED DELIBERATELY. The broadcast half is no longer write-only: workers
    # act on sandbox policy events between tasks, and read the feed into the
    # task context before starting one. The page now says so, and says what is
    # still missing -- the ADDRESSED bus, which the check below still guards.
    assert "AgentBus consumption is partial" in advanced
    assert "The addressed bus (`/agentbus/traffic`) has no consumer." in advanced

    consumers = []
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "/agentbus/traffic" in text and path.name not in ("api.py", "cli.py", "dispatch.py"):
            consumers.append(str(path.relative_to(ROOT)))

    assert not consumers, (
        "something now consumes AgentBus traffic (%s) -- update "
        "docs/guide/03-advanced.md, which still tells operators nothing does"
        % consumers
    )


def test_mermaid_blocks_are_balanced():
    for page in PAGES:
        text = page.read_text(encoding="utf-8")
        assert text.count("```mermaid") <= text.count("```") // 2, page.name
        assert text.count("```") % 2 == 0, "unclosed code fence in %s" % page.name
