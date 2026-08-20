"""`skills/record-user-directed-work/SKILL.md` had no test.

It is almost entirely instructions in the imperative -- file this, close that,
use this flag -- so a stale command in it does not read as stale, it reads as
an instruction that fails when someone follows it. Once ADR 0023 publishes the
skill into every harness, every session follows it.

The checks here are mechanical: every `mac` command the skill tells a reader to
run must be a real command with the flags it names, and the claims it makes
about where the record lives must hold against the repository.
"""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "record-user-directed-work" / "SKILL.md"


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _subparsers(parser: argparse.ArgumentParser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _resolve(parser: argparse.ArgumentParser, path: tuple[str, ...]):
    """Walk `mac a b c` through the real parser, or return None."""

    current = parser
    for name in path:
        action = _subparsers(current)
        if action is None or name not in action.choices:
            return None
        current = action.choices[name]
    return current


def test_frontmatter_names_the_skill_and_its_trigger():
    front = yaml.safe_load(_text().split("---\n", 2)[1])
    assert front["name"] == "record-user-directed-work"
    description = front["description"].lower()
    assert "task" in description
    assert "before" in description


def test_every_mac_command_the_skill_tells_you_to_run_exists():
    from mac.cli import build_parser

    parser = build_parser()
    invocations = {
        tuple(match.group(1).split())
        for match in re.finditer(r"`mac ((?:[a-z][a-z-]*)(?: [a-z][a-z-]*)+)", _text())
    }
    assert invocations, "the skill names no mac commands at all"
    unknown = sorted(
        " ".join(path) for path in invocations if _resolve(parser, path) is None
    )
    assert unknown == [], "record-user-directed-work names commands mac does not have: %s" % unknown


def test_the_flags_the_skill_insists_on_are_real():
    """`--description-file` is the skill's central shell-quoting advice."""

    from mac.cli import build_parser

    create = _resolve(build_parser(), ("task", "create"))
    assert create is not None
    options = {option for action in create._actions for option in action.option_strings}
    for flag in ("--description-file", "--as-human"):
        assert flag in _text(), "%s is part of the skill's advice" % flag
        assert flag in options, "the skill tells readers to pass %s" % flag

    close = _resolve(build_parser(), ("task", "close"))
    assert close is not None
    close_options = {option for action in close._actions for option in action.option_strings}
    assert "--reason" in close_options


def test_the_durable_store_claim_matches_the_repository():
    """"The ledger, not a roadmap file" -- and `.tickets/` stays local."""

    text = _text()
    assert "`.tickets/`" in text
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".tickets/" in gitignore, ".tickets must stay a gitignored local mirror"
    tracked = [
        line
        for line in subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", ".tickets"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.splitlines()
        if line.strip()
    ]
    assert tracked == [], "the ledger is the record; .tickets is a local mirror: %s" % tracked


def test_the_non_retryable_failure_class_it_names_is_real():
    """The ordering rule ("deploy the fix before releasing work") rests on it."""

    text = _text()
    assert "repository_test_failed" in text
    classifier = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (ROOT / "src" / "mac").rglob("*.py")
    )
    assert "repository_test_failed" in classifier, (
        "the skill warns that this class burns the attempt; if the class is gone "
        "the warning is a superstition"
    )
