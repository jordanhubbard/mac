"""Extract the checkable claims out of a SKILL.md.

`tests/test_mac_cli_skill.py` already pins the CLI skill this way, and the same
enforcement belongs on every skill: a documented command that does not exist is
a usage error someone hits on a live fleet, and publishing skills to coding
harnesses (ADR 0023) turns each one into an instruction a harness obeys rather
than advice a human weighs.

Two differences from the CLI skill's private helpers, both forced by skills
that were written before anything checked them:

* **Fenced blocks count too.** `setup-mac-fleet` writes its commands in
  ```bash fences, not four-space blocks. Reading only the latter found zero
  commands in it and would have gone on passing forever.
* **A non-word token does not end the invocation.** `mac task create "title"
  --description-file=f.txt` stops the CLI skill's flag extractor at `"title"`,
  so every flag after a positional argument went unchecked. Here the leading
  words are the command path and every `--flag` in the same invocation is
  attributed to it.

Prose stays excluded, deliberately and for the reason the CLI skill gives: a
skill names a command precisely BECAUSE it does not work (`mac memory` moved
under `admin`, and warning about that is the point). Checking prose would force
the skills to stop documenting their own traps.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, List, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]

_WORD = re.compile(r"[a-z][a-z0-9-]*")

#: Repository-relative paths look like this. Anchored on the top-level
#: directories that actually exist, so prose like "read/write" is not mistaken
#: for a path.
_REPO_PATH = re.compile(r"\b(?:deploy|scripts|src|tests|docs|skills|ide|observe)/[A-Za-z0-9._/-]+")


def code_lines(text: str) -> Iterator[str]:
    """Lines inside a code block: four-space indented, or ``` fenced."""

    fenced = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
            continue
        if fenced or line.startswith("    "):
            yield line


def mac_invocations(text: str) -> List[Tuple[Tuple[str, ...], Tuple[str, ...]]]:
    """Every ``mac ...`` invocation a code block tells the reader to run.

    Returns ``(command path, flags)``. Column-aligned trailing comments are
    prose, not arguments, so a run of two or more spaces ends the command --
    without that, the description beside a command parses as more subcommands.
    """

    found: List[Tuple[Tuple[str, ...], Tuple[str, ...]]] = []
    for line in code_lines(text):
        for chunk in re.split(r"\s{2,}", line.strip()):
            tokens = chunk.split()
            if not tokens or tokens[0] != "mac":
                continue
            path: List[str] = []
            flags: List[str] = []
            for token in tokens[1:]:
                if token.startswith("--"):
                    flags.append(token.split("=", 1)[0])
                elif not flags and _WORD.fullmatch(token):
                    path.append(token)
            if path:
                found.append((tuple(path), tuple(flags)))
    return found


def repository_paths(text: str) -> Set[str]:
    """Repository-relative paths the skill points the reader at.

    A skill that names a file which has been moved or deleted is the same
    failure as a skill that names a missing command: the reader follows the
    pointer, finds nothing, and improvises.
    """

    paths: Set[str] = set()
    for match in _REPO_PATH.finditer(text):
        candidate = match.group(0).rstrip(".,;:)`")
        if "<" in candidate or ">" in candidate or "*" in candidate:
            continue  # a placeholder, not a path
        paths.add(candidate)
    return paths


def assert_commands_exist(text: str, tree: Set[Tuple[str, ...]]) -> None:
    missing = sorted(
        "mac " + " ".join(path) for path, _ in mac_invocations(text) if path not in tree
    )
    assert not missing, "the skill names commands that do not exist: %s" % ", ".join(
        missing
    )


def assert_flags_exist(text: str, options_for) -> None:
    wrong = []
    for path, flags in mac_invocations(text):
        available = options_for(path)
        for flag in flags:
            if flag not in available:
                wrong.append("mac %s %s" % (" ".join(path), flag))
    assert not wrong, "the skill names flags that do not exist: %s" % ", ".join(
        sorted(set(wrong))
    )


def assert_paths_exist(text: str) -> None:
    missing = sorted(p for p in repository_paths(text) if not (ROOT / p).exists())
    assert not missing, "the skill points at paths that do not exist: %s" % ", ".join(
        missing
    )
