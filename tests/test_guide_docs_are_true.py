"""The guide must describe software that exists.

This repository's README described `src/mac/ui/app.ts`, its compiled `app.js`
and a vendored xterm tree for some time after all three were deleted. The docs
gates check GENERATED references and EXECUTABLE shell blocks; prose naming a
deleted file is exactly what they do not cover, which is why nobody noticed.

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
PAGES = sorted(GUIDE.glob("*.md")) + [ROOT / "CONTRIBUTING.md"]


def _text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in PAGES)


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
    """The README-describing-deleted-files bug, prevented."""
    missing = []
    for page in PAGES:
        for match in re.finditer(
            r"`([a-zA-Z0-9_./-]+\.(?:py|ts|tsx|toml|sql|json|sh|md))`",
            page.read_text(encoding="utf-8"),
        ):
            candidate = match.group(1)
            if "/" not in candidate or "<" in candidate:
                continue
            if not (ROOT / candidate).exists():
                missing.append("%s names %s" % (page.name, candidate))

    assert not missing, "the guide names files that do not exist:\n  " + "\n  ".join(
        sorted(set(missing))
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
    error, mid-incident."""
    sys.argv = ["mac"]
    from mac.cli import build_parser

    def walk(node, prefix=()):
        found = set()
        for action in node._actions:
            mapping = getattr(action, "_name_parser_map", None)
            if not mapping:
                continue
            for name, sub in mapping.items():
                found.add(prefix + (name,))
                found |= walk(sub, prefix + (name,))
        return found

    real = walk(build_parser())
    tops = {path[0] for path in real if len(path) == 1}
    unknown = []
    for page in PAGES:
        for match in re.finditer(r"mac ((?:[a-z][a-z0-9-]*)(?: [a-z][a-z0-9-]*)*)", page.read_text(encoding="utf-8")):
            parts = match.group(1).split()
            if not parts or parts[0] not in tops:
                continue
            if not any(tuple(parts[:n]) in real for n in range(len(parts), 0, -1)):
                unknown.append("%s: mac %s" % (page.name, " ".join(parts)))

    assert not unknown, "commands that do not exist:\n  " + "\n  ".join(sorted(set(unknown)))


def test_the_known_gaps_are_still_gaps():
    """The guide tells operators to work around three gaps. If one is fixed and
    the page still claims it, the page is now misinformation.

    This is a canary, not a prohibition: closing a gap SHOULD fail here, and the
    fix is to update the page in the same change.
    """
    advanced = (GUIDE / "03-advanced.md").read_text(encoding="utf-8")
    assert "AgentBus is write-only" in advanced

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
